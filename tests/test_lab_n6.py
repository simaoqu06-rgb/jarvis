"""Labo v2, déclaration n° 6 : sorties de Gemini (VWAP après 10 h 30, time stop 14 h 30, avalement de SPY)
et filtre d'entrée VWAP mesuré sans essai."""
from datetime import date, datetime

import pytest

from app.lab import collector as CO
from app.lab import rattrapage as RAT
from app.lab import strat_stats as SS
from app.lab import strategies as ST
from app.lab.db import LabDB
from tests.test_lab_strategies import DAY, OPEN, ZERO, bars_from, stock_day_bars

NY = CO.NEW_YORK
T1030 = int(datetime.combine(DAY, ST.VWAP_EXIT_FROM, NY).timestamp())
T1430 = int(datetime.combine(DAY, ST.TIME_STOP_AT, NY).timestamp())


def rising(n=78, dips=()):
    """Séance qui monte de 0,1 par bougie ; une bougie de `dips` finit nettement sous le VWAP, la suivante
    remonte aussitôt."""
    rows, p = [], 100.0
    for i in range(n):
        if i in dips:
            rows.append((p, p + 0.05, p - 2.5, p - 2.4))
            p -= 2.4
        elif i - 1 in dips:
            rows.append((p, p + 2.6, p - 0.05, p + 2.5))
            p += 2.5
        else:
            rows.append((p, p + 0.15, p - 0.05, p + 0.1))
            p += 0.1
    return bars_from(OPEN, rows)


def day_with(session, index5=None):
    hist, _, daily = stock_day_bars()
    d = ST.Day("stock", DAY, CO.regular_hours(hist)[-ST.STOCK_WARMUP_BARS:] + session, daily)
    d.index_down = ST.index_down(index5)
    return d


def run(d, targets=(110.0, 115.0, 120.0)):
    plan = ST._plan(100.05, 95.0, list(targets))
    args = (d, plan, OPEN, d.close_ts, d.exit_at(OPEN), ZERO, "stop", 99.9)
    return ST.measure(*args[:4], ZERO, "stop", 99.9), args


def cut(args, ts, reason):
    d, plan, decided, until, exit_at, cost, order, last = args
    return ST._sim(d, plan, decided, until, min(exit_at, ts), cost, order, last, exit_reason_end=reason)


def test_vwap_seulement_apres_10_h_30():
    early = (T1030 - OPEN) // 300 - 4          # bougie de 10 h 05 : sous le VWAP, mais trop tôt
    late = (T1030 - OPEN) // 300 + 3           # bougie de 10 h 45, close à 10 h 50
    d = day_with(rising(dips=(early, late)))
    assert d.c[d.d0 + early] < d.vwap[d.d0 + early] and d.c[d.d0 + late] < d.vwap[d.d0 + late]
    m, args = run(d)
    expected = cut(args, d.t[d.d0 + late] + 600, "vwap")   # exécutée à la clôture de la bougie suivante
    assert expected["exit_reason"] == "vwap" and m["r_vwap"] == expected["r_net"] != m["r_net"]
    m, _ = run(day_with(rising(dips=(early,))))
    assert m["r_vwap"] == m["r_net"]                        # jamais sous le VWAP après 10 h 30


def test_time_stop_a_14_h_30_si_le_tp1_n_est_pas_touche():
    d = day_with(rising())
    m, args = run(d)
    expected = cut(args, T1430, "time_stop")
    assert expected["exit_reason"] == "time_stop" and m["r_1430"] == expected["r_net"] != m["r_net"]
    m, _ = run(d, targets=(100.5, 115.0, 120.0))           # TP1 touché le matin : la gestion v1 continue
    assert m["r_1430"] == m["r_net"]


def spy(engulf_at=None):
    rows, p = [], 400.0
    for i in range(78):
        if i == engulf_at:
            rows.append((p + 0.3, p + 0.35, p - 1.0, p - 0.9))   # rouge qui englobe la verte précédente
            p -= 0.9
        else:
            rows.append((p, p + 0.25, p - 0.05, p + 0.2))
            p += 0.2
    return bars_from(OPEN, rows)


def test_avalement_baissier_de_spy():
    k = 20
    assert ST.index_down(spy(k)) == [OPEN + (k + 1) * 300] and ST.index_down(spy()) == []
    assert ST.index_down(None) is None
    d = day_with(rising(), spy(k))
    m, args = run(d)
    expected = cut(args, OPEN + (k + 2) * 300, "indice")
    assert expected["exit_reason"] == "indice" and m["r_spy"] == expected["r_net"] != m["r_net"]
    assert run(day_with(rising()))[0]["r_spy"] is None        # pas de bougies de SPY : pas de mesure


def test_lignes_actions_et_crypto():
    hist, session, daily = stock_day_bars()
    rows = ST.stock_day(DAY, "AAA", hist, session, daily, 3, None, spy(30), traded=True)
    assert rows and all(r["r_vwap"] is not None and r["r_1430"] is not None and r["r_spy"] is not None for r in rows)
    assert all(r["sur_vwap"] in (0, 1) for r in rows)        # signaux de séance : position connue
    d = day_with(rising())
    assert ST.above_vwap(d, d.t[d.d0 + 10] + 300) == 1 and ST.above_vwap(d, OPEN - 1200) is None
    crypto = ST._gemini(ST.Day("crypto", DAY, session, daily), {}, 0, 0, 0, ZERO, "stop", 1.0, {"triggered": True})
    assert crypto == {"r_vwap": None, "r_1430": None, "r_spy": None}


def _insert(db, strategy, k, rows):
    day = date.fromordinal(date(2025, 1, 6).toordinal() + k).isoformat()
    t0 = int(datetime.combine(date.fromisoformat(day), ST.VWAP_EXIT_FROM, NY).timestamp())
    db.conn.executemany(
        "INSERT INTO strat_trades (origin, market, symbol, day, strategy, slot, signal_at, sur_vwap, triggered, r_net, "
        "r_vwap, r_1430, r_spy, temoin_r, temoin_triggered, tp1_hit) "
        "VALUES ('replay', 'stock', ?, ?, ?, 0, ?, ?, 1, ?, ?, ?, ?, 0, 1, 0)",
        [(f"S{j}", day, strategy, t0 + dt, above, r, r + gain, r, None) for j, (dt, above, r, gain) in enumerate(rows)])


def test_tableau_ecarts_et_tranches_vwap():
    db = LabDB(":memory:")
    for k in range(40):
        _insert(db, "orb15", k, [(-3600, None, -0.3, 0.2 + 0.05 * (k % 2)), (1800, 1, 0.1, 0.25 + 0.05 * (k % 3)),
                                 (1800, 0, -0.2, 0.15 + 0.05 * (k % 2))])
    rep = SS.report(db.conn)
    item = rep["strategies"]["stock"]["orb15"]
    vwap = item["ecarts_sorties"]["r_vwap"]
    assert vwap["esperance_r"] > 0 and vwap["aide"] and vwap["n"] == 120
    assert item["ecarts_sorties"]["r_1430"]["esperance_r"] == 0 and not item["ecarts_sorties"]["r_1430"]["aide"]
    assert item["ecarts_sorties"]["r_spy"]["n"] == 0 and set(item["sorties"]) == set(ST.EXITS) | set(ST.STOCK_EXITS)
    assert {k: v["n"] for k, v in item["par_vwap"].items()} == dict.fromkeys(SS.VWAP_BUCKETS, 40)
    assert item["par_vwap"]["après 10 h 30, au-dessus du VWAP"]["esperance_r"] == pytest.approx(0.1)
    assert rep["sorties_gemini"]["ecarts"]["r_vwap"]["n"] == 120 and "_n6" not in rep["strategies"]
    text = SS.text(rep)
    assert "Sorties de Gemini (n° 6)" in text and "✅ sur 1 stratégie" in text and "avant 10 h 30" in text


async def test_rattrapage_relit_aussi_spy(tmp_path, monkeypatch):
    db = LabDB(str(tmp_path / "lab.db"))
    hist, session, daily = stock_day_bars()
    db.replace_universe("stock", DAY.isoformat(), [{"symbol": "AAA", "rank": 1, "reason": "test"}])
    asked = []

    async def fake_bars(symbols, tf, start, end, transport=None, adjustment="split"):
        asked.append((tuple(symbols), adjustment))
        return {"SPY": spy(30)} if symbols == ["SPY"] else {"AAA": hist + session}

    monkeypatch.setattr(RAT.SRC, "alpaca_bars", fake_bars)
    monkeypatch.setattr(RAT, "_daily", lambda *a: daily)
    rows = await RAT.stock_rows(db, "replay", DAY)
    assert (("SPY",), "raw") in asked and rows and all(r["r_spy"] is not None for r in rows if r["triggered"])


async def test_rejeu_lit_spy_de_la_seance_et_la_collecte(tmp_path, monkeypatch):
    from app.lab import runner as R
    from tests.lab_fixtures import fill_stock

    start, end = date(2026, 9, 14), date(2026, 9, 15)
    db = LabDB(str(tmp_path / "lab.db"))
    fill_stock(db, ["AAA", "BBB"], CO.sessions_back(start, 125), end)
    o, _ = CO.session_ts(start)
    db.put_bars("stock", "SPY", 5, [dict(b, t=o + b["t"] - OPEN) for b in spy(30)])
    await R.replay(start, end, "stock", db, collect=False, now=datetime(2026, 9, 20))
    # n° 10 : les lignes des actions viennent du rattrapage (prix négociés, SPY relu à la source, test ci-dessus).
    assert db.conn.execute("SELECT COUNT(*) FROM strat_trades").fetchone()[0] == 0
    assert RAT.due_days(db, "stock", "replay") == [start, end]

    calls = []

    async def record(db_, market, symbols, a, b, now=None, transport=None, failed_out=None):
        calls.append((list(symbols), a, b))
        return 0

    async def nothing(*a, **k):
        return None

    monkeypatch.setattr(R, "ensure_daily_range", nothing)
    monkeypatch.setattr(R.U, "build", lambda *a, **k: [{"symbol": "AAA"}])
    monkeypatch.setattr(R.CO, "ensure_intraday", record)
    await R.prepare_day(db, "stock", start)
    assert (["SPY"], *CO.session_ts(start)) in calls
