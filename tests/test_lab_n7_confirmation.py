"""Labo v2, déclaration n° 7 : confirmation des GO du scanner (PR #37) rejouée contre le GO brut, avec le stop
d'origine et le plancher du direct (PR #36)."""
import asyncio
from bisect import bisect_left
from datetime import date, datetime, timezone

import pytest

from app.lab import collector as CO
from app.lab import generator as G
from app.lab import rattrapage as RAT
from app.lab import simulator as SIM
from app.lab import strat_stats as SS
from app.lab import strategies as ST
from app.lab import universe as U
from app.lab.db import LabDB
from app.trading import confirm as CF
from app.trading import service as T
from tests.test_killmode import daily, hist
from tests.test_lab_generator import DAY as GEN_DAY
from tests.test_lab_generator import S, full_db  # noqa: F401  (fixture)
from tests.test_lab_strategies import DAY, OPEN, bars_from

PLAN = {"entry": 100.0, "stop": 98.0, "targets": [{"price": 103.0}, {"price": 105.0}, {"price": 107.0}]}
FLAT = (103.0, 103.1, 102.9, 103.0)
# 9 h 30 au-dessus de l'entrée, repli à 9 h 35, reprise du VWAP sur la bougie de 9 h 45 (finie à 9 h 50),
# déclenchement à 9 h 50, TP1 à 9 h 55, puis plat jusqu'à la clôture.
CONFIRMED = [(101.0, 101.5, 100.6, 101.2), (101.2, 101.3, 99.8, 100.0), (100.0, 100.3, 99.5, 99.9),
             (99.9, 101.0, 99.8, 100.9), (100.9, 101.5, 100.8, 101.4), (101.4, 103.2, 101.3, 103.0)]


def session(rows, n=78, pad=FLAT):
    return rows + [pad] * (n - len(rows))


def make(rows):
    return ST.Day("stock", DAY, CO.regular_hours(hist())[-ST.STOCK_WARMUP_BARS:] + bars_from(OPEN, session(rows)),
                  daily())


def by_name(rows):
    return {r["strategy"]: r for r in rows}


def at(hour, minute):
    return int(datetime.combine(DAY, datetime.min.time().replace(hour=hour, minute=minute), CO.NEW_YORK).timestamp())


# --- Les deux bras --------------------------------------------------------------------------

def test_confirme_rejoue_la_regle_du_direct():
    d = make(CONFIRMED)
    ev = ST.confirm_event(d, 100.0, 98.0, [103.0, 105.0, 107.0])
    assert ev["kind"] == "confirmé" and ev["t"] == OPEN + 15 * 60 and ev["trigger"] == 101.01
    rows = by_name(ST.go_rows(d, "AAA", {"origine": PLAN}, 3, 1.0))
    assert set(rows) == {"go_brut", "go_confirme", "go_confirme_v2", "go_confirme_v3", "go_confirme_v4"}  # n° 8
    brut, conf = rows["go_brut"], rows["go_confirme"]
    assert brut["confirmation"] == conf["confirmation"] == "confirme"
    # Brut : au marché à l'ouverture (101), stop et objectifs du plan, sortie stop fixe.
    assert brut["triggered"] == 1 and brut["entry"] == 101.0 and brut["filled_at"] == OPEN
    assert brut["r_gross"] == pytest.approx(2 / 3, abs=1e-4)          # TP1 à 103, reste à 103 à la clôture
    # Confirmé : ordre stop d'achat à 101,01, posé à 9 h 50, exécuté sur la bougie de 9 h 50.
    assert conf["triggered"] == 1 and conf["entry"] == 101.01 and conf["signal_at"] == OPEN + 20 * 60
    assert conf["filled_at"] == OPEN + 20 * 60 and conf["entry_mode"] == "stop"
    assert conf["r_gross"] == pytest.approx(1.99 / 3.01, abs=1e-4)
    for r in (brut, conf):
        assert r["r_net"] is None and r["r_sans_be"] is not None   # seule la sortie déclarée
        assert r["r_glisse_x3"] < r["r_glisse_x2"] < r["r_sans_be"] and r["r_manque"] == r["r_sans_be"]
        assert r["temoin_triggered"] == 1 and r["vol15_adv"] is None and r["stop_avant_entree"] is None


def test_sortie_stop_fixe_comme_le_simulateur():
    d = make(CONFIRMED)
    conf = by_name(ST.go_rows(d, "AAA", {"origine": PLAN}, 3))["go_confirme"]
    p = ST._plan(101.01, 98.0, [103.0, 105.0, 107.0])
    until = at(11, 30)
    ref = ST._sim(d, p, OPEN + 20 * 60, until, d.close_ts, SIM.costs("stock", 101.01, 3), "stop", 100.9,
                  exit_mode="sans_be")
    assert conf["r_sans_be"] == ref["r_net"] and conf["exit_reason"] == ref["exit_reason"] == "cloture"


def test_annule_si_tp1_touche_ou_cloture_sous_le_stop():
    tp1_first = [(101.0, 103.5, 100.9, 102.0)] + CONFIRMED[1:]
    rows = by_name(ST.go_rows(make(tp1_first), "AAA", {"origine": PLAN}))
    assert rows["go_confirme"]["confirmation"] == "annule" and rows["go_confirme"]["triggered"] == 0
    assert rows["go_confirme"]["signal_at"] == OPEN + 5 * 60 and rows["go_brut"]["triggered"] == 1
    below = [(101.0, 101.2, 97.0, 97.5)] + CONFIRMED[1:]
    rows = by_name(ST.go_rows(make(below), "AAA", {"origine": PLAN}))
    assert rows["go_confirme"]["confirmation"] == "annule" and rows["go_brut"]["exit_reason"] == "stop"


def test_brut_sans_trade_si_l_ouverture_est_sous_le_stop():
    gap_down = [(97.5, 97.8, 97.2, 97.6)] + CONFIRMED[1:]
    rows = by_name(ST.go_rows(make(gap_down), "AAA", {"origine": PLAN}))
    assert rows["go_brut"]["triggered"] == 0 and rows["go_brut"].get("r_sans_be") is None
    assert rows["go_confirme"]["confirmation"] == "annule"     # clôture 97,6 sous le stop


def test_expire_a_11h30_et_ordre_annule_apres():
    # Repli à 9 h 35, clôtures sous le VWAP jusqu'à 11 h 20 ; la bougie de 11 h 25 (finie à 11 h 30) reprend le VWAP.
    under = [(101.0, 101.2, 100.8, 101.0)] + [(100.0, 100.1, 99.9, 99.95)] * 22
    late = under + [(99.95, 100.6, 99.9, 100.5), (100.5, 101.0, 100.4, 100.9)]
    d = make(late)
    assert d.t[d.d0 + 23] + ST.BAR == at(11, 30)
    go = {"status": "attente", "entry": 100.0, "stop": 98.0, "targets": [103.0, 105.0, 107.0], "last_bar": None}
    full = CF.evaluate(go, d.bars[d.d0:d.d0 + 24], datetime.fromtimestamp(OPEN, timezone.utc))
    assert full["kind"] == "confirmé"                                   # lue à 11 h 30, elle confirmerait…
    assert ST.confirm_event(d, 100.0, 98.0, [103.0, 105.0, 107.0]) is None   # … mais le direct a expiré avant
    rows = by_name(ST.go_rows(d, "AAA", {"origine": PLAN}))
    assert rows["go_confirme"]["confirmation"] == "expire" and rows["go_confirme"]["signal_at"] == at(11, 30)
    # Confirmé sur la bougie de 11 h 20 (finie à 11 h 25), déclencheur 100,61 atteint seulement à 11 h 30 : l'ordre
    # n'est plus valable (message du direct), pas de trade.
    early = under[:-1] + [(99.95, 100.6, 99.9, 100.5), (100.5, 100.55, 100.4, 100.5), (100.5, 101.0, 100.4, 100.9)]
    rows = by_name(ST.go_rows(make(early), "AAA", {"origine": PLAN}))
    conf = rows["go_confirme"]
    assert conf["confirmation"] == "confirme" and conf["entry"] == 100.61 and conf["triggered"] == 0


def test_plans_go_par_stop():
    floor = {"entry": 100.0, "stop": 97.0, "targets": [{"price": 104.0}, {"price": 106.0}, {"price": 108.0}]}
    assert ST.go_plans(None) == {} and ST.go_plans({"plan": PLAN, "go": False}) == {}
    assert ST.go_plans({"plan": PLAN, "go": True, "plancher": {"plan": floor, "go": False}}) == {"origine": PLAN}
    assert ST.go_plans({"plan": PLAN, "go": False, "plancher": {"plan": floor, "go": True}}) == {"plancher": floor}
    rows = ST.go_rows(make(CONFIRMED), "AAA", {"plancher": floor})
    assert sorted(r["strategy"] for r in rows) == ["go_brut_plancher", "go_confirme_plancher"]
    assert all(r["stop"] == 97.0 for r in rows)
    # stock_day : les bras viennent avec la ligne v1, rien sans GO.
    v1 = {"plan": PLAN, "decided_at": OPEN - 20 * 60, "entry_until": d_close(), "last_price": 100.0,
          "cost": SIM.costs("stock", 100.0), "go": True, "plancher": None}
    names = {r["strategy"] for r in ST.stock_day(DAY, "AAA", hist(), bars_from(OPEN, session(CONFIRMED)), daily(), 3, v1)}
    assert {"v1", "go_brut", "go_confirme"} <= names and "go_brut_plancher" not in names
    names = {r["strategy"] for r in ST.stock_day(DAY, "AAA", hist(), bars_from(OPEN, session(CONFIRMED)), daily(), 3,
                                                 v1 | {"go": False})}
    assert "v1" in names and not names & set(ST.GO_STRATEGIES)


def d_close():
    return CO.session_ts(DAY)[1]


def test_rattrapage_lit_le_go_et_le_plan_du_direct():
    plan = {"entry": 100.0, "stop": 98.0, "targets": [{"price": 103.0}], "score": {"go": True}}
    v = RAT._v1("stock", {"planned_at": 1, "plan": plan}, 2, 5)
    assert v["go"] is True and v["plancher"] is None
    plan["plancher"] = {"eloigne": True, "entry": 100.0, "stop": 97.0, "targets": [104.0, 106.0], "go": True}
    v = RAT._v1("stock", {"planned_at": 1, "plan": plan}, 2, 5)
    assert v["plancher"] == {"plan": {"entry": 100.0, "stop": 97.0, "targets": [{"price": 104.0}, {"price": 106.0}]},
                             "go": True}
    assert ST.go_plans(v) == {"origine": v["plan"], "plancher": v["plancher"]["plan"]}


# --- Plan du direct dans le générateur ------------------------------------------------------

def _live_plan(monkeypatch, inp, now):
    by_interval = {15: inp["bars"], 1440: inp["daily"], 240: inp["h4"]}

    async def fake_candles(symbol, market, interval, source=None, max_age=None, outputsize=None):
        return by_interval[interval]

    async def no_cat(symbol, now):
        return None

    monkeypatch.setattr(T, "candles", fake_candles)
    monkeypatch.setattr(T.C, "safe_check", no_cat)
    monkeypatch.delenv("JARVIS_CAPITAL", raising=False)
    return asyncio.run(T.build_plan("AAA", "stock", now=now, risk_pct=T.RISK_PCT))


def test_plan_du_direct_identique_a_build_plan(full_db, monkeypatch):  # noqa: F811
    monkeypatch.setattr(T, "STOP_FLOOR_DAILY_ATR", 3.0)   # plancher forcé : le stop est éloigné
    inp = G.stock_inputs(full_db, "AAA", GEN_DAY)
    now = datetime.fromtimestamp(U.stock_decision_ts(GEN_DAY), timezone.utc)
    plan, floored = G.stock_plans("AAA", inp, T.settings)
    live = _live_plan(monkeypatch, inp, now)
    assert {k: v for k, v in live.items() if k != "grade"} == floored     # même code, même plan, même note
    assert floored["stop"] < plan["stop"] and "1/3 de l'ATR journalier" in floored["stop_source"]
    assert G.make_plan("AAA", "stock", inp["bars"], inp["daily"], inp["h4"], inp["moment"], T.settings) == plan
    summary = G.floor_summary(plan, floored)
    assert summary["eloigne"] and summary["stop"] == floored["stop"] and summary["go"] == floored["score"]["go"]


def test_generateur_garde_le_plan_du_direct(full_db, monkeypatch):  # noqa: F811
    monkeypatch.setattr(T, "STOP_FLOOR_DAILY_ATR", 0.0)   # pas de plancher : le même plan
    inp = G.stock_inputs(full_db, "AAA", GEN_DAY)
    plan, floored = G.stock_plans("AAA", inp, S)
    assert floored is plan and G.floor_summary(plan, floored)["eloigne"] is False
    monkeypatch.setattr(T, "STOP_FLOOR_DAILY_ATR", 3.0)
    out: list = []
    index5 = full_db.get_bars("stock", "SPY", CO.BASE_TF, *CO.session_ts(GEN_DAY))
    row = G.stock_trade(full_db, "AAA", GEN_DAY, S, {"rank": 1}, None, out, index5)
    floor = row["plan"]["plancher"]
    assert floor["eloigne"] and floor["stop"] < row["plan"]["stop"] and len(floor["targets"]) == 3
    names = {r["strategy"] for r in out}
    assert ("go_brut" in names) == row["plan"]["score"]["go"]
    assert ("go_brut_plancher" in names) == floor["go"]


# --- Tableau --------------------------------------------------------------------------------

def _go_rows(db, days=40):
    statuses = ("confirme", "annule", "expire")
    for k in range(days):
        day = date.fromordinal(date(2025, 1, 6).toordinal() + k).isoformat()
        rows = []
        for j, st in enumerate(statuses):
            wobble = 0.1 * ((j + k) % 3 - 1)
            rows.append((f"S{j}", day, "go_brut", 1, -0.2 + wobble, st))
            rows.append((f"S{j}", day, "go_confirme", int(st == "confirme"),
                         0.5 + wobble if st == "confirme" else None, st))
        db.conn.executemany(
            "INSERT INTO strat_trades (origin, market, symbol, day, strategy, slot, triggered, r_sans_be, r_gross, "
            "r_glisse_x2, r_glisse_x3, r_retard, r_manque, temoin_r, temoin_triggered, confirmation) "
            "VALUES ('replay', 'stock', ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, 0.0, 1, ?)",
            [(s, d, n, t, r, r, r, r, r, r, st) for s, d, n, t, r, st in rows])


def test_tableau_de_la_confirmation():
    db = LabDB(":memory:")
    _go_rows(db)
    rep = SS.report(db.conn)
    items = rep["strategies"]["stock"]
    brut, conf = items["go_brut"], items["go_confirme"]
    assert list(brut["sorties"]) == ["r_sans_be"] and brut["esperance_r"] == pytest.approx(-0.2, abs=1e-3)
    assert brut["declenches"] == 120 and brut["signaux"] == 120 and brut["reussite"] < 0.4
    assert conf["declenches"] == 40 and conf["signaux"] == 120 and conf["reussite"] == 1.0
    assert conf["esperance_r"] == pytest.approx(0.5, abs=0.01) and conf["tranches"] is None
    assert rep["sorties_gemini"] is None and rep["killmode"] is None   # les GO ne touchent pas les mesures de la n° 6
    n7 = rep["confirmation_go"]["origine"]
    assert n7["go"] == 120 and list(n7["par_statut"]) == ["confirmé", "annulé", "expiré"]
    ok = n7["par_statut"]["confirmé"]
    assert ok["go"] == ok["executes"] == 40 and ok["confirme"]["esperance_r"] == pytest.approx(0.5, abs=0.01)
    assert n7["par_statut"]["annulé"]["confirme"] is None
    assert n7["par_statut"]["expiré"]["brut"]["esperance_r"] == pytest.approx(-0.2, abs=0.01)
    diff = n7["ecart_par_go"]
    assert diff["n"] == 120 and diff["esperance_r"] == pytest.approx((0.7 + 0.2 + 0.2) / 3, abs=0.01)
    assert diff["aide"] is (diff["z"] >= SS.candidate_t(diff["jours"]))  # n° 8, D4 : seuil en t
    assert "plancher" not in rep["confirmation_go"]
    text = SS.text(rep)
    assert "Confirmation des GO (n° 7, stop d'origine) : 120 GO" in text and "confirmés 40" in text


def test_la_crypto_garde_son_rattrapage():
    # Les n° 7 et 8 ne touchent que les actions : le rattrapage crypto de la n° 6 continue (rien n'y change). Une
    # déclaration qui ajoute des essais crypto doit remonter CRYPTO_DECLARATION (ce test la rappelle).
    assert ST.progress_job("stock") == "strats:stock:d8" and ST.progress_job("crypto") == "strats:crypto:d6"
    assert len([t for t in ST.trials() if t["market"] == "crypto"]) == 49 and ST.CRYPTO_DECLARATION == 6


def test_reference_des_bras_et_commission():
    assert ST.ref_exit("go_confirme_plancher") == "r_sans_be" and ST.ref_exit("pullback") == "r_net"
    assert list(ST.strategy_exits("stock", "go_brut")) == ["r_sans_be"]
    assert ST.strategy_exits("stock", "orb15") == ST.exits("stock")
    db = LabDB(":memory:")
    _go_rows(db, days=5)
    db.conn.execute("UPDATE strat_trades SET entry = 10.0, stop = 9.9 WHERE strategy = 'go_brut'")
    item = SS.report(db.conn)["strategies"]["stock"]["go_brut"]
    assert item["esperance_ibkr_r"] == pytest.approx(-0.3, abs=1e-3)       # 0,1 R de commission sur 10 cents
    n7 = SS.report(db.conn)["confirmation_go"]["origine"]
    assert n7["par_statut"]["annulé"]["brut"]["esperance_r"] == pytest.approx(-0.18 - 0.1, abs=1e-3)


def test_confirmation_entre_les_bougies_closes_seulement():
    d = make(CONFIRMED)
    k = bisect_left(d.t, OPEN + 15 * 60)
    assert d.vwap[k] == pytest.approx(100.4833, abs=1e-3) and d.c[k] > d.vwap[k]
