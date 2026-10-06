"""Simulateur du labo : journées construites à la main dont on connaît l'issue."""
from datetime import date, datetime, timezone

import pytest

from app.lab import collector as CO
from app.lab import simulator as SIM
from app.lab import universe as U

DAY = date(2026, 10, 6)          # mardi, séance normale
OPEN, CLOSE = CO.session_ts(DAY)
DECIDED = U.stock_decision_ts(DAY)
ZERO = {"slippage": 0.0, "maker": 0.0, "taker": 0.0}


def plan(entry=100.0, stop=98.0, tps=(102.0, 104.0, 106.0), side="long"):
    return {"side": side, "entry": entry, "stop": stop, "targets": [{"price": p} for p in tps]}


def session(rows, start=OPEN):
    """rows : (o, h, l, c) ; une bougie de 5 min par ligne à partir de l'ouverture."""
    return [{"t": start + 300 * i, "o": o, "h": h, "l": lo, "c": c, "v": 1000.0} for i, (o, h, lo, c) in enumerate(rows)]


def run(bars, p=None, last=101.0, cost=ZERO, market="stock", exit_at=CLOSE, decided=DECIDED, until=None):
    return SIM.simulate(p or plan(), bars, market=market, decided_at=decided, entry_until=until or exit_at,
                        exit_at=exit_at, last_price=last, cost=cost)


def test_entree_jamais_atteinte():
    r = run(session([(101, 101.5, 100.5, 101)] * 10))
    assert not r["triggered"] and r["exit_reason"] == "non_declenche" and r["r_net"] is None


def test_entree_touchee_mais_pas_traversee():
    r = run(session([(101, 101.5, 100.0, 101), (101, 101.2, 99.995, 101)] + [(101, 101.5, 100.5, 101)] * 5))
    assert not r["triggered"]  # 100,00 puis 99,995 : il faut traverser d'un pas (0,01)


def test_entree_traversee_d_un_pas():
    r = run(session([(101, 101.5, 99.99, 100.5)] + [(100.5, 101, 100.2, 100.8)] * 5))
    assert r["triggered"] and r["fill_price"] == 100.0 and r["entry_mode"] == "limite"


def test_entree_au_marche_a_l_ouverture():
    r = run(session([(99.5, 100.2, 99.3, 100)] * 3), last=100.0)
    assert r["triggered"] and r["entry_mode"] == "ouverture" and r["fill_price"] == 99.5
    assert r["filled_at"] == OPEN and r["minutes_to_fill"] == 0.0  # 9 h 10 → 9 h 30 : aucune minute de cotation


def test_entree_au_marche_jamais_au_dessus_de_la_limite():
    # Ouverture en gap au-dessus de l'entrée : l'ordre limité reste posé, exécuté plus tard à 100.
    r = run(session([(101, 101.5, 100.5, 101), (100.5, 100.6, 99.9, 100.2)] + [(100.2, 100.5, 99.5, 100)] * 3),
            last=100.0)
    assert r["triggered"] and r["fill_price"] == 100.0 and r["filled_at"] == OPEN + 300


def test_ordre_pose_rempli_a_l_ouverture_en_gap_baissier():
    r = run(session([(101, 101.5, 100.5, 101), (99.0, 99.5, 98.8, 99.2)] + [(99.2, 99.5, 99.0, 99.3)] * 3))
    assert r["fill_price"] == 99.0 and r["entry_mode"] == "ouverture"


def test_gap_sous_le_stop_a_l_ouverture():
    r = run(session([(100, 100.5, 99.5, 100), (97.0, 97.5, 96.5, 97.2)]), last=100.0)
    assert r["exit_reason"] == "stop" and r["exits"][0]["price"] == 97.0
    assert r["r_gross"] == pytest.approx(-1.5)


def test_bougie_ambigue_tp_et_stop_compte_stop():
    r = run(session([(100, 100.5, 99.5, 100), (100, 102.5, 97.5, 100)]), last=100.0)
    assert r["exit_reason"] == "stop" and not r["tp_hits"][0]
    assert r["r_gross"] == pytest.approx(-1.0)


def test_tp1_puis_stop_a_l_entree():
    bars = session([(100, 100.5, 99.5, 100), (100, 102.5, 100.2, 102), (102, 102.2, 101, 101.2),
                    (101, 101.1, 99.8, 100)])
    cost = {"slippage": 0.0003, "maker": 0.0, "taker": 0.0}
    r = run(bars, last=100.0, cost=cost)
    assert r["exit_reason"] == "stop_be" and r["tp_hits"] == [True, False, False]
    assert [e["reason"] for e in r["exits"]] == ["tp1", "stop_be"]
    assert r["r_gross"] == pytest.approx((1 / 3 * 2 + 2 / 3 * 0) / 2, abs=1e-4)
    fill = 100 * 1.0003
    net = (1 / 3 * (102 * 0.9997 - fill) + 2 / 3 * (100 * 0.9997 - fill)) / 2
    assert r["r_net"] == pytest.approx(net, abs=1e-4) and r["r_net"] < r["r_gross"]


def test_trois_tp():
    bars = session([(100, 100.5, 99.5, 100), (100, 102.5, 100.1, 102), (102, 104.5, 101.5, 104),
                    (104, 106.5, 103.5, 106)])
    r = run(bars, last=100.0)
    assert r["exit_reason"] == "tp3" and r["tp_hits"] == [True, True, True]
    assert r["r_gross"] == pytest.approx((2 + 4 + 6) / 3 / 2)
    assert r["minutes_to_tp"] == [7.5, 12.5, 17.5]  # milieu des bougies de 9 h 35, 9 h 40, 9 h 45 ; 9 h 10-9 h 30 ne compte pas


def test_tp_touche_sans_etre_traverse_ne_compte_pas():
    bars = session([(100, 100.5, 99.5, 100), (100, 102.0, 99.8, 101.5)] + [(101.5, 101.8, 101, 101.5)] * 3)
    r = run(bars, last=100.0)
    assert not r["tp_hits"][0] and r["exit_reason"] == "cloture"


def test_tp_et_retour_a_l_entree_dans_la_meme_bougie():
    r = run(session([(100, 100.5, 99.5, 100), (100, 102.5, 99.9, 100)]), last=100.0)
    assert r["tp_hits"][0] and r["exit_reason"] == "stop_be"


def test_entree_en_cours_de_bougie_ignore_son_plus_haut():
    r = run(session([(101, 103.0, 99.9, 101)] + [(101, 101.5, 100.5, 101)] * 3))
    assert r["triggered"] and not r["tp_hits"][0]


def test_cloture_de_seance():
    rows = [(100, 100.5, 99.5, 100)] + [(100, 101, 99.5, 100.5)] * 77
    r = run(session(rows), last=100.0)
    assert len(rows) == 78 and r["exit_reason"] == "cloture"
    assert r["exits"][-1]["at"] == CLOSE - 300 and r["exits"][-1]["price"] == 100.5
    assert not r["data_incomplete"]


def test_cloture_a_13h_un_jour_court():
    day = date(2026, 11, 27)  # lendemain de Thanksgiving : clôture à 13 h
    open_, close = CO.session_ts(day)
    assert (close - open_) / 60 == 210
    rows = [(100, 100.5, 99.5, 100)] + [(100, 101, 99.5, 100.5)] * 41 + [(100.5, 110, 100, 109)] * 6
    bars = session(rows, start=open_)
    r = run(bars, last=100.0, exit_at=close, decided=U.stock_decision_ts(day))
    assert r["exit_reason"] == "cloture" and r["exits"][-1]["at"] == close - 300 and not r["tp_hits"][2]


def test_horizon_crypto():
    t0 = int(datetime(2026, 10, 6, 8, tzinfo=timezone.utc).timestamp())
    rows = [(100, 100.5, 99.5, 100)] + [(100, 101, 99.5, 100.7)] * (288 + 20)
    bars = session(rows, start=t0)
    r = run(bars, market="crypto", decided=t0, exit_at=t0 + 86400, last=100.0)
    assert r["exit_reason"] == "horizon" and r["exits"][-1]["at"] == t0 + 86400 - 300
    assert r["exits"][-1]["price"] == 100.7


def test_couts():
    assert SIM.stock_slippage_bps(50) == 3 and SIM.stock_slippage_bps(10) == 6
    assert SIM.stock_slippage_bps(2) == 15 and SIM.stock_slippage_bps(0.5) == 40
    assert SIM.costs("crypto", 1.0, 3) == {"slippage": 0.0003, "maker": 0.0008, "taker": 0.001}
    assert SIM.costs("crypto", 1.0, 30)["slippage"] == 0.0008
    assert SIM.costs("stock", 30.0) == {"slippage": 0.0003, "maker": 0.0, "taker": 0.0}
    t0 = int(datetime(2026, 10, 6, 8, tzinfo=timezone.utc).timestamp())
    bars = session([(100, 100.5, 99.5, 100), (100, 106.5, 99.9, 106)], start=t0)
    okx = SIM.costs("crypto", 100.0, 3)
    r = run(bars, market="crypto", decided=t0, exit_at=t0 + 86400, last=100.0, cost=okx)
    assert r["exit_reason"] == "tp3" and r["r_gross"] == pytest.approx(2.0)
    # Entrée au marché : taker 0,10 % ; TP posés : maker 0,08 %.
    assert r["entry_liquidity"] == "taker" and [e["fee"] for e in r["exits"]] == [0.0008] * 3
    fill = 100 * 1.0013
    expected = sum((p * (1 - 0.0011) - fill) / 3 for p in (102, 104, 106)) / 2
    assert r["r_net"] == pytest.approx(expected, abs=1e-4)


def test_frais_crypto_entree_posee_maker_et_stop_taker():
    t0 = int(datetime(2026, 10, 6, 8, tzinfo=timezone.utc).timestamp())
    bars = session([(101, 101.5, 99.9, 100.5), (100.5, 100.6, 97.5, 98)], start=t0)
    okx = SIM.costs("crypto", 100.0, 30)  # hors top 10 : 8 pb de glissement
    r = run(bars, market="crypto", decided=t0, exit_at=t0 + 86400, last=101.0, cost=okx)
    assert r["entry_liquidity"] == "maker" and r["exit_reason"] == "stop" and r["exits"][0]["fee"] == 0.001
    fill = 100 * (1 + 0.0008 + 0.0008)
    assert r["r_net"] == pytest.approx((98 * (1 - 0.0008 - 0.001) - fill) / 2, abs=1e-4)


def test_mfe_mae():
    bars = session([(100, 100.5, 99.0, 100), (100, 101.5, 99.5, 101), (101, 101.2, 100.5, 100.8)])
    r = run(bars, last=100.0)
    assert r["mfe_r"] == pytest.approx(0.75) and r["mae_r"] == pytest.approx(-0.5)


def test_mae_bornee_au_stop():
    r = run(session([(100, 100.5, 99.5, 100), (99.5, 99.6, 95.0, 96)]), last=100.0)
    assert r["exit_reason"] == "stop" and r["mae_r"] == pytest.approx(-1.0)


def test_minutes_de_cotation_a_cheval_sur_deux_seances():
    friday_1550 = int(datetime(2026, 10, 2, 15, 50, tzinfo=CO.NEW_YORK).timestamp())
    monday_0940 = int(datetime(2026, 10, 5, 9, 40, tzinfo=CO.NEW_YORK).timestamp())
    assert SIM._minutes("stock", friday_1550, monday_0940) == 20.0
    assert SIM._minutes("crypto", friday_1550, monday_0940) == (monday_0940 - friday_1550) / 60


def test_simulateur_refuse_le_passe():
    with pytest.raises(ValueError):
        run(session([(100, 100.5, 99.5, 100)], start=DECIDED - 300))


def test_prix_d_ordre_arrondis_au_pas_de_cotation():
    r = run(session([(100, 100.5, 99.5, 100)] * 2), p=plan(entry=100.004, stop=98.0071), last=100.0)
    assert r["order_prices"]["entry"] == 100.0 and r["order_prices"]["stop"] == 98.01


def test_amplitude_du_jour():
    assert SIM.day_range_pct(session([(100, 102, 99, 101), (101, 103, 100, 102)])) == pytest.approx(4.040, abs=1e-3)
    assert SIM.day_range_pct([]) is None
