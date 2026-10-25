"""Challenger « méthode Gemini » : forward-test en direct des plans que Gemini donnait à Simão (25 octobre 2026).

Simão tient la méthode pour gagnante (plans PACS, CHRN et TRAW du printemps 2026) ; aucun résultat ne l'accompagne,
donc elle est mesurée, pas crue. Spécification : /mnt/project-files/labo/methode-challenger-gemini-2026-10-25.md.
Pari noté : Simão parie qu'elle bat son témoin après frais ; lecture à READ_AT plans déclenchés. Seul Simão décide
d'une déclaration au labo ou d'une promotion : ce module mesure, il ne change rien d'autre dans JARVIS.

Trois passages par séance (heure de New York), sur les plus fortes hausses et les plus actives du screener d'Alpaca
(plus, pour C, les sociétés qui ont publié leurs résultats ces 5 dernières séances), bougies de 5 min IEX en temps réel :
- **A, cassure des 5 premières minutes (CHRN)**, 9 h 36 : gap d'ouverture de +5 à +25 %. Ordre stop d'achat 1 pas
  au-dessus du plus haut des 5 premières minutes, pris seulement si la minute de cassure clôture au-dessus avec au moins
  1,5 fois le volume moyen par minute des 5 premières ; stop 1 pas sous leur plus bas ou sous le VWAP, au plus proche ;
  TP1 +10 % (moitié, stop à l'entrée), TP2 +20 % ; rien à 10 h 30 = annulé ; clôture forcée à 15 h 45.
- **B, drapeau de mi-séance (TRAW)**, 12 h 36 : en hausse d'au moins 5 % sur la journée, au-dessus du VWAP et du plus
  haut de la première heure, consolidation de 30 min sous le pic du matin (amplitude au plus 40 % de la montée du
  matin). Ordre stop 1 pas au-dessus du drapeau jusqu'à 15 h ; stop 1 pas sous le drapeau ou sous le VWAP, au plus
  proche ; TP1 le pic du matin (moitié, stop à l'entrée), TP2 +10 % ; sortie à 15 h 59.
- **C, reprise du VWAP en fin de séance (PACS)**, 15 h 31 : résultats battus publiés il y a 1 à 5 séances ; la bougie
  de 15 min qui clôture à 15 h 30 ferme au-dessus du VWAP après une bougie de 15 min close dessous. Achat au marché ;
  stop 1 pas sous le plus bas de la séance ; TP1 le plus haut de la séance, TP2 le plus haut des 20 dernières séances
  (à défaut 1 et 2 R) ; sortie à 15 h 59.
Tri commun : volume relatif (cumul du jour à cette heure contre la moyenne des 20 séances précédentes à la même heure,
IEX des deux côtés) au-dessus de 4 ; moins de 30 M d'actions en circulation (Finnhub ne donne pas le flottant, qui est
plus petit : le filtre est donc plus strict que la règle) ; catalyseur frais (le pilier de Killmode : actu positive de
moins de 48 h ou résultats battus, ni augmentation de capital ni résultats dans 24 h) ; pas plus de +15 % sur la
dernière demi-heure. Écarts à la spécification, pour C seulement, et dits : volume relatif et nombre d'actions notés
sans filtrer (le plan PACS n'a pas de volume relatif et compte 42 M d'actions), le catalyseur de C étant les résultats.
Les jours de clôture anticipée sont sautés (les heures des plans n'y existent pas).

Mesure : chaque plan est soldé après la clôture sur les bougies SIP d'1 min d'Alpaca, en deux versions, sortie le jour
même (la règle des plans) et sortie à la clôture du lendemain (« parfois ça prenait un peu plus de 24 h »). Une minute
qui touche le stop et un objectif compte comme stop. Le chiffre qui décide est le R aux frais de Simão : 20 € de risque
(JARVIS_RISK_EUR) en actions entières, un bracket IBKR par objectif au barème du compte (JARVIS_IBKR_PLAN, Dégressif par
défaut ici), glissement du labo (au moins un demi-pas) sur les ordres stop et au marché. Le témoin : une action tirée
au hasard dans la population du même passage (graine fixe, tirée au moment du plan), achetée au marché à la minute où le
plan s'est déclenché, avec le même stop et les mêmes objectifs en pourcentage et les mêmes sorties. L'écart plan −
témoin se lit avec un intervalle de confiance groupé par jour (stats du labo).
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app import config
from app.lab import collector as CO
from app.lab import simulator as SIM
from app.lab import stats as LS
from app.sources import alpaca, finnhub
from app.trading import calendar as CAL
from app.trading import catalyst as C
from app.trading import killmode as KM
from app.trading.service import _num

log = logging.getLogger(__name__)
NEW_YORK = ZoneInfo("America/New_York")
STATE_PATH = os.getenv("JARVIS_GEMINI_PATH", "data/gemini_challenger.json")
JOURNAL_PATH = os.getenv("JARVIS_GEMINI_SCANS_PATH", "data/gemini_scans.jsonl")
# Version des règles : le bilan ne mélange pas deux versions. À changer à chaque modification d'un seuil ou d'un plan.
VERSION = 1
SEUILS = {"gap_min": 5.0, "gap_max": 25.0, "rvol_min": 4.0, "actions_max": 30e6, "parabole_pct": 15.0,
          "hausse_b_min": 5.0, "drapeau_bougies": 6, "drapeau_part_max": 0.4, "volume_cassure": 1.5,
          "resultats_seances": 5, "tp_a": (10.0, 20.0), "tp_b2": 10.0}
NAMES = {"A": "cassure des 5 premières minutes", "B": "drapeau de mi-séance", "C": "reprise du VWAP en fin de séance"}
PASSES = {"A": time(9, 36), "B": time(12, 36), "C": time(15, 31)}
WINDOW = timedelta(minutes=9)
A_UNTIL, A_CLOSE = time(10, 30), time(15, 45)
B_UNTIL = time(15, 0)
LAST_MINUTE = time(15, 59)      # sortie de B, de C et de la version « lendemain »
SETTLE_AFTER = timedelta(minutes=20)  # Alpaca gratuit : SIP avec 16 min de retard
RVOL_DAYS = 20
RVOL_MIN_DAYS = 5
DEEP_MAX = 30          # valeurs dont on charge les bougies de 5 min
FINNHUB_MAX = 8        # valeurs qui passent au profil et au catalyseur (Finnhub gratuit : 60 appels/min)
CHUNK = 200           # symboles par requête Alpaca
TEMOINS = 3            # témoins tirés par plan (le premier qui a des bougies sert)
READ_AT = 100          # lecture du pari : 100 plans déclenchés
IBKR_PLAN = (os.getenv("JARVIS_IBKR_PLAN") or "degressif").strip().lower()
VARIANTS = {"j0": "sortie le jour même", "j1": "sortie le lendemain"}


# --- État -------------------------------------------------------------------------------------

def load() -> dict:
    try:
        return json.loads(Path(STATE_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(state: dict) -> None:
    p = Path(STATE_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def journal(entry: dict) -> None:
    p = Path(JOURNAL_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _ts(day: date, at: time) -> int:
    return int(datetime.combine(day, at, NEW_YORK).timestamp())


def _tod(ts: int) -> time:
    return datetime.fromtimestamp(ts, NEW_YORK).time()


def _fr(x: float, digits: int = 1) -> str:
    return f"{x:+.{digits}f}".replace(".", ",").replace("-", "−")


# --- Briques pures --------------------------------------------------------------------------------

def tick(price: float) -> float:
    return SIM.tick_size("stock", price)


def vwaps(bars: list[dict]) -> list[float]:
    """VWAP cumulé de la séance à la fin de chaque bougie."""
    out, pv, vol = [], 0.0, 0.0
    for b in bars:
        tp = (b["h"] + b["l"] + b["c"]) / 3
        pv += tp * b["v"]
        vol += b["v"]
        out.append(pv / vol if vol else tp)
    return out


def session(bars: list[dict], day: date) -> list[dict]:
    """Bougies de la séance régulière de `day`."""
    bounds = CO.session_ts(day)
    if not bounds:
        return []
    return [b for b in bars if bounds[0] <= b["t"] < bounds[1]]


def rvol(bars5: list[dict], day: date, until: time) -> float | None:
    """Volume du jour jusqu'à `until` contre la moyenne des RVOL_DAYS séances précédentes jusqu'à la même heure."""
    by_day: dict[date, float] = {}
    for b in alpaca.regular_hours(bars5):
        if _tod(b["t"]) < until:
            d = CO.ny_day(b["t"])
            by_day[d] = by_day.get(d, 0.0) + b["v"]
    today = by_day.pop(day, None)
    past = [by_day[d] for d in sorted(by_day) if d < day][-RVOL_DAYS:]
    if not today or len(past) < RVOL_MIN_DAYS or not sum(past):
        return None
    return today / (sum(past) / len(past))


def parabolic(sess: list[dict]) -> float | None:
    """Hausse des 30 dernières minutes en % (ouverture de la bougie d'il y a 30 min → dernière clôture)."""
    if len(sess) < 6 or sess[-6]["o"] <= 0:
        return None
    return (sess[-1]["c"] / sess[-6]["o"] - 1) * 100


def plan_a(sess: list[dict], prev_close: float) -> tuple[dict | None, str | None]:
    """Plan A sur la première bougie de 5 min de la séance."""
    if not sess or not prev_close:
        return None, "pas de première bougie"
    first = sess[0]
    gap = (first["o"] / prev_close - 1) * 100
    if not SEUILS["gap_min"] <= gap <= SEUILS["gap_max"]:
        return None, f"gap {_fr(gap)} % hors de +5 à +25 %"
    hi, lo = first["h"], first["l"]
    entry = round(hi + tick(hi), 4)
    return {"entry": entry, "stop_range": round(lo - tick(lo), 4), "range_haut": hi, "range_bas": lo,
            "vol_min_minute": round(SEUILS["volume_cassure"] * first["v"] / 5, 1), "gap_pct": round(gap, 2),
            "tp_pct": list(SEUILS["tp_a"])}, None


def plan_b(sess: list[dict], prev_close: float) -> tuple[dict | None, str | None]:
    """Plan B : drapeau des DRAPEAU_BOUGIES dernières bougies de 5 min sous le pic du matin."""
    n = SEUILS["drapeau_bougies"]
    if len(sess) < 12 + n or not prev_close:
        return None, "séance trop courte"
    price = sess[-1]["c"]
    up = (price / prev_close - 1) * 100
    if up < SEUILS["hausse_b_min"]:
        return None, f"{_fr(up)} % sur la journée (au moins +5 %)"
    vw = vwaps(sess)[-1]
    first_hour = max(b["h"] for b in sess[:12])
    if price <= vw:
        return None, "sous le VWAP"
    if price <= first_hour:
        return None, "sous le plus haut de la première heure"
    flag, before = sess[-n:], sess[:-n]
    peak = max(b["h"] for b in before)
    low_day = min(b["l"] for b in before)
    f_hi, f_lo = max(b["h"] for b in flag), min(b["l"] for b in flag)
    if f_hi > peak:
        return None, "pas de pic du matin au-dessus du drapeau"
    if peak <= low_day or (f_hi - f_lo) > SEUILS["drapeau_part_max"] * (peak - low_day):
        return None, "pas de consolidation horizontale"
    entry = round(f_hi + tick(f_hi), 4)
    stop = max(f_lo, vw if vw < entry else f_lo)
    stop = round(stop - tick(stop), 4)
    tp1 = peak if peak > entry else round(entry * 1.05, 4)
    tp2 = round(max(entry * (1 + SEUILS["tp_b2"] / 100), tp1 + tick(tp1)), 4)
    return {"entry": entry, "stop": stop, "tp1": tp1, "tp2": tp2, "drapeau_haut": f_hi, "drapeau_bas": f_lo,
            "pic_matin": peak, "vwap": round(vw, 4), "hausse_pct": round(up, 2)}, None


def plan_c(sess: list[dict], daily_before: list[dict]) -> tuple[dict | None, str | None]:
    """Plan C : la bougie de 15 min qui clôture à 15 h 30 ferme au-dessus du VWAP, la précédente dessous."""
    at = {_tod(b["t"]): i for i, b in enumerate(sess)}
    i_last, i_prev = at.get(time(15, 25)), at.get(time(15, 10))  # dernières bougies de 5 min des deux bougies de 15 min
    if i_last is None or i_prev is None:
        return None, "bougies manquantes avant 15 h 30"
    vw = vwaps(sess)
    close, prev = sess[i_last]["c"], sess[i_prev]["c"]
    if not (close > vw[i_last] and prev <= vw[i_prev]):
        return None, "pas de reprise du VWAP"
    low, high = min(b["l"] for b in sess[:i_last + 1]), max(b["h"] for b in sess[:i_last + 1])
    stop = round(low - tick(low), 4)
    risk = close - stop
    hi20 = max((r["h"] for r in daily_before[-20:]), default=0)
    tp1 = high if high > close else close + risk
    tp2 = hi20 if hi20 > tp1 else max(close + 2 * risk, tp1 + tick(tp1))
    return {"ref": close, "stop": stop, "tp1": round(tp1, 4), "tp2": round(tp2, 4), "vwap": round(vw[i_last], 4),
            "plus_bas": low, "plus_haut": high}, None


def slip(price: float) -> float:
    """Glissement par action d'un ordre stop ou au marché : celui du labo, au moins un demi-pas."""
    return max(SIM.stock_slippage_bps(price) / 10000 * price, tick(price) / 2)


def fill(p: dict, bars: list[dict]) -> dict | None:
    """Déclenchement du plan sur les bougies d'1 min : {t, prix, stop, tp1, tp2} (prix bruts, sans glissement)."""
    day = date.fromisoformat(p["day"])
    start = p["actif_des"]
    if p["kind"] == "A":
        until = _ts(day, A_UNTIL)
        today = session(bars, day)
        vw = vwaps(today)
        for i, b in enumerate(today):
            if b["t"] < start:
                continue
            if b["t"] >= until:
                return None
            if b["h"] >= p["entry"] and b["c"] >= p["entry"] and b["v"] >= p["vol_min_minute"]:
                price = b["c"]
                stop = p["stop_range"]
                v_stop = round(vw[i] - tick(vw[i]), 4)
                if stop < v_stop < price:
                    stop = v_stop
                return {"t": b["t"] + 60, "prix": price, "stop": stop, "tp1": round(price * (1 + p["tp_pct"][0] / 100), 4),
                        "tp2": round(price * (1 + p["tp_pct"][1] / 100), 4)}
        return None
    if p["kind"] == "B":
        until = _ts(day, B_UNTIL)
        for b in bars:
            if b["t"] < start:
                continue
            if b["t"] >= until:
                return None
            if b["h"] >= p["entry"]:
                return {"t": b["t"], "prix": max(p["entry"], b["o"]), "stop": p["stop"], "tp1": p["tp1"],
                        "tp2": p["tp2"]}
        return None
    b = next((b for b in bars if start <= b["t"] < start + 300), None)  # C : au marché, à la minute active
    if not b or b["o"] <= p["stop"]:
        return None
    return {"t": b["t"], "prix": b["o"], "stop": p["stop"], "tp1": p["tp1"], "tp2": p["tp2"]}


def manage(f: dict, bars: list[dict], exit_ts: int) -> dict:
    """Moitié à TP1 (stop remonté à l'entrée), moitié à TP2, stop fixe, reste vendu au marché à `exit_ts`.
    Sorties : [(moitié, prix, raison, au_marché)] ; la minute qui touche le stop et un objectif compte comme stop."""
    stop, legs, out, last = f["stop"], ["tp1", "tp2"], [], None

    def close_all(px: float, why: str) -> None:
        out.extend((k, px, why, True) for k in legs)
        legs.clear()

    for b in bars:
        if b["t"] < f["t"]:  # A : `t` est la minute qui suit la cassure ; B et C : la minute du remplissage
            continue
        if b["t"] >= exit_ts:  # sans échange à l'heure de sortie (titre peu actif) : la dernière clôture d'avant
            close_all(b["o"] if b["t"] < exit_ts + 300 or not last else last["c"], "temps")
            break
        last = b
        if b["o"] <= stop:  # ouverture sous le stop (trou entre deux séances, suspension)
            close_all(b["o"], "stop")
            break
        if b["l"] <= stop:
            close_all(stop, "stop" if stop < f["prix"] else "entree")
            break
        for k in ("tp1", "tp2"):
            if k in legs and b["h"] >= f[k]:
                legs.remove(k)
                out.append((k, max(f[k], b["o"]), k, False))
                if k == "tp1":
                    stop = max(stop, f["prix"])
        if not legs:
            break
    incomplete = bool(legs)
    if legs and last:  # données arrêtées avant la sortie : soldé à la dernière clôture, marqué incomplet
        close_all(last["c"], "fin des données")
    return {"sorties": out, "incomplet": incomplete}


def commission(shares: int, price: float) -> float:
    return KM.order_commission(shares, price, KM.IBKR_PLANS.get(IBKR_PLAN, KM.IBKR_PLANS["degressif"]))


def result(f: dict, m: dict, eurusd: float) -> dict:
    """R brut (sans frais ni glissement) et R aux frais de Simão (20 € de risque, un bracket IBKR par objectif)."""
    risk = f["prix"] - f["stop"]
    if risk <= 0 or not m["sorties"]:
        return {"r_brut": None, "r_frais": None, "hors_mise": False}
    brut = sum(0.5 * (px - f["prix"]) for _, px, _, _ in m["sorties"]) / risk
    entry = f["prix"] + slip(f["prix"])
    risk_paid = entry - f["stop"]
    total = math.floor(KM.RISK_EUR * eurusd / risk_paid + 1e-9) if risk_paid > 0 else 0
    if total < 2:  # une action ne se coupe pas en deux brackets : pas à la taille de Simão
        return {"r_brut": round(brut, 4), "r_frais": None, "hors_mise": True}
    sizes = {"tp1": math.ceil(total / 2), "tp2": total // 2}
    pnl = 0.0
    for leg, px, _, market in m["sorties"]:
        n = sizes[leg]
        exit_px = px - (slip(px) if market else 0.0)
        pnl += n * (exit_px - entry) - commission(n, entry) - commission(n, exit_px)
    return {"r_brut": round(brut, 4), "r_frais": round(pnl / (total * risk_paid), 4), "hors_mise": False,
            "actions": total}


def witness_fill(f: dict, bars: list[dict]) -> dict | None:
    """Le témoin, acheté au marché à la minute où le plan s'est déclenché, même géométrie en pourcentage."""
    b = next((b for b in bars if f["t"] <= b["t"] < f["t"] + 300), None)
    if not b or b["o"] <= 0:
        return None
    px = b["o"]
    k = lambda x: px * x / f["prix"]  # noqa: E731
    return {"t": b["t"], "prix": px, "stop": k(f["stop"]), "tp1": k(f["tp1"]), "tp2": k(f["tp2"])}


def exit_ts(p: dict, variant: str, next_day: date | None) -> int | None:
    day = date.fromisoformat(p["day"])
    if variant == "j1":
        return _ts(next_day, LAST_MINUTE) if next_day else None
    return _ts(day, A_CLOSE if p["kind"] == "A" else LAST_MINUTE)


def settle_plan(p: dict, bars: dict[str, list[dict]], next_day: date, eurusd: float, variants=("j0", "j1")) -> dict:
    """Solde d'un plan et de son témoin dans chaque version. `bars` : bougies d'1 min SIP du jour et du lendemain."""
    f = fill(p, bars.get(p["symbol"]) or [])
    out = {"declenche": bool(f), "eurusd": eurusd}
    if not f:
        return out
    out["entree"] = {"t": f["t"], "prix": f["prix"], "stop": f["stop"], "tp1": f["tp1"], "tp2": f["tp2"]}
    wsym, wf = None, None
    for s in p.get("temoins") or []:
        wf = witness_fill(f, bars.get(s) or [])
        if wf:
            wsym = s
            break
    out["temoin"] = wsym
    for v in variants:
        end = exit_ts(p, v, next_day)
        m = manage(f, bars.get(p["symbol"]) or [], end)
        row = result(f, m, eurusd) | {"sorties": [[leg, px, why] for leg, px, why, _ in m["sorties"]],
                                      "incomplet": m["incomplet"]}
        if wf:
            wm = manage(wf, bars.get(wsym) or [], end)
            wr = result(wf, wm, eurusd)
            row["temoin"] = {"r_brut": wr["r_brut"], "r_frais": wr["r_frais"], "incomplet": wm["incomplet"]}
        out[v] = row
    return out


# --- Bilan ----------------------------------------------------------------------------------------

def plans(state: dict | None = None) -> list[dict]:
    return [p for p in (state or load()).get("plans", []) if p.get("version") == VERSION]


def pairs(book: list[dict], variant: str, key: str = "r_frais") -> list[tuple[str, float, float | None]]:
    """(jour, R du plan, R du témoin) des plans déclenchés et soldés, complets, à la taille de Simão."""
    out = []
    for p in book:
        row = (p.get("solde") or {}).get(variant)
        if not row or row.get("incomplet") or row.get(key) is None:
            continue
        w = row.get("temoin") or {}
        out.append((p["day"], row[key], None if w.get("incomplet") else w.get(key)))
    return out


def _mean(rows: list[tuple[str, float]]) -> dict:
    sums: dict[str, list] = {}
    for d, x in rows:
        s = sums.setdefault(d, [0.0, 0])
        s[0] += x
        s[1] += 1
    m, _, ci = LS.mean_ci(sums, sum(x for _, x in rows), len(rows))
    return {"n": len(rows), "jours": len(sums), "m": m, "ic": ci}


def summary(book: list[dict], variant: str) -> dict:
    rows = pairs(book, variant)
    gaps = [(d, r - w) for d, r, w in rows if w is not None]
    return {"plan": _mean([(d, r) for d, r, _ in rows]),
            "temoin": _mean([(d, w) for d, _, w in rows if w is not None]),
            "ecart": _mean(gaps), "gagnants": sum(r > 0 for _, r, _ in rows),
            "brut": _mean([(p["day"], p["solde"][variant]["r_brut"]) for p in book
                           if ((p.get("solde") or {}).get(variant) or {}).get("r_brut") is not None
                           and not p["solde"][variant].get("incomplet")])}


def verdict(e: dict) -> str:
    if e["n"] < READ_AT:
        return f"lecture à {READ_AT} plans déclenchés ({e['n']} pour l'instant) : descriptif, rien n'est conclu"
    if not e["ic"]:
        return "intervalle incalculable (un seul jour)"
    if e["ic"][0] > 0:
        return "la méthode bat son témoin après frais : Simão peut la déclarer au labo ou la promouvoir"
    if e["ic"][1] < 0:
        return "la méthode fait moins bien que son témoin après frais"
    return "pas d'écart démontré avec le témoin après frais"


def _r(x: float | None) -> str:
    return "—" if x is None else _fr(x, 2)


def _ci(m: dict) -> str:
    return f" [{_r(m['ic'][0])} ; {_r(m['ic'][1])}]" if m.get("ic") else ""


def bilan_text(state: dict | None = None) -> str:
    book = plans(state)
    settled = [p for p in book if p.get("solde")]
    fired = [p for p in settled if p["solde"].get("declenche")]
    by_kind = {k: sum(p["kind"] == k for p in fired) for k in NAMES}
    lines = [f"🧪 **Challenger Gemini · bilan** (règles v{VERSION}) · {len(book)} plans publiés, {len(settled)} soldés, "
             f"{len(fired)} déclenchés (A {by_kind['A']}, B {by_kind['B']}, C {by_kind['C']})"]
    if not fired:
        lines.append("Rien de déclenché pour l'instant.")
    for v, label in VARIANTS.items():
        s = summary(book, v)
        if not s["plan"]["n"]:
            continue
        lines.append(f"__{label.capitalize()}__ · {s['plan']['n']} trades à 20 € sur {s['plan']['jours']} jours · "
                     f"{s['gagnants']} gagnants après frais")
        lines.append(f"plan {_r(s['plan']['m'])} R{_ci(s['plan'])} · témoin {_r(s['temoin']['m'])} R · "
                     f"écart {_r(s['ecart']['m'])} R{_ci(s['ecart'])} (brut, sans frais : {_r(s['brut']['m'])} R)")
        lines.append(f"-# {verdict(s['ecart'])}.")
    lines.append("-# Pari noté le 25/10 : Simão parie que la méthode bat son témoin après frais. Un taux de gagnants "
                 "n'est pas un gain. Seul Simão décide d'une déclaration au labo ou d'une promotion.")
    return "\n".join(lines)


def today_text(state: dict | None = None, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    day = now.astimezone(NEW_YORK).date().isoformat()
    todays = [p for p in plans(state) if p["day"] == day]
    if not todays:
        return "🧪 Challenger Gemini : aucun plan aujourd'hui (passages à 15 h 36, 18 h 36 et 21 h 31 Paris)."
    return "\n".join(plan_text(p) for p in todays)


# --- Direct -----------------------------------------------------------------------------------------

def due(now: datetime, state: dict) -> str | None:
    """Passage à faire maintenant (une fois par séance), sinon None."""
    now_ny = now.astimezone(NEW_YORK)
    bounds = CAL.session_bounds(now_ny.date())
    if not bounds or bounds[1].time() != time(16, 0) or not alpaca.configured():
        return None
    day = now_ny.date().isoformat()
    done = state.get("faits", []) if state.get("day") == day else []
    for kind, at in PASSES.items():
        planned = datetime.combine(now_ny.date(), at, NEW_YORK)
        if timedelta(0) <= now_ny - planned < WINDOW and kind not in done:
            return kind
    return None


def mark_done(state: dict, kind: str, now: datetime) -> None:
    day = now.astimezone(NEW_YORK).date().isoformat()
    if state.get("day") != day:
        state["day"], state["faits"] = day, []
    state.setdefault("faits", []).append(kind)


async def _universe(kind: str, today: date) -> tuple[list[str], dict[str, dict]]:
    calls = [alpaca.most_actives(100), alpaca.top_gainers(50)]
    if kind == "C":
        calls.append(finnhub.earnings_calendar(CO.sessions_back(today, SEUILS["resultats_seances"]), today))
    groups = await asyncio.gather(*calls, return_exceptions=True)
    syms, earnings = [], {}
    for i, g in enumerate(groups):
        if isinstance(g, Exception):
            log.warning("Challenger Gemini : source de la population en échec : %s", g)
            continue
        if kind == "C" and i == 2:
            for e in g:
                if _earnings_ok(e, today)[0]:
                    earnings.setdefault(e["symbol"].upper(), e)
            continue
        syms += [s.upper() for s in g]
    syms += [s.upper() for s in config.WATCHLIST_STOCKS]
    if kind == "C":
        syms += list(earnings)
    return [s for s in dict.fromkeys(syms) if s.isalpha() and len(s) <= 5], earnings


def _earnings_ok(e: dict | None, today: date) -> tuple[bool, str]:
    """C : résultats battus, publiés il y a 1 à 5 séances (pas le jour même : la recette joue la digestion)."""
    if not e:
        return False, "pas de résultats ces 5 dernières séances"
    d = date.fromisoformat(e["date"])
    if d >= today:
        return False, "résultats aujourd'hui"
    if e.get("eps_actual") is None or e.get("eps_estimate") is None or e["eps_actual"] <= e["eps_estimate"]:
        return False, "résultats pas au-dessus des attentes"
    return True, f"résultats du {d.strftime('%d/%m')} battus (BPA {e['eps_actual']:g} contre {e['eps_estimate']:g})"


async def scan(kind: str, now: datetime | None = None) -> dict:
    """Un passage : tri, plans et témoins tirés. Rien n'est envoyé ici."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(NEW_YORK).date()
    symbols, earnings = await _universe(kind, today)
    if not symbols:
        return {"kind": kind, "day": today.isoformat(), "plans": [], "ecartees": {}, "population": 0}
    snaps, daily = {}, {}
    for i in range(0, len(symbols), CHUNK):  # C : des centaines de publications en saison des résultats
        part = symbols[i:i + CHUNK]
        sn, da = await asyncio.gather(alpaca.snapshots(part), alpaca.bars(part, 1440, days=60, now=now))
        snaps.update(sn)
        daily.update(da)
    pre, ecartees = [], {}

    def drop(s: str, why: str) -> None:
        ecartees[s] = why

    for s in symbols:
        rows = [r for r in daily.get(s) or [] if CO.ny_day(r["t"]) < today]
        snap = snaps.get(s) or {}
        if len(rows) < 21 or not snap.get("price") or rows[-1]["c"] <= 0:
            continue
        prev, adv = rows[-1]["c"], sum(r["v"] for r in rows[-20:]) / 20
        change = (snap["price"] / prev - 1) * 100
        if kind == "A":
            o = snap.get("open") if snap.get("open_day") == today.isoformat() else None
            if not o or not SEUILS["gap_min"] <= (o / prev - 1) * 100 <= SEUILS["gap_max"]:
                continue
        elif kind == "B" and change < SEUILS["hausse_b_min"]:
            continue
        elif kind == "C":
            ok, why = _earnings_ok(earnings.get(s), today)
            if not ok:
                continue
        pre.append((s, (snap.get("day_volume") or 0) / adv if adv else 0, prev, rows))
    population = [s for s in symbols if snaps.get(s)]
    pre = sorted(pre, key=lambda x: -x[1])[:DEEP_MAX]
    bars5 = await alpaca.bars([s for s, *_ in pre], 5, days=35, now=now, feed="iex") if pre else {}
    until = PASSES[kind]
    until = time(until.hour, until.minute - until.minute % 5)  # fin de la dernière bougie de 5 min close
    cands = []
    for s, _, prev, rows in pre:
        b5 = bars5.get(s) or []
        sess = [b for b in session(b5, today) if b["t"] + 300 <= now.timestamp()]
        rv = rvol(b5, today, until)
        if kind == "A":
            bounds = CO.session_ts(today)
            if not sess or sess[0]["t"] != bounds[0]:
                drop(s, "pas de bougie de 9 h 30 sur IEX")
                continue
            p, why = plan_a(sess[:1], prev)
        elif kind == "B":
            p, why = plan_b(sess, prev)
        else:
            p, why = plan_c(sess, rows)
        if not p:
            drop(s, why)
            continue
        if kind != "C" and (rv is None or rv < SEUILS["rvol_min"]):
            drop(s, "volume relatif " + ("inconnu" if rv is None else f"{rv:.1f}".replace(".", ",")))
            continue
        para = parabolic(sess)
        if kind != "A" and para is not None and para > SEUILS["parabole_pct"]:
            drop(s, f"parabolique ({_fr(para)} % en 30 min)")
            continue
        cands.append((s, p, rv, para, prev))
    plans_out = []
    for s, p, rv, para, prev in cands[:FINNHUB_MAX]:
        prof, cat = await asyncio.gather(finnhub.profile(s), C.safe_check(s, now), return_exceptions=True)
        shares = prof.get("shares") if isinstance(prof, dict) else None
        if shares is None:
            drop(s, "nombre d'actions inconnu (Finnhub)")
            continue
        if kind != "C" and shares >= SEUILS["actions_max"]:
            drop(s, f"{shares / 1e6:.0f} M d'actions (moins de 30 M)")
            continue
        if kind == "C":
            ok, cat_why = _earnings_ok(earnings.get(s), today)
            cat_why = [cat_why]
        else:
            ok, cat_why = KM.catalyst_pillar(cat if isinstance(cat, dict) else None, now)
        if not ok:
            drop(s, "catalyseur : " + "; ".join(cat_why))
            continue
        plans_out.append({"symbol": s, "kind": kind, **p, "rvol": None if rv is None else round(rv, 2),
                          "hausse_30min": None if para is None else round(para, 2), "actions": shares,
                          "nom": prof.get("name"), "catalyseur": cat_why, "veille": prev})
    for p in plans_out:
        rng = random.Random(f"{today.isoformat()}|{kind}|{p['symbol']}|{VERSION}")
        pool = sorted(x for x in population if x not in {q["symbol"] for q in plans_out})
        p["temoins"] = rng.sample(pool, min(TEMOINS, len(pool)))
        p["day"], p["version"], p["note_a"] = today.isoformat(), VERSION, now.isoformat(timespec="seconds")
        # L'ordre est actif une minute pleine après le message (le temps de le lire et de saisir l'ordre).
        p["actif_des"] = (int(now.timestamp()) // 60 + 2) * 60
    entry = {"kind": kind, "day": today.isoformat(), "at": now.isoformat(timespec="seconds"), "version": VERSION,
             "population": len(population), "examinees": len(pre), "plans": [p["symbol"] for p in plans_out],
             "ecartees": ecartees}
    journal(entry)
    return entry | {"plans": plans_out}


def _size(entry: float, stop: float, rate: float | None, risk_eur: float | None) -> str:
    if risk_eur is None:
        return "pas de taille (arrêt de la journée ou budget de risque plein)"
    if not rate or entry <= stop:
        return ""
    q = math.floor(risk_eur * rate / (entry - stop) + 1e-9)
    if q < 1:
        return f"1 action = {(entry - stop) / rate:.0f} € de risque, au-delà de {risk_eur:g} €"
    return f"{q} actions pour {risk_eur:g} € de risque (ta règle, hors avis de JARVIS : challenger non mesuré)"


def plan_text(p: dict, rate: float | None = None, risk_eur: float | None = KM.RISK_EUR) -> str:
    s = p["symbol"]
    head = f"🧪 **Challenger Gemini · {p['kind']} {NAMES[p['kind']]}** · **{s}**" + (f" ({p['nom']})" if p.get("nom") else "")
    facts = []
    if p.get("rvol") is not None:
        facts.append(f"volume relatif {p['rvol']:.1f}".replace(".", ","))
    if p.get("actions"):
        facts.append(f"{p['actions'] / 1e6:.1f} M d'actions".replace(".", ","))
    facts.append("catalyseur : " + "; ".join(p.get("catalyseur") or []))
    lines = [head, " · ".join(facts)]
    if p["kind"] == "A":
        e, tp = p["entry"], p["tp_pct"]
        lines.append(f"ordre stop d'achat **{_num(e)}** (gap {_fr(p['gap_pct'])} %), seulement si la minute de cassure "
                     f"clôture au-dessus avec au moins 1,5 fois le volume moyen par minute des 5 premières · stop "
                     f"**{_num(p['stop_range'])}**, ou 1 cent sous le VWAP s'il est plus proche · TP1 +{tp[0]:g} % "
                     f"(moitié, stop à l'entrée) · TP2 +{tp[1]:g} % · annulé à 10 h 30 NY · clôture à 15 h 45 NY")
        lines.append(_size(e, p["stop_range"], rate, risk_eur))
    elif p["kind"] == "B":
        lines.append(f"ordre stop d'achat **{_num(p['entry'])}** (haut du drapeau) jusqu'à 15 h NY · stop "
                     f"**{_num(p['stop'])}** · TP1 {_num(p['tp1'])} (pic du matin, moitié, stop à l'entrée) · TP2 "
                     f"{_num(p['tp2'])} · sortie à 15 h 59 NY")
        lines.append(_size(p["entry"], p["stop"], rate, risk_eur))
    else:
        lines.append(f"achat au marché (VWAP {_num(p['vwap'])} repris, dernier {_num(p['ref'])}) · stop "
                     f"**{_num(p['stop'])}** (plus bas de la séance) · TP1 {_num(p['tp1'])} (moitié, stop à l'entrée) · "
                     f"TP2 {_num(p['tp2'])} · sortie à 15 h 59 NY")
        lines.append(_size(p["ref"], p["stop"], rate, risk_eur))
    lines.append("-# Méthode de Gemini en forward-test contre un témoin aléatoire, pas un GO de JARVIS. `!gemini` : bilan.")
    return "\n".join(x for x in lines if x)


async def settle_due(now: datetime | None = None, state: dict | None = None) -> int:
    """Solde les plans dont la séance du lendemain est close (les deux versions se soldent ensemble)."""
    now = now or datetime.now(timezone.utc)
    state = state if state is not None else load()
    todo = []
    for p in state.get("plans", []):
        if p.get("solde"):
            continue
        day = date.fromisoformat(p["day"])
        nxt = day + timedelta(days=1)
        while not CAL.is_trading_day(nxt):
            nxt += timedelta(days=1)
        bounds = CAL.session_bounds(nxt)
        if bounds and now >= bounds[1] + SETTLE_AFTER:
            todo.append((p, day, nxt, bounds[1]))
    if not todo:
        return 0
    rate = await KM.eurusd() or KM.EURUSD_DEFAULT
    done = 0
    for p, day, nxt, end in todo:
        syms = [p["symbol"], *(p.get("temoins") or [])]
        start = datetime.combine(day, time(9, 30), NEW_YORK)
        try:
            bars = await alpaca.bars_range(syms, 1, start, end, feed="sip")
        except Exception as exc:
            log.warning("Challenger Gemini : bougies de %s indisponibles : %s", p["symbol"], exc)
            continue
        bars = {s: alpaca.regular_hours(rows) for s, rows in bars.items()}
        p["solde"] = settle_plan(p, bars, nxt, rate)
        done += 1
    save(state)
    return done


async def command(args: list[str]) -> str:
    """`!gemini` : plans du jour et bilan ; `!gemini bilan` : le bilan seul."""
    state = load()
    if args and args[0].lower() == "bilan":
        return bilan_text(state)
    return today_text(state) + "\n\n" + bilan_text(state)
