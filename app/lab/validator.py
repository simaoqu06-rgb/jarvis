"""Validateur du labo : une idée de règle est-elle vraiment meilleure, hors de l'échantillon ?

Format d'une règle (JSON) — v1 : règles de FILTRE seulement :

    {"nom": "pas de RSI tendu", "description": "…",
     "exclude": {"field": "context.rvol", "op": ">", "value": 5},
     "markets": ["stock"]}

- "exclude" (on retire les trades qui vérifient la condition) ou "keep" (on ne garde qu'eux) ;
- condition : feuille {"field", "op", "value"} ou {"and": [...]}, {"or": [...]}, {"not": {...}} ;
- op : > >= < <= == != in not_in between is_null not_null ; "value": "auto" (seulement pour une
  règle à une seule feuille > >= < <=) : le seuil est choisi sur la période d'ajustement de chaque
  fenêtre (déciles), puis jugé sur la période de test qui suit ;
- champs : connus à l'heure du plan seulement — "plan.<chemin>" (ex. plan.stop_pct,
  plan.targets[0].r_multiple, plan.checklist.items.vwap), "context.<chemin>" (ex. context.setup,
  context.rvol, context.regime.vol20_pct), "composite", "decision", "market", "symbol", "weekday"
  (0 = dimanche), "hour_utc", "regime" (étiquette de la table regimes). Les résultats (r_net,
  tp1_hit, mfe_r, result.*…) sont refusés : ce serait regarder l'avenir. context.gap_open_pct aussi :
  un ordre posé à 9 h 10 et déjà « au marché » est exécuté à l'ouverture, au moment même où le gap
  est connu ; un filtre sur le gap ne serait pas applicable en direct ;
- "markets" (facultatif) : marchés concernés (sinon tous) ;
- une règle qui demanderait de régénérer les plans (nouveaux paramètres de stop, de TP…) est refusée
  en v1 : la régénération sur les bougies stockées coûterait des heures de calcul par idée sur
  1 cœur ; elle viendra plus tard.

Validation « walk-forward » sur les trades de la version de méthode en cours (rejeu et direct) :
les jours sont coupés en W+1 blocs chronologiques (W = 3) ; fenêtre i : ajustement sur les blocs
0..i, test sur le bloc i+1 privé de ses premiers jours (embargo d'1 jour : un trade crypto du dernier
jour d'ajustement se dénoue le lendemain). Les fenêtres de test ne se recouvrent pas.

Critères, tous obligatoires (seuils dans la table lab_config, défauts DEFAULTS) :
1. au moins 100 trades déclenchés touchés par la règle sur les périodes de test ;
2. amélioration moyenne (sur les fenêtres) d'au moins +0,05 R par trade déclenché ;
3. meilleure dans au moins 2 fenêtres sur 3 ;
4. pas de baisse sensible de la capture du potentiel +10 (au plus 10 % des mouvements capturés, ou un
   seul quand il y en a moins de 10) ;
5. significativité corrigée du nombre d'idées testées : p unilatéral (z en grappes par jour sur toutes
   les périodes de test) < 6·alpha / (π²·n²), n = numéro de l'essai (compteur gardé en base) :
   la somme sur tous les essais reste ≤ alpha, le risque global de faux positif est tenu.
Mémoire : chaque règle a une empreinte canonique (ordre des clés et des conditions sans effet, nom et
description ignorés) ; une règle déjà testée renvoie son verdict gardé, sans nouvel essai.
Un verdict « validée » ne change rien au direct : c'est une candidate pour une proposition (Labo 3).
"""
import hashlib
import json
import logging
import math
import re
import time
from pathlib import Path

from app.lab import stats as STATS
from app.lab.db import LabDB, now_iso

log = logging.getLogger(__name__)
DEFAULTS = {"windows": 3, "embargo_days": 1, "min_affected": 100, "min_improvement_r": 0.05,
            "min_windows_better": 2, "alpha": 0.05, "max_capture_drop": 0.10, "min_days": 60,
            "auto_min_affected": 30, "missing_max": 0.95}
OPS = {">": ">", ">=": ">=", "<": "<", "<=": "<=", "==": "=", "!=": "<>"}
SET_OPS = ("in", "not_in")
ALL_OPS = (*OPS, *SET_OPS, "between", "is_null", "not_null")
AUTO_OPS = (">", ">=", "<", "<=")
FLAT = {"market": "t.market", "symbol": "t.symbol", "composite": "t.composite", "decision": "t.decision",
        "side": "t.side", "weekday": "CAST(strftime('%w', t.day) AS INTEGER)",
        "hour_utc": "CAST(strftime('%H', t.planned_at, 'unixepoch') AS INTEGER)",
        "regime": "r.label", "regime_trend": "r.trend", "regime_vol_tercile": "r.vol_tercile"}
OUTCOMES = {"triggered", "filled_at", "exit_reason", "r_net", "r_gross", "tp1_hit", "tp2_hit", "tp3_hit",
            "minutes_to_tp1", "minutes_to_tp2", "minutes_to_tp3", "mfe_r", "mae_r", "day_range_pct", "result"}
# Champs du contexte connus seulement après la décision (au moment de l'exécution ou plus tard).
LATE_CONTEXT = {"context.gap_open_pct"}
# Règle du propriétaire : jamais de filtre de liquidité ni de capitalisation. Le volume en dollars,
# le rang et le motif d'entrée dans l'univers (classés au volume en dollars) en sont.
SIZE_FIELDS = {"context.dollar_volume", "context.universe_rank", "context.universe_reason"}
SIZE_WORDS = ("dollar_volume", "market_cap", "marketcap", "capitalis", "capitaliz", "liquid")
PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[\d{1,2}\])?(\.[A-Za-z_][A-Za-z0-9_]*(\[\d{1,2}\])?)*$")
MAX_LEAVES, MAX_DEPTH = 12, 6
RULE_KEYS = {"nom", "name", "description", "exclude", "keep", "markets"}
REGEN_KEYS = {"parametres", "parameters", "overrides", "regenerer", "regenerate", "settings"}
QUEUE_MAX = 50


class RuleError(ValueError):
    """Règle illisible ou interdite (message en français, montré tel quel)."""


# --- Format et empreinte --------------------------------------------------------------------

def _field_sql(field: str) -> tuple[str, tuple]:
    if not isinstance(field, str) or not field:
        raise RuleError("champ manquant")
    head = field.split(".", 1)[0].split("[", 1)[0]
    if head in OUTCOMES or field in OUTCOMES:
        raise RuleError(f"champ « {field} » interdit : c'est un résultat du trade (regard vers l'avenir)")
    if field in LATE_CONTEXT:
        raise RuleError(f"champ « {field} » interdit : connu seulement à l'exécution (regard vers l'avenir)")
    if field in SIZE_FIELDS or any(w in field.lower() for w in SIZE_WORDS):
        raise RuleError(f"champ « {field} » interdit : jamais de filtre de liquidité ni de capitalisation")
    if field in FLAT:
        return FLAT[field], ()
    for prefix, col in (("plan.", "t.plan"), ("context.", "t.context")):
        if field.startswith(prefix):
            path = field[len(prefix):]
            if not PATH.match(path):
                raise RuleError(f"chemin illisible : {field}")
            return f"json_extract({col}, ?)", ("$." + path,)
    raise RuleError(f"champ inconnu : {field} (plan.…, context.… ou {', '.join(sorted(FLAT))})")


def _scalar(v) -> bool:
    return v is None or isinstance(v, (bool, int, float, str))


def _num(v):
    """5.0 et 5 sont la même règle (même empreinte) ; NaN et infinis refusés."""
    if isinstance(v, float):
        if not math.isfinite(v):
            raise RuleError("valeur numérique finie attendue")
        if v.is_integer() and abs(v) < 2 ** 53:
            return int(v)
    return v


def canonical_cond(cond, depth: int = 0, leaves: list | None = None) -> dict:
    leaves = [] if leaves is None else leaves
    if depth > MAX_DEPTH:
        raise RuleError(f"conditions imbriquées sur plus de {MAX_DEPTH} niveaux")
    if not isinstance(cond, dict) or not cond:
        raise RuleError("condition attendue : objet {field, op, value} ou {and|or|not: …}")
    keys = set(cond)
    if keys in ({"and"}, {"or"}):
        k = keys.pop()
        items = cond[k]
        if not isinstance(items, list) or not items:
            raise RuleError(f"« {k} » attend une liste non vide")
        children = []
        for c in (canonical_cond(c, depth + 1, leaves) for c in items):
            # (a ET (b ET c)) = (a ET b ET c), doublons sans effet : même empreinte.
            for child in (c[k] if set(c) == {k} else [c]):
                if child not in children:
                    children.append(child)
        if len(children) == 1:
            return children[0]
        return {k: sorted(children, key=lambda c: json.dumps(c, sort_keys=True))}
    if keys == {"not"}:
        return {"not": canonical_cond(cond["not"], depth + 1, leaves)}
    if not keys <= {"field", "op", "value"} or "field" not in keys or "op" not in keys:
        raise RuleError(f"condition illisible : {json.dumps(cond, ensure_ascii=False)[:120]}")
    op = cond["op"]
    if op not in ALL_OPS:
        raise RuleError(f"opérateur inconnu : {op} (permis : {' '.join(ALL_OPS)})")
    _field_sql(cond["field"])
    leaves.append(cond)
    if len(leaves) > MAX_LEAVES:
        raise RuleError(f"au plus {MAX_LEAVES} conditions par règle")
    out = {"field": cond["field"], "op": op}
    value = cond.get("value")
    if op in ("is_null", "not_null"):
        return out
    if op in SET_OPS:
        if not isinstance(value, list) or not value or not all(_scalar(v) for v in value) or len(value) > 50:
            raise RuleError(f"« {op} » attend une liste de 1 à 50 valeurs simples")
        value = [_num(v) for v in value]
        uniq = {json.dumps(v): v for v in value}
        return out | {"value": [uniq[k] for k in sorted(uniq)]}
    if op == "between":
        if not (isinstance(value, list) and len(value) == 2 and all(isinstance(v, (int, float)) for v in value)):
            raise RuleError("« between » attend [minimum, maximum]")
        return out | {"value": sorted(_num(v) for v in value)}
    if value == "auto":
        if op not in AUTO_OPS:
            raise RuleError("« auto » seulement avec > >= < <=")
        return out | {"value": "auto"}
    if not _scalar(value) or value is None:
        raise RuleError(f"valeur simple attendue pour « {op} »")
    return out | {"value": _num(value)}


def parse(raw) -> dict:
    """Règle canonique (ce qui est testé) ; RuleError si elle est illisible ou hors du format v1."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise RuleError("JSON illisible") from exc
    if not isinstance(raw, dict):
        raise RuleError("objet JSON attendu")
    if set(raw) & REGEN_KEYS:
        raise RuleError("v1 : règles de filtre seulement ; les règles qui régénèrent les plans (nouveaux "
                        "paramètres) ne sont pas encore testables")
    unknown = set(raw) - RULE_KEYS
    if unknown:
        raise RuleError(f"clé inconnue : {', '.join(sorted(unknown))}")
    kinds = [k for k in ("exclude", "keep") if k in raw]
    if len(kinds) != 1:
        raise RuleError("il faut exactement une clé « exclude » ou « keep »")
    leaves: list = []
    cond = canonical_cond(raw[kinds[0]], 0, leaves)
    if any(leaf.get("value") == "auto" for leaf in leaves) and "field" not in cond:
        raise RuleError("« auto » seulement pour une règle à une seule condition")
    markets = raw.get("markets")
    if markets is not None:
        if not isinstance(markets, list) or not markets or not set(markets) <= {"stock", "crypto"}:
            raise RuleError("« markets » : liste parmi stock, crypto")
        markets = sorted(set(markets))
        if markets == ["crypto", "stock"]:
            markets = None  # tous les marchés : même règle que sans « markets »
    return {"kind": kinds[0], "cond": cond, "markets": markets}


def rule_hash(canon: dict) -> str:
    return hashlib.sha256(json.dumps(canon, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def compile_cond(cond: dict) -> tuple[str, list]:
    """Condition → expression SQL paramétrée (aucune valeur collée dans le texte SQL)."""
    if "and" in cond or "or" in cond:
        k = "and" if "and" in cond else "or"
        parts = [compile_cond(c) for c in cond[k]]
        return "(" + f" {k.upper()} ".join(f"COALESCE({s}, 0)" for s, _ in parts) + ")", \
            [p for _, ps in parts for p in ps]
    if "not" in cond:
        s, p = compile_cond(cond["not"])
        return f"(NOT COALESCE({s}, 0))", p
    expr, fparams = _field_sql(cond["field"])
    op, value = cond["op"], cond.get("value")
    if op == "is_null":
        return f"({expr} IS NULL)", list(fparams)
    if op == "not_null":
        return f"({expr} IS NOT NULL)", list(fparams)
    if op in SET_OPS:
        marks = ", ".join("?" * len(value))
        return f"({expr} {'NOT IN' if op == 'not_in' else 'IN'} ({marks}))", [*fparams, *value]
    if op == "between":
        return f"({expr} BETWEEN ? AND ?)", [*fparams, *value]
    return f"({expr} {OPS[op]} ?)", [*fparams, value]


def leaves_of(cond: dict) -> list[dict]:
    if "and" in cond or "or" in cond:
        return [leaf for c in cond.get("and") or cond.get("or") for leaf in leaves_of(c)]
    if "not" in cond:
        return leaves_of(cond["not"])
    return [cond]


# --- Seuils ---------------------------------------------------------------------------------

def config(conn) -> dict:
    """Seuils du validateur : défauts, remplacés par la table lab_config (changés seulement sur
    approbation du propriétaire, Labo 3)."""
    out = dict(DEFAULTS)
    try:
        for key, value in conn.execute("SELECT key, value FROM lab_config"):
            if key in out:
                out[key] = json.loads(value)
    except Exception:  # base sans la table (ancienne) : défauts
        pass
    return out


def set_config(db: LabDB, key: str, value) -> None:
    if key not in DEFAULTS:
        raise KeyError(key)
    db.conn.execute("INSERT OR REPLACE INTO lab_config VALUES (?, ?, ?)", (key, json.dumps(value), now_iso()))


def trial_level(trial: int, alpha: float = DEFAULTS["alpha"]) -> float:
    """Seuil de p à l'essai n° `trial` : 6·alpha / (π²·n²). Σ 1/n² = π²/6, donc la somme des seuils
    sur tous les essais ne dépasse jamais alpha (alpha / n, lui, cumule ≈ alpha·ln N)."""
    n = max(1, trial)
    return 6 * alpha / (math.pi ** 2 * n * n)


def z_threshold(trial: int, alpha: float = DEFAULTS["alpha"]) -> float:
    """z minimum à l'essai n° `trial` (unilatéral, seuil `trial_level`) ; croît avec n."""
    from statistics import NormalDist

    return -NormalDist().inv_cdf(trial_level(trial, alpha))  # queue basse : pas d'arrondi à 1 en flottant


# --- Walk-forward (fonctions pures) ---------------------------------------------------------

def windows(days: list[str], n: int = 3, embargo: int = 1) -> list[tuple[set, set]]:
    """Fenêtres chronologiques : ajustement sur les blocs 0..i, test sur le bloc i+1 (moins l'embargo)."""
    days = sorted(set(days))
    blocks = [days[len(days) * k // (n + 1): len(days) * (k + 1) // (n + 1)] for k in range(n + 1)]
    out = []
    for i in range(n):
        fit = {d for b in blocks[:i + 1] for d in b}
        test = set(blocks[i + 1][embargo:])
        out.append((fit, test))
    return out


def _cmp(op: str, x, thr) -> bool:
    if not isinstance(x, (int, float)) or isinstance(x, bool):
        return False
    return x > thr if op == ">" else x >= thr if op == ">=" else x < thr if op == "<" else x <= thr


def choose_threshold(rows: list[tuple], op: str, kind: str, min_affected: int) -> float | None:
    """Seuil « auto » : parmi les déciles de la période d'ajustement, celui qui fait gagner le plus de
    R au total (somme des R retirés la plus négative, au moins `min_affected` trades touchés). Le
    total plutôt que la moyenne : retirer aussi des trades neutres ne doit pas être récompensé.
    Ne voit que `rows` (l'ajustement)."""
    trig = [(r[4], r[6]) for r in rows if r[3] and r[4] is not None]
    values = sorted(v for _, v in trig if isinstance(v, (int, float)) and not isinstance(v, bool))
    if len(values) < 10:
        return None
    best, best_gain = None, 0.0
    for q in range(1, 10):
        thr = values[len(values) * q // 10]
        removed = [r for r, v in trig if _cmp(op, v, thr) == (kind == "exclude")]
        if len(removed) < min_affected or len(removed) == len(trig):
            continue
        gain = -sum(removed)
        if gain > best_gain:
            best, best_gain = thr, gain
    return best


def walk_forward(rows: list[tuple], movers: set, cfg: dict, kind: str, auto_op: str | None = None) -> dict:
    """Juge une règle sur des lignes (jour, marché, symbole, déclenché, r_net, tp1, touché|valeur).

    Sans `auto_op`, la 7e colonne dit si la condition est vraie ; avec, c'est la valeur du champ et
    le seuil est choisi sur l'ajustement de chaque fenêtre (choose_threshold)."""
    wins = windows([r[0] for r in rows], cfg["windows"], cfg["embargo_days"])
    out_windows, day_sums = [], {}
    cap_before = cap_after = 0
    affected_total = 0
    for fit, test in wins:
        thr = None
        if auto_op:
            thr = choose_threshold([r for r in rows if r[0] in fit], auto_op, kind, cfg["auto_min_affected"])

        def affected(r) -> bool:
            if auto_op:
                if thr is None:
                    return False  # aucun seuil sur l'ajustement : la règle ne change rien dans cette fenêtre
                hit = _cmp(auto_op, r[6], thr)
            else:
                hit = bool(r[6])
            return hit if kind == "exclude" else not hit

        base_s = base_n = kept_s = kept_n = 0.0
        n_aff = 0
        mover_state: dict[tuple, list] = {}
        for r in rows:
            if r[0] not in test:
                continue
            aff = affected(r)
            key = (r[1], r[0], r[2])
            if key in movers:
                st = mover_state.setdefault(key, [False, False])
                if r[3] and r[5]:
                    st[0] = True
                    st[1] = st[1] or not aff
            if not r[3] or r[4] is None:
                continue
            rr = r[4]
            base_s += rr
            base_n += 1
            d = day_sums.setdefault(r[0], [0.0, 0, 0.0, 0])
            d[0] += rr
            d[1] += 1
            if aff:
                n_aff += 1
            else:
                kept_s += rr
                kept_n += 1
                d[2] += rr
                d[3] += 1
        cap_before += sum(1 for s in mover_state.values() if s[0])
        cap_after += sum(1 for s in mover_state.values() if s[1])
        affected_total += n_aff
        base_m = base_s / base_n if base_n else None
        kept_m = kept_s / kept_n if kept_n else None
        delta = (kept_m - base_m) if base_m is not None and kept_m is not None else None
        removed_r = base_s - kept_s
        out_windows.append({"ajustement": [min(fit), max(fit)] if fit else None,
                            "test": [min(test), max(test)] if test else None,
                            "trades_test": int(base_n), "touches": n_aff,
                            "esperance_avant_r": round(base_m, 4) if base_m is not None else None,
                            "esperance_apres_r": round(kept_m, 4) if kept_m is not None else None,
                            "amelioration_r": round(delta, 4) if delta is not None else None,
                            "gain_total_r": round(-removed_r, 2), "seuil": thr})
    deltas = [w["amelioration_r"] for w in out_windows]
    mean_delta = sum(d or 0.0 for d in deltas) / len(deltas) if deltas else 0.0
    z, pooled = pooled_z(day_sums)
    return {"fenetres": out_windows, "touches_test": affected_total,
            "amelioration_moyenne_r": round(mean_delta, 4),
            "amelioration_globale_r": round(pooled, 4) if pooled is not None else None,
            "fenetres_meilleures": sum(1 for d in deltas if d is not None and d > 0),
            "gain_total_r": round(sum(w["gain_total_r"] for w in out_windows), 2),
            "z": round(z, 3) if z is not None else None,
            "p": STATS.p_one_sided(z) if z is not None else None,
            "capture_avant": cap_before, "capture_apres": cap_after}


def pooled_z(day_sums: dict) -> tuple[float | None, float | None]:
    """Écart (espérance gardée - espérance de base) sur toutes les périodes de test et son z, erreur
    type en grappes par jour (linéarisation des deux moyennes ; les trades d'un jour se ressemblent)."""
    A = sum(v[0] for v in day_sums.values())
    a = sum(v[1] for v in day_sums.values())
    K = sum(v[2] for v in day_sums.values())
    k = sum(v[3] for v in day_sums.values())
    g = len(day_sums)
    if not a or not k or g < 2:
        return None, None
    ma, mk = A / a, K / k
    delta = mk - ma
    var = sum(((v[2] - mk * v[3]) / k - (v[0] - ma * v[1]) / a) ** 2 for v in day_sums.values()) * g / (g - 1)
    if var <= 0:
        return None, delta
    return delta / math.sqrt(var), delta


def judge(m: dict, cfg: dict, trial: int) -> tuple[str, list[str], list[str]]:
    """Verdict (validee / rejetee), raisons des échecs et critères tenus."""
    fails, ok = [], []

    def check(cond: bool, text: str) -> None:
        (ok if cond else fails).append(text)

    check(m["touches_test"] >= cfg["min_affected"],
          f"{m['touches_test']} trades touchés en test (minimum {cfg['min_affected']})")
    check(m["amelioration_moyenne_r"] >= cfg["min_improvement_r"],
          f"amélioration moyenne {m['amelioration_moyenne_r']:+.3f} R par trade (minimum +{cfg['min_improvement_r']:g})")
    n_win = len(m["fenetres"])
    check(m["fenetres_meilleures"] >= min(cfg["min_windows_better"], n_win),
          f"meilleure dans {m['fenetres_meilleures']}/{n_win} fenêtres (minimum {cfg['min_windows_better']})")
    before, after = m["capture_avant"], m["capture_apres"]
    allowed = before * cfg["max_capture_drop"] if before >= 10 else 1
    check(before - after <= allowed + 1e-9,
          f"capture du potentiel +10 : {before} → {after} mouvements capturés (baisse permise {allowed:g})")
    level = trial_level(trial, cfg["alpha"])
    p = m["p"]
    check(p is not None and p < level,
          f"significativité : p = {p:.2g} pour un seuil de {level:.2g} (alpha {cfg['alpha']:g}, essai n° {trial})"
          if p is not None else "significativité : non mesurable")
    return ("validee" if not fails else "rejetee"), fails, ok


# --- Base -----------------------------------------------------------------------------------

def current_version(conn) -> str | None:
    row = conn.execute("SELECT version FROM method_versions ORDER BY created_at DESC, version DESC LIMIT 1").fetchone()
    return row[0] if row else None


def load_rows(conn, canon: dict, version: str) -> list[tuple]:
    """(jour, marché, symbole, déclenché, r_net, tp1, touché|valeur) de tous les trades de la version,
    triés par jour. La condition est évaluée par SQLite (json_extract) : aucun JSON lu en Python."""
    cond = canon["cond"]
    if cond.get("value") == "auto":
        col, cparams = _field_sql(cond["field"])
    else:
        col, cparams = compile_cond(cond)
        col = f"COALESCE({col}, 0)"
    sql = (f"SELECT t.day, t.market, t.symbol, t.triggered, t.r_net, t.tp1_hit, {col} "
           "FROM trades t LEFT JOIN regimes r ON r.market = t.market AND r.day = t.day WHERE t.method_version = ?")
    params = [*cparams, version]
    if canon["markets"]:
        sql += f" AND t.market IN ({', '.join('?' * len(canon['markets']))})"
        params += canon["markets"]
    return conn.execute(sql + " ORDER BY t.day", params).fetchall()


def load_movers(conn, markets: list | None, threshold: float = STATS.MOVE_PCT) -> set:
    sql, params = "SELECT market, day, symbol FROM day_moves WHERE up_pct >= ?", [threshold]
    if markets:
        sql += f" AND market IN ({', '.join('?' * len(markets))})"
        params += markets
    return {tuple(r) for r in conn.execute(sql, params)}


def missing_share(conn, canon: dict, version: str) -> dict[str, float]:
    """Part des trades sans valeur pour chaque champ JSON de la règle (marchés de la règle seulement)."""
    out = {}
    msql, mparams = "", []
    if canon.get("markets"):
        msql = f" AND t.market IN ({', '.join('?' * len(canon['markets']))})"
        mparams = list(canon["markets"])
    for leaf in leaves_of(canon["cond"]):
        field = leaf["field"]
        if field in out or field in FLAT:
            continue
        expr, params = _field_sql(field)
        row = conn.execute(f"SELECT AVG({expr} IS NULL) FROM trades t WHERE t.method_version = ?{msql}",
                           (*params, version, *mparams)).fetchone()
        out[field] = row[0] if row[0] is not None else 1.0
    return out


def evaluate(conn, canon: dict, trial: int, cfg: dict | None = None) -> dict:
    """Teste une règle canonique ; renvoie {verdict, raisons, mesures}. verdict « attente » si les
    données ne suffisent pas encore (aucun essai consommé)."""
    cfg = cfg or config(conn)
    version = current_version(conn)
    if not version:
        return {"verdict": "attente", "raisons": ["aucun trade journalisé"], "mesures": {}}
    missing = missing_share(conn, canon, version)
    absent = [f for f, share in missing.items() if share >= cfg["missing_max"]]
    if absent:
        return {"verdict": "invalide", "mesures": {"champs_absents": missing},
                "raisons": [f"champ presque toujours absent des trades : {', '.join(absent)}"]}
    rows = load_rows(conn, canon, version)
    days = {r[0] for r in rows}
    if len(days) < cfg["min_days"]:
        return {"verdict": "attente", "mesures": {"jours": len(days)},
                "raisons": [f"{len(days)} jours de trades, il en faut {cfg['min_days']}"]}
    movers = load_movers(conn, canon["markets"])
    auto_op = canon["cond"]["op"] if canon["cond"].get("value") == "auto" else None
    m = walk_forward(rows, movers, cfg, canon["kind"], auto_op)
    verdict, fails, ok = judge(m, cfg, trial)
    m.update(version=version, jours=len(days), trades=len(rows), essai=trial,
             seuil_p=trial_level(trial, cfg["alpha"]), criteres_tenus=ok)
    return {"verdict": verdict, "raisons": fails, "mesures": m}


def _row(r) -> dict:
    rid, h, name, rule, status, reason, metrics, trial, submitted, tested = r
    return {"id": rid, "empreinte": h, "nom": name, "regle": json.loads(rule), "etat": status,
            "raisons": json.loads(reason) if reason else [], "mesures": json.loads(metrics) if metrics else None,
            "essai": trial, "soumise_le": submitted, "testee_le": tested}


RULE_COLUMNS = "id, hash, name, rule, status, reason, metrics, trial, submitted_at, tested_at"


def get_rule(conn, h: str) -> dict | None:
    row = conn.execute(f"SELECT {RULE_COLUMNS} FROM rules WHERE hash = ?", (h,)).fetchone()
    return _row(row) if row else None


def list_rules(conn, status: str | None = None, limit: int = 200) -> list[dict]:
    sql = f"SELECT {RULE_COLUMNS} FROM rules" + (" WHERE status = ?" if status else "") + " ORDER BY id DESC LIMIT ?"
    return [_row(r) for r in conn.execute(sql, (status, limit) if status else (limit,))]


def submit(db: LabDB, raw) -> dict:
    """Enregistre une règle à tester (ou renvoie celle déjà connue, verdict compris)."""
    canon = parse(raw)
    h = rule_hash(canon)
    known = get_rule(db.conn, h)
    if known:
        return known | {"deja_connue": True}
    name = (raw.get("nom") or raw.get("name") or "") if isinstance(raw, dict) else ""
    db.conn.execute("INSERT INTO rules (hash, name, rule, status, submitted_at) VALUES (?, ?, ?, 'en_attente', ?)",
                    (h, str(name)[:120], json.dumps(canon, ensure_ascii=False), now_iso()))
    return get_rule(db.conn, h) | {"deja_connue": False}


def test(db: LabDB, raw, cfg: dict | None = None) -> dict:
    """Soumet et teste tout de suite (tests, ligne de commande) ; mémoire des verdicts respectée."""
    rule = submit(db, raw)
    if rule["etat"] != "en_attente":
        return rule
    return run_one(db, rule["empreinte"], cfg)


def run_one(db: LabDB, h: str, cfg: dict | None = None) -> dict:
    rule = get_rule(db.conn, h)
    canon = rule["regle"]
    trial = (db.get("validator", "trials", 0) or 0) + 1
    res = evaluate(db.conn, canon, trial, cfg)
    if res["verdict"] == "attente":
        db.conn.execute("UPDATE rules SET reason = ? WHERE hash = ?", (json.dumps(res["raisons"], ensure_ascii=False), h))
        return get_rule(db.conn, h)
    with db.transaction():
        if res["verdict"] != "invalide":
            db.set("validator", "trials", trial)
        db.conn.execute("UPDATE rules SET status = ?, reason = ?, metrics = ?, trial = ?, tested_at = ? WHERE hash = ?",
                        (res["verdict"], json.dumps(res["raisons"], ensure_ascii=False),
                         json.dumps(res["mesures"], ensure_ascii=False),
                         trial if res["verdict"] != "invalide" else None, now_iso(), h))
    log.info("Labo : règle %s %s (%s)", h[:10], res["verdict"], " ; ".join(res["raisons"])[:300])
    return get_rule(db.conn, h)


def process_pending(db: LabDB, max_rules: int) -> list[dict]:
    """Teste au plus `max_rules` règles en attente (les plus anciennes d'abord)."""
    out = []
    if max_rules <= 0:
        return out
    pending = [h for (h,) in db.conn.execute("SELECT hash FROM rules WHERE status = 'en_attente' ORDER BY id")]
    # Une règle « en attente de données » passe après les autres : elle ne bloque pas la file.
    pending.sort(key=lambda h: db.get("validator_wait", h, 0) or 0)
    for h in pending[:max_rules]:
        rule = run_one(db, h)
        if rule["etat"] == "en_attente":
            db.set("validator_wait", h, time.time())
        out.append(rule)
    return out


# --- File de soumission (le serveur web n'écrit jamais dans la base) ------------------------

def queue_dir(db_file: str) -> Path:
    return Path(db_file).parent / "lab_queue"


def enqueue(db_file: str, raw: dict) -> dict:
    """Dépose une règle dans la file (fichier <empreinte>.json, écriture atomique)."""
    canon = parse(raw)
    h = rule_hash(canon)
    q = queue_dir(db_file)
    q.mkdir(parents=True, exist_ok=True)
    target = q / f"{h}.json"
    if not target.exists():
        if sum(1 for _ in q.glob("*.json")) >= QUEUE_MAX:
            raise RuleError(f"file pleine ({QUEUE_MAX} règles en attente de lecture par le worker)")
        tmp = q / f".{h}.tmp"
        tmp.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        tmp.replace(target)
    return {"empreinte": h, "etat": "en_file", "regle": canon}


def ingest_queue(db: LabDB) -> int:
    """Le worker lit la file : chaque règle passe en base (en attente), le fichier est effacé."""
    q = queue_dir(db.path)
    if not q.exists():
        return 0
    n = 0
    for f in sorted(q.glob("*.json")):
        try:
            submit(db, json.loads(f.read_text(encoding="utf-8")))
            n += 1
        except (RuleError, ValueError) as exc:
            log.warning("Labo : règle %s illisible, écartée (%s)", f.name, exc)
        f.unlink(missing_ok=True)
    return n


def counts(conn) -> dict:
    out = {"en_attente": 0, "validee": 0, "rejetee": 0, "invalide": 0}
    try:
        for status, n in conn.execute("SELECT status, COUNT(*) FROM rules GROUP BY status"):
            out[status] = n
        row = conn.execute("SELECT value FROM progress WHERE job = 'validator' AND key = 'trials'").fetchone()
        out["essais"] = json.loads(row[0]) if row else 0
    except Exception:
        pass
    return out


def rejected(conn, limit: int = 30) -> list[dict]:
    return [{"empreinte": h[:12], "nom": name, "regle": json.loads(rule),
             "raison": (json.loads(reason) or ["?"])[0] if reason else None}
            for h, name, rule, reason in conn.execute(
                "SELECT hash, name, rule, reason FROM rules WHERE status IN ('rejetee', 'invalide') "
                "ORDER BY id DESC LIMIT ?", (limit,))]

