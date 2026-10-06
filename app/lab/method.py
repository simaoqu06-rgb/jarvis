"""Méthodes du labo (Labo 3) : champion, challenger et filtres des règles approuvées par Simão.

- Une règle validée (validator.py) et approuvée par Simão crée la version suivante : v2 = v1 + ses
  filtres. Elle est d'abord CHALLENGER : le labo compare, sur les trades en direct journalisés depuis sa
  création, ce que garde le challenger et ce que garde le champion (mêmes plans, filtres différents ;
  rien n'est journalisé deux fois). Quand le challenger a au moins `challenger_min_trades` trades
  déclenchés et fait mieux en R net moyen par trade, sans perdre de capture du potentiel +10 (même
  tolérance que le validateur), il devient champion ; après `challenger_max_trades` sans faire mieux,
  il est retiré. Seuils dans lab_config (validator.DEFAULTS).
- Une règle refusée est rangée dans la mémoire des échecs (« refusée par Simão ») : jamais reproposée.
- Le champion est écrit dans un petit fichier JSON (lab_champion.json, à côté de la base) que JARVIS
  en direct lit : ses filtres s'y appliquent avec EXACTEMENT le même code SQL que le labo (kept_sql,
  évalué en direct sur une base SQLite en mémoire, voir evaluate_rows).
- Le serveur web n'écrit jamais dans la base : les décisions de Simão et les types de catalyseurs
  passent par la file data/lab_queue/ (sous-dossiers decisions/ et types/), lue par le worker.
"""
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.lab import stats as STATS
from app.lab import validator as V
from app.lab.db import LabDB, db_path, generation_version, now_iso

log = logging.getLogger(__name__)
CHAMPION_FILE = "lab_champion.json"
DECISIONS = ("approuvee", "refusee")
REFUSED_REASON = "refusée par Simão"
TYPES_MAX = 500


# --- Filtres : le même SQL au labo et en direct ----------------------------------------------

def concrete(rule: dict) -> dict | None:
    """Règle applicable (seuil « auto » figé) depuis une ligne de validator.get_rule. Le seuil « auto »
    retenu est celui de la dernière fenêtre (ajusté sur le plus de données) ; None s'il n'y en a pas."""
    canon = rule["regle"]
    cond = canon["cond"]
    if cond.get("value") == "auto":
        thresholds = [w.get("seuil") for w in ((rule.get("mesures") or {}).get("fenetres") or [])]
        thresholds = [t for t in thresholds if t is not None]
        if not thresholds:
            return None
        cond = cond | {"value": V._num(thresholds[-1])}
    return {"empreinte": rule["empreinte"], "nom": rule.get("nom") or rule["empreinte"][:10],
            "kind": canon["kind"], "cond": cond, "markets": canon.get("markets")}


def rule_sql(rule: dict) -> tuple[str, list]:
    """1 si la règle garde le trade (alias t = trades, r = regimes). Condition fausse ou inconnue
    (champ absent) : « pas vérifiée », comme au validateur (COALESCE … 0)."""
    expr, params = V.compile_cond(rule["cond"])
    hit = f"COALESCE({expr}, 0)"
    kept = f"(NOT {hit})" if rule["kind"] == "exclude" else f"({hit})"
    if rule.get("markets"):
        marks = ", ".join("?" * len(rule["markets"]))
        return f"(t.market NOT IN ({marks}) OR {kept})", [*rule["markets"], *params]
    return kept, list(params)


def kept_sql(rules: list[dict]) -> tuple[str, list]:
    """Trades gardés par toutes les règles ; « 1 » sans règle."""
    if not rules:
        return "1", []
    parts = [rule_sql(r) for r in rules]
    return "(" + " AND ".join(s for s, _ in parts) + ")", [p for _, ps in parts for p in ps]


LIVE_SCHEMA = """
CREATE TABLE trades (method_version TEXT, origin TEXT, market TEXT, symbol TEXT, side TEXT, day TEXT,
                     planned_at INTEGER, plan TEXT, context TEXT, composite REAL, decision TEXT);
CREATE TABLE regimes (market TEXT, day TEXT, trend TEXT, vol_tercile INTEGER, label TEXT);
"""


def evaluate_rows(rules: list[dict], rows: list[dict]) -> list[dict]:
    """Applique des règles à des plans hors de la base du labo (JARVIS en direct, tests) avec le même
    SQL que le labo : chaque ligne est insérée dans une base SQLite en mémoire aux colonnes du journal.

    Ligne : market, symbol, side, day (AAAA-MM-JJ), planned_at (epoch), plan (dict compact, comme
    generator.compact_plan), context (dict), composite, decision, regime_label/regime_trend/
    regime_vol_tercile (facultatifs). Renvoie, par ligne : {"garde": bool, "regles": [noms qui écartent]}."""
    out = [{"garde": True, "regles": []} for _ in rows]
    if not rules or not rows:
        return out
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(LIVE_SCHEMA)
        for i, r in enumerate(rows):
            conn.execute("INSERT INTO trades (rowid, method_version, origin, market, symbol, side, day, planned_at, plan, "
                         "context, composite, decision) VALUES (?, 'direct', 'live', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (i + 1, r["market"], r.get("symbol"), r.get("side", "long"), r["day"], int(r["planned_at"]),
                          json.dumps(r.get("plan") or {}, ensure_ascii=False),
                          json.dumps(r.get("context") or {}, ensure_ascii=False), r.get("composite"),
                          r.get("decision")))
            if r.get("regime_label") is not None or r.get("regime_trend") is not None:
                conn.execute("INSERT INTO regimes VALUES (?, ?, ?, ?, ?)",
                             (r["market"], r["day"], r.get("regime_trend"), r.get("regime_vol_tercile"),
                              r.get("regime_label")))
        for rule in rules:
            expr, params = rule_sql(rule)
            sql = (f"SELECT t.rowid, {expr} FROM trades t LEFT JOIN regimes r ON r.market = t.market "
                   "AND r.day = t.day ORDER BY t.rowid")
            for rowid, kept in conn.execute(sql, params):
                if not kept:
                    out[rowid - 1]["garde"] = False
                    out[rowid - 1]["regles"].append(rule.get("nom") or rule.get("empreinte", "?")[:10])
    finally:
        conn.close()
    return out


# --- Versions : champion et challenger --------------------------------------------------------

def _version_row(row) -> dict:
    version, created, base, rules, role, started, decided, note = row
    return {"version": version, "creee_le": created, "base": base, "regles": json.loads(rules) if rules else [],
            "role": role, "debut_ts": started, "decidee_le": decided, "note": note}


VERSION_COLUMNS = "version, created_at, base, rules, role, started_ts, decided_at, note"


def versions(conn) -> list[dict]:
    try:
        return [_version_row(r) for r in conn.execute(
            f"SELECT {VERSION_COLUMNS} FROM method_versions ORDER BY created_at, version")]
    except sqlite3.OperationalError:  # base d'avant le Labo 3 (lecture seule)
        return [{"version": v, "creee_le": c, "base": None, "regles": [], "role": None, "debut_ts": None,
                 "decidee_le": None, "note": None}
                for v, c in conn.execute("SELECT version, created_at FROM method_versions ORDER BY created_at, version")]


def champion(conn) -> dict:
    """Méthode en place : la version de rôle « champion », sinon la version de génération (v1, sans filtre)."""
    rows = versions(conn)
    for r in rows:
        if r["role"] == "champion":
            return r
    gen = generation_version(conn)
    base = next((r for r in rows if r["version"] == gen), None)
    return (base or {"version": gen, "creee_le": None, "base": None, "regles": [], "debut_ts": None,
                     "decidee_le": None, "note": None}) | {"role": "champion", "base": None, "regles": []}


def challenger(conn) -> dict | None:
    return next((r for r in versions(conn) if r["role"] == "challenger"), None)


def base_of(v: dict, conn) -> str | None:
    return v.get("base") or v.get("version") or generation_version(conn)


def next_version(conn) -> str:
    nums = [int(m.group(1)) for (v,) in conn.execute("SELECT version FROM method_versions")
            if (m := re.fullmatch(r"v(\d+)", v))]
    return f"v{max(nums, default=1) + 1}"


def compare(conn, champ: dict, chall: dict, cfg: dict | None = None) -> dict:
    """Champion contre challenger sur les trades EN DIRECT journalisés depuis la création du challenger
    (même version de génération, mêmes plans) : trades déclenchés gardés, R net moyen, capture du potentiel +10."""
    cfg = cfg or V.config(conn)
    base = base_of(chall, conn)
    ce, cp = kept_sql(champ["regles"])
    he, hp = kept_sql(chall["regles"])
    since = int(chall.get("debut_ts") or 0)
    rows = conn.execute(
        f"SELECT t.day, t.market, t.symbol, t.triggered, t.r_net, t.tp1_hit, {ce}, {he} FROM trades t "
        "LEFT JOIN regimes r ON r.market = t.market AND r.day = t.day "
        "WHERE t.method_version = ? AND t.origin = 'live' AND t.planned_at >= ?", (*cp, *hp, base, since)).fetchall()
    acc = {"champion": [0, 0.0], "challenger": [0, 0.0]}
    first_day = None
    caught: dict[str, set] = {"champion": set(), "challenger": set()}
    for day, market, symbol, trig, r, tp1, keep_c, keep_h in rows:
        first_day = day if first_day is None or day < first_day else first_day
        for name, kept in (("champion", keep_c), ("challenger", keep_h)):
            if not kept or not trig or r is None:
                continue
            acc[name][0] += 1
            acc[name][1] += r
            if tp1:
                caught[name].add((market, day, symbol))
    movers = set()
    if first_day:
        movers = {tuple(m) for m in conn.execute(
            "SELECT market, day, symbol FROM day_moves WHERE up_pct >= ? AND day >= ?", (STATS.MOVE_PCT, first_day))}
    out = {"base": base, "depuis_ts": since, "trades_lignes": len(rows)}
    for name in ("champion", "challenger"):
        n, total = acc[name]
        out[name] = {"version": (champ if name == "champion" else chall)["version"], "declenches": n,
                     "esperance_r": round(total / n, 4) if n else None,
                     "capture": len(caught[name] & movers)}
    before, after = out["champion"]["capture"], out["challenger"]["capture"]
    allowed = before * cfg["max_capture_drop"] if before >= 10 else 1
    out["capture_ok"] = before - after <= allowed + 1e-9
    out["baisse_capture_permise"] = allowed
    ec, eh = out["champion"]["esperance_r"], out["challenger"]["esperance_r"]
    out["meilleur"] = ec is not None and eh is not None and eh > ec
    return out


def verdict(cmp: dict, cfg: dict) -> str:
    """« promouvoir », « retirer » ou « attendre »."""
    n = cmp["challenger"]["declenches"]
    if n >= cfg["challenger_min_trades"] and cmp["meilleur"] and cmp["capture_ok"]:
        return "promouvoir"
    if n >= cfg["challenger_max_trades"]:
        return "retirer"
    return "attendre"


# --- Textes -----------------------------------------------------------------------------------

def fr(x: float | None, digits: int = 2, unit: str = " R") -> str:
    if x is None:
        return "n/d"
    return f"{x:+.{digits}f}".replace(".", ",").replace("-", "−") + unit


OPS_FR = {">": ">", ">=": "≥", "<": "<", "<=": "≤", "==": "=", "!=": "≠"}


def field_fr(field: str) -> str:
    for prefix in ("context.", "plan."):
        if field.startswith(prefix):
            return field[len(prefix):]
    return field


def _value_fr(v) -> str:
    if isinstance(v, bool):
        return "oui" if v else "non"
    if isinstance(v, float):
        return f"{v:g}".replace(".", ",")
    return str(v)


def cond_fr(cond: dict) -> str:
    """Condition en clair : « rvol > 5 et setup = cassure_pivot »."""
    if "and" in cond or "or" in cond:
        k = "and" if "and" in cond else "or"
        return f" {'et' if k == 'and' else 'ou'} ".join(
            f"({cond_fr(c)})" if ("and" in c or "or" in c) else cond_fr(c) for c in cond[k])
    if "not" in cond:
        return f"non ({cond_fr(cond['not'])})"
    f, op, v = field_fr(cond["field"]), cond["op"], cond.get("value")
    if op in OPS_FR:
        return f"{f} {OPS_FR[op]} {_value_fr(v)}"
    if op == "in":
        return f"{f} parmi {', '.join(_value_fr(x) for x in v)}"
    if op == "not_in":
        return f"{f} hors de {', '.join(_value_fr(x) for x in v)}"
    if op == "between":
        return f"{f} entre {_value_fr(v[0])} et {_value_fr(v[1])}"
    return f"{f} {'absent' if op == 'is_null' else 'connu'}"


def rule_fr(rule: dict) -> str:
    """« exclure : rvol > 5 (actions) »."""
    markets = rule.get("markets")
    where = f" ({', '.join('actions' if m == 'stock' else m for m in markets)})" if markets else ""
    verb = "exclure" if rule["kind"] == "exclude" else "ne garder que"
    return f"{verb} {cond_fr(rule['cond'])}{where}"


def proof_fr(rule: dict) -> str:
    """Preuve chiffrée d'une règle validée, en une phrase (mesures du validateur, périodes de test)."""
    m = rule.get("mesures") or {}
    canon = concrete(rule) or {"kind": rule["regle"]["kind"], "cond": rule["regle"]["cond"],
                               "markets": rule["regle"].get("markets")}
    touched = m.get("touches_test") or 0
    gain = m.get("gain_total_r") or 0.0
    mean_touched = -gain / touched if touched else None
    n_win = len(m.get("fenetres") or [])
    who = f"les trades où {cond_fr(canon['cond'])}" if canon["kind"] == "exclude" else \
        f"les trades hors de « {cond_fr(canon['cond'])} »"
    verb = "les exclure" if canon["kind"] == "exclude" else "ne garder que les autres"
    return (f"{who[0].upper()}{who[1:]} font {fr(mean_touched)} en moyenne sur {touched} trades ; {verb} donne "
            f"{fr(m.get('amelioration_moyenne_r'))} par trade sur {n_win} périodes de test "
            f"(meilleur dans {m.get('fenetres_meilleures', 0)} sur {n_win}).")


# --- Worker : décisions, challenger, bascule --------------------------------------------------

def queue_root(db_file: str | None = None) -> Path:
    return V.queue_dir(db_file or db_path())


def _write_atomic(target: Path, data) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".{target.name}.{uuid.uuid4().hex[:6]}.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)


def enqueue_decision(db_file: str, rule_hash: str, decision: str, proposal_id: int | None = None) -> Path:
    """Serveur web : dépose la décision de Simão sur une règle validée (lue par le worker)."""
    if decision not in DECISIONS or not re.fullmatch(r"[0-9a-f]{64}", rule_hash or ""):
        raise ValueError("décision illisible")
    target = queue_root(db_file) / "decisions" / f"{rule_hash}.json"
    _write_atomic(target, {"empreinte": rule_hash, "decision": decision, "proposition": proposal_id, "le": now_iso()})
    return target


def enqueue_types(db_file: str, items: list[dict]) -> Path:
    """Serveur web : dépose des types de catalyseurs à enregistrer (lus par le worker)."""
    target = queue_root(db_file) / "types" / f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}.json"
    _write_atomic(target, items)
    return target


def ingest_decisions(db: LabDB) -> int:
    """Approbations et refus déposés par le serveur web. Refus : mémoire des échecs (jamais reproposée)."""
    q = queue_root(db.path) / "decisions"
    if not q.exists():
        return 0
    n = 0
    for f in sorted(q.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            rule = V.get_rule(db.conn, data["empreinte"])
            if rule is None:
                log.warning("Labo : décision sur une règle inconnue (%s), ignorée", f.name)
            elif rule.get("decision"):
                log.info("Labo : règle %s déjà décidée (%s)", rule["empreinte"][:10], rule["decision"])
            elif data["decision"] == "approuvee" and rule["etat"] == "validee":
                db.conn.execute("UPDATE rules SET decision = 'approuvee', decided_at = ? WHERE hash = ?",
                                (data.get("le") or now_iso(), rule["empreinte"]))
                log.info("Labo : règle %s approuvée par Simão", rule["empreinte"][:10])
                n += 1
            elif data["decision"] == "refusee":
                reasons = [REFUSED_REASON] + [r for r in rule["raisons"] if r != REFUSED_REASON]
                db.conn.execute("UPDATE rules SET status = 'rejetee', reason = ?, decision = 'refusee', decided_at = ? "
                                "WHERE hash = ?", (json.dumps(reasons, ensure_ascii=False), data.get("le") or now_iso(),
                                                   rule["empreinte"]))
                log.info("Labo : règle %s refusée par Simão (mémoire des échecs)", rule["empreinte"][:10])
                n += 1
            else:
                log.warning("Labo : décision %s impossible sur une règle %s", data.get("decision"), rule["etat"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("Labo : décision %s illisible, écartée (%s)", f.name, exc)
        f.unlink(missing_ok=True)
    return n


def ingest_types(db: LabDB) -> int:
    """Types de catalyseurs classés par la routine du soir (validés contre catalysts.TYPES)."""
    from app.lab import catalysts as CAT

    q = queue_root(db.path) / "types"
    if not q.exists():
        return 0
    n = 0
    for f in sorted(q.glob("*.json")):
        try:
            items = json.loads(f.read_text(encoding="utf-8"))
            with db.transaction():
                for item in items[:TYPES_MAX]:
                    try:
                        n += CAT.set_catalyst_type(db, int(item["id"]), item.get("type"))
                    except (ValueError, KeyError, TypeError) as exc:
                        log.warning("Labo : type de fiche écarté (%s)", exc)
        except (OSError, ValueError, TypeError) as exc:
            log.warning("Labo : fichier de types %s illisible, écarté (%s)", f.name, exc)
        f.unlink(missing_ok=True)
    return n


def event(db: LabDB, kind: str, version: str | None, text: str, data: dict | None = None,
          now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    db.conn.execute("INSERT INTO lab_events (created_at, ts, kind, version, text, data) VALUES (?, ?, ?, ?, ?, ?)",
                    (now.isoformat(timespec="seconds"), int(now.timestamp()), kind, version, text,
                     json.dumps(data or {}, ensure_ascii=False)))


def events(conn, after_id: int = 0, limit: int = 20) -> list[dict]:
    try:
        return [{"id": i, "le": c, "ts": ts, "genre": k, "version": v, "texte": t}
                for i, c, ts, k, v, t in conn.execute(
                    "SELECT id, created_at, ts, kind, version, text FROM lab_events WHERE id > ? ORDER BY id LIMIT ?",
                    (after_id, limit))]
    except sqlite3.OperationalError:
        return []


def _approved_waiting(conn) -> list[dict]:
    return [V.get_rule(conn, h) for (h,) in conn.execute(
        "SELECT hash FROM rules WHERE decision = 'approuvee' AND status = 'validee' ORDER BY decided_at, id")]


def ensure_challenger(db: LabDB, now: datetime | None = None) -> dict | None:
    """Règles approuvées en attente → challenger. S'il en existe déjà un sans aucun trade déclenché en
    direct (approbations du même matin), il est complété ; sinon elles attendent sa fin."""
    now = now or datetime.now(timezone.utc)
    waiting = _approved_waiting(db.conn)
    if not waiting:
        return None
    champ = champion(db.conn)
    chall = challenger(db.conn)
    if chall and compare(db.conn, champ, chall)["challenger"]["declenches"] > 0:
        return None
    new_rules, unusable = [], []
    for rule in waiting:
        c = concrete(rule)
        (new_rules if c else unusable).append(c or rule)
    with db.transaction():
        for rule in unusable:
            db.conn.execute("UPDATE rules SET decision = 'inapplicable' WHERE hash = ?", (rule["empreinte"],))
            log.warning("Labo : règle %s approuvée mais sans seuil applicable", rule["empreinte"][:10])
        if not new_rules:
            return None
        base = generation_version(db.conn)
        if chall:
            rules = chall["regles"] + [r for r in new_rules if r["empreinte"] not in {x["empreinte"] for x in chall["regles"]}]
            db.conn.execute("UPDATE method_versions SET rules = ?, started_ts = ?, created_at = ? WHERE version = ?",
                            (json.dumps(rules, ensure_ascii=False), int(now.timestamp()),
                             now.isoformat(timespec="seconds"), chall["version"]))
            version = chall["version"]
        else:
            version = next_version(db.conn)
            have = {x["empreinte"] for x in champ["regles"]}
            rules = champ["regles"] + [r for r in new_rules if r["empreinte"] not in have]
            db.conn.execute(
                "INSERT INTO method_versions (version, created_at, settings, base, rules, role, started_ts) "
                "VALUES (?, ?, ?, ?, ?, 'challenger', ?)",
                (version, now.isoformat(timespec="seconds"),
                 json.dumps({"base": base, "filtres": rules, "champion_au_depart": champ["version"]}, ensure_ascii=False),
                 base, json.dumps(rules, ensure_ascii=False), int(now.timestamp())))
        for rule in new_rules:
            db.conn.execute("UPDATE rules SET decision = 'en_essai' WHERE hash = ?", (rule["empreinte"],))
    log.info("Labo : challenger %s en essai (%d règles)", version, len(rules))
    return challenger(db.conn)


def _names(rules: list[dict]) -> str:
    return ", ".join(f"« {r['nom']} »" for r in rules) or "aucun filtre"


def judge_challenger(db: LabDB, now: datetime | None = None, cfg: dict | None = None) -> str | None:
    """Bascule automatique ou retrait du challenger ; renvoie le verdict appliqué (ou None)."""
    now = now or datetime.now(timezone.utc)
    cfg = cfg or V.config(db.conn)
    chall = challenger(db.conn)
    if not chall:
        return None
    champ = champion(db.conn)
    cmp = compare(db.conn, champ, chall, cfg)
    v = verdict(cmp, cfg)
    if v == "attendre":
        return None
    new = [r for r in chall["regles"] if r["empreinte"] not in {x["empreinte"] for x in champ["regles"]}]
    c, h = cmp["champion"], cmp["challenger"]
    stamp = now.isoformat(timespec="seconds")
    with db.transaction():
        if v == "promouvoir":
            db.conn.execute("UPDATE method_versions SET role = 'ancienne', decided_at = ? WHERE version = ?",
                            (stamp, champ["version"]))
            db.conn.execute("UPDATE method_versions SET role = 'champion', decided_at = ?, note = ? WHERE version = ?",
                            (stamp, json.dumps(cmp, ensure_ascii=False), chall["version"]))
            for r in new:
                db.conn.execute("UPDATE rules SET decision = 'adoptee' WHERE hash = ?", (r["empreinte"],))
            text = (f"🧪 Méthode {chall['version']} en place : {_names(new)} s'applique désormais aux plans de JARVIS. "
                    f"En direct depuis sa création : {fr(h['esperance_r'], 3)} par trade sur {h['declenches']} trades, "
                    f"contre {fr(c['esperance_r'], 3)} sur {c['declenches']} pour {champ['version']} ; "
                    f"potentiel +10 capturé {h['capture']} fois (contre {c['capture']}).")
        else:
            db.conn.execute("UPDATE method_versions SET role = 'retiree', decided_at = ?, note = ? WHERE version = ?",
                            (stamp, json.dumps(cmp, ensure_ascii=False), chall["version"]))
            for r in new:
                db.conn.execute("UPDATE rules SET decision = 'retiree' WHERE hash = ?", (r["empreinte"],))
            why = "sans faire mieux" if not cmp["meilleur"] else "en perdant de la capture du potentiel +10"
            text = (f"🧪 Méthode {chall['version']} retirée : après {h['declenches']} trades en direct {why} "
                    f"({fr(h['esperance_r'], 3)} par trade contre {fr(c['esperance_r'], 3)} pour {champ['version']}). "
                    f"{champ['version']} reste en place ; {_names(new)} rejoint les idées écartées.")
        event(db, "bascule" if v == "promouvoir" else "retrait", chall["version"], text, cmp, now)
    log.info("Labo : %s", text)
    return v


def champion_path(db_file: str | None = None) -> Path:
    override = os.getenv("JARVIS_LAB_CHAMPION", "").strip()
    return Path(override) if override else Path(db_file or db_path()).parent / CHAMPION_FILE


def champion_payload(conn) -> dict:
    champ = champion(conn)
    chall = challenger(conn)
    return {"version": champ["version"], "base": base_of(champ, conn), "regles": champ["regles"],
            "challenger": chall["version"] if chall else None}


def write_champion(db: LabDB) -> bool:
    """Écrit lab_champion.json (lu par JARVIS en direct) s'il a changé ; écriture atomique."""
    if db.path == ":memory:":
        return False
    data = champion_payload(db.conn)
    path = champion_path(db.path)
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
        if {k: current.get(k) for k in data} == data:
            return False
    except (OSError, ValueError):
        pass
    _write_atomic(path, data | {"ecrit_le": now_iso()})
    return True


def step(db: LabDB, now: datetime | None = None) -> dict:
    """Passage du worker : décisions, types, challenger, bascule, fichier du champion. Chaque partie
    est isolée : une erreur n'empêche pas les suivantes."""
    now = now or datetime.now(timezone.utc)
    report: dict = {}
    for name, fn in (("decisions", lambda: ingest_decisions(db)), ("types", lambda: ingest_types(db)),
                     ("challenger", lambda: (c := ensure_challenger(db, now)) and c["version"]),
                     ("bascule", lambda: judge_challenger(db, now)), ("fichier", lambda: write_champion(db))):
        try:
            report[name] = fn()
        except Exception as exc:
            log.warning("Labo : méthode, étape %s en échec : %s: %s", name, type(exc).__name__, exc)
            report[name] = f"échec : {type(exc).__name__}"
    return report


def state(conn, cfg: dict | None = None) -> dict:
    """Champion, challenger et comparaison en direct (lecture seule : API, cockpit, !labo)."""
    champ = champion(conn)
    chall = challenger(conn)
    out = {"champion": {"version": champ["version"], "regles": [{"nom": r["nom"], "regle": rule_fr(r)}
                                                                for r in champ["regles"]]},
           "challenger": None}
    if chall:
        cfg = cfg or V.config(conn)
        cmp = compare(conn, champ, chall, cfg)
        out["challenger"] = {"version": chall["version"], "depuis": chall["creee_le"],
                             "regles": [{"nom": r["nom"], "regle": rule_fr(r)} for r in chall["regles"]],
                             "comparaison": cmp, "objectif": cfg["challenger_min_trades"],
                             "limite": cfg["challenger_max_trades"]}
    return out


def state_line(st: dict) -> str:
    champ = st["champion"]
    line = f"Méthode en place {champ['version'] or 'n/d'}" + (f" ({len(champ['regles'])} filtres)" if champ["regles"] else "")
    ch = st.get("challenger")
    if ch:
        cmp = ch["comparaison"]
        line += (f" · challenger {ch['version']} : {cmp['challenger']['declenches']}/{ch['objectif']} trades en direct, "
                 f"{fr(cmp['challenger']['esperance_r'], 3)} contre {fr(cmp['champion']['esperance_r'], 3)}")
    else:
        line += " · pas de challenger"
    return line
