"""Frais estimés d'un trade suivi (`!prends`), en R du risque prévu : aucune exécution réelle n'est lue.

Actions : barème IBKR de Killmode (killmode.IBKR_PLAN, minimum et plafond par ordre, order_commission), à la
taille du trade en actions entières (risque de sa taille en € × change noté avec elle, sinon les 20 € de Killmode
à 1,15), un bracket par objectif réparti comme le journal de Killmode (killmode._legs) : une commission à l'entrée
de chaque bracket et une à sa sortie (objectif ou stop) ; une sortie à la main (`!sortie`, cockpit) part en un
seul ordre : les frais de killmode._at_size, sans le gain ni le glissement. Crypto : frais du labo (taker,
SIM.costs) à l'entrée et à la sortie, sur le prix d'entrée.

Pas de strat_stats.ibkr_commission_r : sans le minimum de 1 $ par ordre, il sous-estime les frais d'environ 50 fois
(0,005 R au lieu de 0,27 R pour un 100/98) et ne répond pas pour un short.
"""
import math

from app.lab import simulator as SIM
from app.trading import killmode as K


def risk_usd(trade: dict) -> float:
    """Risque du trade en $ : celui de sa taille (`sizing`, sinon ta règle `sizing_regle` quand JARVIS n'en
    proposait pas), en € × le change noté avec elle ; une taille sans euros (ancienne, `risk_amount` en € du
    capital) au change de Killmode ; sans taille, les RISK_EUR de Killmode."""
    for s in (trade.get("sizing"), trade.get("sizing_regle")):
        if not isinstance(s, dict):
            continue
        if s.get("risk_amount_eur"):
            return s["risk_amount_eur"] * (s.get("eurusd") or K.EURUSD_DEFAULT)
        if s.get("risk_amount"):
            return s["risk_amount"] * K.EURUSD_DEFAULT
    return K.RISK_EUR * K.EURUSD_DEFAULT


def shares(trade: dict, risk: float) -> int:
    """Actions du trade, entières, même arrondi que killmode.shares : son risque en $ sur le risque par action. Au
    moins une (un trade pris l'a été avec au moins une action)."""
    return max(1, math.floor(risk_usd(trade) / risk + 1e-9))


def _exits(trade: dict) -> list[dict]:
    """Sortie de chaque tiers : prix de l'objectif touché (celui de l'événement, corrigé par `!sortie`), sinon
    celui de la dernière sortie (stop ou clôture)."""
    legs = len(trade["targets"])
    hit = min(trade.get("targets_hit", 0), legs)
    events = trade.get("events") or []
    prices = {e["kind"]: e["price"] for e in events}
    out = [{"prix": prices.get(f"tp{i}", t["price"]), "raison": f"tp{i}", "part": 1 / legs}
           for i, t in enumerate(trade["targets"][:hit], start=1)]
    last = next((e for e in reversed(events) if e["kind"] in ("stop", "close")), None)
    rest = {"prix": last["price"] if last else trade.get("last_price", trade["entry"]),
            "raison": "cloture" if last and last["kind"] == "close" else "stop", "part": 1 / legs}
    return out + [rest] * (legs - hit)


def fees_r(trade: dict) -> float | None:
    """Frais estimés d'un trade terminé, en R du risque prévu ; None s'il n'est pas terminé."""
    risk = abs(trade["entry"] - trade["stop"])
    if trade.get("status") != "closed" or not trade.get("targets") or not risk:
        return None
    if trade.get("market", "stock") == "crypto":
        return round(2 * SIM.costs("crypto", trade["entry"])["taker"] * trade["entry"] / risk, 4)
    q = shares(trade, risk)
    plan = K.IBKR_PLANS.get(K.IBKR_PLAN, K.IBKR_PLANS["fixe"])
    legs = K._legs(q, len(trade["targets"]), _exits(trade))
    usd = sum(K.order_commission(n, trade["entry"], plan) for n, _ in legs)
    usd += sum(K.order_commission(n, e["prix"], plan) for n, e in legs if e["raison"] != "cloture")
    closing = [(n, e["prix"]) for n, e in legs if e["raison"] == "cloture"]
    if closing:  # TP et stops restants annulés, le reste vendu en un seul ordre : un seul minimum
        usd += K.order_commission(sum(n for n, _ in closing), closing[0][1], plan)
    return round(usd / (q * risk), 4)
