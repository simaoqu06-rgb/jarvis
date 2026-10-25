"""Plans prêts à saisir chez IBKR : la taille du plan en brackets par TP et commissions estimées.

Simão (2026-10-08) : 20 € de perte maximale par trade (JARVIS_RISK_EUR). La quantité est la taille du plan
(service.apply_grade, règle unique : risque en € = min(capital × % du grade, JARVIS_RISK_EUR), au change EUR/USD
du jour, actions entières), puis la position est découpée en un bracket IBKR par TP (même entrée, même SL ; un TP
touché ne ferme que sa part). Un bracket IBKR n'ayant qu'un TP, chaque part a son ordre d'entrée et son ordre de
sortie : 2 ordres par bracket, donc 2 minimums de commission. Avec moins d'actions que de TP, chaque bracket vise
l'objectif du milieu de sa part (1 action : TP2 ; 2 actions : TP1 et TP3), comme killmode.bracket_orders et la mesure
du labo à 20 €. Quand le grade propose 0 %, JARVIS ne propose pas de taille : le bloc suit alors ta règle de 20 €
(`sizing_regle`), marqué « ta règle, hors avis de JARVIS », comme la ligne 💰.

Commissions IBKR Pro estimées au barème de Killmode (killmode.IBKR_PLANS et order_commission ; plan du compte :
JARVIS_IBKR_PLAN ; comparatif : /mnt/project-files/trading/plateforme-actions-us.md) :
- Fixe : 0,005 $ par action, minimum 1 $, plafond 1 % de la valeur de l'ordre ;
- Dégressif : 0,0035 $ par action, minimum 0,35 $, plafond 1 %, plus frais de place (≈ 0,003 $ par
  action en prenant la liquidité) et de compensation (0,0002 $), estimation haute.
Frais réglementaires à la vente (SEC, FINRA) négligés : quelques centimes.

Une seule sortie par défaut depuis le 25/10/2026 (single_exit, choix de Simão) : un seul bracket de toute la position
sur TP2 (l'objectif du milieu), TP1 et TP3 restant des repères sans ordre. Critère fixé avant la mesure avec GPT et
Gemini (au moins +0,02 R par trade au Dégressif contre un bracket par objectif, IC au-dessus de zéro), tenu sur les GO
confirmés de l'export qu'ils ont recalculé : go_confirme_v2 +0,036 R [+0,021 ; +0,051], go_confirme +0,032 R
[+0,020 ; +0,043] (1 078 et 2 380 trades). Les deux gestions comparées gardent le stop fixe (sortie de référence des
GO : r_sans_be), la règle de Simão pour une sortie à TP2. Le gain vient des frais évités (+0,048 à +0,057 R) ; vendre
tout à TP2 plutôt que par tiers coûte un peu sur le chemin (−0,012 à −0,021 R). JARVIS_IBKR_BRACKETS=3 rend un
bracket par objectif.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

LABELS = {"fixe": "Fixe", "degressif": "Dégressif"}


def single_exit() -> bool:
    """Un seul bracket sur TP2 (défaut) ; JARVIS_IBKR_BRACKETS=3 : un bracket par objectif."""
    return (os.getenv("JARVIS_IBKR_BRACKETS") or "1").strip() != "3"


def single_target(p: dict) -> int | None:
    """Indice de l'objectif de la sortie unique d'un plan (1 : TP2), None pour un bracket par objectif : sortie
    unique désactivée, plan sans trois objectifs (la mesure ne porte que sur eux), crypto, ou KILL (son journal
    mesure un bracket par objectif)."""
    if (not single_exit() or p.get("killmode") or p.get("market", "stock") != "stock"
            or len(p.get("targets") or []) != 3):
        return None
    return 1


def trade_commission(sizes: list[int], entry: float, exits: list[float], plan: dict) -> float:
    """Aller-retour de tous les brackets : une entrée et une sortie par bracket."""
    from app.trading import killmode as K  # import tardif : killmode importe service, qui importe ce module

    return sum(K.order_commission(n, entry, plan) + K.order_commission(n, x, plan) for n, x in zip(sizes, exits))


def brackets(p: dict) -> dict | None:
    """Brackets IBKR d'un plan d'action (long ou short) à sa taille en euros (`sizing`), ou None sans elle."""
    from app.trading import killmode as K  # import tardif : killmode importe service, qui importe ce module

    # Grade à 0 % : JARVIS ne propose pas de taille ; les brackets suivent alors ta règle de 20 € (`sizing_regle`),
    # dite comme telle, comme la ligne 💰 (service.sizing_lines).
    s = p.get("sizing") or p.get("sizing_regle") or {}
    rule = not p.get("sizing") and bool(p.get("sizing_regle"))
    entry, stop = p["entry"], p["stop"]
    per_share = abs(entry - stop)
    if not per_share or p.get("market") != "stock" or s.get("risk_amount_eur") is None:
        return None
    risk_eur, risk_usd, eurusd, quantity = s["risk_amount_eur"], s["risk_amount"], s.get("eurusd"), s["quantity"]
    if quantity < 1:
        return {"risk_eur": risk_eur, "risk_usd": risk_usd, "eurusd": eurusd, "quantity": 0, "hors_avis": rule,
                "raison": f"stop trop loin : 1 action risque déjà {per_share:.2f} $"}
    tps = [t["price"] for t in p["targets"]] or [entry]
    names = [t["name"] for t in p["targets"]] or ["TP"]
    # Chaque bracket vise l'objectif du milieu de sa part (1 action : TP2 ; 2 actions : TP1 et TP3), comme
    # killmode.bracket_orders et la mesure du labo à 20 € (r_20e, `!frais`).
    parts = [{"prix": x, "part": 1 / len(tps), "raison": i} for i, x in enumerate(tps)]
    legs = K._legs(quantity, len(tps), parts)
    per_target = [{"name": names[e["raison"]], "quantity": n, "take_profit": e["prix"]} for n, e in legs]
    k = single_target(p)
    rows = per_target if k is None else [{"name": names[k], "quantity": quantity, "take_profit": tps[k]}]
    real_risk = quantity * per_share

    def cost(rws: list[dict], plan: dict) -> float:
        # Pire cas pour les frais : tous les brackets sortent (au TP ou au SL), 2 ordres chacun.
        sizes = [r["quantity"] for r in rws]
        return max(trade_commission(sizes, entry, [stop] * len(sizes), plan),
                   trade_commission(sizes, entry, [r["take_profit"] for r in rws], plan))

    fees = {}
    for key, plan in K.IBKR_PLANS.items():
        mine, one = cost(rows, plan), trade_commission([quantity], entry, [stop], plan)
        split = cost(per_target, plan)
        fees[key] = {"usd": round(mine, 2), "r": round(mine / real_risk, 3),
                     "un_bracket_usd": round(one, 2), "un_bracket_r": round(one / real_risk, 3),
                     "par_objectif_usd": round(split, 2), "par_objectif_r": round(split / real_risk, 3)}
    cheapest = min(fees, key=lambda k: fees[k]["usd"])
    return {
        "risk_eur": risk_eur, "risk_usd": risk_usd, "eurusd": eurusd,
        "quantity": quantity, "notional_usd": round(quantity * entry, 2), "real_risk_usd": round(real_risk, 2),
        "entry": entry, "stop": stop, "side": p["side"], "brackets": rows, "hors_avis": rule,
        "sortie_unique": k is not None, "reperes": [] if k is None else [n for i, n in enumerate(names) if i != k],
        "fees": fees, "plan": K.IBKR_PLAN if K.IBKR_PLAN in K.IBKR_PLANS else "fixe", "cheapest": cheapest,
    }


def attach(p: dict) -> None:
    """Ajoute p["ibkr"] à un plan d'action. Jamais d'exception : plan inchangé en cas de souci."""
    if p.get("market") != "stock":
        return
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
    rule = " · ta règle, hors avis de JARVIS" if b.get("hors_avis") else ""
    if not b["quantity"]:
        return [f"🧾 IBKR · risque {b['risk_eur']:g} € ({fx}) : {b['raison']}{rule}"]
    sl = "SL" if b["side"] == "long" else "SL (rachat)"
    out = [f"🧾 **IBKR · {b['quantity']} actions** ({_px(b['notional_usd'])} $) · perte au SL "
           f"{b['real_risk_usd']:.2f} $ (max {b['risk_eur']:g} € = {fx}){rule}"]
    for i, r in enumerate(b["brackets"], start=1):
        out.append(f"-# Bracket {i} : {r['quantity']} @ entrée {_px(b['entry'])} · {sl} {_px(b['stop'])} · "
                   f"{r['name']} {_px(r['take_profit'])}")
    f, mine = b["fees"], b["plan"]
    other = "degressif" if mine == "fixe" else "fixe"
    tip = (f" · en {LABELS[other]} : {f[other]['usd']:.2f} $ ({f[other]['r']:.2f} R)"
           if f[other]["usd"] < f[mine]["usd"] else "")
    if b.get("sortie_unique"):
        out.append(f"-# Sortie unique sur {b['brackets'][0]['name']} ({' et '.join(b['reperes'])} : repères, sans ordre) : "
                   "stop fixe jusque-là, comme au labo : +0,03 à +0,04 R par trade face à un bracket par objectif, tout "
                   "en frais évités (écart relatif, pas un gain net)")
        out.append(f"-# Commissions estimées ({LABELS[mine]}, 1 bracket) : {f[mine]['usd']:.2f} $ = {f[mine]['r']:.2f} R"
                   f"{tip} · un bracket par objectif : {f[mine]['par_objectif_usd']:.2f} $")
        return out
    out.append(f"-# Commissions estimées ({LABELS[mine]}, {len(b['brackets'])} brackets) : "
               f"{f[mine]['usd']:.2f} $ = {f[mine]['r']:.2f} R{tip} · 1 seul bracket : {f[mine]['un_bracket_usd']:.2f} $")
    return out
