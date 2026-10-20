"""Générateur du labo : même code que le direct, aucune donnée postérieure à la décision."""
from datetime import date, datetime, timedelta, timezone

import pytest

from app.lab import collector as CO
from app.lab import generator as G
from app.lab import universe as U
from app.lab.db import LabDB
from app.trading import service as T
from app.trading.settings import DEFAULTS
from tests.lab_fixtures import fill_crypto, fill_stock

DAY = date(2026, 9, 15)
S = dict(DEFAULTS)
STOCKS = ["AAA", "BBB", "SPY"]
CRYPTO = ["BTC-USDT", "ETH-USDT"]


@pytest.fixture(scope="module")
def full_db(tmp_path_factory):
    db = LabDB(str(tmp_path_factory.mktemp("lab") / "full.db"))
    fill_stock(db, STOCKS, CO.sessions_back(DAY, 125), DAY + timedelta(days=3))
    fill_crypto(db, CRYPTO, DAY - timedelta(days=45), DAY + timedelta(days=2))
    return db


def copy_until(src: LabDB, dst: LabDB, market: str, symbols: list[str], moment: int, daily_cutoff: int) -> None:
    for s in symbols:
        dst.put_bars(market, s, 5, [b for b in src.get_bars(market, s, 5, 0, 2**40) if b["t"] + 300 <= moment])
        dst.put_bars(market, s, 1440, [b for b in src.get_bars(market, s, 1440, 0, 2**40) if b["t"] < daily_cutoff])


def test_plan_action_ignore_les_bougies_futures(full_db, tmp_path):
    moment = U.stock_decision_ts(DAY)
    cut = LabDB(str(tmp_path / "cut.db"))
    copy_until(full_db, cut, "stock", ["AAA"], moment, CO.ny_midnight(DAY))
    a = G.stock_inputs(full_db, "AAA", DAY)
    b_bars = CO.aggregate(cut.get_bars("stock", "AAA", 5, 0, 2**40), 15, "stock")[-G.PLAN_BARS["stock"]:]
    assert a["bars"] == b_bars  # les bougies du jour et des jours suivants n'arrivent jamais au plan
    p_full = G.make_plan("AAA", "stock", a["bars"], a["daily"], a["h4"], moment, S)
    daily_cut = cut.get_bars("stock", "AAA", 1440, 0, 2**40)[-G.DAILY_BARS:]
    h4_cut = CO.aggregate(cut.get_bars("stock", "AAA", 5, 0, 2**40), 240, "stock")[-G.H4_BARS["stock"]:]
    p_cut = G.make_plan("AAA", "stock", b_bars, daily_cut, h4_cut, moment, S)
    assert G.compact_plan(p_full) == G.compact_plan(p_cut)
    assert a["session"][0]["t"] == CO.session_ts(DAY)[0] and all(b["t"] >= moment for b in a["session"])


def test_changer_la_seance_change_le_resultat_pas_le_plan(full_db, tmp_path):
    db = LabDB(str(tmp_path / "x.db"))
    copy_until(full_db, db, "stock", ["AAA"], 2**40, 2**40)
    before = G.stock_trade(db, "AAA", DAY, S)
    o, c = CO.session_ts(DAY)
    crash = [b | {"o": b["o"] * 0.5, "h": b["h"] * 0.5, "l": b["l"] * 0.5, "c": b["c"] * 0.5}
             for b in db.get_bars("stock", "AAA", 5, o, c)]
    db.put_bars("stock", "AAA", 5, crash)
    after = G.stock_trade(db, "AAA", DAY, S)
    assert before["plan"] == after["plan"] and after["result"]["exit_reason"] in ("stop", "non_declenche")
    assert before["result"] != after["result"]


def test_plan_crypto_ignore_les_bougies_futures(full_db, tmp_path):
    moment = CO.utc_midnight(DAY) + 8 * 3600
    cut = LabDB(str(tmp_path / "cut.db"))
    copy_until(full_db, cut, "crypto", ["BTC-USDT"], moment, CO.utc_midnight(DAY))
    full = G.crypto_inputs(G.crypto_symbol_day(full_db, "BTC-USDT", DAY), moment)
    part = G.crypto_inputs(G.crypto_symbol_day(cut, "BTC-USDT", DAY), moment)
    for k in ("bars", "h4", "daily", "last"):
        assert full[k] == part[k]
    assert part["sim"] == [] and full["sim"][0]["t"] == moment
    assert full["bars"][-1] == {"t": moment, "o": full["last"], "h": full["last"], "l": full["last"],
                                "c": full["last"], "v": 0.0}  # bougie d'1 h qui vient de s'ouvrir
    assert full["daily"][-1]["t"] == CO.utc_midnight(DAY) and full["daily"][-1]["v"] > 0  # journée en cours
    p1 = G.make_plan("BTC-USDT", "crypto", full["bars"], full["daily"], full["h4"], moment, S)
    p2 = G.make_plan("BTC-USDT", "crypto", part["bars"], part["daily"], part["h4"], moment, S)
    assert G.compact_plan(p1) == G.compact_plan(p2)
    assert len(full["bars"]) == 720  # comme Kraken en direct


def test_garde_fou_regard_vers_l_avenir():
    with pytest.raises(G.LookAhead):
        G.check_known([{"t": 1000}], 5, 1200, "test")
    G.check_known([{"t": 1000}], 5, 1300, "test")


def test_journee_action_complete(full_db):
    universe = [{"symbol": s, "rank": i + 1, "reason": "volume_dollars", "metrics": {"rvol": 1.2, "change_pct": 3.0}}
                for i, s in enumerate(STOCKS)]
    moments, rows = G.stock_day(full_db, DAY, S, universe)
    assert moments == [U.stock_decision_ts(DAY)] and len(rows) == 3
    r = rows[0]
    assert r["side"] == "long" and r["day"] == DAY.isoformat() and r["planned_at"] == moments[0]
    assert r["context"]["catalyst_replayed"] is False and r["context"]["regime"] is not None
    assert r["context"]["rvol"] == 1.2 and r["context"]["setup"]
    assert r["flat"]["decision"] in ("GO", "NO GO") and r["flat"]["composite"] == r["plan"]["score"]["composite"]
    assert len(r["plan"]["targets"]) == 3 and "minutes_median" in r["plan"]["targets"][0]
    assert r["plan"]["score"]["parts"]["catalyst"]["score"] is None
    assert G.stock_day(full_db, date(2026, 9, 7), S, universe) == ([], [])  # Labor Day


def test_journee_crypto_seulement_les_creneaux_echus(full_db):
    universe = [{"symbol": s, "rank": i + 1, "reason": "volume_dollars", "metrics": {}} for i, s in enumerate(CRYPTO)]
    now = CO.utc_midnight(DAY) + 86400 + 9 * 3600  # le lendemain 9 h : créneaux de 0 h, 4 h et 8 h échus
    moments, rows = G.crypto_day(full_db, DAY, S, universe, now)
    assert moments == [CO.utc_midnight(DAY) + h * 3600 for h in (0, 4, 8)]
    assert len(rows) == 6 and {r["context"]["window_utc"] for r in rows} == {"00:00", "04:00", "08:00"}


def test_plan_from_bars_identique_a_build_plan(full_db, monkeypatch):
    now = datetime.fromtimestamp(U.stock_decision_ts(DAY), timezone.utc)
    inp = G.stock_inputs(full_db, "AAA", DAY)
    by_interval = {15: inp["bars"], 1440: inp["daily"], 240: inp["h4"]}

    async def fake_candles(symbol, market, interval, source=None, max_age=None, outputsize=None):
        return by_interval[interval]

    cat = {"available": True, "fresh_news": True, "positive": 2, "negative": 0, "news": [], "dilution": False,
           "hours_to_earnings": None, "last_earnings": None}

    async def fake_cat(symbol, now):
        return cat

    monkeypatch.setattr(T, "candles", fake_candles)
    monkeypatch.setattr(T.C, "safe_check", fake_cat)
    monkeypatch.setenv("JARVIS_CAPITAL", "10000")
    import asyncio
    # Risque demandé explicitement : gardé (en euros, plafonné à 20 €, au change du jour) ; seuls le grade et la
    # taille du direct changent le plan.
    live = asyncio.run(T.build_plan("AAA", "stock", now=now, risk_pct=T.RISK_PCT))
    core = T.plan_from_bars("AAA", "stock", "long", inp["bars"], inp["daily"], inp["h4"], now, cat)
    assert live["grade"]["grade"] == "non noté" and live["grade"]["hors_budget"]
    assert ({k: v for k, v in live.items() if k not in ("grade", "sizing")}
            == {k: v for k, v in core.items() if k != "sizing"})
    assert live["sizing"]["risk_amount_eur"] == 20.0 and live["sizing"]["eurusd"] == 1.15
    assert isinstance(live["sizing"]["quantity"], int) and live["targets"][0]["eta_text"]


def test_type_de_configuration():
    def p(trigger, **ok):
        items = [{"key": k, "ok": v, "detail": "R1 cassé" if k == "pivot" else ""} for k, v in ok.items()]
        return {"checklist": {"trigger": trigger, "items": items}}
    assert G.setup_type(p(True)) == "declencheur_complet"
    assert G.setup_type(p(False, pivot=True)) == "cassure_pivot"
    assert G.setup_type(p(False, pivot=False, divergence=True)) == "divergence"
    assert G.setup_type(p(False, pivot=False, divergence=None, trend_daily=False)) == "contre_tendance"
