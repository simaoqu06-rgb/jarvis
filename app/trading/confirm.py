"""Contrôle des GO du scanner après l'ouverture (idée de Simão, 2026-10-08).

Le scanner de pré-ouverture donne ses GO avec une confirmation à observer (service.confirmation_lines).
De 15 min après l'ouverture jusqu'à 11 h 30 New York, toutes les 5 min, chaque GO est relu sur des
bougies de 5 min en temps réel (Twelve Data ; Alpaca a 16 min de retard) :

- annulé : une bougie 5 min clôture sous le stop, ou le TP1 est touché avant la confirmation ;
- confirmé : le prix est revenu vers l'entrée (plus bas ≤ entrée) puis une bougie 5 min, close après
  le premier quart d'heure, clôture au-dessus du VWAP de la séance → ordre stop d'achat 1 cent
  au-dessus de son plus haut, stop du plan, taille pour JARVIS_RISK_EUR (20 € par défaut) ;
- expiré : rien à 11 h 30 New York.

Mesure, à la clôture : chaque GO est rejoué deux fois sur les bougies du jour, « brut » (acheté à
l'ouverture) et « confirmé » (acheté au déclenchement de l'ordre stop, s'il a été touché), avec la
gestion habituelle (un tiers par objectif, stop fixe, reste vendu à la clôture ; une bougie qui touche
objectif et stop compte comme stop). Gains en R avant frais, gardés dans data/confirmations.jsonl,
lus par `!confirmations`. Rien ne change dans la note ni dans les plans.
"""
from __future__ import annotations

import json
import logging
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
        gos[p["symbol"]] = {
            "status": "attente", "entry": plan["entry"], "stop": plan["stop"],
            "targets": [t["price"] for t in plan["targets"]], "composite": plan["score"]["composite"],
            "last_bar": None, "events": [],
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
    if not risk_usd or trigger <= stop:
        return None
    return int(risk_usd // (trigger - stop))


def event_text(symbol: str, go: dict, ev: dict, now: datetime, risk_eur: float, usd_per_eur: float | None) -> str:
    if ev["kind"] == "annulé":
        return f"❌ **{symbol} annulé** : {ev['why']}. Pas de trade."
    risk_usd = risk_eur * usd_per_eur if usd_per_eur else None
    qty = shares(ev["trigger"], go["stop"], risk_usd)
    size = (f" · **{qty} actions** ({risk_eur:g} € ≈ {risk_usd:.0f} $ de risque, "
            f"{_num(round(qty * ev['trigger'], 2))} $)" if qty else "")
    tps = " · ".join(f"TP{i} {_num(p)}" for i, p in enumerate(go["targets"], start=1))
    r = (go["targets"][1] - ev["trigger"]) / (ev["trigger"] - go["stop"]) if len(go["targets"]) > 1 else None
    return (f"✅ **{symbol} confirmé** ({time_fr(now, now)}) : repli tenu, clôture 5 min au-dessus du VWAP "
            f"({_num(ev['vwap'])}).\nOrdre : **stop d'achat {_num(ev['trigger'])}** · stop {_num(go['stop'])}"
            f"{size}\n{tps}" + (f" · TP2 à {r:.1f} R" if r is not None else "")
            + "\n-# Valable jusqu'à 11 h 30 New York ; au-delà, annule l'ordre s'il n'est pas déclenché.")


def expired_text(symbols: list[str]) -> str:
    return f"⌛ Pas de confirmation à 11 h 30 New York pour {', '.join(symbols)} : GO expirés, pas de trade."


# --- Mesure à la clôture ------------------------------------------------------------------

def simulate(entry: float, stop: float, targets: list[float], bars: list[dict]) -> float | None:
    """R d'un achat à `entry` géré par tiers (stop fixe, reste à la clôture), avant frais."""
    risk = entry - stop
    if risk <= 0 or not bars:
        return None
    legs, hit, total = len(targets), 0, 0.0
    for b in bars:
        if b["l"] <= stop:
            return round((total + (legs - hit) * -1.0) / legs, 2)
        while hit < legs and b["h"] >= targets[hit]:
            total += (targets[hit] - entry) / risk
            hit += 1
        if hit == legs:
            return round(total / legs, 2)
    return round((total + (legs - hit) * (bars[-1]["c"] - entry) / risk) / legs, 2)


def outcome(go: dict, bars: list[dict]) -> dict:
    """Brut (acheté à l'ouverture) et confirmé (acheté au déclenchement de l'ordre stop)."""
    raw = None
    if bars and bars[0]["o"] > go["stop"]:
        raw = simulate(bars[0]["o"], go["stop"], go["targets"], bars)
    confirmed = filled = None
    if go["status"] == "confirmé":
        after = [b for b in bars if b["t"] > go["confirm_bar"]]
        for i, b in enumerate(after):
            if b["h"] >= go["trigger"]:
                filled = max(go["trigger"], b["o"])
                # bougie du déclenchement : on ne sait pas si le stop vient avant ; il compte s'il est touché
                confirmed = simulate(filled, go["stop"], go["targets"], after[i:])
                break
    return {"brut_r": raw, "confirme_r": confirmed, "rempli": filled}


def close_day(state: dict, bars_by_symbol: dict[str, list[dict]], path: str | None = None) -> list[dict]:
    rows = []
    for symbol, go in state.get("gos", {}).items():
        bars = bars_by_symbol.get(symbol)
        if not bars:
            continue
        rows.append({"day": state["day"], "symbol": symbol, "status": go["status"],
                     "composite": go["composite"], "entry": go["entry"], "stop": go["stop"],
                     "targets": go["targets"], "trigger": go.get("trigger"), **outcome(go, bars)})
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


def report_text(rows: list[dict], state: dict | None = None) -> str:
    lines = ["🔎 **GO du scanner : confirmés ou pas** (R avant frais, un tiers par objectif)"]
    if state and state.get("gos"):
        today = " · ".join(f"{s} {g['status']}" for s, g in state["gos"].items())
        lines.append(f"Aujourd'hui ({state['day']}) : {today}")
    if not rows:
        lines.append("Aucune séance mesurée pour l'instant.")
        return "\n".join(lines)
    days = len({r["day"] for r in rows})
    raw = [r["brut_r"] for r in rows if r["brut_r"] is not None]
    lines.append(f"{len(rows)} GO sur {days} séance(s) · brut (achat à l'ouverture) : {_mean(raw)} sur {len(raw)}")
    for status in ("confirmé", "annulé", "expiré"):
        group = [r for r in rows if r["status"] == status]
        if not group:
            continue
        g_raw = [r["brut_r"] for r in group if r["brut_r"] is not None]
        line = f"• {status} : {len(group)} · brut {_mean(g_raw)}"
        if status == "confirmé":
            conf = [r["confirme_r"] for r in group if r["confirme_r"] is not None]
            line += f" · avec l'ordre confirmé {_mean(conf)} ({len(conf)} déclenchés)"
        lines.append(line)
    if len(rows) < 30:
        lines.append(f"-# Lecture à partir de 30 GO ({len(rows)} pour l'instant) : avant, c'est du bruit.")
    return "\n".join(lines)

