"""Brackets IBKR : risque fixe en euros, quantité entière, commissions Fixe et Dégressif."""
import asyncio

from app.trading import ibkr
from app.trading import service as S


def _plan(entry=5.0, stop=4.5, tps=(5.5, 6.0, 6.5), market="stock", side="long"):
    return {"symbol": "TEST", "market": market, "side": side, "entry": entry, "stop": stop,
            "targets": [{"name": f"TP{i}", "price": x} for i, x in enumerate(tps, start=1)]}


def test_quantite_decoule_du_stop_et_du_risque_en_euros():
    p = _plan()
    asyncio.run(ibkr.attach(p, risk_eur=20))
    b = p["ibkr"]
    assert b["risk_usd"] == 22.0  # 20 € × 1,10
    assert b["quantity"] == 44  # 22 $ / 0,50 $ par action, arrondi en dessous
    assert b["real_risk_usd"] <= b["risk_usd"]
    assert [r["quantity"] for r in b["brackets"]] == [15, 15, 14]
    assert [r["take_profit"] for r in b["brackets"]] == [5.5, 6.0, 6.5]


def test_commissions_minimums_par_ordre():
    b = ibkr.brackets(_plan(), 22.0, 20, 1.1)
    # Fixe : 6 ordres de moins de 100 $, chacun plafonné à 1 % de sa valeur (sous le minimum de 1 $),
    # pire cas = tous les TP touchés ; un seul bracket de 44 actions : 2 × 1 $.
    assert b["fees"]["fixe"]["usd"] == 4.83
    assert b["fees"]["fixe"]["un_bracket_usd"] == 2.0
    assert b["fees"]["degressif"]["usd"] < b["fees"]["fixe"]["usd"]
    assert b["cheapest"] == "degressif"


def test_plafond_un_pour_cent_sur_les_actions_a_quelques_centimes():
    assert ibkr.order_commission(100, 0.5, ibkr.FIXED) == 0.5  # 1 % de 50 $ < minimum de 1 $


def test_stop_trop_loin_pour_une_action():
    b = ibkr.brackets(_plan(entry=500.0, stop=470.0, tps=(530.0,)), 22.0, 20, 1.1)
    assert b["quantity"] == 0 and "stop trop loin" in b["raison"]


def test_moins_d_actions_que_de_tp():
    b = ibkr.brackets(_plan(entry=10.0, stop=0.0), 22.0, 20, 1.1)
    assert b["quantity"] == 2 and [r["quantity"] for r in b["brackets"]] == [1, 1]


def test_crypto_et_grade_a_zero_sans_brackets():
    c = _plan(market="crypto")
    asyncio.run(ibkr.attach(c))
    assert "ibkr" not in c
    g = _plan() | {"grade": {"risque_propose": 0}}
    asyncio.run(ibkr.attach(g))
    assert "ibkr" not in g


def test_lignes_discord():
    p = _plan()
    asyncio.run(ibkr.attach(p, risk_eur=20))
    text = "\n".join(ibkr.lines(p))
    assert "44 actions" in text and "Bracket 3 : 14 @ entrée 5.00" in text
    assert "en Dégressif" in text  # le moins cher est signalé quand le plan du compte est le Fixe
    assert S.IBKR is ibkr
