"""Plans prêts à saisir chez IBKR : risque fixe en euros, brackets par TP et commissions estimées.

Simão (2026-10-08) : 20 € de perte maximale par trade (JARVIS_RISK_EUR). La quantité découle du stop
(risque ÷ écart entrée-stop), puis la position est découpée en un bracket IBKR par TP (même entrée,
même SL ; un TP touché ne ferme que sa part). Un bracket IBKR n'ayant qu'un TP, chaque part a son
ordre d'entrée et son ordre de sortie : 2 ordres par bracket, donc 2 minimums de commission.

Commissions IBKR Pro estimées (comparatif : /mnt/project-files/trading/plateforme-actions-us.md) :
- Fixe : 0,005 $ par action, minimum 1 $, plafond 1 % de la valeur de l'ordre ;
- Dégressif : 0,0035 $ par action, minimum 0,35 $, plafond 1 %, plus frais de place (≈ 0,003 $ par
  action en prenant la liquidité) et de compensation (0,0002 $), estimation haute.
Frais réglementaires à la vente (SEC, FINRA) négligés : quelques centimes.
"""
from __future__ import annotations

import logging
import math
import os
import time

from app.config import env_float
from app.sources import frankfurter

log = logging.getLogger(__name__)

RISK_EUR = env_float("JARVIS_RISK_EUR", 20.0)
PLAN = (os.environ.get("JARVIS_IBKR_PLAN") or "fixe").strip().lower()  # « fixe » ou « degressif »

FIXED = {"per_share": 0.005, "min": 1.0, "max_pct": 0.01, "extra": 0.0}
TIERED = {"per_share": 0.0035, "min": 0.35, "max_pct": 0.01, "extra": 0.0032}
PLANS = {"fixe": FIXED, "degressif": TIERED}
LABELS = {"fixe": "Fixe", "degressif": "Dégressif"}

FX_TTL = 12 * 3600
_fx: dict = {"rate": None, "at": 0.0}


def order_commission(shares: int, price: float, plan: dict) -> float:
    """Commission d'un ordre exécuté de `shares` actions à `price` (en $)."""
    if shares <= 0:
        return 0.0
    base = max(plan["min"], plan["per_share"] * shares)
    base = min(base, plan["max_pct"] * shares * price)
    return base + plan["extra"] * shares


def split(quantity: int, parts: int) -> list[int]:
    """Parts aussi égales que possible, les premières reçoivent le reste (TP1 en premier)."""
    parts = max(1, min(parts, quantity))
    q, r = divmod(quantity, parts)
    return [q + (1 if i < r else 0) for i in range(parts)]


def trade_commission(sizes: list[int], entry: float, exits: list[float], plan: dict) -> float:
    """Aller-retour de tous les brackets : une entrée et une sortie par bracket."""
    return sum(order_commission(n, entry, plan) + order_commission(n, x, plan) for n, x in zip(sizes, exits))


def brackets(p: dict, risk_usd: float, risk_eur: float, eurusd: float | None) -> dict | None:
    """Brackets IBKR d'un plan d'action (long ou short), ou None si le risque ne permet pas 1 action."""
    entry, stop = p["entry"], p["stop"]
    per_share = abs(entry - stop)
    if not per_share or p.get("market") != "stock":
        return None
    quantity = math.floor(risk_usd / per_share)
    if quantity < 1:
        return {"risk_eur": risk_eur, "risk_usd": round(risk_usd, 2), "eurusd": eurusd, "quantity": 0,
                "raison": f"stop trop loin : 1 action risque déjà {per_share:.2f} $"}
    tps = [t["price"] for t in p["targets"]] or [entry]
    sizes = split(quantity, len(tps))
    rows = [{"name": p["targets"][i]["name"] if p["targets"] else "TP", "quantity": n, "take_profit": tps[i]}
            for i, n in enumerate(sizes)]
    real_risk = quantity * per_share
    fees = {}
    for key, plan in PLANS.items():
        # Pire cas pour les frais : tous les brackets sortent (au TP ou au SL), 2 ordres chacun.
        worst = trade_commission(sizes, entry, [stop] * len(sizes), plan)
        best = trade_commission(sizes, entry, tps[:len(sizes)], plan)
        single = trade_commission([quantity], entry, [stop], plan)
        cost = max(worst, best)
        fees[key] = {"usd": round(cost, 2), "r": round(cost / real_risk, 3),
                     "un_bracket_usd": round(single, 2), "un_bracket_r": round(single / real_risk, 3)}
    cheapest = min(fees, key=lambda k: fees[k]["usd"])
    return {
        "risk_eur": risk_eur, "risk_usd": round(risk_usd, 2), "eurusd": eurusd,
        "quantity": quantity, "notional_usd": round(quantity * entry, 2), "real_risk_usd": round(real_risk, 2),
        "entry": entry, "stop": stop, "side": p["side"], "brackets": rows,
        "fees": fees, "plan": PLAN if PLAN in PLANS else "fixe", "cheapest": cheapest,
    }


async def eur_usd() -> float | None:
    """Taux BCE du jour (Frankfurter), gardé 12 h ; dernier taux connu si la source tombe."""
    if _fx["rate"] and time.time() - _fx["at"] < FX_TTL:
        return _fx["rate"]
    try:
        rate = (await frankfurter.fx_rates("EUR", ["USD"]))["rates"]["USD"]
        if rate:
            _fx.update(rate=rate, at=time.time())
    except Exception as exc:
        log.warning("Taux EUR/USD indisponible (%s: %s)", type(exc).__name__, exc)
    return _fx["rate"]


async def attach(p: dict, risk_eur: float | None = None) -> None:
    """Ajoute p["ibkr"] à un plan d'action. Jamais d'exception : plan inchangé en cas de souci."""
    if p.get("market") != "stock":
        return
    grade = p.get("grade") or {}
    if grade and grade.get("risque_propose", 1) <= 0:
        return  # le grade arrête la journée : pas de taille proposée
    try:
        risk_eur = RISK_EUR if risk_eur is None else risk_eur
        rate = await eur_usd()
        risk_usd = risk_eur * rate if rate else risk_eur
        out = brackets(p, risk_usd, risk_eur, rate)
        if out is not None:
            p["ibkr"] = out
    except Exception as exc:
        log.warning("Brackets IBKR non calculés (%s: %s)", type(exc).__name__, exc)


def _px(x: float) -> str:
    return f"{x:.4f}".rstrip("0").rstrip(".") if x < 1 else f"{x:.2f}"


def lines(p: dict) -> list[str]:
    """Le bloc « à saisir chez IBKR » pour Discord."""
    b = p.get("ibkr")
    if not b:
        return []
    fx = f"{b['risk_usd']:g} $ au taux BCE {b['eurusd']:.4f}" if b["eurusd"] else f"{b['risk_usd']:g} $, taux EUR/USD indisponible"
    if not b["quantity"]:
        return [f"🧾 IBKR · risque {b['risk_eur']:g} € ({fx}) : {b['raison']}"]
    sl = "SL" if b["side"] == "long" else "SL (rachat)"
    out = [f"🧾 **IBKR · {b['quantity']} actions** ({_px(b['notional_usd'])} $) · perte au SL "
           f"{b['real_risk_usd']:.2f} $ (max {b['risk_eur']:g} € = {fx})"]
    for i, r in enumerate(b["brackets"], start=1):
        out.append(f"-# Bracket {i} : {r['quantity']} @ entrée {_px(b['entry'])} · {sl} {_px(b['stop'])} · "
                   f"{r['name']} {_px(r['take_profit'])}")
    f, mine = b["fees"], b["plan"]
    other = "degressif" if mine == "fixe" else "fixe"
    tip = (f" · en {LABELS[other]} : {f[other]['usd']:.2f} $ ({f[other]['r']:.2f} R)"
           if f[other]["usd"] < f[mine]["usd"] else "")
    out.append(f"-# Commissions estimées ({LABELS[mine]}, {len(b['brackets'])} brackets) : "
               f"{f[mine]['usd']:.2f} $ = {f[mine]['r']:.2f} R{tip} · 1 seul bracket : {f[mine]['un_bracket_usd']:.2f} $")
    return out
