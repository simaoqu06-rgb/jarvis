"""Archive d'audit des mesures remplacées par une nouvelle déclaration, et audit apparié ancien → nouveau (2e tour d'avis,
deuxième tour d'avis de GPT et Gemini, 10 octobre 2026 : scénario 4 de GPT, « garder l'ancien résultat comme
référence d'audit »).

Archive (step, en tête de chaque passage du worker et du rattrapage) : avant que le rattrapage, Killmode complet ou la
n° 9 ne réécrivent une ligne mesurée autrement, elle est copiée telle quelle (toutes ses colonnes) dans un fichier à
côté de lab.db : data/archives/labo2-avant-n<déclaration>.db (n° 10 : labo2-avant-n10.db), mêmes noms de tables que
la base :
- strat_trades : par marché, les lignes d'une autre déclaration que celle du marché (declaration NULL, d'avant le
  suivi, ou différente) ;
- kill_complet : les lignes d'une autre empreinte de coût (kill_complet.cost_fingerprint) ;
- plus10_trades : les lignes des séances d'une autre mesure (plus10.MESURE), et leurs mesures par séance
  (plus10_seances, la valeur de progress telle quelle).
Une clé de progress (job JOB) par marché et déclaration, par empreinte de coût, par mesure : faite une fois, plus
rien à lire ensuite. Si une table existe déjà dans le fichier (copie manuelle de DEPLOY.md, CREATE TABLE … AS SELECT
*), elle n'est ni recréée ni modifiée : on n'y ajoute que les lignes absentes (même clé que la base, avec le coût pour
Killmode complet). Ajouter plutôt que ne rien faire : une copie faite à la main peut dater d'avant ou d'après des jours
déjà réécrits, ou ne couvrir qu'une partie ; seule l'union garantit que toute ligne effacée par la suite est dans
l'archive, sans jamais toucher à ce qui y est déjà. Un index sur la clé y est ajouté (sans lui, chercher les absentes
parcourrait toute la table pour chaque ligne). Table archive_info : une ligne par archivage (heure, ce qui est
remplacé, lignes par table, note).
Jamais lues : les tables *_sealed. Le bloc scellé n'est d'ailleurs jamais dans strat_trades, kill_complet ni
plus10_trades.

Audit (report, cache CACHE_KEY recalculé par le worker) : les lignes archivées et les lignes de la déclaration en
vigueur appariées sur (origin, market, symbol, day, strategy, slot) ; par marché et par stratégie, sur la sortie de
référence (strategies.ref_exit, nette de la commission IBKR en actions comme le tableau) : nombre de paires,
déclenchements avant et après, et sur les paires déclenchées des deux côtés, moyenne de (nouveau − ancien) pour
l'espérance, le témoin et l'écart stratégie − témoin, avec IC en grappes par jour (semaines en crypto,
strat_stats.Mean). C'est la mesure sur données réelles du biais du simulateur en révision 1 (+0,23 R annoncé, mesuré
seulement en rejeu à blanc) et de l'« Effet attendu » déclaré de la n° 10, sur les mêmes trades.

L'archive ne sert JAMAIS à décider : ni « candidate », ni le grade, ni le brief, ni les leçons ne la lisent. Seuls ce
module, la route GET /api/lab/strategies/audit et la section « Audit » de `!strategies` y touchent
(tests/test_lab_n10_audit.py le vérifie par un balayage du code).
"""
import json
import logging
import sqlite3
from pathlib import Path

from app.lab import empreinte as E
from app.lab import stats as STATS
from app.lab import strat_stats as SS
from app.lab import strategies as ST
from app.lab.db import LabDB, now_iso

log = logging.getLogger(__name__)
JOB = "audit:archive"
FOLDER = "archives"
PREFIX = "labo2-avant-n"
ALIAS = "avant"
CACHE_KEY = "audit_declaration"
# Ce que la déclaration a changé dans les mesures (note d'archive_info).
NOTES = {10: "mesures d'avant la n° 10 : simulateur révision 1, témoin sur la journée, 5 tirages, prix ajustés"}
STRAT_KEY = ("origin", "market", "symbol", "day", "strategy", "slot")
KILL_KEY = ("day", "symbol", "cout")
PLUS10_KEY = ("day", "symbol", "essai")
# n° 9 : séances calculées sous une autre mesure (leurs listes et amplitudes sont aussi réécrites, même sans ligne).
SESSIONS = "job = ? AND json_extract(value, '$.mesure') IS NOT ? AND json_extract(value, '$.absente') IS NULL"
INFO_DDL = (f"CREATE TABLE IF NOT EXISTS {ALIAS}.archive_info (archived_at TEXT NOT NULL, cle TEXT NOT NULL, "
            "tables TEXT, remplace TEXT, declaration INTEGER, lignes TEXT, note TEXT)")


def declaration(market: str) -> int:
    return ST.CRYPTO_DECLARATION if market == "crypto" else ST.DECLARATION


def archive_path(db_file: str | Path, decl: int) -> Path:
    """data/archives/labo2-avant-n<decl>.db, à côté de la base du labo."""
    return Path(db_file).parent / FOLDER / f"{PREFIX}{decl}.db"


def note(decl: int) -> str:
    return NOTES.get(decl, f"mesures d'avant la n° {decl}")


# --- Archive (worker) ---------------------------------------------------------------------------------------------

def _jobs() -> list[dict]:
    """Ce qui peut être à archiver, en lecture : clé de progress, fichier (déclaration), table, sélection et clé."""
    from app.lab import kill_complet as KC  # imports tardifs : tirent killmode
    from app.lab import plus10 as P10

    out = [{"key": f"strats:{m}:d{declaration(m)}", "decl": declaration(m), "table": "strat_trades", "unique": STRAT_KEY,
            "where": "market = ? AND declaration IS NOT ?", "args": (m, declaration(m)),
            "replaced": "SELECT DISTINCT declaration FROM main.strat_trades WHERE market = ? AND declaration IS NOT ?"}
           for m in ("stock", "crypto")]
    cost = KC.cost_fingerprint()
    out.append({"key": f"kill_complet:{cost}", "decl": ST.DECLARATION, "table": "kill_complet", "unique": KILL_KEY,
                "where": "cout IS NOT ?", "args": (cost,),
                "replaced": "SELECT DISTINCT cout FROM main.kill_complet WHERE cout IS NOT ?"})
    other = "day NOT IN (SELECT key FROM main.progress WHERE job = ? AND json_extract(value, '$.mesure') = ?)"
    out.append({"key": f"plus10:m{P10.MESURE}", "decl": ST.DECLARATION, "table": "plus10_trades", "unique": PLUS10_KEY,
                "where": other, "args": (P10.JOB, P10.MESURE), "mesure": P10.MESURE, "job": P10.JOB,
                "replaced": f"SELECT DISTINCT json_extract(value, '$.mesure') FROM main.progress WHERE {SESSIONS}"})
    return out



def _columns(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA {schema}.table_info({table})")]


def _copy(conn: sqlite3.Connection, table: str, unique: tuple, where: str, args: tuple) -> tuple[int, int]:
    """Lignes de main.table choisies par `where` ajoutées à l'archive si absentes (clé `unique`) ; la table d'archive
    est créée vide au schéma de la base si elle n'existe pas. Renvoie (lignes choisies, lignes ajoutées)."""
    if table not in {r[0] for r in conn.execute(f"SELECT name FROM {ALIAS}.sqlite_master WHERE type = 'table'")}:
        conn.execute(f"CREATE TABLE {ALIAS}.{table} AS SELECT * FROM main.{table} WHERE 0")
    conn.execute(f"CREATE INDEX IF NOT EXISTS {ALIAS}.{table}_audit ON {table} ({', '.join(unique)})")
    have = set(_columns(conn, ALIAS, table))
    cols = [c for c in _columns(conn, "main", table) if c in have]  # copie manuelle d'avant une colonne : sans elle
    match = " AND ".join(f"a.{c} IS s.{c}" for c in unique)
    total = conn.execute(f"SELECT COUNT(*) FROM main.{table} WHERE {where}", args).fetchone()[0]
    cur = conn.execute(f"INSERT INTO {ALIAS}.{table} ({', '.join(cols)}) SELECT {', '.join('s.' + c for c in cols)} "
                       f"FROM main.{table} s WHERE {where} AND NOT EXISTS "
                       f"(SELECT 1 FROM {ALIAS}.{table} a WHERE {match})", args)
    return total, cur.rowcount


def _sessions(conn: sqlite3.Connection, job: str, mesure: int) -> int:
    """n° 9 : mesures par séance (listes, amplitudes…) d'une autre mesure, telles quelles (plus10_seances)."""
    conn.execute(f"CREATE TABLE IF NOT EXISTS {ALIAS}.plus10_seances (day TEXT PRIMARY KEY, value TEXT)")
    return conn.execute(f"INSERT OR IGNORE INTO {ALIAS}.plus10_seances SELECT key, value FROM main.progress WHERE "
                        f"{SESSIONS}", (job, mesure)).rowcount


def pending(db: LabDB) -> list[dict]:
    """Archivages pas encore faits qui ont des lignes à garder ; ceux qui n'en ont pas sont notés faits."""
    out = []
    for job in _jobs():
        if db.get(JOB, job["key"]):
            continue
        try:
            n = db.conn.execute(f"SELECT COUNT(*) FROM {job['table']} WHERE {job['where'].replace('main.', '')}",
                                job["args"]).fetchone()[0]
        except sqlite3.OperationalError:  # table absente (base d'avant la déclaration qui l'a créée)
            n = 0
        if "job" in job:  # n° 9 : des séances sans ligne ont aussi leurs mesures à garder
            n += db.conn.execute(f"SELECT COUNT(*) FROM progress WHERE {SESSIONS}", job["args"]).fetchone()[0]
        if n:
            out.append(job)
        else:
            db.set(JOB, job["key"], {"lignes": 0, "le": now_iso()})
    return out


def step(db: LabDB) -> dict:
    """Archive ce qui doit l'être avant toute réécriture ; idempotent (une lecture de progress par clé une fois fait).
    Une erreur remonte : l'appelant ne réécrit rien tant que l'archive n'est pas faite. Base en mémoire : rien.
    L'archive est validée avant la clé de progress : une panne entre les deux refait l'archivage (sans doublon), jamais
    l'inverse (clé posée, lignes absentes du fichier)."""
    if db.path == ":memory:":
        return {}
    todo = pending(db)
    out: dict = {}
    for decl in sorted({j["decl"] for j in todo}):
        path = archive_path(db.path, decl)
        path.parent.mkdir(parents=True, exist_ok=True)
        db.conn.execute(f"ATTACH DATABASE ? AS {ALIAS}", (str(path),))
        try:
            for job in (j for j in todo if j["decl"] == decl):
                with db.transaction():
                    db.conn.execute(INFO_DDL)
                    replaced = sorted({r[0] for r in db.conn.execute(job["replaced"], job["args"])}, key=str)
                    total, added = _copy(db.conn, job["table"], job["unique"], job["where"], job["args"])
                    lignes = {job["table"]: {"choisies": total, "ajoutees": added, "deja_la": total - added}}
                    if job["table"] == "plus10_trades":
                        lignes["plus10_seances"] = {"ajoutees": _sessions(db.conn, job["job"], job["mesure"])}
                    db.conn.execute(f"INSERT INTO {ALIAS}.archive_info VALUES (?, ?, ?, ?, ?, ?, ?)",
                                    (now_iso(), job["key"], job["table"],
                                     json.dumps([("inconnue (d'avant le suivi)" if x is None else x) for x in replaced],
                                                ensure_ascii=False), decl, json.dumps(lignes), note(decl)))
                db.set(JOB, job["key"], {"fichier": path.name, "lignes": total, "ajoutees": added, "le": now_iso()})
                out[job["key"]] = lignes
        finally:
            db.conn.execute(f"DETACH DATABASE {ALIAS}")
        log.info("Labo : archive d'audit %s : %s", path.name, out)
    return out


# --- Audit apparié (lecture seule) ----------------------------------------------------------------------------------

def _ref(x: str) -> str:
    """Sortie de référence de la ligne `x`, nette de la commission IBKR en actions (strat_stats.ibkr_commission_r)."""
    go = ", ".join(f"'{g}'" for g in ST.GO_STRATEGIES)
    return (f"(CASE WHEN {x}.strategy IN ({go}) THEN {x}.r_sans_be ELSE {x}.r_net END - CASE WHEN {x}.market = 'stock' "
            f"AND {x}.entry > {x}.stop THEN 2 * MIN({SS.IBKR_PER_SHARE}, {SS.IBKR_MAX_PCT} * {x}.entry) / "
            f"({x}.entry - {x}.stop) ELSE 0 END)")


def _col(x: str) -> str:
    go = ", ".join(f"'{g}'" for g in ST.GO_STRATEGIES)
    return f"(CASE WHEN {x}.strategy IN ({go}) THEN {x}.r_sans_be ELSE {x}.r_net END)"


def _sql(markets: list[str]) -> str:
    both = "a.triggered = 1 AND n.triggered = 1"
    wit = f"{both} AND a.temoin_triggered = 1 AND n.temoin_triggered = 1"
    diffs = {"esperance": (both, f"{_ref('n')} - {_ref('a')}"), "r_net": (both, "n.r_net - a.r_net"),
             "temoin": (wit, "n.temoin_r - a.temoin_r"),
             "ecart": (wit, f"({_col('n')} - n.temoin_r) - ({_col('a')} - a.temoin_r)")}
    sums = ", ".join(f"SUM(CASE WHEN {cond} THEN {expr} END), COUNT(CASE WHEN {cond} THEN {expr} END)"
                     for cond, expr in diffs.values())
    on = " AND ".join(f"a.{c} = n.{c}" for c in STRAT_KEY)
    return (f"SELECT n.market, n.strategy, n.day, COUNT(*), SUM(a.triggered = 1), SUM(n.triggered = 1), "
            f"SUM({both}), {sums} FROM main.strat_trades n JOIN {ALIAS}.strat_trades a ON {on} "
            f"WHERE n.market IN ({', '.join('?' * len(markets))}) AND n.declaration = CASE n.market WHEN 'crypto' "
            f"THEN {int(ST.CRYPTO_DECLARATION)} ELSE {int(ST.DECLARATION)} END GROUP BY n.market, n.strategy, n.day")


DIFFS = ("esperance", "r_net", "temoin", "ecart")  # dans l'ordre des colonnes de _sql


def _mean(market: str) -> SS.Mean:
    return SS.Mean(SS._week if market == "crypto" else None)  # comme le tableau : semaines ISO en crypto


def _add(m: SS.Mean, day: str, total, n) -> None:
    if n:
        cell = m.days[day]
        cell[0] += total
        cell[1] += n
        m.total += total
        m.n += n


def _pairs(conn: sqlite3.Connection, markets: list[str]) -> dict:
    out: dict = {}
    for row in conn.execute(_sql(markets), markets):
        market, name, day, pairs, before, after, both = row[:7]
        mk = out.setdefault(market, {"strategies": {}, "ensemble": {k: _mean(market) for k in DIFFS}})
        s = mk["strategies"].setdefault(name, {"paires": 0, "declenchees_avant": 0, "declenchees_apres": 0,
                                               "declenchees_les_deux": 0, **{k: _mean(market) for k in DIFFS}})
        s["paires"] += pairs
        s["declenchees_avant"] += before or 0
        s["declenchees_apres"] += after or 0
        s["declenchees_les_deux"] += both or 0
        for i, k in enumerate(DIFFS):
            total, n = row[7 + 2 * i], row[8 + 2 * i]
            _add(s[k], day, total, n)
            _add(mk["ensemble"][k], day, total, n)
    return out


def _attach(conn: sqlite3.Connection, path: Path, uri: bool) -> None:
    """Lecture seule quand la connexion comprend les URI (serveur : db.open_readonly) ; sinon le chemin seul (worker)."""
    target = f"file:{path.resolve()}?mode=ro" if uri else str(path)
    conn.execute(f"ATTACH DATABASE ? AS {ALIAS}", (target,))


def report(conn: sqlite3.Connection, uri: bool = False) -> dict | None:
    """Audit apparié ancien → nouveau, par marché et par stratégie ; None sans archive (ou base en mémoire). `uri` :
    connexion du serveur (db.open_readonly), archive ouverte en lecture seule ; sinon (worker), l'index de la clé est
    ajouté s'il manque (copie manuelle d'avant le premier archivage : sans lui, l'appariement parcourt la table)."""
    main = next((r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"), "")
    if not main:
        return None
    files: dict[int, list[str]] = {}
    for market in ("stock", "crypto"):
        files.setdefault(declaration(market), []).append(market)
    out: dict = {"declaration": ST.LATEST_DECLARATION, "marches": {}, "archives": {}}
    for decl, markets in files.items():
        path = archive_path(main, decl)
        if not path.exists():
            continue
        _attach(conn, path, uri)
        try:
            tables = {r[0] for r in conn.execute(f"SELECT name FROM {ALIAS}.sqlite_master WHERE type = 'table'")}
            info = ([dict(zip(("le", "cle", "tables", "remplace", "declaration", "lignes", "note"), r)) for r in
                     conn.execute(f"SELECT * FROM {ALIAS}.archive_info ORDER BY archived_at")]
                    if "archive_info" in tables else [])
            out["archives"][path.name] = {"tables": sorted(tables), "archivages": info, "note": note(decl)}
            if "strat_trades" not in tables:
                continue
            if not uri:
                conn.execute(f"CREATE INDEX IF NOT EXISTS {ALIAS}.strat_trades_audit ON strat_trades "
                             f"({', '.join(STRAT_KEY)})")
            archived = dict(conn.execute(f"SELECT market, COUNT(*) FROM {ALIAS}.strat_trades GROUP BY market"))
            for market, mk in _pairs(conn, markets).items():
                out["marches"][market] = {
                    "archivees": archived.get(market, 0),
                    "appariees": sum(s["paires"] for s in mk["strategies"].values()),
                    "ensemble": {k: v.result() for k, v in mk["ensemble"].items()},
                    "strategies": {name: {"nom": ST.LABELS.get(name, name), "sortie": ST.ref_exit(name),
                                          **{k: (v.result() if isinstance(v, SS.Mean) else v) for k, v in s.items()}}
                                   for name, s in sorted(mk["strategies"].items())}}
        finally:
            conn.execute(f"DETACH DATABASE {ALIAS}")
    return out if out["archives"] else None


# --- Cache (écrit par le worker, lu par le serveur) ------------------------------------------------------------------

def signature(conn: sqlite3.Connection) -> list:
    """Archivages faits, compteur rev:strat (lignes réécrites) et code des stats."""
    done = sorted(r[0] for r in conn.execute("SELECT key FROM progress WHERE job = ?", (JOB,)))
    return [done, *E.sig(conn, "strat")]


def cached(conn: sqlite3.Connection) -> dict | None:
    return STATS.cached(conn, CACHE_KEY)


def refresh(db: LabDB, now, max_age: float) -> int | None:
    """Worker : audit recalculé quand l'archive ou les lignes ont changé, au plus toutes les `max_age` secondes (tout de
    suite après un déploiement) ; renvoie le nombre de paires, ou None."""
    sig = signature(db.conn)
    last = db.get("audit", "signature")
    at = db.get("audit", "at", 0) or 0
    if sig == last or (cached(db.conn) and now.timestamp() - at < max_age and not E.code_changed(last)):
        return None
    data = report(db.conn)
    pairs = sum(m["appariees"] for m in (data or {}).get("marches", {}).values())
    with db.transaction():
        if data is None:
            db.conn.execute("DELETE FROM stats_cache WHERE key = ?", (CACHE_KEY,))
        else:
            STATS.store(db.conn, CACHE_KEY, data, pairs, now_iso())
        db.set("audit", "signature", sig)
        db.set("audit", "at", now.timestamp())
    return pairs


# --- Discord ---------------------------------------------------------------------------------------------------------

def _k(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def _ci(res: dict) -> str:
    ci = res.get("ic95")
    return f" [{SS._r(ci[0])} ; {SS._r(ci[1])}]" if ci else ""


def text(rep: dict | None) -> str | None:
    """Section courte de `!strategies` ; None sans archive."""
    if not rep:
        return None
    lines = [f"🔎 **Audit de la n° {rep['declaration']} (ancien → nouveau, mêmes trades)** : pour audit, jamais pour "
             "décider · nouveau − ancien sur les paires déclenchées des deux côtés, IC en grappes"]
    if not rep["marches"]:
        lines.append("Aucune ligne archivée n'est encore refaite par la n° " f"{rep['declaration']}.")
    for market, m in rep["marches"].items():
        e = m["ensemble"]
        per = [f"{name} {SS._r(s['ecart']['esperance_r'])}" for name, s in m["strategies"].items()
               if (s["ecart"]["n"] or 0) >= SS.MIN_N]
        lines.append(f"{SS.MARKET_FR.get(market, market)} : {_k(m['appariees'])} lignes appariées sur "
                     f"{_k(m['archivees'])} archivées · espérance {SS._r(e['esperance']['esperance_r'])} R"
                     f"{_ci(e['esperance'])} · témoin {SS._r(e['temoin']['esperance_r'])} R · écart au témoin "
                     f"{SS._r(e['ecart']['esperance_r'])} R{_ci(e['ecart'])} ({_k(e['ecart']['n'] or 0)} trades)"
                     + (" · écart au témoin par stratégie : " + " ; ".join(per) if per else ""))
    return "\n".join(lines)
