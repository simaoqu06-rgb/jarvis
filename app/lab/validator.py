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
- "pourquoi" (≤ 1 000 caractères) et "council" (≤ 2 000) (facultatifs, Labo 3) : raison courte et
  verdict du conseil de la routine du soir, gardés avec la règle, sans effet sur l'empreinte ;
- une règle qui demanderait de régénérer les plans (nouveaux paramètres de stop, de TP…) est refusée
  en v1 : la régénération sur les bougies stockées coûterait des heures de calcul par idée sur
  1 cœur ; elle viendra plus tard.

Hors échantillon (avis de GPT et vérification du 10 octobre 2026) : une règle à seuil fixe est imaginée en
lisant les trades du labo ; la juger sur ces mêmes jours serait un test dans l'échantillon. À la soumission,
la règle note le dernier jour journalisé par origine et marché (rules.seen_until, même frontière que les
leçons, lessons.seen_until) : SEULS COMPTENT LES JOURS JOURNALISÉS APRÈS LA SOUMISSION (rejeu pas encore
calculé et direct ; un jour réécrit ou recalculé ne compte jamais). Tant qu'il n'y en a pas `min_days`, la
règle reste « en attente » sans consommer d'essai. Le même walk-forward sur les jours déjà vus est gardé à
part (« exploratoire » dans les mesures, affiché) et ne compte pas dans le verdict. Règles d'avant la
colonne (seen_until NULL) : tous les jours, comme avant.

Validation « walk-forward » sur les trades de la version de méthode en cours (rejeu et direct) :
les jours de chaque marché sont coupés en W+1 blocs chronologiques (W = 3 ; blocs par marché, les rejeux
actions et crypto avancent chacun de leur côté) ; fenêtre i : ajustement sur les blocs 0..i, test sur le
bloc i+1 privé de ses premiers jours (embargo d'1 jour : un trade crypto du dernier jour d'ajustement se
dénoue le lendemain). Les fenêtres de test ne se recouvrent pas. L'ajustement ne sert qu'au seuil « auto ».
Chaque fenêtre porte le plus grand trou du calendrier dans son test (trou_max_jours : rejeu interrompu
puis direct) et son nombre de jours en direct (jours_direct) ; un bloc à cheval sur la frontière rejeu /
direct n'est pas refusé (le trou agit comme un embargo, il ne crée pas de fuite), il est annoncé.

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
Labo 3 : les trades testés sont ceux que garde la méthode championne (ses filtres déjà appliqués),
pour qu'une idée ne soit pas créditée d'un gain que le champion fait déjà.
"""
import hashlib
import json
import logging
import math
import re
import time
from datetime import date
from pathlib import Path
from statistics import NormalDist

from app.lab import stats as STATS
from app.lab.db import LabDB, now_iso

log = logging.getLogger(__name__)
DEFAULTS = {"windows": 3, "embargo_days": 1, "min_affected": 100, "min_improvement_r": 0.05,
            "min_windows_better": 2, "alpha": 0.05, "max_capture_drop": 0.10, "min_days": 60,
            "auto_min_affected": 30, "missing_max": 0.95,
            # Labo 3 : challenger en direct (trades déclenchés depuis sa création, gardés par lui), jugé à
            # des regards fixes en jours de trades (method.verdict) ; challenger_max_trades n'est plus qu'affiché.
            "challenger_min_trades": 100, "challenger_max_trades": 300,
            "challenger_min_gain": 0.02, "challenger_max_days": 100,
            "challenger_looks_days": [20, 40, 60],
            # 3 regards fixes, alpha global unilatéral 0,05 (Bonferroni) : z ≥ 2,13.
            "challenger_min_z": NormalDist().inv_cdf(1 - 0.05 / 3)}
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
# Champs du contexte connus seulement après la décision (au moment de l'exécution ou plus tard) ; couverture des
# bougies de la séance simulée (avis du 10 octobre, I4) : une mesure de la donnée, pas un signal.
LATE_CONTEXT = {"context.gap_open_pct", "context.barres_seance", "context.ouverture_presente", "context.couverture"}
# Règle du propriétaire : jamais de filtre de liquidité ni de capitalisation. Le volume en dollars,
# le rang et le motif d'entrée dans l'univers (classés au volume en dollars) en sont.
SIZE_FIELDS = {"context.dollar_volume", "context.universe_rank", "context.universe_reason"}
SIZE_WORDS = ("dollar_volume", "market_cap", "marketcap", "capitalis", "capitaliz", "liquid")
PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\[\d{1,2}\])?(\.[A-Za-z_][A-Za-z0-9_]*(\[\d{1,2}\])?)*$")
MAX_LEAVES, MAX_DEPTH = 12, 6
NOTE_KEYS = {"pourquoi": 1000, "council": 2000}  # notes de la routine du soir (longueur maximale)
RULE_KEYS = {"nom", "name", "description", "exclude", "keep", "markets", *NOTE_KEYS}
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


def market_windows(rows: list[tuple], n: int = 3, embargo: int = 1) -> list[tuple[set, set]]:
    """`windows` sur les jours de chaque marché (les rejeux actions et crypto avancent chacun de leur
    côté), réunies fenêtre par fenêtre ; clés (marché, jour). Un seul marché : mêmes fenêtres que `windows`."""
    days: dict[str, set] = {}
    for r in rows:
        days.setdefault(r[1], set()).add(r[0])
    per = [windows(list(d), n, embargo) for d in days.values()]
    markets = list(days)
    return [({(m, d) for m, w in zip(markets, per) for d in w[i][0]},
             {(m, d) for m, w in zip(markets, per) for d in w[i][1]}) for i in range(n)]


def gap_days(days) -> int:
    """Plus grand trou du calendrier (jours) entre deux jours consécutifs ; 0 avec moins de deux jours."""
    ds = sorted(date.fromisoformat(d) for d in set(days))
    return max(((b - a).days for a, b in zip(ds, ds[1:])), default=0)


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


def walk_forward(rows: list[tuple], movers: set, cfg: dict, kind: str, auto_op: str | None = None,
                 live: set | None = None) -> dict:
    """Juge une règle sur des lignes (jour, marché, symbole, déclenché, r_net, tp1, touché|valeur).

    Sans `auto_op`, la 7e colonne dit si la condition est vraie ; avec, c'est la valeur du champ et
    le seuil est choisi sur l'ajustement de chaque fenêtre (choose_threshold). `live` : (marché, jour)
    journalisés en direct, pour annoter chaque fenêtre (jours_direct)."""
    wins = market_windows(rows, cfg["windows"], cfg["embargo_days"])
    out_windows, day_sums = [], {}
    cap_before = cap_after = 0
    affected_total = 0
    for fit, test in wins:
        thr = None
        if auto_op:
            thr = choose_threshold([r for r in rows if (r[1], r[0]) in fit], auto_op, kind, cfg["auto_min_affected"])

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
            if (r[1], r[0]) not in test:
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
        fit_days, test_days = [d for _, d in fit], [d for _, d in test]
        by_market: dict[str, list] = {}
        for m, d in test:
            by_market.setdefault(m, []).append(d)
        out_windows.append({"ajustement": [min(fit_days), max(fit_days)] if fit_days else None,
                            "test": [min(test_days), max(test_days)] if test_days else None,
                            "trades_test": int(base_n), "touches": n_aff,
                            "esperance_avant_r": round(base_m, 4) if base_m is not None else None,
                            "esperance_apres_r": round(kept_m, 4) if kept_m is not None else None,
                            "amelioration_r": round(delta, 4) if delta is not None else None,
                            "gain_total_r": round(-removed_r, 2), "seuil": thr,
                            "trou_max_jours": max((gap_days(ds) for ds in by_market.values()), default=0),
                            "jours_direct": len(test & live) if live else 0})
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
    """Version de génération (celle sous laquelle les trades sont journalisés)."""
    from app.lab.db import generation_version

    return generation_version(conn)


def champion_filter(conn) -> tuple[str, list]:
    """Expression SQL (alias t et r) des trades gardés par la méthode championne ; « 1 » sans filtre."""
    from app.lab import method as M

    return M.kept_sql(M.champion(conn)["regles"])


def frontier_sql(seen: dict | None, unseen: bool = True) -> tuple[str, list]:
    """Condition SQL (alias t) : trade d'un jour postérieur à la frontière `seen` de la règle (`unseen`),
    ou d'un jour qu'elle a pu voir ; « 1 » sans frontière (règle d'avant la colonne seen_until)."""
    if seen is None:
        return "1", []
    from app.lab import lessons as L  # import tardif : lessons importe ce module

    cond, params = L.unseen_sql(seen)
    return (cond, params) if unseen else (f"(NOT {cond})", params)


def frontier(conn, h: str) -> dict | None:
    """Frontière notée à la soumission (dernier jour journalisé par origine et marché) ; None avant la colonne."""
    row = conn.execute("SELECT seen_until FROM rules WHERE hash = ?", (h,)).fetchone()
    return json.loads(row[0]) if row and row[0] else None


def load_rows(conn, canon: dict, version: str, seen: dict | None = None, unseen: bool = True) -> list[tuple]:
    """(jour, marché, symbole, déclenché, r_net, tp1, touché|valeur) de tous les trades de la version,
    triés par jour. La condition est évaluée par SQLite (json_extract) : aucun JSON lu en Python.
    `seen` : frontière de la règle, ne garde que les jours d'après (`unseen`) ou ceux d'avant."""
    cond = canon["cond"]
    if cond.get("value") == "auto":
        col, cparams = _field_sql(cond["field"])
    else:
        col, cparams = compile_cond(cond)
        col = f"COALESCE({col}, 0)"
    kept, kparams = champion_filter(conn)
    fcond, fparams = frontier_sql(seen, unseen)
    sql = (f"SELECT t.day, t.market, t.symbol, t.triggered, t.r_net, t.tp1_hit, {col} "
           "FROM trades t LEFT JOIN regimes r ON r.market = t.market AND r.day = t.day WHERE t.method_version = ? "
           f"AND {kept} AND {fcond}")
    params = [*cparams, version, *kparams, *fparams]
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


def live_days(conn, version: str, markets: list | None) -> set:
    """(marché, jour) journalisés en direct (annotation des fenêtres du walk-forward)."""
    sql, params = "SELECT DISTINCT market, day FROM trades WHERE origin = 'live' AND method_version = ?", [version]
    if markets:
        sql += f" AND market IN ({', '.join('?' * len(markets))})"
        params += markets
    return {tuple(r) for r in conn.execute(sql, params)}


def missing_share(conn, canon: dict, version: str, seen: dict | None = None) -> dict[str, float]:
    """Part des trades sans valeur pour chaque champ JSON de la règle (marchés de la règle seulement ;
    `seen` : jours postérieurs à sa frontière seulement)."""
    out = {}
    kept, kparams = champion_filter(conn)
    fcond, fparams = frontier_sql(seen)
    msql, mparams = f" AND {kept} AND {fcond}", [*kparams, *fparams]
    if canon.get("markets"):
        msql += f" AND t.market IN ({', '.join('?' * len(canon['markets']))})"
        mparams += list(canon["markets"])
    for leaf in leaves_of(canon["cond"]):
        field = leaf["field"]
        if field in out or field in FLAT:
            continue
        expr, params = _field_sql(field)
        row = conn.execute(f"SELECT AVG({expr} IS NULL) FROM trades t LEFT JOIN regimes r ON r.market = t.market "
                           f"AND r.day = t.day WHERE t.method_version = ?{msql}",
                           (*params, version, *mparams)).fetchone()
        out[field] = row[0] if row[0] is not None else 1.0
    return out


EXPLORE_KEYS = ("touches_test", "amelioration_moyenne_r", "amelioration_globale_r", "fenetres_meilleures", "z", "p")


def explore_text(e: dict) -> str:
    z = f"z {e['z']:.2f}" if e.get("z") is not None else "z n/d"
    return (f"exploratoire sur les {e['jours']} jours déjà vus (sans effet sur le verdict) : "
            f"{e['amelioration_moyenne_r']:+.3f} R par trade, meilleure dans {e['fenetres_meilleures']} fenêtres, {z}")


def evaluate(conn, canon: dict, trial: int, cfg: dict | None = None, seen: dict | None = None,
             explore: bool = False) -> dict:
    """Teste une règle canonique ; renvoie {verdict, raisons, mesures}. verdict « attente » si les
    données ne suffisent pas encore (aucun essai consommé). `seen` : frontière de la règle (seuls les
    jours d'après comptent) ; `explore` : mesurer aussi les jours déjà vus (« exploratoire », sans effet)."""
    cfg = cfg or config(conn)
    version = current_version(conn)
    if not version:
        return {"verdict": "attente", "raisons": ["aucun trade journalisé"], "mesures": {}}
    missing = missing_share(conn, canon, version)  # format des données : sur tous les jours
    absent = [f for f, share in missing.items() if share >= cfg["missing_max"]]
    if absent:
        return {"verdict": "invalide", "mesures": {"champs_absents": missing},
                "raisons": [f"champ presque toujours absent des trades : {', '.join(absent)}"]}
    rows = load_rows(conn, canon, version, seen)
    days = {r[0] for r in rows}
    auto_op = canon["cond"]["op"] if canon["cond"].get("value") == "auto" else None
    movers, live, explo = None, None, None
    if seen is not None and explore:
        old = load_rows(conn, canon, version, seen, unseen=False)
        old_days = {r[0] for r in old}
        if len(old_days) >= cfg["min_days"]:
            movers, live = load_movers(conn, canon["markets"]), live_days(conn, version, canon["markets"])
            e = walk_forward(old, movers, cfg, canon["kind"], auto_op, live)
            explo = {"jours": len(old_days), "trades": len(old)} | {k: e[k] for k in EXPLORE_KEYS}
    extra = ({"frontiere": seen} if seen is not None else {}) | ({"exploratoire": explo} if explo else {})
    if len(days) < cfg["min_days"]:
        new = " neufs (journalisés après la soumission)" if seen is not None else ""
        return {"verdict": "attente", "mesures": {"jours": len(days)} | extra,
                "raisons": [f"{len(days)} jours de trades{new}, il en faut {cfg['min_days']}"]
                + ([explore_text(explo)] if explo else [])}
    if seen is not None:
        missing = missing_share(conn, canon, version, seen)
        absent = [f for f, share in missing.items() if share >= cfg["missing_max"]]
        if absent:
            return {"verdict": "invalide", "mesures": {"champs_absents": missing} | extra,
                    "raisons": [f"champ presque toujours absent des trades neufs : {', '.join(absent)}"]}
    if movers is None:
        movers, live = load_movers(conn, canon["markets"]), live_days(conn, version, canon["markets"])
    m = walk_forward(rows, movers, cfg, canon["kind"], auto_op, live)
    verdict, fails, ok = judge(m, cfg, trial)
    m.update(version=version, jours=len(days), trades=len(rows), essai=trial,
             seuil_p=trial_level(trial, cfg["alpha"]), criteres_tenus=ok, **extra)
    return {"verdict": verdict, "raisons": fails, "mesures": m}


def _row(r) -> dict:
    rid, h, name, rule, status, reason, metrics, trial, submitted, tested, *labo3 = r
    out = {"id": rid, "empreinte": h, "nom": name, "regle": json.loads(rule), "etat": status,
           "raisons": json.loads(reason) if reason else [], "mesures": json.loads(metrics) if metrics else None,
           "essai": trial, "soumise_le": submitted, "testee_le": tested}
    if labo3:
        out.update(zip(("pourquoi", "council", "soiree", "decision", "decidee_le"), labo3))
    return out


RULE_COLUMNS = ("id, hash, name, rule, status, reason, metrics, trial, submitted_at, tested_at, "
                "pourquoi, council, soiree, decision, decided_at")


def get_rule(conn, h: str) -> dict | None:
    row = conn.execute(f"SELECT {RULE_COLUMNS} FROM rules WHERE hash = ?", (h,)).fetchone()
    return _row(row) if row else None


def list_rules(conn, status: str | None = None, limit: int = 200) -> list[dict]:
    sql = f"SELECT {RULE_COLUMNS} FROM rules" + (" WHERE status = ?" if status else "") + " ORDER BY id DESC LIMIT ?"
    return [_row(r) for r in conn.execute(sql, (status, limit) if status else (limit,))]


def notes(raw) -> dict:
    """Notes de la routine du soir (pourquoi, council) : texte, tronqué à la longueur permise."""
    out = {}
    if isinstance(raw, dict):
        for key, limit in NOTE_KEYS.items():
            value = raw.get(key)
            if value is not None and not isinstance(value, str):
                raise RuleError(f"« {key} » : texte attendu")
            out[key] = value.strip()[:limit] if value else None
    return out


def submit(db: LabDB, raw, soiree: str | None = None) -> dict:
    """Enregistre une règle à tester (ou renvoie celle déjà connue, verdict compris). `soiree` : soirée
    (date de Paris) de la soumission par la routine ; peut venir du fichier de file (« _soiree »)."""
    if isinstance(raw, dict) and "_soiree" in raw:
        raw = dict(raw)
        soiree = soiree or raw.pop("_soiree")
    canon = parse(raw)
    h = rule_hash(canon)
    known = get_rule(db.conn, h)
    if known:
        return known | {"deja_connue": True}
    name = (raw.get("nom") or raw.get("name") or "") if isinstance(raw, dict) else ""
    note = notes(raw)
    from app.lab import lessons as L  # import tardif : lessons importe ce module

    # Frontière hors échantillon : tout ce qui est journalisé à l'ingestion a pu être lu par la routine.
    db.conn.execute("INSERT INTO rules (hash, name, rule, status, submitted_at, pourquoi, council, soiree, seen_until) "
                    "VALUES (?, ?, ?, 'en_attente', ?, ?, ?, ?, ?)",
                    (h, str(name)[:120], json.dumps(canon, ensure_ascii=False), now_iso(), note.get("pourquoi"),
                     note.get("council"), soiree, json.dumps(L.seen_until(db.conn))))
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
    explo = (rule["mesures"] or {}).get("exploratoire")  # jours déjà vus : mesurés une seule fois
    res = evaluate(db.conn, canon, trial, cfg, frontier(db.conn, h), explore=explo is None)
    if explo is not None and res["verdict"] != "invalide":
        res["mesures"]["exploratoire"] = explo
        if res["verdict"] == "attente":
            res["raisons"].append(explore_text(explo))
    if res["verdict"] == "attente":
        db.conn.execute("UPDATE rules SET reason = ?, metrics = ? WHERE hash = ?",
                        (json.dumps(res["raisons"], ensure_ascii=False), json.dumps(res["mesures"], ensure_ascii=False), h))
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


def enqueue(db_file: str, raw: dict, soiree: str | None = None) -> dict:
    """Dépose une règle dans la file (fichier <empreinte>.json, écriture atomique)."""
    canon = parse(raw)
    notes(raw)
    h = rule_hash(canon)
    q = queue_dir(db_file)
    q.mkdir(parents=True, exist_ok=True)
    target = q / f"{h}.json"
    if not target.exists():
        if sum(1 for _ in q.glob("*.json")) >= QUEUE_MAX:
            raise RuleError(f"file pleine ({QUEUE_MAX} règles en attente de lecture par le worker)")
        tmp = q / f".{h}.tmp"
        tmp.write_text(json.dumps(raw | ({"_soiree": soiree} if soiree else {}), ensure_ascii=False), encoding="utf-8")
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

