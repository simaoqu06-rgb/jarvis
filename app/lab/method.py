"""Méthodes du labo (Labo 3) : champion, challenger et filtres des règles approuvées par Simão.

- Une règle validée (validator.py) et approuvée par Simão crée la version suivante : v2 = v1 + ses
  filtres. Elle est d'abord CHALLENGER : le labo compare, sur les trades en direct journalisés depuis sa
  création et sur les seuls marchés de ses nouvelles règles, ce que garde le challenger et ce que garde
  le champion (mêmes plans, filtres différents ; rien n'est journalisé deux fois). R comparé : pour les
  actions, celui de la mise de Simão (grade.cost_r, au barème IBKR du compte) ; écart et z en grappes par
  jour (validator.pooled_z). Jugé seulement à des regards fixes (`challenger_looks_days`, en jours de
  trades distincts) : écart ≥ challenger_min_gain, z ≥ seuil du challenger n° k (validator.challenger_z :
  alpha dépensé sur le rang k, Bonferroni sur 3 regards, t de Student ; k mémorisé à sa création), aucun
  marché touché en recul, capture du potentiel +10 tenue, au moins challenger_min_trades trades.
  Promouvable, il n'est PAS mis en place seul : une proposition « promouvoir vX » attend l'accord de
  Simão (même circuit que les règles) ; sans réponse, il reste mesuré et le regard suivant remplace la
  proposition. Pas assez net au dernier regard, il est retiré ; une promotion du dernier regard restée
  sans réponse `challenger_answer_days` jours aussi. Seuils dans lab_config (validator.DEFAULTS).
- Une promotion note la frontière de ce qui était journalisé (« vu_a_la_promotion », comme les leçons) :
  le grade des marchés touchés repart des jours d'après (grade._scope), jamais des jours de l'essai.
- Une règle refusée est rangée dans la mémoire des échecs (« refusée par Simão ») : jamais reproposée.
- Le champion est écrit dans un petit fichier JSON (lab_champion.json, à côté de la base) que JARVIS
  en direct lit : ses filtres s'y appliquent avec EXACTEMENT le même code SQL que le labo (kept_sql,
  évalué en direct sur une base SQLite en mémoire, voir evaluate_rows).
- Le serveur web n'écrit jamais dans la base : les décisions de Simão et les types de catalyseurs
  passent par la file data/lab_queue/ (sous-dossiers decisions/ et types/), lue par le worker.
"""
import hashlib
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
MARKET_FR = {"stock": "actions", "crypto": "crypto"}


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


def version_settings(conn, version: str | None) -> dict:
    """Données gardées avec une version (colonne settings) ; {} si absente ou illisible."""
    try:
        row = conn.execute("SELECT settings FROM method_versions WHERE version = ?", (version,)).fetchone()
        data = json.loads(row[0]) if row and row[0] else {}
    except (sqlite3.OperationalError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def judged_challengers(conn, exclude: str | None = None) -> int:
    """Challengers (versions filtres) jugés à au moins un regard, hors `exclude` : ceux qui ont dépensé leur alpha.
    Un challenger retiré sans aucun regard (filet des 100 jours) n'a rien dépensé."""
    return sum(1 for v in versions(conn) if v.get("base") and v["version"] != exclude
               and (_progress(conn, f"regard:{v['version']}") or 0) > 0)


def challenger_rank(conn, chall: dict) -> int:
    """Rang k du challenger dans la dépense de l'alpha (validator.challenger_z) : 1 + les challengers précédents
    jugés à au moins un regard. Mémorisé à sa création (settings) pour ne pas bouger pendant l'essai ; calculé de
    la même façon pour un challenger créé avant ce suivi (un seul challenger à la fois : les autres sont finis)."""
    k = version_settings(conn, chall["version"]).get("rang_challenger")
    if isinstance(k, int) and not isinstance(k, bool) and k >= 1:
        return k
    return 1 + judged_challengers(conn, chall["version"])


def base_of(v: dict, conn) -> str | None:
    return v.get("base") or v.get("version") or generation_version(conn)


def next_version(conn) -> str:
    nums = [int(m.group(1)) for (v,) in conn.execute("SELECT version FROM method_versions")
            if (m := re.fullmatch(r"v(\d+)", v))]
    return f"v{max(nums, default=1) + 1}"


def new_rules(champ: dict, chall: dict) -> list[dict]:
    have = {x["empreinte"] for x in champ["regles"]}
    return [r for r in chall["regles"] if r["empreinte"] not in have]


def touched_markets(champ: dict, chall: dict) -> list[str] | None:
    """Marchés des nouvelles règles du challenger ; None : tous (une nouvelle règle sans `markets`)."""
    new = new_rules(champ, chall)
    if not new or any(not r.get("markets") for r in new):
        return None
    return sorted({m for r in new for m in r["markets"]})


def market_rules(rules: list[dict], market: str) -> list[dict]:
    """Règles qui s'appliquent au marché (`markets` absent : tous les marchés)."""
    return [r for r in rules if not r.get("markets") or market in r["markets"]]


def filters_key(rules: list[dict], market: str) -> str:
    """Empreinte des filtres EFFECTIFS d'un marché (règles qui le touchent, seuils figés compris) : une promotion
    qui ne la change pas ne change aucun trade gardé sur ce marché (grade gardé)."""
    items = sorted(json.dumps({"kind": r["kind"], "cond": r["cond"]}, sort_keys=True, ensure_ascii=False)
                   for r in market_rules(rules, market))
    return hashlib.sha256(json.dumps(items).encode()).hexdigest()[:16]


def compared_r(market: str, result: str | None, r_net: float) -> float:
    """R comparé : le R du grade (grade.cost_r) ; pour les actions, celui de la mise de Simão (frais IBKR réels,
    au barème du compte) quand le résultat du simulateur le permet ; sinon le R net du labo."""
    from app.lab import grade as GR  # import tardif : grade importe ce module

    return GR.cost_r(market, r_net, result)


def measure_days(day_sums: dict) -> dict:
    """Écart challenger - champion et son z, en grappes par jour ({jour: [ΣR champion, n, ΣR challenger, n]},
    l'estimateur du validateur pour des ensembles emboîtés)."""
    z, delta = V.pooled_z(day_sums)
    return {"jours": len(day_sums), "z": None if z is None else round(z, 3),
            "delta_r": None if delta is None else round(delta, 4)}


def compare(conn, champ: dict, chall: dict, cfg: dict | None = None) -> dict:
    """Champion contre challenger sur les trades EN DIRECT journalisés depuis la création du challenger
    (même version de génération, mêmes plans), sur les seuls marchés de ses nouvelles règles : trades
    déclenchés gardés, R moyen (actions : à la mise de Simão), écart et z par jour, capture du potentiel +10."""
    cfg = cfg or V.config(conn)
    base = base_of(chall, conn)
    ce, cp = kept_sql(champ["regles"])
    he, hp = kept_sql(chall["regles"])
    since = int(chall.get("debut_ts") or 0)
    markets = touched_markets(champ, chall)
    where, wp = (f" AND t.market IN ({', '.join('?' * len(markets))})", markets) if markets else ("", [])
    rows = conn.execute(
        f"SELECT t.day, t.market, t.symbol, t.triggered, t.r_net, t.tp1_hit, t.result, {ce}, {he} FROM trades t "
        "LEFT JOIN regimes r ON r.market = t.market AND r.day = t.day "
        f"WHERE t.method_version = ? AND t.origin = 'live' AND t.planned_at >= ?{where}",
        (*cp, *hp, base, since, *wp)).fetchall()
    acc = {"champion": [0, 0.0], "challenger": [0, 0.0]}
    first_day = None
    caught: dict[str, set] = {"champion": set(), "challenger": set()}
    by_market: dict[str, dict] = {}
    for day, market, symbol, trig, r, tp1, result, keep_c, keep_h in rows:
        first_day = day if first_day is None or day < first_day else first_day
        if not keep_c or not trig or r is None:
            continue
        r = compared_r(market, result, r)
        d = by_market.setdefault(market, {}).setdefault(day, [0.0, 0, 0.0, 0])
        d[0] += r
        d[1] += 1
        if keep_h:
            d[2] += r
            d[3] += 1
        for name, kept in (("champion", keep_c), ("challenger", keep_h)):
            if not kept:
                continue
            acc[name][0] += 1
            acc[name][1] += r
            if tp1:
                caught[name].add((market, day, symbol))
    movers = set()
    if first_day:
        movers = {tuple(m) for m in conn.execute(
            "SELECT market, day, symbol FROM day_moves WHERE up_pct >= ? AND day >= ?", (STATS.MOVE_PCT, first_day))}
    out = {"base": base, "depuis_ts": since, "trades_lignes": len(rows), "marches": markets}
    day_sums: dict = {}
    for days in by_market.values():
        for day, v in days.items():
            day_sums[day] = [a + b for a, b in zip(day_sums.get(day, [0.0, 0, 0.0, 0]), v)]
    for name, (i, j) in (("champion", (0, 1)), ("challenger", (2, 3))):
        n, total = acc[name]
        # IC95 de l'espérance absolue (grappes par jour, t de Student, comme le grade) : l'étiquette absolue
        # (absolute_fr) dit « positive mais non prouvée » quand sa borne basse n'est pas > 0.
        ci = STATS.mean_ci({d: (v[i], v[j]) for d, v in day_sums.items() if v[j]}, total, n)[2] if n else None
        out[name] = {"version": (champ if name == "champion" else chall)["version"], "declenches": n,
                     "esperance_r": round(total / n, 4) if n else None, "ic95": ci,
                     "capture": len(caught[name] & movers)}
    before, after = out["champion"]["capture"], out["challenger"]["capture"]
    allowed = before * cfg["max_capture_drop"] if before >= 10 else 1
    out["capture_ok"] = before - after <= allowed + 1e-9
    out["baisse_capture_permise"] = allowed
    out.update(measure_days(day_sums))
    out["par_marche"] = {m: measure_days(days) for m, days in sorted(by_market.items())}
    out["meilleur"] = out["delta_r"] is not None and out["delta_r"] >= cfg["challenger_min_gain"]
    return out


def looks(cfg: dict) -> list[int]:
    return sorted(int(k) for k in cfg["challenger_looks_days"])


def look_reached(cmp: dict, cfg: dict, done: int = 0) -> int | None:
    """Regard fixe (en jours de trades) atteint et pas encore fait, le plus avancé si plusieurs ; None
    entre deux regards."""
    passed = [k for k in looks(cfg) if done < k <= cmp["jours"]]
    return passed[-1] if passed else None


def next_look(cfg: dict, done: int = 0) -> int | None:
    return next((k for k in looks(cfg) if k > done), None)


def judged_cfg(conn, chall: dict, cmp: dict, cfg: dict, upcoming: int | None = None) -> dict:
    """`cfg` avec le seuil z EFFECTIF du challenger (challenger_min_z) et son rang (challenger_rang), lus par
    promotion_fails, verdict, promotion_text et les affichages : la décision et les textes disent le même seuil.
    Jours du regard : ceux de la comparaison ; `upcoming` (prochain regard) pour l'affichage entre deux regards,
    le seuil qui s'appliquera alors."""
    k = challenger_rank(conn, chall)
    return cfg | {"challenger_min_z": V.challenger_z(k, max(cmp["jours"], upcoming or 0), cfg), "challenger_rang": k}


def z_min(cmp: dict, cfg: dict) -> float:
    """Seuil z effectif : celui de judged_cfg, sinon celui d'un premier challenger aux jours de la comparaison."""
    z = cfg.get("challenger_min_z")
    return z if z is not None else V.challenger_z(cfg.get("challenger_rang", 1), cmp["jours"], cfg)


def threshold_fr(cmp: dict, cfg: dict) -> str:
    """« seuil z 2,53 (challenger n° 1) »."""
    return f"seuil z {num_fr(z_min(cmp, cfg))} (challenger n° {cfg.get('challenger_rang', 1)})"


def promotion_fails(cmp: dict, cfg: dict) -> list[str]:
    """Conditions de promotion non tenues (vide : promouvable)."""
    fails = []
    n, delta, z = cmp["challenger"]["declenches"], cmp["delta_r"], cmp["z"]
    if n < cfg["challenger_min_trades"]:
        fails.append(f"{n} trades gardés (minimum {cfg['challenger_min_trades']})")
    if delta is None or delta < cfg["challenger_min_gain"]:
        fails.append(f"écart {fr(delta, 3)} par trade (minimum {fr(cfg['challenger_min_gain'])})")
    if z is None or z < z_min(cmp, cfg):
        fails.append(f"z = {num_fr(z)}, {threshold_fr(cmp, cfg)}")
    for m, x in cmp["par_marche"].items():
        if x["delta_r"] is not None and x["delta_r"] < 0:
            fails.append(f"{MARKET_FR.get(m, m)} : {fr(x['delta_r'], 3)} par trade")
    if not cmp["capture_ok"]:
        fails.append(f"capture du potentiel +10 en baisse ({cmp['champion']['capture']} → "
                     f"{cmp['challenger']['capture']})")
    return fails


def promotion_expired(pending, now: datetime | None, cfg: dict) -> bool:
    """Promotion proposée depuis au moins `challenger_answer_days` jours sans réponse de Simão (`pending` : la
    promotion en attente, avec sa date « le » ; un simple booléen n'expire jamais)."""
    if not isinstance(pending, dict) or not pending.get("le") or now is None:
        return False
    try:
        at = datetime.fromisoformat(pending["le"])
    except (TypeError, ValueError):
        return False
    at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    return (now - at).total_seconds() >= cfg["challenger_answer_days"] * 86400


def verdict(cmp: dict, cfg: dict, now: datetime | None = None, done: int = 0, pending=False) -> str:
    """« promouvoir » (proposé à Simão), « retirer » ou « attendre ». Jugé seulement aux regards fixes
    `challenger_looks_days` (jours de trades distincts ; `done` : dernier regard fait) : promouvable si
    écart ≥ challenger_min_gain, z ≥ seuil du challenger (z_min), aucun marché touché en recul, capture
    tenue et au moins challenger_min_trades trades ; sinon retrait au dernier regard. Entre deux regards :
    « attendre ». `pending` : promotion en attente de Simão (dict, ou booléen). Elle protège le challenger
    du filet (retrait après `challenger_max_days` jours) pendant `challenger_answer_days` jours ; au-delà,
    plus rien ne la protège, et une promotion du DERNIER regard (qu'aucun regard ne remplacera) est retirée."""
    look = look_reached(cmp, cfg, done)
    if look is not None:
        if not promotion_fails(cmp, cfg):
            return "promouvoir"
        return "retirer" if look >= looks(cfg)[-1] else "attendre"
    expired = promotion_expired(pending, now, cfg)
    if expired and done >= looks(cfg)[-1]:
        return "retirer"
    since = cmp.get("depuis_ts") or 0
    if (not pending or expired) and now is not None and since and \
            now.timestamp() - since >= cfg["challenger_max_days"] * 86400:
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


def _kept_mean(windows: list[dict]) -> float | None:
    """Espérance des trades gardés sur les périodes de test (moyenne des fenêtres pondérée par leurs trades gardés) ;
    None si une fenêtre ne la donne pas (règle testée avant la colonne)."""
    total = n = 0.0
    for w in windows:
        e, kept = w.get("esperance_apres_r"), (w.get("trades_test") or 0) - (w.get("touches") or 0)
        if e is None or kept <= 0:
            return None
        total += e * kept
        n += kept
    return total / n if n else None


def days_fr(m: dict) -> str | None:
    """Catégorie des jours après la frontière (validator.day_categories, avis de GPT du 10 octobre 2026) : « Jours
    neufs depuis la soumission : 70 historiques jamais lus (rejeu), 5 du direct. » ; sans jour du direct, la règle
    n'est validée que sur l'historique et c'est le challenger, jugé sur le direct seul, qui décidera. Pas « jours de
    test » : le compte inclut le 1er bloc du walk-forward (ajustement seul) et les jours d'embargo, jamais testés.
    None pour une règle testée avant ce compte."""
    cats = m.get("jours_par_categorie")
    if not cats:
        return None
    direct = cats.get("prospectif") or 0
    if m.get("frontiere") is None:  # règle d'avant la frontière : ses jours ont pu être vus
        head = (f"Jours de données : {cats.get('historique_exploratoire') or 0} jours qui ont pu être lus avant la règle "
                "(sans frontière)")
    else:
        head = (f"Jours neufs depuis la soumission : {cats.get('historique_non_consulte') or 0} historiques jamais lus "
                f"(rejeu), {direct} du direct")
    return head + (" ; validée sur l'historique seulement : le challenger en direct décidera." if not direct
                   or m.get("frontiere") is None else ".")


def proof_fr(rule: dict) -> str:
    """Preuve chiffrée d'une règle validée (mesures du validateur, périodes de test) : écart RELATIF en R du labo
    (avant commission IBKR à 20 € pour les actions : le challenger, lui, juge à 20 €), espérance des trades gardés
    quand le validateur l'a notée, et catégorie des jours de test."""
    m = rule.get("mesures") or {}
    canon = concrete(rule) or {"kind": rule["regle"]["kind"], "cond": rule["regle"]["cond"],
                               "markets": rule["regle"].get("markets")}
    touched = m.get("touches_test") or 0
    gain = m.get("gain_total_r") or 0.0
    mean_touched = -gain / touched if touched else None
    windows = m.get("fenetres") or []
    n_win = len(windows)
    who = f"les trades où {cond_fr(canon['cond'])}" if canon["kind"] == "exclude" else \
        f"les trades hors de « {cond_fr(canon['cond'])} »"
    verb = "les exclure" if canon["kind"] == "exclude" else "ne garder que les autres"
    kept = _kept_mean(windows)
    stock = "stock" in (canon.get("markets") or ["stock"])
    text = (f"{who[0].upper()}{who[1:]} font {fr(mean_touched)} en moyenne sur {touched} trades ; {verb} donne "
            f"{fr(m.get('amelioration_moyenne_r'))} par trade sur {n_win} périodes de test "
            f"(meilleur dans {m.get('fenetres_meilleures', 0)} sur {n_win}).")
    if kept is not None:  # l'écart est relatif : « moins mauvais » n'est pas « gagnant »
        text += (f" Écart relatif : les trades gardés font encore {fr(kept)} (R du labo"
                 + (", avant commission IBKR à 20 €" if stock else "") + ").")
    days = days_fr(m)
    return text + (f" {days}" if days else "")


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


def enqueue_promotion(db_file: str, promo_id: str, approve: bool, proposal_id: int | None = None) -> Path:
    """Serveur web : décision de Simão sur la promotion du challenger proposée à un regard (« v2-20 »)."""
    if not re.fullmatch(r"v\d+-\d+", promo_id or ""):
        raise ValueError("promotion illisible")
    target = queue_root(db_file) / "decisions" / f"promotion-{promo_id}.json"
    _write_atomic(target, {"promotion": promo_id, "decision": "approuvee" if approve else "refusee",
                           "proposition": proposal_id, "le": now_iso()})
    return target


def enqueue_types(db_file: str, items: list[dict]) -> Path:
    """Serveur web : dépose des types de catalyseurs à enregistrer (lus par le worker)."""
    target = queue_root(db_file) / "types" / f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}.json"
    _write_atomic(target, items)
    return target


def ingest_decisions(db: LabDB, now: datetime | None = None) -> int:
    """Approbations et refus déposés par le serveur web. Refus : mémoire des échecs (jamais reproposée).
    Décisions sur une promotion du challenger : decide_promotion."""
    q = queue_root(db.path) / "decisions"
    if not q.exists():
        return 0
    n = 0
    for f in sorted(q.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            rule = None if "promotion" in data else V.get_rule(db.conn, data["empreinte"])
            if "promotion" in data:
                n += decide_promotion(db, data, now)
            elif rule is None:
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
            # Rang dans la dépense de l'alpha (validator.challenger_z), figé pour tout l'essai.
            rank = 1 + judged_challengers(db.conn, version)
            db.conn.execute(
                "INSERT INTO method_versions (version, created_at, settings, base, rules, role, started_ts) "
                "VALUES (?, ?, ?, ?, ?, 'challenger', ?)",
                (version, now.isoformat(timespec="seconds"),
                 json.dumps({"base": base, "filtres": rules, "champion_au_depart": champ["version"],
                             "rang_challenger": rank}, ensure_ascii=False),
                 base, json.dumps(rules, ensure_ascii=False), int(now.timestamp())))
        for rule in new_rules:
            db.conn.execute("UPDATE rules SET decision = 'en_essai' WHERE hash = ?", (rule["empreinte"],))
    log.info("Labo : challenger %s en essai (%d règles)", version, len(rules))
    return challenger(db.conn)


def _names(rules: list[dict]) -> str:
    return ", ".join(f"« {r['nom']} »" for r in rules) or "aucun filtre"


def _progress(conn, key: str):
    """Valeur du job « methode » de la table progress (lecture seule possible) ; None si absente."""
    try:
        row = conn.execute("SELECT value FROM progress WHERE job = 'methode' AND key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return json.loads(row[0]) if row else None


def pending_promotion(conn, version: str | None = None) -> dict | None:
    """Promotion proposée à Simão, en attente de sa réponse (une à la fois : celle du dernier regard)."""
    p = _progress(conn, "promotion")
    return p if p and (version is None or p.get("version") == version) else None


def num_fr(x: float | None) -> str:
    return "n/d" if x is None else f"{x:.2f}".replace(".", ",")


def _where(cmp: dict) -> str:
    return f" ({', '.join(MARKET_FR.get(m, m) for m in cmp['marches'])})" if cmp.get("marches") else ""


def absolute_label(h: dict, delta: float | None = None) -> str:
    """Étiquette absolue des trades gardés (« méthode toujours perdante »…) ; vide si rien à dire. Un filtre peut
    seulement perdre moins : « moins perdant » seulement quand l'écart relatif `delta` est connu et positif (inconnu
    sous 2 jours de trades : rien ne dit que le filtre perd moins). Espérance positive : « non prouvée » tant que la
    borne basse de son IC95 n'est pas > 0."""
    e = h.get("esperance_r")
    if e is None:
        return ""
    if e <= 0:
        return "filtre moins perdant, méthode toujours perdante" if delta is not None and delta > 0 else \
            "méthode toujours perdante"
    ci = h.get("ic95")
    if ci and ci[0] is not None and ci[0] <= 0:
        return f"méthode positive mais non prouvée (IC95 {fr(ci[0], 3, '')} ; {fr(ci[1], 3, '')})"
    return ""


def absolute_fr(h: dict, delta: float | None = None) -> str:
    """L'espérance absolue des trades gardés compte aussi : « Filtre moins perdant, méthode toujours perdante. »"""
    label = absolute_label(h, delta)
    return f" {label[0].upper()}{label[1:]}." if label else ""


def promotion_text(cmp: dict, cfg: dict, champ: dict, chall: dict, look: int) -> str:
    c, h, n_looks = cmp["champion"], cmp["challenger"], looks(cfg)
    return (f"{chall['version']} = {champ['version']} + {_names(new_rules(champ, chall))}. En direct sur {cmp['jours']} "
            f"jours de trades{_where(cmp)} : {fr(cmp['delta_r'], 3)} par trade de mieux que {champ['version']}, "
            f"z = {num_fr(cmp['z'])}, {threshold_fr(cmp, cfg)} ; {fr(h['esperance_r'], 3)} par trade sur "
            f"{h['declenches']} trades gardés, contre {fr(c['esperance_r'], 3)} sur {c['declenches']} ; potentiel +10 "
            f"capturé {h['capture']} fois (contre {c['capture']}).{absolute_fr(h, cmp['delta_r'])} "
            f"Regard {n_looks.index(look) + 1} sur {len(n_looks)}.")


def _noted(cmp: dict, cfg: dict) -> dict:
    """Comparaison gardée avec la version (note) : avec le seuil z et le rang qui l'ont jugée."""
    return cmp | {"seuil_z": round(z_min(cmp, cfg), 3), "rang_challenger": cfg.get("challenger_rang", 1)}


def _promote(db: LabDB, champ: dict, chall: dict, cmp: dict, now: datetime) -> str:
    from app.lab import lessons as L  # import tardif, comme validator.frontier_sql

    new, (c, h) = new_rules(champ, chall), (cmp["champion"], cmp["challenger"])
    stamp = now.isoformat(timespec="seconds")
    db.conn.execute("UPDATE method_versions SET role = 'ancienne', decided_at = ? WHERE version = ?",
                    (stamp, champ["version"]))
    # Frontière de ce qui est journalisé à la promotion (comme les leçons) : le grade des marchés touchés ne
    # compte que les jours d'après, jamais ceux de l'essai qui ont décidé la promotion.
    settings = version_settings(db.conn, chall["version"]) | {"vu_a_la_promotion": L.seen_until(db.conn)}
    db.conn.execute("UPDATE method_versions SET role = 'champion', decided_at = ?, note = ?, settings = ? "
                    "WHERE version = ?", (stamp, json.dumps(cmp, ensure_ascii=False),
                                          json.dumps(settings, ensure_ascii=False), chall["version"]))
    for r in new:
        db.conn.execute("UPDATE rules SET decision = 'adoptee' WHERE hash = ?", (r["empreinte"],))
    db.set("methode", "promotion", None)
    text = (f"🧪 Méthode {chall['version']} en place : {_names(new)} s'applique désormais aux plans de JARVIS "
            f"(promotion approuvée par Simão). En direct sur {cmp['jours']} jours de trades{_where(cmp)} : "
            f"{fr(h['esperance_r'], 3)} par trade sur {h['declenches']} trades, contre {fr(c['esperance_r'], 3)} sur "
            f"{c['declenches']} pour {champ['version']} ; potentiel +10 capturé {h['capture']} fois "
            f"(contre {c['capture']}).{absolute_fr(h, cmp['delta_r'])}")
    event(db, "bascule", chall["version"], text, cmp, now)
    return text


def _retire(db: LabDB, champ: dict, chall: dict, cmp: dict, now: datetime, why: str) -> str:
    new, (c, h) = new_rules(champ, chall), (cmp["champion"], cmp["challenger"])
    db.conn.execute("UPDATE method_versions SET role = 'retiree', decided_at = ?, note = ? WHERE version = ?",
                    (now.isoformat(timespec="seconds"), json.dumps(cmp, ensure_ascii=False), chall["version"]))
    for r in new:
        db.conn.execute("UPDATE rules SET decision = 'retiree' WHERE hash = ?", (r["empreinte"],))
    db.set("methode", "promotion", None)
    text = (f"🧪 Méthode {chall['version']} retirée : {why} ({fr(h['esperance_r'], 3)} par trade contre "
            f"{fr(c['esperance_r'], 3)} pour {champ['version']}, {cmp['jours']} jours de trades en direct). "
            f"{champ['version']} reste en place ; {_names(new)} rejoint les idées écartées.")
    event(db, "retrait", chall["version"], text, cmp, now)
    return text


def judge_challenger(db: LabDB, now: datetime | None = None, cfg: dict | None = None) -> str | None:
    """Regards fixes sur le challenger. « promouvoir » ne bascule rien : la promotion attend l'accord de
    Simão (proposition « promouvoir vX », decide_promotion) et le challenger reste mesuré ; le regard
    suivant la remplace. « retirer » le retire. Renvoie le verdict d'un regard ou d'un retrait, None
    entre deux regards."""
    now = now or datetime.now(timezone.utc)
    cfg = cfg or V.config(db.conn)
    chall = challenger(db.conn)
    if not chall:
        return None
    champ = champion(db.conn)
    cmp = compare(db.conn, champ, chall, cfg)
    cfg = judged_cfg(db.conn, chall, cmp, cfg)  # seuil z du challenger n° k à ce regard
    key = f"regard:{chall['version']}"
    done = db.get("methode", key, 0)
    pending = pending_promotion(db.conn, chall["version"])
    look = look_reached(cmp, cfg, done)
    v = verdict(cmp, cfg, now, done, pending)
    if look is None and v == "attendre":
        return None
    with db.transaction():
        if look is not None:
            db.set("methode", key, look)
        if v == "promouvoir":
            promo = {"id": f"{chall['version']}-{look}", "version": chall["version"], "champion": champ["version"],
                     "regard": look, "le": now.isoformat(timespec="seconds"),
                     "texte": promotion_text(cmp, cfg, champ, chall, look)}
            db.set("methode", "promotion", promo)
            log.info("Labo : promotion de %s proposée à Simão : %s", chall["version"], promo["texte"])
        elif v == "retirer":
            if look is not None:
                why = f"pas assez net au dernier regard ({look} jours de trades) : " + " ; ".join(promotion_fails(cmp, cfg))
            elif promotion_expired(pending, now, cfg):
                why = f"promotion sans réponse de Simão ({cfg['challenger_answer_days']} jours)"
            else:
                why = f"{cfg['challenger_max_days']} jours sans atteindre le dernier regard"
            log.info("Labo : %s", _retire(db, champ, chall, _noted(cmp, cfg), now, why))
        elif pending:
            db.set("methode", "promotion", None)  # le regard suivant n'est plus assez net : proposition retirée
    return v


def decide_promotion(db: LabDB, data: dict, now: datetime | None = None) -> int:
    """Réponse de Simão à la promotion proposée : appliquée seulement si c'est toujours la promotion en
    attente (un regard suivant l'a peut-être remplacée). Approuvée : le challenger devient champion ;
    refusée : il est retiré."""
    now = now or datetime.now(timezone.utc)
    if data.get("decision") not in DECISIONS:
        raise ValueError(f"décision {data.get('decision')} illisible")
    pending, chall = pending_promotion(db.conn), challenger(db.conn)
    if not pending or pending["id"] != data["promotion"] or not chall or chall["version"] != pending["version"]:
        log.info("Labo : décision sur la promotion %s dépassée, ignorée", data["promotion"])
        return 0
    champ = champion(db.conn)
    cfg = V.config(db.conn)
    cmp = compare(db.conn, champ, chall, cfg)
    cmp = _noted(cmp, judged_cfg(db.conn, chall, cmp, cfg))  # gardé avec le seuil et le rang du challenger
    with db.transaction():
        if data["decision"] == "approuvee":
            text = _promote(db, champ, chall, cmp, now)
        else:
            text = _retire(db, champ, chall, cmp, now, "promotion refusée par Simão")
    log.info("Labo : %s", text)
    return 1


def champion_path(db_file: str | None = None) -> Path:
    override = os.getenv("JARVIS_LAB_CHAMPION", "").strip()
    return Path(override) if override else Path(db_file or db_path()).parent / CHAMPION_FILE


def champion_payload(conn) -> dict:
    champ = champion(conn)
    chall = challenger(conn)
    return {"version": champ["version"], "base": base_of(champ, conn), "regles": champ["regles"],
            "challenger": chall["version"] if chall else None,
            "promotion": pending_promotion(conn, chall["version"]) if chall else None}


def sync_proposals(proposals, data: dict | None = None) -> list[dict]:
    """Serveur : la promotion en attente (lab_champion.json) devient une proposition « promouvoir vX » ;
    celle d'un regard dépassé est remplacée. Renvoie les nouvelles propositions (à envoyer en privé)."""
    if data is None:
        try:
            data = json.loads(champion_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
    promo = data.get("promotion")
    p = promo and proposals.propose_promotion(promo)
    proposals.retire_promotion({promo["id"]} if promo else set())
    return [p] if p else []


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
    for name, fn in (("decisions", lambda: ingest_decisions(db, now)), ("types", lambda: ingest_types(db)),
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
        done = _progress(conn, f"regard:{chall['version']}") or 0
        nxt = next_look(cfg, done)
        cfg = judged_cfg(conn, chall, cmp, cfg, nxt)  # le seuil du prochain regard (celui qui jugera)
        out["challenger"] = {"version": chall["version"], "depuis": chall["creee_le"],
                             "regles": [{"nom": r["nom"], "regle": rule_fr(r)} for r in chall["regles"]],
                             "comparaison": cmp, "objectif": cfg["challenger_min_trades"],
                             "limite": cfg["challenger_max_trades"], "regards": looks(cfg), "regard_fait": done,
                             "prochain_regard": nxt, "seuil_z": cfg["challenger_min_z"],
                             "rang": cfg["challenger_rang"], "seuil_texte": threshold_fr(cmp, cfg),
                             "etiquette": absolute_label(cmp["challenger"], cmp["delta_r"]),
                             "promotion": pending_promotion(conn, chall["version"])}
    return out


def state_line(st: dict) -> str:
    champ = st["champion"]
    line = f"Méthode en place {champ['version'] or 'n/d'}" + (f" ({len(champ['regles'])} filtres)" if champ["regles"] else "")
    ch = st.get("challenger")
    if ch:
        cmp = ch["comparaison"]
        nxt = ch.get("prochain_regard")
        seuil = ch.get("seuil_texte") or f"seuil z {num_fr(ch['seuil_z'])}"
        label = ch.get("etiquette")
        line += (f" · challenger {ch['version']} : {cmp['jours']} jours de trades en direct"
                 + (f" (prochain regard à {nxt})" if nxt else "")
                 + f", {fr(cmp['challenger']['esperance_r'], 3)} contre {fr(cmp['champion']['esperance_r'], 3)}, "
                 f"z {num_fr(cmp['z'])}, {seuil}"
                 + (f" · {label}" if label else "")
                 + (" · promotion proposée à Simão" if ch.get("promotion") else ""))
    else:
        line += " · pas de challenger"
    return line
