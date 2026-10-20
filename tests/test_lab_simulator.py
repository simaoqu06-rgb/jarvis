"""Simulateur du labo : journées construites à la main dont on connaît l'issue."""
import random
import statistics
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


# --- Résolution des bougies ambiguës (journal de Killmode, 8 octobre) -------------------------------------

def minutes(rows, start=OPEN):
    return [{"t": start + 60 * i, "o": o, "h": h, "l": lo, "c": c, "v": 1000.0} for i, (o, h, lo, c) in enumerate(rows)]


def run_stop(rows, resolution):
    return SIM.simulate(plan(), minutes(rows), market="stock", decided_at=OPEN, entry_until=CLOSE, exit_at=CLOSE,
                        last_price=99.5, cost=ZERO, entry_order="stop", bar_seconds=60, resolution=resolution)


def r_of(rows):
    return {k: run_stop(rows, k)["r_gross"] for k in SIM.RESOLUTIONS}


def test_resolution_v1_inchangee_et_inconnue_refusee():
    assert "ambigus" not in run_stop([(99.5, 100.5, 99.4, 100.2)], "v1")
    with pytest.raises(ValueError):
        run_stop([(99.5, 100.5, 99.4, 100.2)], "optimiste")


def test_entree_et_stop_dans_la_bougie_d_entree():
    rows = [(99.5, 100.5, 97.5, 99.0)] + [(99.5, 99.5, 99.5, 99.5)] * 3  # le plus bas a pu précéder l'exécution
    assert r_of(rows) == {"v1": -1.0, "prudente": -1.0, "favorable": -0.25}
    assert run_stop(rows, "prudente")["ambigus"] == [{"t": OPEN, "cas": "entree_et_stop"}]
    assert r_of([(99.5, 100.5, 97.5, 97.8)] + [(97.8,) * 4] * 3)["favorable"] == -1.0  # clôture sous le stop : certain


def test_objectif_dans_la_bougie_d_entree():
    # Elle ouvre sous l'entrée : son plus haut vient après l'exécution, TP1 est certain ; le retour à l'entrée non.
    r = r_of([(99.5, 102.5, 99.4, 102.2)] + [(102.2,) * 4] * 3)
    assert r["v1"] == pytest.approx(1.1) and r["prudente"] == pytest.approx(1 / 3, abs=1e-3)
    assert r["favorable"] == pytest.approx(1 / 3 + 2 / 3 * 1.1, abs=1e-3)


def test_plusieurs_objectifs_puis_retour_a_l_entree():
    rows = [(99.5, 100.2, 99.4, 100.1), (100.1, 104.5, 99.9, 100.5)] + [(103.0,) * 4] * 3
    assert r_of(rows) == pytest.approx({"v1": 1.0, "prudente": 1 / 3, "favorable": 1.5}, abs=1e-3)
    assert run_stop(rows, "prudente")["ambigus"] == [{"t": OPEN + 60, "cas": "objectif_puis_entree"}]
    # Clôture à l'entrée : le retour vient après le plus haut, certain même dans l'ordre favorable (troisième avis).
    rows = [(99.5, 100.2, 99.4, 100.1), (100.1, 104.5, 99.9, 100.0)] + [(103.0,) * 4] * 3
    assert r_of(rows) == pytest.approx({"v1": 1.0, "prudente": 1 / 3, "favorable": 1.0}, abs=1e-3)


def test_stop_et_objectif_apres_l_entree():
    rows = [(99.5, 100.2, 99.4, 100.1), (100.1, 102.5, 97.9, 100.0)] + [(100.0,) * 4] * 3
    assert r_of(rows) == pytest.approx({"v1": -1.0, "prudente": -1.0, "favorable": 1 / 3}, abs=1e-3)
    assert run_stop(rows, "favorable")["ambigus"] == [{"t": OPEN + 60, "cas": "stop_et_objectif"}]


def test_objectif_au_prix_de_l_ordre():
    rows = [(99.5, 100.2, 99.4, 100.1), (102.5, 102.8, 102.4, 102.6)] + [(102.6,) * 4] * 3
    assert run_stop(rows, "v1")["exits"][0]["price"] == 102.5  # le labo : à l'ouverture au-delà
    assert run_stop(rows, "prudente")["exits"][0]["price"] == 102.0  # posé, il s'exécute à son prix
    gap = run_stop([(102.5, 102.8, 102.3, 102.6)] + [(102.6,) * 4] * 3, "prudente")
    assert gap["fill_price"] == 102.5 and gap["exits"][0]["price"] == 102.5  # posé au-delà : au prix d'entrée


# --- Troisième avis de GPT et Gemini (8 octobre) : pas entiers, tous les ordres, transactions -------------------

def test_objectif_d_un_pas_en_pas_entiers():
    """16,01 + 0,01 vaut 16,020000000000003 en flottants : un plus haut à 16,02 ne remplissait pas un objectif à 16,01.
    Le journal compare en pas entiers ; le labo (v1) reste tel quel jusqu'à sa déclaration."""
    p = plan(entry=15.0, stop=14.0, tps=(16.01, 17.0, 18.0))
    rows = [(14.9, 15.05, 14.9, 15.0), (15.5, 16.02, 15.4, 16.0)] + [(16.0,) * 4] * 3
    kw = dict(market="stock", decided_at=OPEN, entry_until=CLOSE, exit_at=CLOSE, last_price=14.9, cost=ZERO,
              entry_order="stop", bar_seconds=60)
    assert SIM.simulate(p, minutes(rows), resolution="prudente", **kw)["tp_hits"][0]
    assert not SIM.simulate(p, minutes(rows), resolution="v1", **kw)["tp_hits"][0]
    two = SIM.simulate(p, minutes(rows), resolution="prudente", tp_ticks=2, **kw)
    assert not two["tp_hits"][0]  # stress de Gemini : 2 pas au-delà
    rows[1] = (15.5, 16.03, 15.4, 16.0)
    assert SIM.simulate(p, minutes(rows), resolution="prudente", tp_ticks=2, **kw)["tp_hits"][0]


def test_autre_ordre_dans_une_minute_choisie():
    """`flip` joue une minute dans l'autre ordre : le pire ordre minute par minute n'est pas le pire résultat."""
    rows = [(99.5, 100.3, 97.9, 100.1), (100.1, 100.15, 99.6, 99.7), (96.0, 96.2, 95.8, 96.1)] + [(96.1,) * 4] * 3
    base = run_stop(rows, "prudente")
    assert base["exit_reason"] == "stop" and base["r_gross"] == -1.0  # entrée et stop dans la minute : stoppé
    other = SIM.simulate(plan(), minutes(rows), market="stock", decided_at=OPEN, entry_until=CLOSE, exit_at=CLOSE,
                         last_price=99.5, cost=ZERO, entry_order="stop", bar_seconds=60, resolution="prudente",
                         flip=frozenset({OPEN}))
    assert other["exits"][0]["price"] == 96.0 and other["r_gross"] == -2.0  # le plus bas avant : gap plus tard


def test_objectif_depasse_a_l_ouverture_est_certain():
    """Une minute qui ouvre au-delà d'un objectif l'exécute avant tout le reste : ce qui suit ne peut plus le défaire
    (relecture du troisième avis). Avant, l'objectif comptait comme ambigu, voire comme un stop."""
    flat = [(96.0,) * 4] * 3
    rows = [(99.5, 100.2, 99.4, 100.1), (102.5, 102.6, 99.9, 100.5), (96.0, 96.2, 95.8, 96.0)] + flat
    r = run_stop(rows, "prudente")
    assert [(e["reason"], e["price"]) for e in r["exits"]] == [("tp1", 102.0), ("stop_be", 100.0)]
    assert r["ambigus"] == [] and r["r_gross"] == pytest.approx(1 / 3, abs=1e-3)
    r = run_stop([(99.5, 100.2, 99.4, 100.1), (102.5, 102.6, 97.5, 97.8)] + flat, "prudente")  # puis sous le stop
    assert [(e["reason"], e["price"]) for e in r["exits"]] == [("tp1", 102.0), ("stop_be", 100.0)]
    r = run_stop([(106.5, 106.6, 99.9, 100.5)] + flat, "prudente")  # achat à l'ouverture au-delà des trois objectifs
    assert r["exit_reason"] == "tp3" and r["r_gross"] == 0.0 and {e["price"] for e in r["exits"]} == {106.5}
    assert run_stop(rows, "v1")["exits"] == SIM.simulate(
        plan(), minutes(rows), market="stock", decided_at=OPEN, entry_until=CLOSE, exit_at=CLOSE, last_price=99.5,
        cost=ZERO, entry_order="stop", bar_seconds=60)["exits"]  # le labo : inchangé


def test_troisieme_ordre_objectifs_puis_entree():
    """`("be", t)` dans `flip` : l'achat, tous les objectifs traversés, puis le retour à l'entrée dans la minute."""
    rows = [(99.5, 104.5, 97.5, 100.5), (95.0, 95.2, 94.8, 95.0)] + [(95.0,) * 4] * 3
    kw = dict(market="stock", decided_at=OPEN, entry_until=CLOSE, exit_at=CLOSE, last_price=99.5, cost=ZERO,
              entry_order="stop", bar_seconds=60, resolution="prudente")
    got = {k: SIM.simulate(plan(), minutes(rows), flip=frozenset(f), **kw)["r_gross"]
           for k, f in (("pire", ()), ("autre", {OPEN}), ("be", {("be", OPEN)}))}
    assert got == pytest.approx({"pire": -1.0, "autre": 1 / 6, "be": 1.0}, abs=1e-3)


def test_bougie_d_une_transaction_transmet_son_bid_et_son_ask():
    bars = [{"t": OPEN, "o": 99.9, "h": 99.9, "l": 99.9, "c": 99.9, "v": 100.0, "ns": 1, "bid": 99.8, "ask": 99.95},
            {"t": OPEN, "o": 100.2, "h": 100.2, "l": 100.2, "c": 100.2, "v": 100.0, "ns": 2, "bid": 100.1, "ask": 100.3},
            {"t": OPEN + 60, "o": 97.5, "h": 97.5, "l": 97.5, "c": 97.5, "v": 100.0, "ns": 3, "bid": 97.4, "ask": 97.6}]
    r = SIM.simulate(plan(), bars, market="stock", decided_at=OPEN, entry_until=CLOSE, exit_at=CLOSE, last_price=99.5,
                     cost=ZERO, entry_order="stop", bar_seconds=60, resolution="prudente")
    assert r["fill_price"] == 100.2 and r["fill_tick"] == {"ns": 2, "bid": 100.1, "ask": 100.3}
    assert r["exits"][0]["price"] == 97.5 and r["exits"][0]["bid"] == 97.4 and r["exits"][0]["ns"] == 3
    assert "fill_tick" not in run_stop([(99.5, 100.5, 99.4, 100.2)], "prudente")  # bougies ordinaires : rien


# --- Déclaration n° 10 (10 octobre 2026) : révision 2 du simulateur, celle du labo v2 -------------------------------

def run2(rows, p=None, last=100.0, order="limit", sim_rev=2, **kw):
    return SIM.simulate(p or plan(), session(rows), market="stock", decided_at=DECIDED, entry_until=CLOSE,
                        exit_at=CLOSE, last_price=last, cost=ZERO, entry_order=order, sim_rev=sim_rev, **kw)


def test_n10_revision_1_par_defaut_et_revision_inconnue_refusee():
    rows = [(99.5, 102.5, 99.4, 102.2)] + [(102.2,) * 4] * 3
    assert run2(rows, last=99.5, order="stop", sim_rev=1) == SIM.simulate(
        plan(), session(rows), market="stock", decided_at=DECIDED, entry_until=CLOSE, exit_at=CLOSE, last_price=99.5,
        cost=ZERO, entry_order="stop")
    with pytest.raises(ValueError):
        run2(rows, sim_rev=3)


def test_n10_objectif_et_limite_d_un_pas_en_pas_entiers():
    """I1 : en révision 2, un pas exact suffit, en v1 comme ailleurs ; la révision 1 (Labo 1) ne bouge pas."""
    p = plan(entry=15.0, stop=14.0, tps=(16.01, 17.0, 18.0))
    rows = [(14.9, 15.05, 14.9, 15.0), (15.5, 16.02, 15.4, 16.0)] + [(16.0,) * 4] * 3
    kw = dict(market="stock", decided_at=OPEN, entry_until=CLOSE, exit_at=CLOSE, last_price=14.9, cost=ZERO,
              entry_order="stop", bar_seconds=60)
    assert SIM.simulate(p, minutes(rows), sim_rev=2, **kw)["tp_hits"][0]
    assert not SIM.simulate(p, minutes(rows), **kw)["tp_hits"][0]
    # Bougie suivante qui ouvre à 16,02, un pas au-dessus de l'objectif : sortie à l'ouverture.
    rows = [(14.9, 15.05, 14.9, 15.0), (16.02, 16.05, 16.0, 16.03)] + [(16.03,) * 4] * 3
    assert SIM.simulate(p, minutes(rows), sim_rev=2, **kw)["exits"][0]["price"] == 16.02
    assert SIM.simulate(p, minutes(rows), **kw)["exits"][0]["price"] == 16.01
    # Limite d'achat à 16,02, plus bas à 16,01 : exécutée, en v1 comme en prudente.
    buy = plan(entry=16.02, stop=15.5, tps=(16.5, 17.0, 17.5))
    rows = [(16.2, 16.25, 16.01, 16.1)] + [(16.1,) * 4] * 3
    for resolution in ("v1", "prudente"):
        r = run2(rows, p=buy, last=16.2, resolution=resolution)
        assert r["triggered"] and r["entry_mode"] == "limite" and r["fill_price"] == 16.02
    assert not run2(rows, p=buy, last=16.2, sim_rev=1)["triggered"]
    # Vente : limite à 16,01, plus haut à 16,02.
    sell = plan(entry=16.01, stop=16.5, tps=(15.5, 15.0, 14.5), side="short")
    rows = [(15.9, 16.02, 15.85, 15.95)] + [(15.95,) * 4] * 3
    r = run2(rows, p=sell, last=15.9)
    assert r["triggered"] and r["entry_mode"] == "limite" and r["fill_price"] == 16.01
    assert not run2(rows, p=sell, last=15.9, sim_rev=1)["triggered"]


def test_n10_un_pas_exact_suffit_a_tous_les_cents():
    """I1, propriété : de 1 à 1 000 $, un pas exact suffit toujours, 0,99 pas jamais. Le simulateur l'applique à
    l'objectif et à la limite (chaque cent jusqu'à 100 $, un cent sur sept au-delà, pour rester rapide)."""
    assert [c for c in range(100, 100_001) if not SIM._beyond((c + 1) / 100, c / 100, 0.01)
            or SIM._beyond((c + 0.99) / 100, c / 100, 0.01)] == []

    def hit(c, sim_rev=2):
        p = plan(entry=1.0, stop=0.5, tps=(c / 100, (c + 500) / 100, (c + 900) / 100))
        bar = [{"t": OPEN, "o": 1.0, "h": (c + 1) / 100, "l": 1.0, "c": (c + 1) / 100, "v": 1.0}]
        return SIM.simulate(p, bar, market="stock", decided_at=OPEN, entry_until=CLOSE, exit_at=OPEN + 300,
                            last_price=1.0, cost=ZERO, entry_order="market", sim_rev=sim_rev)["tp_hits"][0]

    def filled(c):
        e = (c + 1) / 100
        bar = [{"t": OPEN, "o": e + 0.05, "h": e + 0.05, "l": c / 100, "c": e, "v": 1.0}]
        return SIM.simulate(plan(entry=e, stop=e / 2, tps=(2 * e, 3 * e, 4 * e)), bar, market="stock",
                            decided_at=OPEN, entry_until=CLOSE, exit_at=OPEN + 300, last_price=e + 0.05, cost=ZERO,
                            sim_rev=2)["triggered"]

    cents = [*range(101, 10_001), *range(10_001, 100_001, 7)]
    assert [c for c in cents if not hit(c)] == [] and [c for c in cents if not filled(c)] == []
    assert not hit(1601, sim_rev=1)  # 16,01 + 0,01 > 16,02 en flottants : la révision 1 le manque


def test_n10_bougie_d_entree_symetrique_pour_l_ordre_stop():
    """L2 : un ordre stop rempli en cours de bougie compte l'objectif qu'elle traverse et son plus haut, comme une
    entrée au marché à l'ouverture sur le même chemin. Le retour à l'entrée y reste supposé si la clôture l'atteint."""
    p = plan(entry=10.0, stop=9.8, tps=(10.2, 10.4, 10.6))
    after = [(10.15, 10.15, 9.7, 9.7)] + [(9.7,) * 4] * 2
    r = run2([(9.95, 10.3, 9.9, 10.15)] + after, p=p, last=9.95, order="stop")
    assert r["entry_mode"] == "stop" and r["tp_hits"] == [True, False, False] and r["exit_reason"] == "stop_be"
    assert r["r_gross"] == pytest.approx(1 / 3, abs=1e-4) and r["mfe_r"] > 0
    old = run2([(9.95, 10.3, 9.9, 10.15)] + after, p=p, last=9.95, order="stop", sim_rev=1)
    assert old["exit_reason"] == "stop" and old["r_gross"] == -1.0  # révision 1 : objectif ignoré
    market = run2([(10.0, 10.3, 10.0, 10.15)] + after, p=p, last=10.0, order="market")
    assert market["entry_mode"] == "ouverture" and market["r_gross"] == r["r_gross"]
    # test_objectif_dans_la_bougie_d_entree en révision 2 : TP1 compté, le reste soldé à 102,2 ; prudente inchangée.
    rows = [(99.5, 102.5, 99.4, 102.2)] + [(102.2,) * 4] * 3
    kw = dict(market="stock", decided_at=OPEN, entry_until=CLOSE, exit_at=CLOSE, last_price=99.5, cost=ZERO,
              entry_order="stop", bar_seconds=60, sim_rev=2)
    assert SIM.simulate(plan(), minutes(rows), **kw)["r_gross"] == pytest.approx(1 / 3 + 2 / 3 * 1.1, abs=1e-3)
    assert SIM.simulate(plan(), minutes(rows), resolution="prudente", **kw)["r_gross"] == pytest.approx(1 / 3, abs=1e-3)


def test_n10_temoin_objectif_dans_sa_premiere_bougie():
    """L2b : acheté au marché à l'ouverture, TP1 dans la première bougie qui clôture au-dessus de l'entrée : le retour
    à l'entrée n'est plus supposé (la révision 1 le supposait toujours, son plus bas étant l'ouverture)."""
    first = (100.0, 102.5, 100.0, 102.2)
    up = [(102.2, 104.5, 102.0, 104.2), (104.2, 106.5, 104.0, 106.3)] + [(106.3,) * 4] * 2
    r = run2([first] + up, order="market")
    assert r["exit_reason"] == "tp3" and r["r_gross"] == pytest.approx(2.0)
    assert run2([first] + up, order="market", sim_rev=1)["exit_reason"] == "stop_be"
    flat = run2([(100.0, 102.5, 100.0, 100.0)] + up, order="market")  # clôture à l'entrée : retour certain
    assert flat["exit_reason"] == "stop_be" and flat["r_gross"] == pytest.approx(1 / 3, abs=1e-4)
    assert run2([first] + up, order="market", resolution="prudente")["r_gross"] == pytest.approx(1 / 3, abs=1e-4)
    stop = run2([(99.5, 102.5, 99.5, 102.2)] + up, last=99.5, order="stop")  # même chemin, ordre stop
    assert stop["entry_mode"] == "stop" and stop["r_gross"] == r["r_gross"]


def test_n10_objectif_deja_depasse_au_remplissage():
    """L4 (« tp_at_fill ») : acheté à l'ouverture au-dessus des objectifs, il les vend au prix d'achat, pas dessous."""
    p = plan(entry=10.3, stop=9.9, tps=(10.05, 10.1, 10.2))
    rows = [(10.3, 10.35, 10.25, 10.3)] + [(10.3,) * 4] * 3
    r = run2(rows, p=p, last=10.3, order="market")
    assert r["exit_reason"] == "tp3" and {e["price"] for e in r["exits"]} == {10.3} and r["r_gross"] == 0.0
    assert run2(rows, p=p, last=10.3, order="market", sim_rev=1)["r_gross"] == pytest.approx(-0.4583, abs=1e-4)


def test_be_tp2_stop_a_l_entree_apres_le_tp2():
    """X1 : « be_tp2 » (la règle que suit Simão) ne remonte le stop qu'après le TP2, quelle que soit la révision."""
    tp1_entree_stop = [(100, 100.5, 99.5, 100), (100, 102.5, 100.2, 102), (102, 102.2, 99.9, 100.2),
                       (100.2, 100.3, 97.5, 97.8)]
    tp1_tp2_entree = [(100, 100.5, 99.5, 100), (100, 102.5, 100.2, 102), (102, 104.5, 101.5, 104),
                      (104, 104.2, 99.0, 99.5), (99.5, 99.6, 97.5, 97.8)]
    tp2_et_entree = [(100, 100.5, 99.5, 100), (100, 102.5, 100.2, 102), (102, 104.5, 99.9, 100.2),
                     (100.2, 100.3, 97.5, 97.8)]
    for sim_rev in (1, 2):
        got = {m: run2(tp1_entree_stop, sim_rev=sim_rev, exit_mode=m) for m in ("v1", "be_tp2", "sans_be")}
        assert (got["v1"]["exit_reason"], got["v1"]["r_gross"]) == ("stop_be", pytest.approx(1 / 3, abs=1e-4))
        for m in ("be_tp2", "sans_be"):
            assert (got[m]["exit_reason"], got[m]["r_gross"]) == ("stop", pytest.approx(1 / 3 - 2 / 3, abs=1e-4))
        got = {m: run2(tp1_tp2_entree, sim_rev=sim_rev, exit_mode=m) for m in ("v1", "be_tp2", "sans_be")}
        assert (got["be_tp2"]["exit_reason"], got["be_tp2"]["r_gross"]) == ("stop_be", pytest.approx(1.0))
        assert got["v1"]["r_gross"] == pytest.approx(1.0) and got["sans_be"]["r_gross"] == pytest.approx(2 / 3, abs=1e-4)
        same_bar = run2(tp2_et_entree, sim_rev=sim_rev, exit_mode="be_tp2")  # retour dans la bougie du TP2
        assert (same_bar["exit_reason"], same_bar["r_gross"]) == ("stop_be", pytest.approx(1.0))


def _walk(rng, n_bars, m, p0=5000):
    """Marche aléatoire de ±1 cent par transaction, `m` transactions par bougie : sans avantage, et rien ne saute un
    niveau (pas de dépassement à l'exécution). Prix en cents."""
    pts = [p0]
    for _ in range(n_bars * m):
        pts.append(pts[-1] + (1 if rng.random() < 0.5 else -1))
    bars = []
    for i in range(n_bars):
        seg = pts[i * m:(i + 1) * m + 1]
        bars.append({"t": OPEN + 300 * i, "o": seg[0] / 100, "h": max(seg) / 100, "l": min(seg) / 100,
                     "c": seg[-1] / 100, "v": 1.0})
    return pts, bars


def _truth(pts, j, entry, risk):
    """R brut sur le chemin transaction par transaction depuis l'exécution (indice `j`), règles de la v1."""
    stop_now, hit, total = entry - risk, 0, 0.0
    for p in pts[j + 1:]:
        if p <= stop_now:
            return total + (3 - hit) / 3 * (stop_now - entry) / risk
        if p >= entry + (hit + 1) * risk + 1:  # objectif traversé d'un pas
            hit += 1
            total += hit / 3
            stop_now = entry
            if hit == 3:
                return total
    return total + (3 - hit) / 3 * (pts[-1] - entry) / risk


def _ecart_ordre_stop_temoin(sim_rev, k_sigma, n=1200, m=25, n_bars=16, seed=3):
    """Écart moyen des biais (simulé − vrai) entre un ordre stop posé 1 cent au-dessus de l'ouverture (valable une
    bougie) et le témoin acheté au marché à cette ouverture, appariés sur le même chemin ; stop à `k_sigma` écarts-types
    d'une bougie (√m cents), objectifs à 1, 2 et 3 R."""
    rng = random.Random(seed)
    risk = round(k_sigma * m ** 0.5)
    end = OPEN + 300 * n_bars
    kw = dict(market="stock", decided_at=OPEN, exit_at=end, cost=ZERO, sim_rev=sim_rev)

    def plan_at(e):
        return plan(entry=e / 100, stop=(e - risk) / 100, tps=[(e + k * risk) / 100 for k in (1, 2, 3)])

    diffs = []
    for _ in range(n):
        pts, bars = _walk(rng, n_bars, m)
        o = pts[0]
        s = SIM.simulate(plan_at(o + 1), bars, entry_until=OPEN + 300, last_price=o / 100, entry_order="stop", **kw)
        if not s["triggered"]:
            continue
        w = SIM.simulate(plan_at(o), bars, entry_until=end, last_price=o / 100, entry_order="market", **kw)
        j = next(j for j, p in enumerate(pts) if p >= o + 1)
        diffs.append((s["r_gross"] - _truth(pts, j, o + 1, risk)) - (w["r_gross"] - _truth(pts, 0, o, risk)))
    return statistics.fmean(diffs), len(diffs)


def test_n10_controle_de_non_biais_sur_marche_aleatoire():
    """Contrôle de non-biais (L2b, recommandé) : sans avantage, le simulateur doit traiter l'ordre stop et le témoin
    pareil. On compare leurs biais (simulé − vrai, le vrai rejoué transaction par transaction) sur le même chemin :
    c'est l'écart stratégie − témoin que le labo lirait sous l'hypothèse nulle, avec beaucoup moins de bruit.

    Tolérance : 0,02 R, environ 2,5 erreurs-types de l'écart apparié (0,008 R pour ~1 000 trades). Sur huit graines,
    stop à 1,5 σ de 5 min, la révision 2 reste entre −0,017 et +0,001 R ; la révision 1 entre +0,025 et +0,079 R (L2 et
    L2b poussaient dans le même sens, pour la stratégie). Stop serré (1 σ) : il reste un biais connu, contre l'ordre
    stop (« entree_et_stop » : un plus bas sous le stop dans la bougie d'exécution compte −1 R alors qu'il a pu venir
    avant), de −0,04 à −0,06 R ; il peut cacher un avantage, pas en inventer un."""
    rev2, n = _ecart_ordre_stop_temoin(2, 1.5)
    rev1, _ = _ecart_ordre_stop_temoin(1, 1.5)
    assert n > 900 and abs(rev2) <= 0.02 and rev1 >= 0.03
    tight2, _ = _ecart_ordre_stop_temoin(2, 1.0)
    tight1, _ = _ecart_ordre_stop_temoin(1, 1.0)
    assert tight1 >= 0.05 and -0.1 < tight2 < 0
