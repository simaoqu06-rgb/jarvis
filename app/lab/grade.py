"""Grade de JARVIS (F- à S+) : la preuve, mesurée par le code, que la méthode en place gagne.

Barème v2 validé par Simão le 2026-10-07 (après avis de GPT et Gemini) :

- Lettre = rentabilité : espérance nette en R par trade sur la fenêtre (FENETRE_JOURS derniers jours
  de bourse du marché, trades déclenchés de la méthode championne, hors échantillon ; actions : R après
  commission IBKR à la mise de Simão, strategies.cost_variants), avec son intervalle de confiance à 95 %
  en grappes par jour, en t de Student (stats.mean_ci). Une fenêtre coupée par un trou de plus de
  TROU_JOURS jours (rejeu puis direct) est notée sur son dernier segment s'il suffit ; sinon elle garde
  sa lettre, marquée « fenêtre discontinue », sans risque réel :
    S ≥ +0,40 R, A ≥ +0,25, B ≥ +0,15, C ≥ +0,05 (borne basse de l'IC > 0 dès C),
    D positive mais pas prouvée, E entre −0,10 et 0, F < −0,10.
- − / neutre / + = stabilité et calibration (2 ou 3 critères : +, 1 : neutre, 0 : −) :
    séries de pertes normales (pire série ≤ 1,25 × celle attendue pour des trades indépendants),
    régularité (écart ≤ 0,15 R entre les deux moitiés chronologiques de la fenêtre),
    délais calibrés (40 à 65 % des TP touchés au plus tard au délai médian prévu).
  Réussite et % de TP sont affichés à côté, sans effet sur la lettre ni sur le risque (un taux de
  réussite se gonfle en rapprochant le TP1).
- Moins de MIN_TRADES trades sur MIN_JOURS jours : « non noté », risque 0 %.

Le grade donne le DROIT de risquer ; la taille se calcule à part (¼ de Kelly sur la borne basse de
l'espérance, variance des R, corrigé de la corrélation des trades d'un même jour), plafonnée par la
lettre : F, E, non noté 0 % (plans en observation, forward-test papier seulement), D 0,25 %, C 0,5 %,
B 0,75 %, A 1 %, S 1,5 % (depuis le 25/10/2026, après les audits de GPT et Gemini ; avant : C 1 %, B 1,5 %,
A 3 %, S 5 %, plafond EmperorBTC). En direct s'y ajoutent le risque ouvert total (≤ 3 %)
et l'arrêt de la journée à −3 R sur les trades pris (risk_now) ; un grade que le labo n'a pas revérifié
depuis MAX_AGE_H heures ne donne plus aucun droit (périmé : 0 %).

Changements du grade officiel (worker, chaque passage, zéro jeton) :
- baisse : immédiate ;
- hausse : au moins HAUSSE_TRADES nouveaux trades sur HAUSSE_JOURS nouveaux jours depuis le dernier
  changement (deux mesures sur des fenêtres qui se chevauchent ne prouvent rien) ; une hausse de
  lettre (qui change le risque) attend en plus l'accord de Simão (proposition Approuver/Refuser) ;
- coupe-circuit : à partir de C, une baisse de COUPE_CIRCUIT_R depuis le sommet (trades journalisés
  depuis le dernier changement) ramène tout de suite en D ;
- council du soir : un avis par marché et par soirée (POST /api/lab/grade/avis), un cran au plus ;
  une baisse s'applique, une hausse de lettre passe par Approuver/Refuser ;
- nouvelle méthode : remise à zéro d'un marché seulement si ses filtres EFFECTIFS changent (règles du
  champion qui le touchent, ou nouvelle génération) ; sur ce marché, seuls comptent ensuite les jours
  journalisés après la promotion (frontière par jour, comme les leçons), jamais ceux de l'essai qui l'ont
  décidée. Une promotion d'une règle actions ne touche donc pas le grade crypto ;
- version morte : au moins MORT_TRADES trades et borne haute de l'IC sous 0 : annoncée, mode survie.

Le serveur web n'écrit jamais dans la base : avis et décisions passent par data/lab_queue/grade/ ; le
worker écrit lab_grade.json, lu par JARVIS en direct (plans, !grade, cockpit, propositions).
"""
import json
import logging
import math
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import env_float
from app.lab import stats as STATS
from app.lab import validator as V
from app.lab.db import LabDB, db_path, now_iso

log = logging.getLogger(__name__)
PARIS = ZoneInfo("Europe/Paris")
GRADE_FILE = "lab_grade.json"
MARKETS = ("stock", "crypto")
MARKET_FR = {"stock": "actions", "crypto": "crypto"}
LETTERS = "FEDCBAS"
MODS = ("-", "", "+")
FENETRE_JOURS = 60
MIN_TRADES, MIN_JOURS = 100, 10
HAUSSE_TRADES, HAUSSE_JOURS = 150, 5
MORT_TRADES = 150
COUPE_CIRCUIT_R = 20.0
SEUILS = (("S", 0.40), ("A", 0.25), ("B", 0.15), ("C", 0.05))
E_PLANCHER = -0.10
SERIE_TOLERANCE = 1.25
REGULARITE_R = 0.15
DELAIS = (0.40, 0.65)
# Plafond de risque réel par trade (% du capital) selon la lettre ; D : micro-exposition fixe.
# Abaissé le 25/10/2026 (audits de GPT et Gemini, décision n° 2 confiée à JARVIS par Simão) : avant, C 1 %, B 1,5 %,
# A 3 %, S 5 % (plafond EmperorBTC) et 4 % de risque ouvert. Changé à froid, le grade étant F (0 %) partout.
RISQUE_MAX = {"F": 0.0, "E": 0.0, "D": 0.25, "C": 0.5, "B": 0.75, "A": 1.0, "S": 1.5}
KELLY_PART = 0.25
RISQUE_OUVERT_MAX = 3.0
# Risque compté pour un trade réellement pris dont la taille est illisible : 1 % (l'ancien plafond de C), plus que
# tout plafond de C ou B : en cas de doute, le budget se remplit plus vite, jamais moins vite.
RISQUE_INCONNU = 1.0
ARRET_JOUR_R = -3.0
TROU_JOURS = 10        # écart calendaire entre deux jours de la fenêtre au-delà duquel elle est coupée
# Version du calcul (2 : R des actions après commission IBKR à 20 €, Kelly corrigé ; 3 : après une promotion, seuls
# les jours journalisés après elle comptent, sur les seuls marchés dont elle change les filtres ; barème IBKR du compte ;
# 4 : brackets des plans, sortie unique sur TP2 par défaut depuis le 25/10/2026).
GRADE_CALC = 4
MAX_AGE_H = env_float("JARVIS_GRADE_MAX_AGE_H", 6.0)  # au-delà, le grade est périmé : risque 0 %
MOTIFS_MAX, VOIX_MAX = 1500, 1000
SOURCES_FR = {"calcul": "calcul", "council": "council du soir", "simao": "accord de Simão",
              "coupe_circuit": "coupe-circuit", "nouvelle_version": "nouvelle méthode"}

class AvisError(ValueError):
    """Avis du council illisible (message en français, montré tel quel)."""


# --- Barème (fonctions pures) ------------------------------------------------------------------

def rank_of(letter: str, mod: int) -> int:
    """Rang de 0 (F-) à 20 (S+) ; mod : 0 = −, 1 = neutre, 2 = +."""
    return LETTERS.index(letter) * 3 + mod


def name(rank: int | None) -> str:
    if rank is None:
        return "non noté"
    return LETTERS[rank // 3] + MODS[rank % 3]


def letter_of(rank: int | None) -> str | None:
    return None if rank is None else LETTERS[rank // 3]


D_RANK = rank_of("D", 1)
D_PLUS = rank_of("D", 2)
C_MINUS = rank_of("C", 0)


def letter(mean: float, low: float | None) -> str:
    """Lettre d'après l'espérance et la borne basse de son IC à 95 %."""
    proven = low is not None and low > 0
    for lt, floor in SEUILS:
        if mean >= floor and proven:
            return lt
    if mean > 0:
        return "D"
    return "E" if mean >= E_PLANCHER else "F"


def expected_streak(n: int, loss_share: float) -> float | None:
    """Plus longue série de pertes attendue sur n trades indépendants (≈ ln(n·p) / ln(1/q))."""
    q = loss_share
    if n < 2 or not 0 < q < 1:
        return None
    return max(1.0, math.log(n * (1 - q)) / math.log(1 / q))


def evaluate(rows: list[tuple]) -> dict:
    """Mesures et grade d'une fenêtre. Ligne : (id, jour, r_net, tp1, tp2, tp3, délai_ok_1, délai_ok_2,
    délai_ok_3), dans l'ordre des plans ; délai_ok vaut 1, 0 ou None (stats.delay_ok)."""
    day_sums: dict[str, list] = {}
    curve = STATS.Curve()
    n = wins = losses = 0
    tp = [0, 0, 0]
    dl_ok = dl_n = 0
    s = s2 = 0.0
    for _id, day, r, tp1, tp2, tp3, d1, d2, d3 in rows:
        n += 1
        s += r
        s2 += r * r
        wins += r > 0
        losses += r < 0
        d = day_sums.setdefault(day, [0.0, 0])
        d[0] += r
        d[1] += 1
        curve.add(r)
        for i, hit in enumerate((tp1, tp2, tp3)):
            tp[i] += bool(hit)
        for ok in (d1, d2, d3):
            if ok is not None:
                dl_n += 1
                dl_ok += ok
    out = {"trades": n, "jours": len(day_sums)}
    if n < MIN_TRADES or len(day_sums) < MIN_JOURS:
        return out | {"rang": None, "grade": name(None),
                      "raison": f"échantillon trop petit (au moins {MIN_TRADES} trades sur {MIN_JOURS} jours)"}
    mean, se, ci = STATS.mean_ci(day_sums, s, n)
    var = max(0.0, (s2 - n * mean * mean) / (n - 1))
    curve_res = curve.result()
    streak = expected_streak(n, losses / n)
    days = sorted(day_sums)
    halves = []
    for part in (days[:len(days) // 2], days[len(days) // 2:]):
        k = sum(day_sums[d][1] for d in part)
        halves.append(sum(day_sums[d][0] for d in part) / k if k else None)
    delays = dl_ok / dl_n if dl_n else None
    criteria = {
        "series_normales": streak is not None and curve_res["pire_serie_pertes"] <= SERIE_TOLERANCE * streak,
        "regulier": None not in halves and abs(halves[0] - halves[1]) <= REGULARITE_R,
        "delais_calibres": delays is not None and DELAIS[0] <= delays <= DELAIS[1],
    }
    met = sum(criteria.values())
    lt = letter(mean, ci[0] if ci else None)
    rank = rank_of(lt, 2 if met >= 2 else met)
    low = ci[0] if ci else None
    kelly = KELLY_PART * low / var * 100 if low is not None and low > 0 and var > 0 else 0.0
    # Les trades d'un même jour ne sont pas indépendants : corrélation intra-jour estimée par l'effet de grappe
    # (DEFF = se² × n / var, ρ = (DEFF − 1) / (k̄ − 1), k̄ trades par jour du labo), appliquée aux positions que
    # Simão peut tenir ensemble sous le budget (⌈RISQUE_OUVERT_MAX / plafond de la lettre⌉), pas aux dizaines du labo.
    k_bar = n / len(day_sums)
    rho = (min(1.0, max(0.0, (se * se * n / var - 1) / (k_bar - 1)))
           if se is not None and var > 0 and k_bar > 1 else 0.0)
    k = math.ceil(RISQUE_OUVERT_MAX / RISQUE_MAX[lt]) if RISQUE_MAX[lt] > 0 else 1
    return out | {
        "rang": rank, "grade": name(rank), "lettre": lt,
        "esperance_r": round(mean, 4), "ic95": ci, "variance_r": round(var, 4), "total_r": round(s, 2),
        "kelly_pct": round(kelly / (1 + (k - 1) * rho), 3), "kelly_brut_pct": round(kelly, 3),
        "rho_jour": round(rho, 3), "positions_max": k,
        "criteres": criteria,
        "serie_pertes": curve_res["pire_serie_pertes"], "serie_attendue": round(streak, 1) if streak else None,
        "baisse_max_r": curve_res["baisse_max_r"],
        "moities_r": [round(h, 4) if h is not None else None for h in halves],
        "delais_tenus": round(delays, 4) if delays is not None else None,
        "reussite": round(wins / n, 4), "tp1": round(tp[0] / n, 4), "tp2": round(tp[1] / n, 4),
        "tp3": round(tp[2] / n, 4),
    }


def grade_risk(rank: int | None, kelly_pct: float | None) -> float:
    """Risque proposé par trade (% du capital) avant le budget du portefeuille."""
    lt = letter_of(rank)
    if lt is None or RISQUE_MAX[lt] == 0:
        return 0.0
    if lt == "D":
        return RISQUE_MAX["D"]
    return round(min(RISQUE_MAX[lt], kelly_pct or 0.0), 2)


def discontinuous(m: dict | None) -> bool:
    """Fenêtre mixte coupée par un trou que son dernier segment ne suffit pas à noter (window) : risque 0."""
    return bool(((m or {}).get("fenetre") or {}).get("discontinue"))


def drawdown(rs) -> float:
    curve = STATS.Curve()
    for r in rs:
        curve.add(r)
    return curve.max_dd


# --- Lecture de la base (worker) ----------------------------------------------------------------

def _scope(conn, market: str, seen: dict | None = None) -> tuple[str, list, str | None]:
    """Trades déclenchés de la version de génération gardés par le champion, sur le marché. `seen` : frontière du
    marché (Grader.frontier, jours journalisés à la promotion qui a changé ses filtres) : seuls comptent les jours
    d'après, comme pour les leçons (lessons.unseen_sql), jamais ceux de l'essai qui ont décidé la promotion ;
    None : pas de borne (marché sans filtre du champion)."""
    from app.lab import lessons as L  # import tardif, comme validator.frontier_sql
    from app.lab import method as M

    version = V.current_version(conn)
    champ = M.champion(conn)
    kept, kparams = V.champion_filter(conn)
    sql = (f"t.method_version = ? AND t.market = ? AND t.triggered = 1 AND t.r_net IS NOT NULL AND {kept}")
    params = [version, market, *kparams]
    if seen is not None:
        cond, cparams = L.unseen_sql({k: d for k, d in seen.items() if k.endswith(f":{market}")})
        sql += f" AND {cond}"
        params += cparams
    return sql, params, champ["version"]


def ibkr_plan() -> str:
    """Barème IBKR du compte de Simão (JARVIS_IBKR_PLAN, lu par Killmode et le suivi des trades) : « fixe » ou
    « degressif »."""
    from app.trading import killmode as K  # import tardif : killmode importe le labo

    return "degressif" if K.IBKR_PLAN == "degressif" else "fixe"


def cost_r(market: str, r_net: float, result: str | None) -> float:
    """R d'un trade pour le grade : actions, après commission IBKR à la mise de Simão (RISK_EUR, minimum par ordre),
    au barème du compte, avec les brackets des plans (ibkr.single_target) : une sortie unique sur TP2 pour un plan
    à trois objectifs (défaut depuis le 25/10/2026, la part du milieu du solde comme strategies.cost_variants
    r_20e_1s ; sur le chemin des trades v1, stop à l'entrée dès TP1, la gestion du labo v1, quand la mesure des GO garde
    le stop fixe), sinon un bracket par objectif (r_20e au Fixe, r_20e_deg au Dégressif : l'écart change de signe selon
    le nombre d'actions) ; r_net s'il manque le détail ; crypto : r_net (frais OKX déjà comptés)."""
    if market != "stock" or not result:
        return r_net
    from app.lab import strategies as ST  # import tardif, comme strategies le fait pour killmode
    from app.trading import ibkr as IB
    from app.trading import killmode as K

    try:
        res = json.loads(result)
        tps = (res.get("order_prices") or {}).get("targets") or []
        k = IB.single_target({"targets": tps})
        if k is not None and res.get("triggered") and res.get("exits"):
            o = res["order_prices"]
            done = {"declenche": True, "remplissage": res["fill_price"], "eurusd": K.EURUSD_DEFAULT,
                    "plan_ibkr": ibkr_plan(),
                    "sorties": [{"part": e["part"], "prix": e["price"], "raison": e["reason"]} for e in res["exits"]]}
            r = K.real_r({"entry": o["entry"], "stop": o["stop"], "targets": tps[k:k + 1]}, done)
        else:
            r = ST.cost_variants("stock", res)["r_20e_deg" if ibkr_plan() == "degressif" else "r_20e"]
    except (ValueError, KeyError, TypeError, ZeroDivisionError):
        return r_net
    return r_net if r is None else r


def span(days: list[str], live: set[str]) -> dict:
    """Étendue réelle d'une fenêtre (jours triés) : bornes, jours, jours du direct, rejeu, plus grand trou
    calendaire entre deux jours consécutifs."""
    if not days:
        return {"du": None, "au": None, "jours": 0, "jours_direct": 0, "trou_max_jours": 0}
    dates = [datetime.fromisoformat(d).date() for d in days]
    replay = [d for d in days if d not in live]
    return {"du": days[0], "au": days[-1], "jours": len(days), "jours_direct": sum(d in live for d in days),
            "trou_max_jours": max([(b - a).days for a, b in zip(dates, dates[1:])], default=0),
            "rejeu_du": replay[0] if replay else None, "rejeu_au": replay[-1] if replay else None,
            "direct_du": next((d for d in days if d in live), None)}


def last_segment(days: list[str], gap: int = TROU_JOURS) -> list[str]:
    """Jours (triés) après le dernier écart de plus de `gap` jours calendaires."""
    dates = [datetime.fromisoformat(d).date() for d in days]
    cut = max([i for i in range(1, len(dates)) if (dates[i] - dates[i - 1]).days > gap], default=0)
    return days[cut:]


def window_rows(conn, market: str, seen: dict | None = None) -> tuple[list[tuple], str | None, set[str]]:
    """Lignes de la fenêtre pour evaluate (R du grade, cost_r), version, et jours du direct de la fenêtre."""
    sql, params, version = _scope(conn, market, seen)
    join = "FROM trades t LEFT JOIN regimes r ON r.market = t.market AND r.day = t.day"
    row = conn.execute(f"SELECT DISTINCT t.day {join} WHERE {sql} ORDER BY t.day DESC LIMIT 1 OFFSET ?",
                       (*params, FENETRE_JOURS - 1)).fetchone()
    cutoff = row[0] if row else ""
    cur = conn.execute(
        f"""SELECT t.id, t.day, t.r_net, t.tp1_hit, t.tp2_hit, t.tp3_hit,
                   t.minutes_to_tp1, t.minutes_to_tp2, t.minutes_to_tp3,
                   json_extract(t.plan, '$.targets[0].minutes_median'),
                   json_extract(t.plan, '$.targets[1].minutes_median'),
                   json_extract(t.plan, '$.targets[2].minutes_median'), t.origin, t.result
            {join} WHERE {sql} AND t.day >= ? ORDER BY t.planned_at, t.id""", (*params, cutoff))
    rows, live = [], set()
    for i, day, r, a, b, c, m1, m2, m3, e1, e2, e3, origin, result in cur:
        rows.append((i, day, cost_r(market, r, result), a, b, c, STATS.delay_ok(a, m1, e1),
                     STATS.delay_ok(b, m2, e2), STATS.delay_ok(c, m3, e3)))
        if origin == "live":
            live.add(day)
    return rows, version, live


def window(conn, market: str, seen: dict | None = None) -> tuple[list[tuple], dict]:
    """Fenêtre du grade et son étendue réelle (`fenetre`). Un trou de plus de TROU_JOURS jours (rejeu puis
    direct) : notée sur le dernier segment s'il a MIN_TRADES trades sur MIN_JOURS jours (`coupee_au`), sinon
    gardée entière et marquée `discontinue` (risque 0 : une hausse de risque exige une preuve récente).
    `seen` : frontière du marché (_scope)."""
    rows, _, live = window_rows(conn, market, seen)
    days = sorted({r[1] for r in rows})
    fen = span(days, live)
    if fen["trou_max_jours"] > TROU_JOURS:
        seg = set(last_segment(days))
        kept = [r for r in rows if r[1] in seg]
        if len(seg) >= MIN_JOURS and len(kept) >= MIN_TRADES:
            rows, fen["coupee_au"] = kept, min(seg)
        else:
            fen["discontinue"] = True
    return rows, fen


def since(conn, market: str, after_id: int, seen: dict | None = None) -> dict:
    """Trades journalisés depuis le dernier changement : nombre, jours, baisse depuis le sommet (mêmes R que le
    grade)."""
    sql, params, _ = _scope(conn, market, seen)
    rs, days = [], set()
    for day, r, result in conn.execute(
            f"SELECT t.day, t.r_net, t.result FROM trades t LEFT JOIN regimes r ON r.market = t.market "
            f"AND r.day = t.day WHERE {sql} AND t.id > ? ORDER BY t.planned_at, t.id", (*params, after_id)):
        rs.append(cost_r(market, r, result))
        days.add(day)
    return {"trades": len(rs), "jours": len(days), "baisse_r": round(drawdown(rs), 2)}


def max_trade_id(conn) -> int:
    return conn.execute("SELECT COALESCE(MAX(id), 0) FROM trades").fetchone()[0]


# --- File : avis du council et décisions de Simão -------------------------------------------------

def queue_dir(db_file: str | None = None) -> Path:
    return V.queue_dir(db_file or db_path()) / "grade"


def _write_atomic(target: Path, data) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".{target.name}.{uuid.uuid4().hex[:6]}.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)


def parse_avis(raw) -> dict:
    """Avis du council : {"marche": "stock", "ajustement": -1 | 0 | 1, "motifs": "…",
    "voix": {"sceptique": "…", "pragmatique": "…", "critique": "…"}} (voix facultatives)."""
    if not isinstance(raw, dict):
        raise AvisError("objet JSON attendu")
    unknown = set(raw) - {"marche", "ajustement", "motifs", "voix"}
    if unknown:
        raise AvisError(f"champ inconnu : {', '.join(sorted(unknown))}")
    market = raw.get("marche")
    if market not in MARKETS:
        raise AvisError("« marche » : stock ou crypto")
    adj = raw.get("ajustement")
    if isinstance(adj, bool) or adj not in (-1, 0, 1):
        raise AvisError("« ajustement » : -1, 0 ou 1 (un cran au plus)")
    motifs = raw.get("motifs")
    if not isinstance(motifs, str) or not 10 <= len(motifs.strip()) <= MOTIFS_MAX:
        raise AvisError(f"« motifs » : texte de 10 à {MOTIFS_MAX} caractères (obligatoire)")
    voix = raw.get("voix") or {}
    if not isinstance(voix, dict) or set(voix) - {"sceptique", "pragmatique", "critique"} or any(
            not isinstance(v, str) or len(v) > VOIX_MAX for v in voix.values()):
        raise AvisError(f"« voix » : sceptique, pragmatique, critique ; textes de {VOIX_MAX} caractères au plus")
    return {"marche": market, "ajustement": adj, "motifs": motifs.strip(),
            "voix": {k: v.strip() for k, v in voix.items()}}


def enqueue_avis(db_file: str, raw, soiree: str) -> dict:
    """Serveur web : dépose l'avis du council (lu par le worker ; un seul par marché et par soirée,
    le dernier déposé remplace le précédent tant que le worker ne l'a pas lu)."""
    avis = parse_avis(raw) | {"soiree": soiree, "le": now_iso()}
    _write_atomic(queue_dir(db_file) / f"avis-{avis['marche']}-{soiree}.json", avis)
    return {"en_file": True, "marche": avis["marche"], "soiree": soiree}


def enqueue_decision(db_file: str, hausse_id: str, approve: bool, proposal_id: int | None = None) -> Path:
    """Serveur web : décision de Simão sur une hausse de lettre proposée."""
    if not re.fullmatch(r"(stock|crypto)-\d+", hausse_id or ""):
        raise ValueError("hausse illisible")
    target = queue_dir(db_file) / f"decision-{hausse_id}.json"
    _write_atomic(target, {"hausse": hausse_id, "decision": "approuvee" if approve else "refusee",
                           "proposition": proposal_id, "le": now_iso()})
    return target


# --- Worker : grade officiel ---------------------------------------------------------------------

class Grader:
    """État par marché (table progress, job « grade ») et historique (table grades)."""

    def __init__(self, db: LabDB, now: datetime):
        self.db, self.now = db, now

    def state(self, market: str) -> dict:
        return self.db.get("grade", market) or {"rang": None, "version": None, "depuis_id": 0, "base_id": None,
                                                 "change_le": None, "hausse": None, "morte": False, "seq": 0}

    def save(self, market: str, st: dict) -> None:
        self.db.set("grade", market, st)

    def history(self, market: str, st: dict, rank, measured, source: str, reason: str, metrics=None,
                soiree: str | None = None) -> None:
        self.db.conn.execute(
            "INSERT INTO grades (created_at, market, version, rank, measured, source, reason, metrics, soiree) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.now.isoformat(timespec="seconds"), market, st.get("version"), rank, measured, source, reason,
             json.dumps(metrics or {}, ensure_ascii=False), soiree))

    def event(self, st: dict, text: str, data: dict | None = None) -> None:
        from app.lab import method as M

        M.event(self.db, "grade", st.get("version"), text, data, self.now)

    def set_rank(self, market: str, st: dict, rank: int, source: str, reason: str, measured=None,
                 metrics=None, soiree: str | None = None) -> None:
        old = st["rang"]
        st.update(rang=rank, depuis_id=max_trade_id(self.db.conn), change_le=self.now.isoformat(timespec="seconds"),
                  hausse=None)
        self.history(market, st, rank, measured, source, reason, metrics, soiree)
        if old == rank:
            return
        if old is not None and letter_of(old) == letter_of(rank):
            # − / + dans la même lettre : le risque proposé ne change pas. Gardé dans l'historique
            # (!grade) sans message privé, sinon le rejeu (des milliers de trades par passage) fait
            # osciller F / F+ et envoie un message toutes les 40 minutes.
            log.info("Labo : grade %s %s → %s (%s)", market, name(old), name(rank), reason)
            return
        arrow = "⬆️" if old is not None and rank > old else "⬇️" if old is not None else "🏅"
        risk = 0.0 if discontinuous(metrics) else grade_risk(rank, (metrics or {}).get("kelly_pct"))
        text = (f"{arrow} Grade JARVIS {MARKET_FR[market]} : {name(old) + ' → ' if old is not None else ''}"
                f"**{name(rank)}** ({SOURCES_FR.get(source, source)} : {reason}). Risque proposé par trade : "
                f"{practical(risk)}{' (plans en observation)' if risk == 0 else ''}.")
        self.event(st, text, {"marche": market, "de": old, "vers": rank, "source": source})

    def propose(self, market: str, st: dict, target: int, source: str, reason: str,
                soiree: str | None = None) -> None:
        """Hausse de lettre : attend l'accord de Simão (proposition créée par le serveur)."""
        if st.get("hausse") and st["hausse"]["vers"] == target:
            return
        st["seq"] = st.get("seq", 0) + 1
        st["hausse"] = {"id": f"{market}-{st['seq']}", "de": st["rang"], "vers": target, "source": source,
                        "raison": reason, "le": self.now.isoformat(timespec="seconds")}
        self.history(market, st, st["rang"], target, source, f"hausse proposée vers {name(target)} : {reason}",
                     soiree=soiree)
        log.info("Labo : grade %s, hausse proposée %s → %s (%s)", market, name(st["rang"]), name(target), reason)

    def raise_to(self, market: str, st: dict, target: int, source: str, reason: str, measured, metrics,
                 soiree: str | None = None) -> None:
        """Hausse : immédiate dans la même lettre (le risque ne change pas), sinon proposée."""
        if letter_of(target) == letter_of(st["rang"]):
            self.set_rank(market, st, target, source, reason, measured, metrics, soiree)
        else:
            self.propose(market, st, target, source, reason, soiree)

    def filters(self, version: str | None, market: str) -> dict:
        """Ce qui fait la population mesurée sur un marché : version de génération et empreinte des filtres
        EFFECTIFS du marché (règles de `version` qui le touchent)."""
        from app.lab import method as M

        conn = self.db.conn
        v = next((x for x in M.versions(conn) if x["version"] == version), None)
        champ = M.champion(conn)
        if v is None and version == champ["version"]:
            v = champ  # version de génération sans ligne (base d'avant le Labo 3)
        v = v or {"version": version, "base": None, "regles": []}
        return {"generation": M.base_of(v, conn), "filtres": M.filters_key(v["regles"], market)}

    def frontier(self, champ: dict, market: str, base_id: int | None) -> dict | None:
        """Frontière du marché (hors échantillon) quand des règles du champion le touchent : ce qui était journalisé
        à sa promotion (method._promote, « vu_a_la_promotion »), sinon au trade `base_id` (remise à zéro, ou grade
        d'avant ce suivi) ; None sans règle sur ce marché (aucune borne)."""
        from app.lab import lessons as L
        from app.lab import method as M

        if not M.market_rules(champ["regles"], market):
            return None
        seen = M.version_settings(self.db.conn, champ["version"]).get("vu_a_la_promotion")
        if not isinstance(seen, dict):
            seen = L.seen_until(self.db.conn, base_id)
        return {k: d for k, d in seen.items() if k.endswith(f":{market}")}

    # -- passage --
    def grade_market(self, market: str, force: bool = False) -> dict:
        conn = self.db.conn
        st = self.state(market)
        from app.lab import method as M

        champ = M.champion(conn)
        champ_version = champ["version"]
        key = self.filters(champ_version, market)
        old = ({"generation": st.get("generation"), "filtres": st["filtres"]} if "filtres" in st
               else self.filters(st.get("version"), market))  # grade d'avant ce suivi : sa version le dit
        if st.get("version") is None or old != key:
            # Les trades gardés sur CE marché changent (nouvelle génération, ou filtres du marché) : remise à zéro.
            # Une promotion qui ne touche pas ce marché n'y change rien (avant : toute promotion remettait tout à zéro).
            if st.get("version") is not None:
                self.history(market, st, None, None, "nouvelle_version",
                             f"méthode {champ_version} en place : grade remis à zéro (hors échantillon)")
            base = max_trade_id(conn)
            st = {"rang": None, "version": champ_version, "depuis_id": base, "base_id": base,
                  "change_le": None, "hausse": None, "morte": False, "seq": st.get("seq", 0),
                  "frontiere": self.frontier(champ, market, base), "frontiere_version": champ_version}
        elif st.get("version") != champ_version:
            st["version"] = champ_version
            self.history(market, st, st["rang"], None, "nouvelle_version",
                         f"méthode {champ_version} en place, mêmes filtres sur les {MARKET_FR[market]} : grade gardé")
        if "frontiere" not in st:  # grade d'avant ce suivi : borne recalculée une fois (base_id de sa remise à zéro)
            st |= {"frontiere": self.frontier(champ, market, st.get("base_id")), "frontiere_version": champ_version}
        st |= key
        rows, fen = window(conn, market, st["frontiere"])
        if st["frontiere"] is not None:
            fen["apres"] = st.get("frontiere_version")
        m = evaluate(rows) | {"fenetre": fen}
        st["mesure"] = m
        measured = m.get("rang")
        cur = st["rang"]
        disc = discontinuous(m)
        if measured is not None:
            if cur is None:
                if measured <= D_PLUS:
                    self.set_rank(market, st, measured, "calcul", "premier grade", measured, m)
                elif disc:  # pas de hausse proposée sans preuve récente
                    self.set_rank(market, st, D_PLUS, "calcul", f"premier grade, mesuré {name(measured)} sur une "
                                                                 "fenêtre discontinue", measured, m)
                else:
                    self.set_rank(market, st, D_PLUS, "calcul", f"premier grade, mesuré {name(measured)}", measured, m)
                    self.propose(market, st, measured, "calcul", f"mesuré {name(measured)} dès le premier calcul")
            else:
                new = since(conn, market, st["depuis_id"], st["frontiere"])
                st["nouveaux"] = new
                if cur >= C_MINUS and new["baisse_r"] >= COUPE_CIRCUIT_R:
                    self.set_rank(market, st, min(D_RANK, measured), "coupe_circuit",
                                  f"baisse de {new['baisse_r']:g} R depuis le sommet", measured, m)
                elif measured < cur:
                    self.set_rank(market, st, measured, "calcul",
                                  f"espérance {m['esperance_r']:+.3f} R sur {m['trades']} trades", measured, m)
                elif measured > cur and not disc and new["trades"] >= HAUSSE_TRADES and new["jours"] >= HAUSSE_JOURS:
                    self.raise_to(market, st, measured, "calcul",
                                  f"espérance {m['esperance_r']:+.3f} R sur {m['trades']} trades, dont "
                                  f"{new['trades']} nouveaux sur {new['jours']} jours", measured, m)
        ci = m.get("ic95")
        dead = m["trades"] >= MORT_TRADES and ci is not None and ci[1] < 0
        if dead and not st.get("morte"):
            self.event(st, f"☠️ Méthode {champ_version} déclarée morte sur les {MARKET_FR[market]} : "
                           f"{m['esperance_r']:+.3f} R par trade sur {m['trades']} trades, IC95 [{ci[0]:+.3f} ; "
                           f"{ci[1]:+.3f}] entièrement sous zéro. Mode survie : il faut un challenger.",
                       {"marche": market, "ic95": ci})
        st["morte"] = dead
        self.save(market, st)
        return st

    def apply_avis(self, avis: dict) -> str:
        market = avis["marche"]
        st = self.state(market)
        if st["rang"] is None:
            return "ignoré (marché non noté)"
        done = self.db.conn.execute("SELECT 1 FROM grades WHERE market = ? AND source = 'council' AND soiree = ?",
                                    (market, avis["soiree"])).fetchone()
        if done:
            return "ignoré (déjà un avis ce soir)"
        adj, cur = avis["ajustement"], st["rang"]
        reason = avis["motifs"][:300]
        metrics = {"voix": avis.get("voix")} | (st.get("mesure") or {})
        target = max(0, min(20, cur + adj))
        if adj < 0 and target < cur:
            self.set_rank(market, st, target, "council", reason, (st.get("mesure") or {}).get("rang"), metrics,
                          avis["soiree"])
        elif adj > 0 and target > cur:
            self.raise_to(market, st, target, "council", reason, (st.get("mesure") or {}).get("rang"), metrics,
                          avis["soiree"])
        else:
            self.history(market, st, cur, (st.get("mesure") or {}).get("rang"), "council", f"grade confirmé : {reason}",
                         {"voix": avis.get("voix")}, avis["soiree"])
        self.save(market, st)
        return "appliqué"

    def apply_decision(self, data: dict) -> str:
        market = data["hausse"].split("-")[0]
        st = self.state(market)
        h = st.get("hausse")
        if not h or h["id"] != data["hausse"]:
            return "ignorée (hausse dépassée)"
        if data["decision"] == "approuvee":
            measured = (st.get("mesure") or {}).get("rang")
            target = h["vers"]
            if h["source"] == "calcul" and measured is not None:
                target = min(target, measured)  # la mesure a pu baisser depuis la proposition
            if target <= st["rang"]:
                st["hausse"] = None
                self.save(market, st)
                return "ignorée (la mesure a baissé depuis)"
            self.set_rank(market, st, target, "simao", f"hausse approuvée ({h['raison'][:200]})", measured,
                          st.get("mesure"))
        else:
            st["hausse"] = None
            st["depuis_id"] = max_trade_id(self.db.conn)  # pas reproposée sans nouvelles preuves
            self.history(market, st, st["rang"], h["vers"], "simao", f"hausse vers {name(h['vers'])} refusée")
        self.save(market, st)
        return data["decision"]

    def ingest(self) -> int:
        q = queue_dir(self.db.path)
        if not q.exists():
            return 0
        n = 0
        for f in sorted(q.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                with self.db.transaction():
                    if f.name.startswith("avis-"):
                        avis = parse_avis({k: data.get(k) for k in ("marche", "ajustement", "motifs", "voix")})
                        out = self.apply_avis(avis | {"soiree": data.get("soiree")})
                    else:
                        out = self.apply_decision(data)
                log.info("Labo : grade, %s : %s", f.name, out)
                n += 1
            except (OSError, ValueError, KeyError, TypeError) as exc:
                log.warning("Labo : grade, fichier %s illisible, écarté (%s)", f.name, exc)
            f.unlink(missing_ok=True)
        return n


def signature(db: LabDB) -> list:
    from app.lab import method as M
    from app.lab import empreinte as E  # I3 : trades réécrits sur place, code des stats
    from app.trading import ibkr as IB

    row = db.conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM trades").fetchone()
    return [row[0], row[1], M.champion(db.conn)["version"], GRADE_CALC, ibkr_plan(),
            "1 bracket" if IB.single_exit() else "3 brackets", *E.sig(db.conn, "trades")]


def step(db: LabDB, now: datetime | None = None) -> dict:
    """Passage du worker : file (avis, décisions), grade de chaque marché si les trades ont changé,
    fichier lab_grade.json pour le direct."""
    now = now or datetime.now(timezone.utc)
    g = Grader(db, now)
    out = {"file": g.ingest()}
    sig = signature(db)
    if sig != db.get("grade_calc", "signature") or out["file"]:
        for market in MARKETS:
            with db.transaction():
                st = g.grade_market(market)
            out[market] = name(st["rang"])
        db.set("grade_calc", "signature", sig)
    out["fichier"] = write_live(db, now)
    return out


# --- Lecture (serveur, routine, cockpit) ---------------------------------------------------------

def payload(conn) -> dict:
    """Ce que le direct lit : grade officiel, mesure, risque du grade, hausse en attente, par marché."""
    markets = {}
    for market in MARKETS:
        row = conn.execute("SELECT value FROM progress WHERE job = 'grade' AND key = ?", (market,)).fetchone()
        st = json.loads(row[0]) if row else None
        if not st:
            continue
        m = st.get("mesure") or {}
        non_note = m.get("rang") is None
        lt = letter_of(st.get("rang"))
        markets[market] = {
            "grade": name(st.get("rang")), "rang": st.get("rang"), "version": st.get("version"),
            "non_note": non_note, "mesure": m, "change_le": st.get("change_le"), "discontinue": discontinuous(m),
            "risque_grade": 0.0 if non_note or discontinuous(m) else grade_risk(st.get("rang"), m.get("kelly_pct")),
            "risque_max": RISQUE_MAX[lt] if lt else 0.0,
            "mode_survie": non_note is False and (lt in ("E", "F") or bool(st.get("morte"))),
            "morte": bool(st.get("morte")), "hausse": st.get("hausse"), "nouveaux": st.get("nouveaux")}
    return {"marches": markets}


def history(conn, market: str | None = None, limit: int = 20) -> list[dict]:
    try:
        sql = "SELECT created_at, market, version, rank, measured, source, reason, soiree FROM grades"
        args: list = []
        if market:
            sql += " WHERE market = ?"
            args.append(market)
        rows = conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"le": c, "marche": m, "version": v, "grade": name(r), "mesure": name(me) if me is not None else None,
             "source": SOURCES_FR.get(s, s), "raison": why, "soiree": so} for c, m, v, r, me, s, why, so in rows]


def live_path(db_file: str | None = None) -> Path:
    override = os.getenv("JARVIS_LAB_GRADE", "").strip()
    return Path(override) if override else Path(db_file or db_path()).parent / GRADE_FILE


def write_live(db: LabDB, now: datetime | None = None) -> bool:
    """lab_grade.json ; `verifie_le` à chaque passage réussi du grade, même sans changement (`ecrit_le` gardé) :
    le direct sait ainsi que le labo vérifie encore son grade (sinon : périmé, risque 0 %). Vrai si le grade a
    changé."""
    if db.path == ":memory:":
        return False
    data = payload(db.conn)
    path = live_path(db.path)
    checked = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
        if old.get("marches") == data["marches"]:
            _write_atomic(path, old | {"verifie_le": checked})
            return False
    except (OSError, ValueError, AttributeError):
        pass
    _write_atomic(path, data | {"ecrit_le": now_iso(), "verifie_le": checked})
    return True


_cache: dict = {"key": None, "data": None}
_warned: set[str] = set()


def current() -> dict | None:
    """lab_grade.json pour le direct ; None si absent ou illisible (risque 0 % : pas de preuve)."""
    try:
        path = live_path()
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
        if _cache["key"] != key:
            _cache.update(key=key, data=json.loads(path.read_text(encoding="utf-8")))
            _warned.discard("lecture")
        return _cache["data"]
    except FileNotFoundError:
        return None
    except Exception as exc:
        if "lecture" not in _warned:
            _warned.add("lecture")
            log.warning("Grade du labo illisible (%s: %s) : risque proposé 0 %%", type(exc).__name__, exc)
    return None


def market_grade(market: str, data: dict | None = None) -> dict | None:
    data = current() if data is None else data
    return ((data or {}).get("marches") or {}).get(market)


def taken_pct(t: dict, capital: float | None = None) -> float:
    """Risque pris sur un trade suivi (% du capital, en euros) : ses euros de risque (`risk_amount_eur` de sa taille,
    ou de la règle de 20 € quand JARVIS n'en proposait pas) rapportés au capital s'il est connu, sinon son % ; à
    défaut RISQUE_INCONNU (1 %) : jamais 0 pour un trade réellement pris."""
    for s in (t.get("sizing"), t.get("sizing_regle")):
        if not isinstance(s, dict):
            continue
        cap = s.get("capital") or capital
        if s.get("risk_amount_eur") and cap:
            return s["risk_amount_eur"] / cap * 100
        if s.get("risk_pct"):
            return s["risk_pct"]
    return RISQUE_INCONNU


def open_risk(trades: list[dict]) -> float:
    """Risque encore en jeu sur les trades ouverts (% du capital) : risque pris (taken_pct) × part de la position
    restante (toute, jusqu'à son objectif, pour une sortie unique) × part du stop encore sous l'entrée (0 une fois le
    stop remonté à l'entrée). Un trade mal formé
    compte pour tout son risque (jamais d'exception ici : elle ferait sauter le budget)."""
    total = 0.0
    capital = env_float("JARVIS_CAPITAL", 0.0) or None
    for t in trades:
        if not isinstance(t, dict) or t.get("status") != "open":
            continue
        try:
            pct = taken_pct(t, capital)
        except (TypeError, ValueError, ZeroDivisionError):
            pct = RISQUE_INCONNU
        try:
            risk = abs(t["entry"] - t["stop"])
            sign = 1 if t["side"] == "long" else -1
            left = max(0.0, sign * (t["entry"] - t.get("stop_current", t["stop"]))) / risk
            legs = len(t.get("targets") or []) or 1
            hit, single = t.get("targets_hit", 0), t.get("sortie_unique")
            # Sortie unique : toute la position reste en jeu jusqu'à son objectif.
            kept = (legs - min(hit, legs)) / legs if single is None else 0.0 if hit > single else 1.0
            total += pct * left * kept
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
            log.warning("Grade : trade suivi %s mal formé (%s: %s), compté pour %g %% de risque ouvert",
                        t.get("id"), type(exc).__name__, exc, pct)
            total += pct
    return round(total, 3)


def day_r(trades: list[dict], now: datetime) -> float:
    """R réalisé aujourd'hui (date de Paris de la clôture) sur les trades pris, net des frais estimés
    (service.realized_r_net : stop exécuté au prix du gap, commissions IBKR à la taille du trade)."""
    from app.trading import service as T

    today = now.astimezone(PARIS).date()
    total = 0.0
    for t in trades:
        r = T.realized_r_net(t)
        if r is None:
            continue
        ends = [e["at"] for e in t.get("events", []) if e["kind"] in ("stop", "close", "tp3", "tp2", "tp1")]
        if ends and datetime.fromisoformat(ends[-1]).astimezone(PARIS).date() == today:
            total += r
    return round(total, 2)


def age_h(data, now: datetime) -> float | None:
    """Heures depuis le dernier passage réussi du grade au labo (`verifie_le`) ; None s'il manque ou est illisible."""
    try:
        at = datetime.fromisoformat(data["verifie_le"])
    except (KeyError, TypeError, ValueError):
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return max(0.0, (now - at).total_seconds() / 3600)


def _day(d: str | None) -> str:
    return f"{d[8:10]}/{d[5:7]}/{d[:4]}" if d else "?"


def window_text(f: dict | None) -> str | None:
    """« fenêtre : rejeu du … au …, direct depuis le … » ; None sans fenêtre publiée."""
    after = (f" · seuls comptent les jours journalisés après la mise en place de la méthode {f['apres']} "
             "(hors échantillon)") if f and f.get("apres") else ""
    if not f or not f.get("jours"):
        return f"fenêtre : aucun jour encore{after}" if after else None
    parts = []
    if f.get("rejeu_du"):
        parts.append(f"rejeu du {_day(f['rejeu_du'])} au {_day(f['rejeu_au'])}")
    if f.get("direct_du"):
        parts.append(f"direct depuis le {_day(f['direct_du'])}")
    out = f"fenêtre : {', '.join(parts)} ({f['jours']} jours, dont {f['jours_direct']} en direct"
    out += f", plus grand trou {f['trou_max_jours']} jours)" if f["trou_max_jours"] > TROU_JOURS else ")"
    if f.get("coupee_au"):
        out += f" · grade calculé sur les jours depuis le {_day(f['coupee_au'])} seulement (après le trou)"
    elif f.get("discontinue"):
        out += (f" · ⚠️ fenêtre discontinue : risque 0 % tant que les jours après le trou n'ont pas {MIN_TRADES} "
                f"trades sur {MIN_JOURS} jours")
    return out + after


def unreadable(exc: Exception) -> dict:
    """Grade illisible (exception) : 0 %, jamais le risque par défaut (fail-closed)."""
    return {"grade": "non noté", "risque_grade": 0.0, "risque_propose": 0.0,
            "raison": f"grade illisible ({type(exc).__name__})", "observation": True}


def risk_now(market: str, trades: list[dict], now: datetime | None = None, data: dict | None = None) -> dict:
    """Risque proposé pour un nouveau plan : grade (s'il est frais), puis budget du portefeuille et arrêt de la
    journée."""
    now = now or datetime.now(timezone.utc)
    data = current() if data is None else data
    g = market_grade(market, data)
    if not g:
        return {"grade": "non noté", "risque_grade": 0.0, "risque_propose": 0.0, "raison": "grade pas encore calculé",
                "observation": True}
    base = g["risque_grade"]
    out = {"grade": g["grade"], "risque_grade": base, "risque_max": g["risque_max"], "mode_survie": g["mode_survie"],
           "non_note": g["non_note"], "delais_tenus": (g.get("mesure") or {}).get("delais_tenus")}
    age = age_h(data, now)
    if age is None or age > MAX_AGE_H:
        return out | {"risque_propose": 0.0, "observation": True, "perime": True,
                      "raison": "grade périmé (" + ("aucun passage du labo daté" if age is None else
                                                    f"dernier passage du labo il y a {age:.0f} h") + ")"}
    if base <= 0:
        f = (g.get("mesure") or {}).get("fenetre") or {}
        why = ("échantillon trop petit" if g["non_note"] else
               f"fenêtre discontinue (rejeu jusqu'au {_day(f.get('rejeu_au'))}, direct depuis le "
               f"{_day(f.get('direct_du'))}) : pas de risque réel sans preuve récente" if g.get("discontinue") else
               f"grade {g['grade']} : pas de capital réel tant que la méthode n'est pas au moins D")
        return out | {"risque_propose": 0.0, "raison": why, "observation": True}
    opened = open_risk(trades)
    today = day_r(trades, now)
    out |= {"risque_ouvert": opened, "r_du_jour": today}
    if today <= ARRET_JOUR_R:
        return out | {"risque_propose": 0.0, "raison": f"arrêt de la journée ({today:+g} R sur les trades pris)",
                      "observation": True}
    room = max(0.0, RISQUE_OUVERT_MAX - opened)
    proposed = round(min(base, room), 2)
    why = f"grade {g['grade']}"
    if proposed < base:
        why += (f", plafonné par le budget de {RISQUE_OUVERT_MAX:g} % ouvert au total"
                + (f" ({opened:g} % déjà ouverts)" if opened else ""))
    return out | {"risque_propose": proposed, "raison": why, "observation": proposed <= 0}


def _eur(x: float) -> str:
    return f"{round(x, 2):g}".replace(".", ",") + " €"


def plan_line(g: dict) -> str:
    """Une ligne pour le plan sur Discord (et l'avertissement d'un risque demandé au-delà du budget)."""
    head = f"🏅 Grade JARVIS **{g['grade']}** · risque proposé **{g['risque_propose']:g} %**"
    line = head + (f" : plan {g['raison']}" if g.get("ecarte") else  # écarté par la méthode : la raison suffit
                   f" : plan en observation ({g['raison']})" if g.get("observation") else f" ({g['raison']})")
    if g.get("hors_budget"):
        line += (f"\n⚠️ **Hors budget** : risque demandé {g['risque_demande']:g} %"
                 + (f" ({_eur(g['risque_demande_eur'])})" if g.get("risque_demande_eur") else "")
                 + (f", au-delà des {_eur(g['propose_eur'])} que propose JARVIS" if g.get("propose_eur") else
                    f", alors que JARVIS ne propose aucun risque ({g['risque_propose']:g} %)")
                 + " : ton choix, gardé tel quel")
    return line


def delay_line(g: dict) -> str | None:
    """Audits de GPT et Gemini (25/10/2026) : la calibration mesurée des délais, à côté des délais du plan. Part des
    objectifs touchés au plus tard au délai médian prévu (fenêtre du grade), contre la cible DELAIS ; None sans mesure."""
    d = g.get("delais_tenus")
    if d is None:
        return None
    target = f"cible {DELAIS[0] * 100:.0f}-{DELAIS[1] * 100:.0f} %"
    if DELAIS[0] <= d <= DELAIS[1]:
        return f"-# Délais calibrés au labo : {d * 100:.0f} % des objectifs touchés au plus tard au délai médian prévu ({target})."
    late = "arrivent plus tard que prévu" if d < DELAIS[0] else "arrivent plus tôt que prévu"
    return (f"-# ⚠️ Délais non calibrés au labo : {d * 100:.0f} % des objectifs touchés au plus tard au délai médian prévu "
            f"({target}) ; les objectifs {late}. Un ordre de grandeur, pas une échéance.")


def _pct(x) -> str:
    return "n/d" if x is None else f"{x:.0%}"


def practical(risk: float) -> str:
    """« 5 % permis, 4 % en pratique (budget total) » quand le risque du grade dépasse le budget ouvert total."""
    if risk > RISQUE_OUVERT_MAX:
        return f"{risk:g} % permis, {RISQUE_OUVERT_MAX:g} % en pratique (budget total)"
    return f"{risk:g} %"


def allowed(risk: float) -> str:
    """« risque permis 3 % », ou « risque 5 % permis, 4 % en pratique (budget total) » (sans « permis » en double)."""
    return f"risque {practical(risk)}" if risk > RISQUE_OUVERT_MAX else f"risque permis {practical(risk)}"


def text(data: dict | None, hist: list[dict] | None = None, now: datetime | None = None) -> str:
    """`!grade` : grade de chaque marché, de quoi il est fait, et ce qu'il change."""
    marches = (data or {}).get("marches") or {}
    if not marches:
        return "🏅 Grade JARVIS pas encore calculé (le worker du labo le calcule à son prochain passage)."
    from app.trading import killmode as K  # import tardif : la mise de Simão (JARVIS_RISK_EUR)

    lines = ["🏅 **Grade JARVIS** (F- à S+ : la preuve que la méthode gagne, mesurée sans jeton)"]
    age = age_h(data, now or datetime.now(timezone.utc))
    if age is None or age > MAX_AGE_H:
        lines.append("⚠️ **Grade périmé** : " + ("aucun passage du labo daté" if age is None else
                                                 f"dernier passage du labo il y a {age:.0f} h")
                     + f" (au-delà de {MAX_AGE_H:g} h, JARVIS ne propose plus aucun risque : 0 %)")
    for market in MARKETS:
        g = marches.get(market)
        if not g:
            continue
        m = g["mesure"] or {}
        lines.append(f"\n**{MARKET_FR[market].capitalize()} : {g['grade']}** · méthode {g['version']} · "
                     f"{allowed(g['risque_grade'])} (plafond {g['risque_max']:g} %)")
        window = window_text(m.get("fenetre"))
        if g["non_note"]:
            lines.append(f"-# {m.get('raison', 'échantillon trop petit')} : {m.get('trades', 0)} trades sur "
                         f"{m.get('jours', 0)} jours")
            if window:
                lines.append(f"-# {window}")
        else:
            ci = m.get("ic95") or [None, None]
            crit = m.get("criteres") or {}
            ok = {True: "✅", False: "❌"}
            cost = f" après commission IBKR à {K.RISK_EUR:g} €" if market == "stock" else " (frais OKX compris)"
            lines.append(f"Mesuré **{m['grade']}** : espérance {m['esperance_r']:+.3f} R par trade{cost} (IC95 "
                         f"{ci[0]:+.3f} ; {ci[1]:+.3f}) sur {m['trades']} trades et {m['jours']} jours")
            if window:
                lines.append(f"-# {window}")
            lines.append(f"{ok[crit.get('series_normales', False)]} séries de pertes ({m['serie_pertes']} d'affilée, "
                         f"~{m['serie_attendue']} attendues) · {ok[crit.get('regulier', False)]} régularité "
                         f"({' / '.join(f'{h:+.2f}' for h in m['moities_r'] if h is not None)} R) · "
                         f"{ok[crit.get('delais_calibres', False)]} délais tenus {_pct(m['delais_tenus'])}")
            lines.append(f"-# Réussite {_pct(m['reussite'])} · TP1 {_pct(m['tp1'])} · TP2 {_pct(m['tp2'])} · "
                         f"TP3 {_pct(m['tp3'])} · baisse max {m['baisse_max_r']:g} R")
            if m.get("rho_jour"):
                lines.append(f"-# ¼ de Kelly {m['kelly_brut_pct']:g} % ramené à {m['kelly_pct']:g} % : trades d'un même "
                             f"jour corrélés (ρ {m['rho_jour']:g}) et jusqu'à {m['positions_max']} positions ouvertes "
                             "ensemble sous le budget")
        if g.get("morte"):
            lines.append("☠️ Méthode déclarée morte (IC95 entièrement sous zéro) : il faut un challenger.")
        elif g.get("mode_survie"):
            lines.append("🆘 Mode survie : la routine du soir travaille à remonter le grade.")
        if g.get("hausse"):
            h = g["hausse"]
            lines.append(f"⏳ Hausse vers {name(h['vers'])} en attente de ton accord ({h['raison']})")
    if hist:
        lines.append("\n**Derniers changements**")
        for h in hist[:5]:
            lines.append(f"-# {h['le'][:16].replace('T', ' ')} · {MARKET_FR.get(h['marche'], h['marche'])} · "
                         f"{h['grade']} · {h['source']} : {h['raison']}")
    return "\n".join(lines)


def reels(trades: list[dict]) -> dict:
    """Troisième population (avis de GPT, 10 oct.) : trades réellement pris et suivis par JARVIS (`!prends`),
    terminés, hors Killmode (mesuré par son propre journal), en R réalisé avant frais et net des frais estimés
    (service.realized_r_net ; gagnants comptés nets). Indicatif : aucun effet. `regle` : ceux pris à ta taille quand
    JARVIS n'en proposait pas autant (service.at_rule, le pari « 20 € quand JARVIS propose 0 % »), et leur R net moyen."""
    from app.trading import service as T

    out = {}
    for market in MARKETS:
        mine = [t for t in trades if t.get("market", "stock") == market and not t.get("killmode")]
        done = [(T.realized_r(t), T.realized_r_net(t), T.at_rule(t)) for t in mine if T.realized_r(t) is not None]
        rs, nets = [r for r, _, _ in done], [n for _, n, _ in done]
        rule = [n for _, n, at in done if at]
        out[market] = {"termines": len(rs), "esperance_r": round(sum(rs) / len(rs), 3) if rs else None,
                       "total_r": round(sum(rs), 2), "gagnants": sum(n > 0 for n in nets),
                       "esperance_net_r": round(sum(nets) / len(nets), 3) if nets else None,
                       "total_net_r": round(sum(nets), 2), "regle": len(rule),
                       "regle_net_r": round(sum(rule) / len(rule), 3) if rule else None}
    return out


def populations_text(indicatif: list[str], real: dict | None) -> str:
    """Lignes ajoutées à `!grade` : GO confirmés (labo) et trades réels, à côté du grade officiel de v1, chacun
    avec la politique qu'il mesure (entrée, break-even, sortie au temps, taille)."""
    from app.trading import service as T

    lines = []
    if indicatif:
        lines.append("\n**GO confirmés, stop fixe, vendus à la clôture** (≠ ton suivi : stop à l'entrée après TP2, "
                     "pas de sortie au temps) · entrée par l'ordre stop de la confirmation, en R du labo (ses coûts, "
                     "pas ta taille) ; indicatif : ne change ni le grade ni le risque")
        lines += indicatif
    real = [(m, g) for m, g in (real or {}).items() if g["termines"]]
    if real:
        lines.append("\n**Tes trades réels** (R nets de frais estimés, bruts entre parenthèses)")
        lines.append(f"-# Politique mesurée : {T.suivi_politique()}.")
    for market, g in real:
        lines.append(f"-# {MARKET_FR[market].capitalize()} : {g['termines']} terminés, {g['gagnants']} gagnants, "
                     f"{g['esperance_net_r']:+.2f} R en moyenne ({g['esperance_r']:+.2f} brut), "
                     f"{g['total_net_r']:+.2f} R au total ({g['total_r']:+.2f} brut)"
                     + (f" · dont {g['regle']} à ta règle de taille ({g['regle_net_r']:+.2f} R nets), "
                        "détail dans `!desaccords`" if g.get("regle") else "")
                     + (" · trop peu pour conclure" if g["termines"] < 30 else ""))
    return "\n".join(lines)


def sync_proposals(proposals, data: dict | None = None) -> list[dict]:
    """Serveur : une proposition par hausse de lettre en attente ; les hausses dépassées sont remplacées.
    Renvoie les nouvelles propositions (à envoyer en privé)."""
    data = current() if data is None else data
    if data is None:
        return []
    new, live = [], set()
    for market, g in (data.get("marches") or {}).items():
        h = g.get("hausse")
        if not h:
            continue
        live.add(h["id"])
        p = proposals.propose_grade(h, MARKET_FR.get(market, market), name(h.get("de")), name(h["vers"]))
        if p:
            new.append(p)
    proposals.retire_grade(live)
    return new


def brief(conn) -> dict:
    """Pour la routine du soir (GET /api/lab/brief) : de quoi tenir le council du grade."""
    data = payload(conn)
    out = {}
    for market, g in data["marches"].items():
        m = g["mesure"] or {}
        out[market] = {k: g[k] for k in ("grade", "non_note", "risque_grade", "mode_survie", "morte", "hausse")} | {
            "mesure": {k: m.get(k) for k in ("grade", "trades", "jours", "esperance_r", "ic95", "criteres",
                                             "serie_pertes", "serie_attendue", "moities_r", "delais_tenus",
                                             "reussite", "tp1", "baisse_max_r", "raison")}}
    return {"marches": out, "historique": history(conn, limit=6),
            "avis": "POST /api/lab/grade/avis {marche, ajustement: -1|0|1, motifs, voix} : un avis par marché et par soir"}
