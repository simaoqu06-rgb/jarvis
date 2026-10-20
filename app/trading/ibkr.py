"""Plans prêts à saisir chez IBKR : la taille du plan en brackets par TP et commissions estimées.

Simão (2026-10-08) : 20 € de perte maximale par trade (JARVIS_RISK_EUR). La quantité est la taille du plan
(service.apply_grade, règle unique : risque en € = min(capital × % du grade, JARVIS_RISK_EUR), au change EUR/USD
du jour, actions entières), puis la position est découpée en un bracket IBKR par TP (même entrée, même SL ; un TP
touché ne ferme que sa part). Un bracket IBKR n'ayant qu'un TP, chaque part a son ordre d'entrée et son ordre de
sortie : 2 ordres par bracket, donc 2 minimums de commission.

Commissions IBKR Pro estimées au barème de Killmode (killmode.IBKR_PLANS et order_commission ; plan du compte :
JARVIS_IBKR_PLAN ; comparatif : /mnt/project-files/trading/plateforme-actions-us.md) :
- Fixe : 0,005 $ par action, minimum 1 $, plafond 1 % de la valeur de l'ordre ;
- Dégressif : 0,0035 $ par action, minimum 0,35 $, plafond 1 %, plus frais de place (≈ 0,003 $ par
  action en prenant la liquidité) et de compensation (0,0002 $), estimation haute.
Frais réglementaires à la vente (SEC, FINRA) négligés : quelques centimes.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

LABELS = {"fixe": "Fixe", "degressif": "Dégressif"}


def trade_commission(sizes: list[int], entry: float, exits: list[float], plan: dict) -> float:
    """Aller-retour de tous les brackets : une entrée et une sortie par bracket."""
    from app.trading import killmode as K  # import tardif : killmode importe service, qui importe ce module

    return sum(K.order_commission(n, entry, plan) + K.order_commission(n, x, plan) for n, x in zip(sizes, exits))


def brackets(p: dict) -> dict | None:
    """Brackets IBKR d'un plan d'action (long ou short) à sa taille en euros (`sizing`), ou None sans elle."""
    from app.trading import killmode as K  # import tardif : killmode importe service, qui importe ce module

    s = p.get("sizing") or {}
    entry, stop = p["entry"], p["stop"]
    per_share = abs(entry - stop)
    if not per_share or p.get("market") != "stock" or s.get("risk_amount_eur") is None:
        return None
    risk_eur, risk_usd, eurusd, quantity = s["risk_amount_eur"], s["risk_amount"], s.get("eurusd"), s["quantity"]
    if quantity < 1:
        return {"risk_eur": risk_eur, "risk_usd": risk_usd, "eurusd": eurusd, "quantity": 0,
                "raison": f"stop trop loin : 1 action risque déjà {per_share:.2f} $"}
    tps = [t["price"] for t in p["targets"]] or [entry]
    sizes = K._brackets(quantity, len(tps))
    rows = [{"name": p["targets"][i]["name"] if p["targets"] else "TP", "quantity": n, "take_profit": tps[i]}
            for i, n in enumerate(sizes)]
    real_risk = quantity * per_share
    fees = {}
    for key, plan in K.IBKR_PLANS.items():
        # Pire cas pour les frais : tous les brackets sortent (au TP ou au SL), 2 ordres chacun.
        worst = trade_commission(sizes, entry, [stop] * len(sizes), plan)
        best = trade_commission(sizes, entry, tps[:len(sizes)], plan)
        single = trade_commission([quantity], entry, [stop], plan)
        cost = max(worst, best)
        fees[key] = {"usd": round(cost, 2), "r": round(cost / real_risk, 3),
                     "un_bracket_usd": round(single, 2), "un_bracket_r": round(single / real_risk, 3)}
    cheapest = min(fees, key=lambda k: fees[k]["usd"])
    return {
        "risk_eur": risk_eur, "risk_usd": risk_usd, "eurusd": eurusd,
        "quantity": quantity, "notional_usd": round(quantity * entry, 2), "real_risk_usd": round(real_risk, 2),
        "entry": entry, "stop": stop, "side": p["side"], "brackets": rows,
        "fees": fees, "plan": K.IBKR_PLAN if K.IBKR_PLAN in K.IBKR_PLANS else "fixe", "cheapest": cheapest,
    }


def attach(p: dict) -> None:
    """Ajoute p["ibkr"] à un plan d'action. Jamais d'exception : plan inchangé en cas de souci."""
    if p.get("market") != "stock":
        return
    grade = p.get("grade") or {}
    if grade and grade.get("risque_propose", 1) <= 0:
        return  # le grade arrête la journée : pas de taille proposée
    try:
        out = brackets(p)
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
    fx = f"{b['risk_usd']:g} $ au change {b['eurusd']:.4f}" if b["eurusd"] else f"{b['risk_usd']:g} $, taux EUR/USD indisponible"
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
