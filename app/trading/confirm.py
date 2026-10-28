"""Contrôle des GO du scanner après l'ouverture (idée de Simão, 2026-10-08).

Le scanner de pré-ouverture donne ses GO avec une confirmation à observer (service.confirmation_lines).
De 15 min après l'ouverture jusqu'à 11 h 30 New York, toutes les 5 min, chaque GO est relu sur des
bougies de 5 min en temps réel (Twelve Data ; Alpaca a 16 min de retard) :

- annulé : une bougie 5 min clôture sous le stop, ou le TP1 est touché avant la confirmation ;
- confirmé : le prix est revenu vers l'entrée (plus bas ≤ entrée) puis une bougie 5 min, close après
  le premier quart d'heure, clôture au-dessus du VWAP de la séance → ordre stop d'achat 1 cent
  au-dessus de son plus haut, stop du plan, taille de la règle unique (risque du grade, JARVIS_RISK_EUR = 20 €
  au plus ; ta règle de 20 €, dite hors avis de JARVIS dans la ligne d'ordre même, quand le grade ne propose rien ;
  aucune taille quand ce 0 % vient de l'arrêt de la journée ou du budget de risque ouvert plein, sauf
  JARVIS_MP_TAILLE_APRES_ARRET=oui) ;
- expiré : rien à 11 h 30 New York.

Mesure, à la clôture : chaque GO est rejoué deux fois sur les bougies du jour, « brut » (acheté à
l'ouverture) et « confirmé » (acheté au déclenchement de l'ordre stop, s'il a été touché), avec la
gestion habituelle (un tiers par objectif, stop fixe, reste vendu à la clôture ; une bougie qui touche
objectif et stop compte comme stop). Gains en R avant frais, gardés dans data/confirmations.jsonl,
lus par `!confirmations`. Rien ne change dans la note ni dans les plans. Deux colonnes de plus : la
même gestion avec le stop remonté à l'entrée après TP2 (`_be2`, la règle du suivi de Simão, sans sa
sortie libre dans le temps) et le confirmé à la taille de Simão (`confirme_r20` : actions entières pour
JARVIS_RISK_EUR, un bracket IBKR par objectif, commission de chaque ordre ; non compté, `confirme_hors_mise`,
quand une seule action dépasse la mise : le MP a dit « pas de taille »). Chaque ligne garde aussi
les indicateurs du scanner (`features`) et les sous-notes (`parts`) du GO.
"""
from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, time, timedelta
from pathlib import Path

from app.trading import calendar as CAL
from app.trading.service import _num, time_fr

log = logging.getLogger(__name__)

STATE_PATH = os.getenv("JARVIS_CONFIRM_PATH", "data/confirmations.json")
LOG_PATH = os.getenv("JARVIS_CONFIRM_LOG", "data/confirmations.jsonl")
BAR = 5 * 60
WAIT = timedelta(minutes=15)       # premier quart d'heure : pas de confirmation (range d'ouverture)
LAST_CHECK = time(11, 30)          # heure de New York : au-delà, un GO non confirmé expire
TICK = 0.01

PENDING = ("attente", "repli")
# Choix par défaut (Simão tranchera) : après l'arrêt de la journée (−3 R) ou avec le budget de risque ouvert plein, les
# MP automatiques (GO confirmé, potentiel +10) ne chiffrent pas de taille. « oui » : ta règle de 20 €, étiquetée, comme
# pour les autres 0 % (F, E, périmé, illisible, discontinu, non noté). Revenir en arrière : cette seule variable.
SIZE_AFTER_STOP = os.getenv("JARVIS_MP_TAILLE_APRES_ARRET", "non").strip().lower() in ("oui", "on", "1", "true")


# --- État du jour -------------------------------------------------------------------------

def load(path: str | None = None) -> dict:
    try:
        return json.loads(Path(path or STATE_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(state: dict, path: str | None = None) -> None:
    p = Path(path or STATE_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def register(state: dict, result: dict, day: str) -> list[str]:
    """Garde les GO du scanner du jour (l'état de la veille est remplacé). Renvoie les symboles."""
    gos = {}
    for p in result["picks"]:
        plan = p["plan"]
        if not plan["score"]["go"] or not plan.get("targets"):
            continue
        features = p.get("features") or {}
        gos[p["symbol"]] = {
            "status": "attente", "entry": plan["entry"], "stop": plan["stop"],
            "targets": [t["price"] for t in plan["targets"]], "composite": plan["score"]["composite"],
            "last_bar": None, "events": [],
            "features": {k: features.get(k) for k in ("rsi", "rvol", "gap_pct")},
            "parts": {k: v["score"] for k, v in (plan["score"].get("parts") or {}).items()},
            # Délais du plan (minutes de cotation) : le journal des recommandations (reco.py) les repart du déclenchement.
            "plan_targets": [{k: t.get(k) for k in ("minutes_median", "minutes_p75", "reached_by_p75",
                                                     "reached_by_p75_given_prev", "still_after_p75")}
                             for t in plan["targets"]],
        }
    state.clear()
    state.update({"day": day, "gos": gos, "closed": False})
    return list(gos)


# --- Lecture des bougies ------------------------------------------------------------------

def session_bars(bars: list[dict], day_open: datetime, now: datetime) -> list[dict]:
    """Bougies 5 min closes de la séance du jour."""
    start, end = day_open.timestamp(), now.timestamp()
    return [b for b in bars if b["t"] >= start and b["t"] + BAR <= end]


def evaluate(go: dict, bars: list[dict], day_open: datetime) -> dict | None:
    """Fait avancer un GO sur les bougies closes ; renvoie l'événement nouveau (confirmé/annulé) ou None.

    Relit toute la séance à chaque fois (VWAP cumulé) mais ne traite que les bougies après `last_bar`.
    """
    num = den = 0.0
    earliest = (day_open + WAIT).timestamp()
    for b in bars:
        typical = (b["h"] + b["l"] + b["c"]) / 3
        num += typical * b["v"]
        den += b["v"]
        if go["last_bar"] is not None and b["t"] <= go["last_bar"]:
            continue
        go["last_bar"] = b["t"]
        if go["status"] not in PENDING:
            continue
        vwap = num / den if den else b["c"]
        if b["c"] < go["stop"]:
            go["status"] = "annulé"
            return {"kind": "annulé", "t": b["t"], "why": f"clôture 5 min à {_num(b['c'])} sous le stop {_num(go['stop'])}"}
        if b["h"] >= go["targets"][0]:
            go["status"] = "annulé"
            return {"kind": "annulé", "t": b["t"], "why": f"TP1 {_num(go['targets'][0])} touché avant la confirmation"}
        if b["l"] <= go["entry"]:
            go["status"] = "repli"
        if go["status"] == "repli" and b["t"] + BAR > earliest and b["c"] > vwap:
            trigger = round(b["h"] + TICK, 2)
            if trigger >= go["targets"][0]:
                continue  # pas de place jusqu'au TP1 : on attend une meilleure bougie
            go.update(status="confirmé", trigger=trigger, confirm_bar=b["t"])
            return {"kind": "confirmé", "t": b["t"], "trigger": trigger, "vwap": round(vwap, 4)}
    return None


def expire_due(now: datetime, day_open: datetime) -> bool:
    return now.astimezone(CAL.NEW_YORK) >= datetime.combine(day_open.date(), LAST_CHECK, CAL.NEW_YORK)


# --- Messages -----------------------------------------------------------------------------

def shares(trigger: float, stop: float, risk_usd: float | None) -> int | None:
    """Actions entières pour `risk_usd` de risque (même arrondi que Killmode) ; 0 si une seule action le dépasse."""
    if not risk_usd or trigger <= stop:
        return None
    return int(risk_usd / (trigger - stop) + 1e-9)


def over_budget(entry: float, stop: float, risk_eur: float, usd_per_eur: float) -> str:
    """« 1 action = 26 € de risque, au-delà de 20 € » : la taille à ce risque vaut 0 action."""
    return f"1 action = {(entry - stop) / usd_per_eur:.0f} € de risque, au-delà de {risk_eur:g} €"


def portfolio_stop(grade: dict | None) -> str | None:
    """Le 0 % vient du portefeuille, pas de la méthode : « arrêt de la journée (−3,3 R) » ou « budget de risque ouvert
    plein » ; None sinon. Lu sur les champs chiffrés de grade.risk_now (r_du_jour, risque_ouvert, risque_grade),
    pas sur le texte de sa raison."""
    if not grade or (grade.get("risque_propose") or 0) > 0 or not (grade.get("risque_grade") or 0) > 0:
        return None
    from app.lab import grade as GRADE  # import tardif : le labo importe ce module

    today = grade.get("r_du_jour")
    if today is not None and today <= GRADE.ARRET_JOUR_R:
        return f"arrêt de la journée ({today:+.1f} R)".replace("-", "−").replace(".", ",")
    if grade.get("risque_ouvert") is not None:
        return "budget de risque ouvert plein"
    return None


def rule_size(grade: dict | None, recommended: float | None, rule: float) -> float | None:
    """Risque en € des tailles des MP automatiques : celui du grade (`recommended`), sinon ta règle (`rule`), sauf
    après l'arrêt de la journée ou avec le budget plein (None : pas de taille), à moins de SIZE_AFTER_STOP."""
    if recommended:
        return recommended
    return rule if SIZE_AFTER_STOP or not portfolio_stop(grade) else None


def off_grade(grade: dict | None, risk_eur: float | None) -> str | None:
    """Taille à la règle de Simão quand le grade ne propose rien (0 %) : dite comme telle, avec la ligne du grade.
    Sans taille (`risk_eur` None) : la ligne du grade seule, qui dit pourquoi."""
    if not grade or grade.get("risque_propose", 0) > 0:
        return None
    from app.lab import grade as GRADE  # import tardif : le labo importe ce module

    if risk_eur is None:
        return GRADE.plan_line(grade)
    return (f"-# Taille à ta règle de {risk_eur:g} € (hors avis de JARVIS : grade {grade['grade']}, 0 % proposé).\n"
            + GRADE.plan_line(grade))


def event_text(symbol: str, go: dict, ev: dict, now: datetime, risk_eur: float | None, usd_per_eur: float | None,
               grade: dict | None = None) -> str:
    """`risk_eur` : la règle unique de taille (grade, plafond de 20 €), ou ta règle de 20 € quand le grade `grade`
    ne propose rien (dit dans la ligne d'ordre même) ; None : pas de taille (arrêt de la journée, budget plein)."""
    if ev["kind"] == "annulé":
        return f"❌ **{symbol} annulé** : {ev['why']}. Pas de trade."
    risk_usd = risk_eur * usd_per_eur if usd_per_eur and risk_eur else None
    qty = shares(ev["trigger"], go["stop"], risk_usd)
    rule = bool(grade) and grade.get("risque_propose", 0) <= 0  # ta règle de 20 €, hors avis de JARVIS
    if risk_eur is None:
        size = f" · pas de taille : {portfolio_stop(grade) or 'aucun risque proposé'}"
    elif qty:
        cost = f"{risk_usd:.0f} $ de risque, {_num(round(qty * ev['trigger'], 2))} $"
        size = (f" · **{qty} actions à ta règle de {risk_eur:g} €** (hors avis de JARVIS ; ≈ {cost})" if rule
                else f" · **{qty} actions** ({risk_eur:g} € ≈ {cost})")
    elif qty == 0:
        size = f" · {over_budget(ev['trigger'], go['stop'], risk_eur, usd_per_eur)} : pas de taille"
    else:
        size = " · pas de taille : change EUR/USD indisponible"
    tps = " · ".join(f"TP{i} {_num(p)}" for i, p in enumerate(go["targets"], start=1))
    r = (go["targets"][1] - ev["trigger"]) / (ev["trigger"] - go["stop"]) if len(go["targets"]) > 1 else None
    off = off_grade(grade, risk_eur if qty else None)
    return (f"✅ **{symbol} confirmé** ({time_fr(now, now)}) : repli tenu, clôture 5 min au-dessus du VWAP "
            f"({_num(ev['vwap'])}).\nOrdre : **stop d'achat {_num(ev['trigger'])}** · stop {_num(go['stop'])}"
            f"{size}\n{tps}" + (f" · TP2 à {r:.1f} R" if r is not None else "")
            + "\n-# Valable jusqu'à 11 h 30 New York ; au-delà, annule l'ordre s'il n'est pas déclenché."
            + (f"\n{off}" if off else ""))


def expired_text(symbols: list[str]) -> str:
    return f"⌛ Pas de confirmation à 11 h 30 New York pour {', '.join(symbols)} : GO expirés, pas de trade."


# --- Mesure à la clôture ------------------------------------------------------------------

def simulate(entry: float, stop: float, targets: list[float], bars: list[dict], be_after: int | None = None,
             detail: bool = False) -> float | None | tuple[float | None, list[dict]]:
    """R d'un achat à `entry` géré par tiers (reste à la clôture), avant frais. Stop fixe, ou remonté à l'entrée
    à partir de la bougie qui suit la touche du `be_after`-ième objectif (2 : la règle du suivi de Simão).
    `detail` : renvoie (R, sorties), une sortie par tiers {prix, raison : tp1…, stop, stop_be, cloture}."""
    risk = entry - stop
    if risk <= 0 or not bars:
        return (None, []) if detail else None
    legs, hit, total, lvl, exits = len(targets), 0, 0.0, stop, []

    def done(r: float):
        return (round(r / legs, 2), exits) if detail else round(r / legs, 2)

    for b in bars:
        if b["l"] <= lvl:
            exits.extend({"prix": lvl, "raison": "stop" if lvl == stop else "stop_be"} for _ in range(legs - hit))
            return done(total + (legs - hit) * (lvl - entry) / risk)
        while hit < legs and b["h"] >= targets[hit]:
            total += (max(targets[hit], entry) - entry) / risk
            exits.append({"prix": max(targets[hit], entry), "raison": f"tp{hit + 1}"})
            hit += 1
        if hit == legs:
            return done(total)
        if be_after and hit >= be_after:
            lvl = entry
    exits.extend({"prix": bars[-1]["c"], "raison": "cloture"} for _ in range(legs - hit))
    return done(total + (legs - hit) * (bars[-1]["c"] - entry) / risk)


def at_size(trigger: float, stop: float, filled: float, exits: list[dict], rate: float | None = None) -> float | None:
    """R du confirmé à la taille de Simão, sur le risque prévu (trigger − stop) : actions entières pour
    JARVIS_RISK_EUR (killmode.shares, change `rate` ou 1,15), un bracket IBKR par objectif (killmode._legs, comme le
    journal de Killmode), commission de chaque ordre avec son minimum et son plafond (entrée et sortie de chaque
    bracket ; le reste vendu à la clôture part en un seul ordre). Sans glissement."""
    from app.trading import killmode as K  # import tardif : killmode tire le labo, qui importe ce module

    risk = trigger - stop
    if risk <= 0 or not exits or over_stake(trigger, stop, rate):
        return None  # une action dépasse la mise : le MP a dit « pas de taille », rien n'est compté
    q = K.shares(risk, rate)
    plan = K.IBKR_PLANS.get(K.IBKR_PLAN, K.IBKR_PLANS["fixe"])
    legs = K._legs(q, len(exits), [e | {"part": 1 / len(exits)} for e in exits])
    pnl = sum(n * (e["prix"] - filled) - K.order_commission(n, filled, plan)
              - (0.0 if e["raison"] == "cloture" else K.order_commission(n, e["prix"], plan)) for n, e in legs)
    closing = [(n, e["prix"]) for n, e in legs if e["raison"] == "cloture"]
    if closing:
        pnl -= K.order_commission(sum(n for n, _ in closing), closing[0][1], plan)
    return round(pnl / (q * risk), 2)


def over_stake(trigger: float, stop: float, rate: float | None = None) -> bool:
    """Une seule action dépasse la mise de Simão (JARVIS_RISK_EUR au change `rate`, 1,15 par défaut) : 0 action."""
    from app.trading import killmode as K  # import tardif : killmode tire le labo, qui importe ce module

    risk = trigger - stop
    return risk > 0 and math.floor(K.RISK_EUR * (rate or K.EURUSD_DEFAULT) / risk + 1e-9) == 0


def outcome(go: dict, bars: list[dict], rate: float | None = None) -> dict:
    """Brut (acheté à l'ouverture) et confirmé (acheté au déclenchement de l'ordre stop), stop fixe et stop à
    l'entrée après TP2 (`_be2`) ; le confirmé aussi à la taille de Simão (`confirme_r20`, frais compris)."""
    raw = raw_be = None
    if bars and bars[0]["o"] > go["stop"]:
        raw = simulate(bars[0]["o"], go["stop"], go["targets"], bars)
        raw_be = simulate(bars[0]["o"], go["stop"], go["targets"], bars, be_after=2)
    confirmed = confirmed_be = confirmed_20 = filled = None
    if go["status"] == "confirmé":
        after = [b for b in bars if b["t"] > go["confirm_bar"]]
        # Comme le message du direct : l'ordre non déclenché est annulé à 11 h 30 New York.
        day = datetime.fromtimestamp(go["confirm_bar"], CAL.NEW_YORK).date()
        cutoff = datetime.combine(day, LAST_CHECK, CAL.NEW_YORK).timestamp()
        for i, b in enumerate(after):
            if b["t"] >= cutoff:
                break
            if b["h"] >= go["trigger"]:
                filled = max(go["trigger"], b["o"])
                # bougie du déclenchement : on ne sait pas si le stop vient avant ; il compte s'il est touché
                confirmed, exits = simulate(filled, go["stop"], go["targets"], after[i:], detail=True)
                confirmed_be = simulate(filled, go["stop"], go["targets"], after[i:], be_after=2)
                confirmed_20 = at_size(go["trigger"], go["stop"], filled, exits, rate)
                break
    out = {"brut_r": raw, "brut_r_be2": raw_be, "confirme_r": confirmed, "confirme_r_be2": confirmed_be,
           "confirme_r20": confirmed_20, "rempli": filled}
    if filled is not None and over_stake(go["trigger"], go["stop"], rate):
        out["confirme_hors_mise"] = True  # compté à part : pas de taille dans le MP, pas de R à 20 €
    return out


def close_day(state: dict, bars_by_symbol: dict[str, list[dict]], path: str | None = None,
              rate: float | None = None) -> list[dict]:
    """Mesure du jour, une ligne par GO dans confirmations.jsonl (avec ses indicateurs et sous-notes du scanner).
    `rate` : change EUR→USD de la taille de `confirme_r20` (1,15 par défaut, comme Killmode)."""
    rows = []
    for symbol, go in state.get("gos", {}).items():
        bars = bars_by_symbol.get(symbol)
        if not bars:
            continue
        rows.append({"day": state["day"], "symbol": symbol, "status": go["status"],
                     "composite": go["composite"], "entry": go["entry"], "stop": go["stop"],
                     "targets": go["targets"], "trigger": go.get("trigger"),
                     "features": go.get("features"), "parts": go.get("parts"), **outcome(go, bars, rate)})
    if rows:
        p = Path(path or LOG_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    state["closed"] = True
    return rows


def read_log(path: str | None = None) -> list[dict]:
    try:
        return [json.loads(line) for line in Path(path or LOG_PATH).read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return []


def _mean(xs: list[float]) -> str:
    return f"{sum(xs) / len(xs):+.2f} R" if xs else "n/d"


def _col(rows: list[dict], key: str, n: int) -> str:
    """Moyenne d'une colonne ; le nombre de lignes mesurées s'il diffère de `n` (lignes écrites avant la colonne)."""
    xs = [r[key] for r in rows if r.get(key) is not None]
    return _mean(xs) + (f" sur {len(xs)}" if xs and len(xs) != n else "")


def report_text(rows: list[dict], state: dict | None = None) -> str:
    from app.trading.killmode import RISK_EUR  # import tardif : killmode tire le labo, qui importe ce module

    lines = ["🔎 **GO du scanner : confirmés ou pas** (R avant frais, un tiers par objectif)",
             "-# Politique mesurée : brut = achat à l'ouverture ; confirmé = ordre stop d'achat au déclencheur, annulé "
             "à 11 h 30 New York s'il n'est pas touché. Un tiers par objectif, stop fixe (ou remonté à l'entrée après "
             f"TP2 : « BE TP2 »), reste vendu à la clôture du jour. « À {RISK_EUR:g} € » : actions entières pour "
             f"{RISK_EUR:g} € de risque, un bracket IBKR par objectif, commissions comprises, sans glissement. "
             "≠ ton suivi `!prends` : stop à l'entrée après TP2 mais pas de vente à la clôture.",
             "-# Pour juger la confirmation sur les mêmes GO, compare brut et ordre confirmé sur la ligne « confirmé ». "
             "Le brut de tous les GO mélange les annulés et les expirés, où l'on n'entre pas : un gain compté par GO "
             "de départ (0 R sans ordre) vient surtout de là, en n'entrant pas."]
    if state and state.get("gos"):
        today = " · ".join(f"{s} {g['status']}" for s, g in state["gos"].items())
        lines.append(f"Aujourd'hui ({state['day']}) : {today}")
    if not rows:
        lines.append("Aucune séance mesurée pour l'instant.")
        return "\n".join(lines)
    days = len({r["day"] for r in rows})
    raw = [r["brut_r"] for r in rows if r["brut_r"] is not None]
    lines.append(f"{len(rows)} GO sur {days} séance(s) · brut (achat à l'ouverture) : {_mean(raw)} sur {len(raw)}"
                 f" · BE TP2 {_col(rows, 'brut_r_be2', len(raw))}")
    for status in ("confirmé", "annulé", "expiré"):
        group = [r for r in rows if r["status"] == status]
        if not group:
            continue
        g_raw = [r["brut_r"] for r in group if r["brut_r"] is not None]
        line = f"• {status} : {len(group)} · brut {_mean(g_raw)} (BE TP2 {_col(group, 'brut_r_be2', len(g_raw))})"
        if status == "confirmé":
            conf = [r["confirme_r"] for r in group if r["confirme_r"] is not None]
            over = sum(bool(r.get("confirme_hors_mise")) for r in group)
            line += (f" · avec l'ordre confirmé {_mean(conf)} ({len(conf)} déclenchés ; BE TP2 "
                     f"{_col(group, 'confirme_r_be2', len(conf))} ; à {RISK_EUR:g} € "
                     f"{_col(group, 'confirme_r20', len(conf))}"
                     + (f", {over} hors mise non comptés : 1 action au-delà de {RISK_EUR:g} € de risque, pas de "
                        "taille dans le MP" if over else "") + ")")
        lines.append(line)
    old = sum("brut_r_be2" not in r for r in rows)
    if old:
        lines.append(f"-# {old} ligne(s) écrite(s) avant le 10 octobre 2026 : brut compté avec un objectif sous "
                     "l'achat en perte, donc le brut global et celui des annulés y sont un peu pessimistes.")
    if len(rows) < 30:
        lines.append(f"-# Lecture à partir de 30 GO ({len(rows)} pour l'instant) : avant, c'est du bruit.")
    return "\n".join(lines)

