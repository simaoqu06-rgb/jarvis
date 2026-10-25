"""Déclarations du council : le council du soir déclare et lance lui-même ses tests (25 octobre 2026).

Pourquoi : Simão a choisi « Autonomie totale » (carte « Laisser le council déclarer et lancer seul ses tests au
labo ? »). Jusque-là, une déclaration demandait un fil Claude, du code et un déploiement ; l'équipe du soir n'écrivait
que des hypothèses, et aucune n'a été testée. Ici une déclaration est une donnée, pas du code : un sous-ensemble d'une
stratégie déjà mesurée par le labo v2 (ses lignes de strat_trades), choisi par une condition connue au moment du
signal, avec une sortie déjà simulée. Rien n'est recalculé : le tableau la juge comme une stratégie (strat_stats).

Garde-fous, ceux des déclarations à la main :
- jugée seulement sur les séances APRÈS le jour de son dépôt (`vu_jusqu_au`, comme seen_until de la n° 6 et de la
  n° 11) : le council a lu tout l'historique visible avant de déclarer, donc seuls les jours suivants prouvent ;
- chaque déclaration compte pour un essai (trials) et relève le seuil des candidates pour toutes les autres ;
- bloc scellé jamais lu (le tableau ne le lit pas) ; jamais de condition de liquidité ni de capitalisation (règle de
  Simão : le rang de volume n'est pas un champ permis) ;
- une déclaration ne se retire pas et ne se reformule pas : la même (même empreinte) n'est comptée qu'une fois ;
- au plus PER_EVENING déclarations par soirée (garde-fou technique contre une boucle, pas un rythme imposé).
Le serveur web n'écrit jamais dans la base : il dépose un fichier dans data/lab_queue/declarations/, lu par le worker.
"""
import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.lab import method as M
from app.lab import strategies as ST
from app.lab.db import LabDB

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")
PREFIX = "council_"
PER_EVENING = 3
QUEUE_MAX = 20
NAME_MAX, WHY_MAX, COUNCIL_MAX = 120, 1000, 2000
# Champs des lignes du labo v2 connus au moment du signal (strat_trades, _row) ; jamais le rang de volume.
FIELDS = {
    "gap_open_pct": "écart d'ouverture du jour, en %",
    "atr_pct": "ATR 14 jours de la veille, en % du cours",
    "vol15_adv": "volume des 15 premières minutes rapporté au volume journalier moyen des 20 séances d'avant",
    "risk_pct": "distance du stop, en % de l'entrée",
    "sur_vwap": "1 si le prix est au-dessus du VWAP à la décision, 0 sinon",
    "ouverture_va": "ouverture par rapport à la zone de valeur de la veille : dessus, dedans, dessous",
    "stop_avant_entree": "1 si le stop a été touché avant l'entrée",
    "stop_structure": "1 si le stop structurel de la n° 11 s'applique",
    "regime": "régime de marché du jour (table regimes)",
    "jour_semaine": "jour de la semaine, 0 = dimanche … 6 = samedi",
    "heure_ny": "heure du signal à New York (entier, 9 à 15)",
}
OPS = (">", ">=", "<", "<=", "==", "!=", "in", "not_in", "between", "is_null", "not_null")
SCHEMA = """CREATE TABLE IF NOT EXISTS council_declarations (
    id INTEGER PRIMARY KEY, hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, soiree TEXT,
    seen_until TEXT NOT NULL, market TEXT NOT NULL, base TEXT NOT NULL, sortie TEXT NOT NULL, nom TEXT NOT NULL,
    quand TEXT NOT NULL, pourquoi TEXT, council TEXT
)"""


class DeclarationError(ValueError):
    pass


def _cond(c, depth: int = 0) -> dict:
    if depth > 6 or not isinstance(c, dict):
        raise DeclarationError("condition illisible (objet attendu, 6 niveaux au plus)")
    for key in ("and", "or"):
        if key in c:
            items = c[key]
            if not isinstance(items, list) or not 1 <= len(items) <= 8:
                raise DeclarationError(f"« {key} » : liste de 1 à 8 conditions")
            return {key: [_cond(x, depth + 1) for x in items]}
    if "not" in c:
        return {"not": _cond(c["not"], depth + 1)}
    field, op = c.get("field"), c.get("op")
    if field not in FIELDS:
        raise DeclarationError(f"champ « {field} » non permis (permis : {', '.join(FIELDS)})")
    if op not in OPS:
        raise DeclarationError(f"opérateur « {op} » inconnu (permis : {' '.join(OPS)})")
    out = {"field": field, "op": op}
    if op in ("is_null", "not_null"):
        return out
    value = c.get("value")
    if op in ("in", "not_in"):
        ok = isinstance(value, list) and 1 <= len(value) <= 20
    elif op == "between":
        ok = isinstance(value, list) and len(value) == 2 and all(isinstance(v, (int, float)) for v in value)
    else:
        ok = isinstance(value, (int, float, str)) and not isinstance(value, bool)
    if not ok:
        raise DeclarationError(f"valeur illisible pour {field} {op}")
    return out | {"value": value}


def parse(raw: dict) -> dict:
    """Déclaration canonique : {nom, market, base, sortie, quand} ; DeclarationError si elle est mal formée."""
    if not isinstance(raw, dict):
        raise DeclarationError("objet JSON attendu")
    name = raw.get("nom")
    if not isinstance(name, str) or not 3 <= len(name.strip()) <= NAME_MAX:
        raise DeclarationError(f"« nom » : texte de 3 à {NAME_MAX} caractères")
    market, base = raw.get("market"), raw.get("base")
    spec = ST.STRATEGIES.get(base)
    if not spec or "go" in spec:
        bases = [k for k, v in ST.STRATEGIES.items() if "go" not in v]
        raise DeclarationError(f"« base » : une stratégie du labo v2 ({', '.join(bases)})")
    if market not in spec["markets"]:
        raise DeclarationError(f"{base} n'est mesurée que sur : {', '.join(spec['markets'])}")
    exits = list(ST.strategy_exits(market, base))
    sortie = raw.get("sortie") or ST.ref_exit(base)
    if sortie not in exits:
        raise DeclarationError(f"« sortie » : une sortie simulée pour {base} ({', '.join(exits)})")
    if "quand" not in raw:
        raise DeclarationError("« quand » : la condition qui choisit les signaux est obligatoire")
    for key, limit in (("pourquoi", WHY_MAX), ("council", COUNCIL_MAX)):
        v = raw.get(key)
        if v is not None and (not isinstance(v, str) or len(v) > limit):
            raise DeclarationError(f"« {key} » : texte de {limit} caractères au plus")
    return {"nom": name.strip(), "market": market, "base": base, "sortie": sortie, "quand": _cond(raw["quand"])}


def decl_hash(canon: dict) -> str:
    """Empreinte sans le nom : la même condition reformulée n'est pas un nouvel essai."""
    core = {k: canon[k] for k in ("market", "base", "sortie", "quand")}
    return hashlib.sha256(json.dumps(core, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def queue_dir(db_file: str) -> Path:
    return M.queue_root(db_file) / "declarations"


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA)


def load(conn: sqlite3.Connection) -> list[dict]:
    """Déclarations du council, dans l'ordre de dépôt ; [] si la table n'existe pas encore (lecture seule)."""
    try:
        rows = conn.execute("SELECT id, hash, created_at, soiree, seen_until, market, base, sortie, nom, quand, "
                            "pourquoi, council FROM council_declarations ORDER BY id").fetchall()
    except sqlite3.OperationalError:
        return []
    keys = ("id", "empreinte", "creee_le", "soiree", "vu_jusqu_au", "market", "base", "sortie", "nom", "quand",
            "pourquoi", "council")
    out = []
    for r in rows:
        d = dict(zip(keys, r))
        d["quand"] = json.loads(d["quand"])
        d["strategie"] = f"{PREFIX}{d['id']}"
        out.append(d)
    return out


def get(conn: sqlite3.Connection, h: str) -> dict | None:
    return next((d for d in load(conn) if d["empreinte"] == h), None)


def evening_count(conn: sqlite3.Connection | None, db_file: str, soiree: str) -> int:
    n = 0
    if conn is not None:
        try:
            n = conn.execute("SELECT COUNT(*) FROM council_declarations WHERE soiree = ?", (soiree,)).fetchone()[0]
        except sqlite3.OperationalError:
            pass
    q = queue_dir(db_file)
    if q.exists():
        for f in q.glob("*.json"):
            try:
                n += json.loads(f.read_text(encoding="utf-8")).get("_soiree") == soiree
            except (OSError, ValueError):
                continue
    return n


def enqueue(db_file: str, raw: dict, soiree: str, conn: sqlite3.Connection | None = None) -> dict:
    """Serveur web : met la déclaration en file (fichier <empreinte>.json) ; DeclarationError si mal formée, déjà
    déclarée ou au-delà du garde-fou du soir."""
    canon = parse(raw)
    h = decl_hash(canon)
    known = get(conn, h) if conn is not None else None
    if known:
        return {"empreinte": h, "etat": "deja_declaree", "strategie": known["strategie"], "declaration": canon}
    q = queue_dir(db_file)
    target = q / f"{h}.json"
    if target.exists():
        return {"empreinte": h, "etat": "en_file", "declaration": canon}
    if evening_count(conn, db_file, soiree) >= PER_EVENING:
        raise DeclarationError(f"garde-fou : {PER_EVENING} déclarations au plus par soirée")
    q.mkdir(parents=True, exist_ok=True)
    if sum(1 for _ in q.glob("*.json")) >= QUEUE_MAX:
        raise DeclarationError(f"file pleine ({QUEUE_MAX} déclarations en attente du worker)")
    tmp = q / f".{h}.tmp"
    keep = {k: raw.get(k) for k in ("pourquoi", "council") if raw.get(k)}
    tmp.write_text(json.dumps(canon | keep | {"_soiree": soiree}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)
    return {"empreinte": h, "etat": "en_file", "declaration": canon}


def ingest(db: LabDB, now: datetime | None = None) -> int:
    """Worker : chaque déclaration en file entre en base, jugée sur les séances après le jour de son dépôt."""
    q = queue_dir(db.path)
    if not q.exists():
        return 0
    now = now or datetime.now(timezone.utc)
    ensure_table(db.conn)
    n = 0
    for f in sorted(q.glob("*.json")):
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
            canon = parse(raw)
            h = decl_hash(canon)
            if not get(db.conn, h):
                seen = now.astimezone(NY).date().isoformat()  # tout ce qui est journalisé ce jour-là a pu être lu
                with db.transaction():
                    cur = db.conn.execute(
                        "INSERT INTO council_declarations (hash, created_at, soiree, seen_until, market, base, sortie, "
                        "nom, quand, pourquoi, council) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (h, now.isoformat(timespec="seconds"), raw.get("_soiree"), seen, canon["market"],
                         canon["base"], canon["sortie"], canon["nom"], json.dumps(canon["quand"], ensure_ascii=False),
                         raw.get("pourquoi"), raw.get("council")))
                    M.event(db, M.COUNCIL, None,
                            f"🧪 Déclaration du council n° {cur.lastrowid} : « {canon['nom']} » ({describe(canon)}). "
                            f"Un essai de plus, jugé seulement sur les séances après le {seen}.",
                            {"empreinte": h}, now)
                n += 1
        except (DeclarationError, ValueError, TypeError, sqlite3.Error) as exc:
            log.warning("Labo : déclaration du council %s écartée (%s)", f.name, exc)
        f.unlink(missing_ok=True)
    return n


def context(day: str, signal_at: int | None, **fields) -> dict:
    """Valeurs des champs permis pour une ligne de strat_trades (strat_stats)."""
    ctx = {k: fields.get(k) for k in FIELDS if k not in ("jour_semaine", "heure_ny")}
    d = datetime.fromisoformat(day)
    ctx["jour_semaine"] = (d.weekday() + 1) % 7
    ctx["heure_ny"] = datetime.fromtimestamp(signal_at, NY).hour if signal_at else None
    return ctx


def matches(cond: dict, ctx: dict) -> bool:
    if "and" in cond:
        return all(matches(c, ctx) for c in cond["and"])
    if "or" in cond:
        return any(matches(c, ctx) for c in cond["or"])
    if "not" in cond:
        return not matches(cond["not"], ctx)
    x, op, v = ctx.get(cond["field"]), cond["op"], cond.get("value")
    if op == "is_null":
        return x is None
    if op == "not_null":
        return x is not None
    if x is None:
        return False
    try:
        if op == "in":
            return x in v
        if op == "not_in":
            return x not in v
        if op == "between":
            return v[0] <= x <= v[1]
        return {">": x > v, ">=": x >= v, "<": x < v, "<=": x <= v, "==": x == v, "!=": x != v}[op]
    except TypeError:
        return False


def cond_fr(cond: dict) -> str:
    if "and" in cond:
        return " et ".join(cond_fr(c) for c in cond["and"])
    if "or" in cond:
        return "(" + " ou ".join(cond_fr(c) for c in cond["or"]) + ")"
    if "not" in cond:
        return f"pas ({cond_fr(cond['not'])})"
    v = cond.get("value")
    return f"{cond['field']} {cond['op']}" + ("" if v is None else f" {v}")


def describe(d: dict) -> str:
    return f"{d['market']}, {d['base']} quand {cond_fr(d['quand'])}, sortie {d['sortie']}"
