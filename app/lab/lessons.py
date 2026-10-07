"""Carnet des leçons du labo : ce que la routine du soir a compris, avec sa preuve, mesuré chaque nuit.

Une leçon n'est pas une règle : elle ne filtre aucun trade. C'est une observation chiffrée (« cassure
de pivot avec RVOL > 3 : TP1 plus souvent atteint ») que le serveur re-mesure sans tokens sur les
trades du labo, et qui ne pèse sur la note des plans en direct qu'une fois confirmée.

Format (POST /api/lab/lessons, mis en file, lu par le worker) :

    {"texte": "Cassure de pivot avec RVOL > 3 : TP1 plus souvent atteint",
     "quand": {"and": [{"field": "context.setup", "op": "==", "value": "cassure_pivot"},
                       {"field": "context.rvol", "op": ">", "value": 3}]},
     "sens": "favorable", "markets": ["stock"], "pourquoi": "…"}

- « quand » : même langage de conditions que les règles (validator.canonical_cond, mêmes champs
  permis, mêmes interdits : pas de résultat du trade, pas de liquidité ni de capitalisation), sans
  seuil « auto » ;
- « sens » : favorable (les trades concernés font mieux que les autres) ou defavorable ;
- retirer une leçon : {"retirer": 12, "raison": "doublon de la 7"}.

Mesure (trades déclenchés de la version de génération gardés par le champion, comme le validateur,
et journalisés APRÈS la création de la leçon : base_trade_id) : espérance en R et taux de TP1 des
trades concernés contre l'ensemble, écart et z en grappes par jour (validator.pooled_z), et le même
écart sur chaque moitié chronologique des jours. Les trades qui ont inspiré la leçon ne comptent pas :
la confirmer sur eux, re-mesurée chaque nuit, donnerait de fausses confirmations (hors échantillon).
- à confirmer : moins de MIN_N trades concernés, ou effet pas encore net ;
- confirmée : z ≥ CONFIRM_Z dans le sens annoncé, écart ≥ MIN_EFFECT R, et même sens sur les deux
  moitiés (un effet qui n'existe que sur une période n'est pas une leçon) ;
- démentie : z ≤ DENY_Z (effet inverse), ou NULL_N trades concernés sans effet (|z| < 1). Une leçon
  démentie reste au carnet (mémoire des échecs : ne pas la retenter) et peut revenir si les trades
  suivants lui donnent raison ;
- invalide : un champ de la condition est presque toujours absent des trades.

Le carnet reste court : au plus MAX_ACTIVE leçons actives (à confirmer + confirmées) ; au-delà, la
plus faible des « à confirmer » est retirée pour faire place. C'est ce qui empêche JARVIS de se perdre.

En direct (app/trading/service.build_plan) : le worker écrit les leçons confirmées dans
lab_lessons.json ; chaque leçon confirmée qui correspond au plan ajoute (favorable) ou retire
(défavorable) ADJ_PER point à la note, au plus ADJ_MAX en tout, avec une ligne « Leçons du labo »
dans le plan. La condition s'évalue avec le SQL du labo sur la même ligne que les filtres du
champion (live.stock_row). Champ inconnu en direct : leçon non appliquée. Une erreur ici ne casse
jamais un plan. Le labo, lui, note ses plans sans les leçons (rien ne se mesure sur lui-même).
"""
import hashlib
import json
import logging
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.lab import validator as V
from app.lab.db import LabDB, db_path, now_iso

log = logging.getLogger(__name__)
MIN_N = 30
CONFIRM_Z = 2.5
MIN_EFFECT = 0.05
DENY_Z = -1.0
NULL_N = 150
MAX_ACTIVE = 50
QUEUE_MAX = 50
TEXT_MIN, TEXT_MAX = 10, 300
ADJ_PER, ADJ_MAX = 0.5, 1.0
LESSONS_FILE = "lab_lessons.json"
SENS = {"favorable": 1, "defavorable": -1}
STATUS_FR = {"a_confirmer": "à confirmer", "confirmee": "confirmée", "dementie": "démentie",
             "invalide": "invalide", "retiree": "retirée"}
ACTIVE = ("a_confirmer", "confirmee")
KEYS = {"texte", "quand", "sens", "markets", "pourquoi"}


class LessonError(ValueError):
    """Leçon illisible (message en français, montré tel quel)."""


# --- Format ------------------------------------------------------------------------------------

def _sens(value) -> str:
    v = str(value or "").strip().lower().replace("é", "e")
    if v not in SENS:
        raise LessonError("« sens » : favorable ou defavorable")
    return v


def parse(raw) -> dict:
    """Leçon canonique ; LessonError si elle est illisible."""
    if not isinstance(raw, dict):
        raise LessonError("objet JSON attendu")
    unknown = set(raw) - KEYS
    if unknown:
        raise LessonError(f"clé inconnue : {', '.join(sorted(unknown))}")
    text = raw.get("texte")
    if not isinstance(text, str) or not TEXT_MIN <= len(text.strip()) <= TEXT_MAX:
        raise LessonError(f"« texte » : phrase de {TEXT_MIN} à {TEXT_MAX} caractères")
    why = raw.get("pourquoi")
    if why is not None and (not isinstance(why, str) or len(why) > V.NOTE_KEYS["pourquoi"]):
        raise LessonError(f"« pourquoi » : texte de {V.NOTE_KEYS['pourquoi']} caractères au plus")
    if "quand" not in raw:
        raise LessonError("« quand » : condition attendue (même format que les règles)")
    try:
        rule = V.parse({"keep": raw["quand"], **({"markets": raw["markets"]} if "markets" in raw else {})})
    except V.RuleError as exc:
        raise LessonError(str(exc)) from exc
    if any(leaf.get("value") == "auto" for leaf in V.leaves_of(rule["cond"])):
        raise LessonError("« auto » n'a pas de sens pour une leçon : donne le seuil")
    return {"texte": text.strip(), "cond": rule["cond"], "markets": rule["markets"], "sens": _sens(raw.get("sens")),
            "pourquoi": why.strip() if why else None}


def lesson_hash(canon: dict) -> str:
    key = {k: canon[k] for k in ("cond", "markets", "sens")}
    return hashlib.sha256(json.dumps(key, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


# --- File (le serveur web n'écrit jamais dans la base) ------------------------------------------

def queue_dir(db_file: str | None = None) -> Path:
    return V.queue_dir(db_file or db_path()) / "lessons"


def _write(target: Path, data) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".{target.name}.{uuid.uuid4().hex[:6]}.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)


def enqueue(db_file: str, raw, soiree: str | None = None) -> dict:
    """Serveur web : dépose une leçon (ou un retrait) à enregistrer par le worker."""
    q = queue_dir(db_file)
    if q.exists() and sum(1 for _ in q.glob("*.json")) >= QUEUE_MAX:
        raise LessonError(f"file pleine ({QUEUE_MAX} leçons en attente de lecture par le worker)")
    if isinstance(raw, dict) and "retirer" in raw:
        lid, reason = raw.get("retirer"), raw.get("raison")
        if not isinstance(lid, int) or isinstance(lid, bool) or set(raw) - {"retirer", "raison"}:
            raise LessonError("retrait : {\"retirer\": numéro, \"raison\": texte}")
        if reason is not None and (not isinstance(reason, str) or len(reason) > 300):
            raise LessonError("« raison » : texte de 300 caractères au plus")
        _write(q / f"retrait-{lid}-{int(time.time() * 1000)}.json", {"retirer": lid, "raison": reason})
        return {"retrait": lid, "etat": "en_file"}
    canon = parse(raw)
    h = lesson_hash(canon)
    target = q / f"{h}.json"
    if not target.exists():  # la même leçon déjà en file : la première version compte
        _write(target, canon | {"_soiree": soiree})
    return {"empreinte": h, "etat": "en_file", "lecon": canon}


def active_count(conn) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM lessons WHERE status IN {ACTIVE}").fetchone()[0]


def _make_room(db: LabDB) -> bool:
    """Retire la plus faible des « à confirmer » (moins de trades, puis la plus ancienne)."""
    rows = db.conn.execute("SELECT id, metrics FROM lessons WHERE status = 'a_confirmer' ORDER BY id").fetchall()
    if not rows:
        return False
    weakest = min(rows, key=lambda r: ((json.loads(r[1]) or {}).get("n", 0) if r[1] else 0, r[0]))
    db.conn.execute("UPDATE lessons SET status = 'retiree', retired_at = ?, retired_reason = ? WHERE id = ?",
                    (now_iso(), "carnet plein : la plus faible des leçons à confirmer laisse sa place", weakest[0]))
    return True


def add(db: LabDB, canon: dict, soiree: str | None = None) -> dict | None:
    """Enregistre une leçon (ou renvoie celle déjà connue). None si le carnet est plein de confirmées."""
    h = lesson_hash(canon)
    row = db.conn.execute("SELECT id FROM lessons WHERE hash = ?", (h,)).fetchone()
    if row:
        return get(db.conn, row[0])
    if active_count(db.conn) >= MAX_ACTIVE and not _make_room(db):
        log.warning("Labo : carnet des leçons plein (%d confirmées), leçon écartée : %s", MAX_ACTIVE, canon["texte"])
        return None
    base = db.conn.execute("SELECT COALESCE(MAX(id), 0) FROM trades").fetchone()[0]
    cur = db.conn.execute(
        "INSERT INTO lessons (hash, text, cond, markets, sens, pourquoi, status, soiree, created_at, base_trade_id) "
        "VALUES (?, ?, ?, ?, ?, ?, 'a_confirmer', ?, ?, ?)",
        (h, canon["texte"], json.dumps(canon["cond"], ensure_ascii=False),
         json.dumps(canon["markets"]) if canon["markets"] else None, canon["sens"], canon.get("pourquoi"), soiree,
         now_iso(), base))
    return get(db.conn, cur.lastrowid)


def retire(db: LabDB, lid: int, reason: str | None) -> bool:
    cur = db.conn.execute("UPDATE lessons SET status = 'retiree', retired_at = ?, retired_reason = ? "
                          "WHERE id = ? AND status <> 'retiree'", (now_iso(), reason or "retirée par la routine", lid))
    return cur.rowcount > 0


def ingest(db: LabDB) -> int:
    """Le worker lit la file : leçons et retraits passent en base, les fichiers sont effacés."""
    q = queue_dir(db.path)
    if not q.exists():
        return 0
    n = 0
    for f in sorted(q.glob("*.json"), key=lambda p: p.stat().st_mtime_ns):
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
            if "retirer" in raw:
                retire(db, int(raw["retirer"]), raw.get("raison"))
            else:
                soiree = raw.pop("_soiree", None)
                canon = parse({"texte": raw["texte"], "quand": raw["cond"], "sens": raw["sens"],
                               **({"markets": raw["markets"]} if raw.get("markets") else {}),
                               **({"pourquoi": raw["pourquoi"]} if raw.get("pourquoi") else {})})
                add(db, canon, soiree)
            n += 1
        except (LessonError, ValueError, KeyError, TypeError) as exc:
            log.warning("Labo : leçon %s illisible, écartée (%s)", f.name, exc)
        f.unlink(missing_ok=True)
    return n


# --- Mesure --------------------------------------------------------------------------------------

def _mean(s: float, n: int) -> float | None:
    return s / n if n else None


def measure(conn, lesson: dict, version: str) -> dict:
    """Mesures d'une leçon : trades concernés contre l'ensemble (trades déclenchés de la version,
    gardés par le champion, marchés de la leçon)."""
    canon = {"cond": lesson["cond"], "markets": lesson["markets"]}
    missing = V.missing_share(conn, canon, version)
    absent = [f for f, share in missing.items() if share >= V.DEFAULTS["missing_max"]]
    if absent:
        return {"invalide": f"champ presque toujours absent des trades : {', '.join(absent)}"}
    expr, params = V.compile_cond(lesson["cond"])
    kept, kparams = V.champion_filter(conn)
    sql = (f"SELECT t.day, t.r_net, t.tp1_hit, COALESCE({expr}, 0) FROM trades t LEFT JOIN regimes r "
           "ON r.market = t.market AND r.day = t.day WHERE t.method_version = ? AND t.triggered = 1 "
           f"AND t.r_net IS NOT NULL AND {kept}")
    args = [*params, version, *kparams]
    if lesson.get("apres_trade") is not None:  # hors échantillon : trades journalisés après la leçon
        sql += " AND t.id > ?"
        args.append(lesson["apres_trade"])
    if lesson["markets"]:
        sql += f" AND t.market IN ({', '.join('?' * len(lesson['markets']))})"
        args += lesson["markets"]
    day_sums: dict[str, list] = {}
    tp_all = tp_hit = 0
    for day, r, tp1, hit in conn.execute(sql, args):
        d = day_sums.setdefault(day, [0.0, 0, 0.0, 0])
        d[0] += r
        d[1] += 1
        tp_all += tp1 or 0
        if hit:
            d[2] += r
            d[3] += 1
            tp_hit += tp1 or 0
    n_all = sum(v[1] for v in day_sums.values())
    n_hit = sum(v[3] for v in day_sums.values())
    s_all = sum(v[0] for v in day_sums.values())
    s_hit = sum(v[2] for v in day_sums.values())
    z, delta = V.pooled_z(day_sums)
    days = sorted(day_sums)
    halves = []
    for part in (days[:len(days) // 2], days[len(days) // 2:]):
        a = [day_sums[d] for d in part]
        m_all, m_hit = _mean(sum(v[0] for v in a), sum(v[1] for v in a)), _mean(sum(v[2] for v in a), sum(v[3] for v in a))
        halves.append(round(m_hit - m_all, 4) if m_all is not None and m_hit is not None else None)
    m_all, m_hit = _mean(s_all, n_all), _mean(s_hit, n_hit)
    return {"n": n_hit, "n_total": n_all, "jours": len(days),
            "esperance_r": round(m_hit, 4) if m_hit is not None else None,
            "esperance_ensemble_r": round(m_all, 4) if m_all is not None else None,
            "tp1": round(tp_hit / n_hit, 4) if n_hit else None, "tp1_ensemble": round(tp_all / n_all, 4) if n_all else None,
            "ecart_r": round(delta, 4) if delta is not None else None, "z": round(z, 3) if z is not None else None,
            "moities": halves, "version": version, "apres_trade": lesson.get("apres_trade")}


def judge(m: dict, sens: str) -> str:
    if m.get("invalide"):
        return "invalide"
    s = SENS[sens]
    z, delta = m.get("z"), m.get("ecart_r")
    if m["n"] < MIN_N or z is None or delta is None:
        return "a_confirmer"
    if (s * z >= CONFIRM_Z and s * delta >= MIN_EFFECT
            and all(h is not None and s * h > 0 for h in m["moities"])):
        return "confirmee"
    if s * z <= DENY_Z or (m["n"] >= NULL_N and abs(z) < 1):
        return "dementie"
    return "a_confirmer"


def measure_all(db: LabDB, only_new: bool = False) -> int:
    """Re-mesure les leçons (toutes, sauf retirées ; `only_new` : seulement celles jamais mesurées)."""
    version = V.current_version(db.conn)
    if not version:
        return 0
    sql = "SELECT id FROM lessons WHERE status <> 'retiree'" + (" AND measured_at IS NULL" if only_new else "")
    n = 0
    for (lid,) in db.conn.execute(sql).fetchall():
        lesson = get(db.conn, lid)
        try:
            m = measure(db.conn, lesson, version)
        except sqlite3.Error as exc:
            log.warning("Labo : leçon %d non mesurée (%s)", lid, exc)
            continue
        status = judge(m, lesson["sens"])
        db.conn.execute("UPDATE lessons SET status = ?, metrics = ?, measured_at = ? WHERE id = ?",
                        (status, json.dumps(m, ensure_ascii=False), now_iso(), lid))
        if status != lesson["etat"]:
            log.info("Labo : leçon %d %s → %s (%s)", lid, lesson["etat"], status, lesson["texte"][:80])
        n += 1
    return n


def step(db: LabDB, now: datetime, max_age: float) -> dict:
    """Passage du worker : file, mesure des nouvelles, re-mesure complète quand les trades ont changé
    (au plus toutes les `max_age` secondes), fichier des leçons confirmées pour le direct."""
    out = {"file": ingest(db), "nouvelles": measure_all(db, only_new=True)}
    sig = db.conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM trades").fetchone()
    sig = [sig[0], sig[1]]
    at = db.get("lessons", "at", 0) or 0
    if sig != db.get("lessons", "signature") and now.timestamp() - at >= max_age:
        out["mesurees"] = measure_all(db)
        db.set("lessons", "signature", sig)
        db.set("lessons", "at", now.timestamp())
    out["fichier"] = write_live(db)
    return out


# --- Lecture -------------------------------------------------------------------------------------

COLUMNS = ("id, hash, text, cond, markets, sens, pourquoi, status, metrics, soiree, created_at, measured_at, "
           "retired_at, retired_reason, base_trade_id")


def _row(r) -> dict:
    (lid, h, text, cond, markets, sens, why, status, metrics, soiree, created, measured, retired, rreason, base) = r
    return {"id": lid, "empreinte": h, "texte": text, "cond": json.loads(cond),
            "markets": json.loads(markets) if markets else None, "sens": sens, "pourquoi": why, "etat": status,
            "mesures": json.loads(metrics) if metrics else None, "soiree": soiree, "creee_le": created,
            "mesuree_le": measured, "retiree_le": retired, "raison_retrait": rreason, "apres_trade": base}


def get(conn, lid: int) -> dict | None:
    row = conn.execute(f"SELECT {COLUMNS} FROM lessons WHERE id = ?", (lid,)).fetchone()
    return _row(row) if row else None


ORDER = "CASE status WHEN 'confirmee' THEN 0 WHEN 'a_confirmer' THEN 1 WHEN 'dementie' THEN 2 ELSE 3 END, id DESC"


def list_lessons(conn, status: str | None = None, limit: int = 200) -> list[dict]:
    sql = f"SELECT {COLUMNS} FROM lessons" + (" WHERE status = ?" if status else "") + f" ORDER BY {ORDER} LIMIT ?"
    return [_row(r) for r in conn.execute(sql, (status, limit) if status else (limit,))]


def counts(conn) -> dict:
    out = {k: 0 for k in STATUS_FR}
    for status, n in conn.execute("SELECT status, COUNT(*) FROM lessons GROUP BY status"):
        out[status] = n
    out["place"] = max(0, MAX_ACTIVE - out["a_confirmer"] - out["confirmee"])
    return out


def _pct(x) -> str:
    return f"{x:.0%}" if x is not None else "n/d"


def proof(lesson: dict) -> str:
    """Preuve chiffrée en clair (« 42 trades : +0,31 R contre +0,05 R, TP1 61 % contre 44 % »)."""
    m = lesson.get("mesures") or {}
    if m.get("invalide"):
        return m["invalide"]
    if not m:
        return "pas encore mesurée"
    if not m.get("n"):
        return "aucun trade concerné depuis la leçon pour l'instant"
    fmt = lambda x: f"{x:+.2f} R".replace(".", ",") if x is not None else "n/d"  # noqa: E731
    return (f"{m['n']} trades depuis la leçon : {fmt(m.get('esperance_r'))} contre {fmt(m.get('esperance_ensemble_r'))}, "
            f"TP1 {_pct(m.get('tp1'))} contre {_pct(m.get('tp1_ensemble'))}")


def line(lesson: dict) -> str:
    icon = {"confirmee": "✅", "a_confirmer": "🔎", "dementie": "❌", "invalide": "⚠️", "retiree": "🗄️"}[lesson["etat"]]
    sens = "👍" if lesson["sens"] == "favorable" else "👎"
    return f"{icon} {sens} **#{lesson['id']}** {lesson['texte']} · {proof(lesson)}"


def text(conn, limit_denied: int = 5) -> str:
    """Carnet pour Discord (!lecons) : confirmées, à confirmer, quelques démenties."""
    try:
        c = counts(conn)
        lessons = list_lessons(conn, limit=MAX_ACTIVE + limit_denied + 20)
    except sqlite3.Error:
        return "📒 Carnet des leçons pas encore créé (le worker du labo doit redémarrer sur la nouvelle version)."
    head = (f"📒 **Leçons du labo** · {c['confirmee']} confirmées, {c['a_confirmer']} à confirmer, "
            f"{c['dementie']} démenties · place libre {c['place']}/{MAX_ACTIVE}")
    if not lessons:
        return head + "\nAucune leçon pour l'instant : la routine du soir les écrit au fil des séances."
    lines = [head]
    for status, title, cap in (("confirmee", "Confirmées (elles comptent dans la note des plans)", MAX_ACTIVE),
                               ("a_confirmer", "À confirmer", MAX_ACTIVE), ("dementie", "Démenties récentes", limit_denied)):
        group = [x for x in lessons if x["etat"] == status][:cap]
        if group:
            lines.append(f"__{title}__")
            lines += [line(x) for x in group]
    return "\n".join(lines)


def brief(conn, limit_denied: int = 10) -> dict:
    """Ce que la routine du soir relit avant d'analyser (compact)."""
    def compact(x: dict) -> dict:
        m = x.get("mesures") or {}
        return {"id": x["id"], "texte": x["texte"], "sens": x["sens"], "etat": x["etat"], "preuve": proof(x),
                "z": m.get("z"), "moities": m.get("moities")}
    lessons = list_lessons(conn, limit=MAX_ACTIVE + 40)
    return {"compteurs": counts(conn), "actives": [compact(x) for x in lessons if x["etat"] in ACTIVE],
            "dementies": [compact(x) for x in lessons if x["etat"] == "dementie"][:limit_denied]}


# --- Direct : fichier des leçons confirmées et ajustement de la note ------------------------------

def live_path(db_file: str | None = None) -> Path:
    override = os.getenv("JARVIS_LAB_LESSONS", "").strip()
    return Path(override) if override else Path(db_file or db_path()).parent / LESSONS_FILE


def write_live(db: LabDB) -> bool:
    """Écrit lab_lessons.json (leçons confirmées) s'il a changé ; écriture atomique."""
    if db.path == ":memory:":
        return False
    data = {"lecons": [{"id": x["id"], "texte": x["texte"], "sens": x["sens"], "cond": x["cond"],
                        "markets": x["markets"]} for x in list_lessons(db.conn, "confirmee")]}
    path = live_path(db.path)
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("lecons") == data["lecons"]:
            return False
    except (OSError, ValueError):
        pass
    _write(path, data | {"ecrit_le": now_iso()})
    return True


_cache: dict = {"key": None, "data": []}
_warned: set[str] = set()
_index: dict = {"day": None, "bars": None}


def _warn_once(key: str, msg: str, *args) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning(msg, *args)


def current() -> list[dict]:
    """Leçons confirmées pour le direct ; [] si le fichier manque ou est illisible."""
    try:
        path = live_path()
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
        if _cache["key"] != key:
            data = json.loads(path.read_text(encoding="utf-8"))
            items = []
            for x in data.get("lecons") or []:
                V.compile_cond(x["cond"])  # condition illisible : erreur ici, pas pendant un plan
                items.append({"id": x["id"], "texte": x["texte"], "sens": _sens(x["sens"]), "cond": x["cond"],
                              "markets": x.get("markets")})
            _cache.update(key=key, data=items)
            _warned.discard("lecture")
        return _cache["data"]
    except FileNotFoundError:
        return []
    except Exception as exc:
        _warn_once("lecture", "Leçons du labo illisibles (%s: %s), aucune appliquée", type(exc).__name__, exc)
    return []


def match(lessons: list[dict], row: dict) -> list[dict]:
    """Leçons dont la condition est vraie pour la ligne (même SQL que le labo ; champ inconnu : faux)."""
    from app.lab import method as M

    if not lessons:
        return []
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(M.LIVE_SCHEMA)
        conn.execute("INSERT INTO trades (rowid, method_version, origin, market, symbol, side, day, planned_at, plan, "
                     "context, composite, decision) VALUES (1, 'direct', 'live', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (row["market"], row.get("symbol"), row.get("side", "long"), row["day"], int(row["planned_at"]),
                      json.dumps(row.get("plan") or {}, ensure_ascii=False),
                      json.dumps(row.get("context") or {}, ensure_ascii=False), row.get("composite"), row.get("decision")))
        if row.get("regime_label") is not None or row.get("regime_trend") is not None:
            conn.execute("INSERT INTO regimes VALUES (?, ?, ?, ?, ?)", (row["market"], row["day"], row.get("regime_trend"),
                                                                        row.get("regime_vol_tercile"), row.get("regime_label")))
        out = []
        for x in lessons:
            expr, params = V.compile_cond(x["cond"])
            hit = conn.execute(f"SELECT COALESCE({expr}, 0) FROM trades t LEFT JOIN regimes r ON r.market = t.market "
                               "AND r.day = t.day", params).fetchone()[0]
            if hit:
                out.append(x)
        return out
    finally:
        conn.close()


def adjustment(matched: list[dict]) -> dict | None:
    """Points ajoutés à la note et lignes du plan ; None sans leçon concernée."""
    if not matched:
        return None
    raw = sum(ADJ_PER * SENS[x["sens"]] for x in matched)
    adj = max(-ADJ_MAX, min(ADJ_MAX, raw))
    return {"ajustement": round(adj, 2),
            "lignes": [f"{'+' if x['sens'] == 'favorable' else '−'}{ADJ_PER:g} #{x['id']} {x['texte']}" for x in matched]}


def crypto_row(plan: dict, daily: list[dict] | None, now: datetime) -> dict:
    """Ligne « journal du labo » d'un plan crypto en direct (contexte du générateur, sans régime)."""
    from app.lab import generator as G
    from app.lab import universe as U

    day = now.astimezone(timezone.utc).date()
    midnight = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
    done = [r for r in daily or [] if r["t"] < midnight]
    metrics = (U._metrics(done) if len(done) >= 2 else None) or {}
    context = {"setup": G.setup_type(plan), "target_sources": [t["source"] for t in plan["targets"]],
               "rvol": metrics.get("rvol"), "day_change_pct": metrics.get("change_pct"),
               "window_utc": now.astimezone(timezone.utc).strftime("%H:%M"), "sector": None, "catalyst_replayed": False}
    return {"market": "crypto", "symbol": plan["symbol"], "side": plan["side"], "day": day.isoformat(),
            "planned_at": int(now.timestamp()), "plan": G.compact_plan(plan), "context": context,
            "composite": plan["score"]["composite"], "decision": plan["score"]["decision"]}


async def _index_daily(now: datetime) -> list[dict] | None:
    """SPY journalier (régime de marché), gardé pour la journée : le scanner fait ~30 plans."""
    from app.trading import scanner

    day = now.astimezone(timezone.utc).date().isoformat()
    if _index["day"] != day:
        _index.update(day=day, bars=await scanner._index_daily(now))
    return _index["bars"]


async def apply(plan: dict, daily: list[dict] | None, cat: dict | None, now: datetime, s=None) -> dict | None:
    """Ajuste la note d'un plan en direct avec les leçons confirmées qui le concernent (plan modifié
    sur place). Renvoie l'ajustement, ou None. Jamais d'exception : en cas de souci, plan inchangé."""
    try:
        lessons = [x for x in current() if not x.get("markets") or plan["market"] in x["markets"]]
        if not lessons or plan["side"] != "long":  # le labo ne mesure que des trades longs
            return None
        from app.lab import live as LIVE
        from app.trading import score as SC
        from app.trading.settings import settings

        s = settings if s is None else s
        if plan["market"] == "stock":
            index = None
            if any(LIVE.needs_index(x) for x in lessons):
                index = await _index_daily(now)
                if not index:
                    lessons = [x for x in lessons if not LIVE.needs_index(x)]
            row = LIVE.stock_row(plan, daily, daily, index, now, s)
        else:
            lessons = [x for x in lessons if not LIVE.needs_index(x)]
            row = crypto_row(plan, daily, now)
        adj = adjustment(match(lessons, row))
        if adj:
            plan["score"] = SC.evaluate(plan, daily, cat, s, lessons=adj)
        return adj
    except Exception as exc:
        _warn_once("apply", "Leçons du labo non appliquées (%s: %s), note inchangée", type(exc).__name__, exc)
        return None
