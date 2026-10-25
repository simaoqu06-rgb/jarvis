"""Challenger « méthode Gemini » : plans A, B et C, solde sur bougies d'1 min, frais IBKR, témoin aléatoire, bilan."""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.trading import gemini as G

NY = ZoneInfo("America/New_York")
DAY = date(2026, 10, 21)       # mercredi
NEXT = date(2026, 10, 22)


def ts(h, m, day=DAY):
    return int(datetime.combine(day, time(h, m), NY).timestamp())


def bar(t, o, h, l, c, v=1000.0):  # noqa: E741
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}


def flat(start, n, price, step=60, v=1000.0):
    return [bar(start + i * step, price, price, price, price, v) for i in range(n)]


# --- Plans ------------------------------------------------------------------------------------------

def test_plan_a_gap_band_and_levels():
    first = [bar(ts(9, 30), 11.0, 11.5, 10.8, 11.3, 5000)]
    p, why = G.plan_a(first, 10.0)
    assert why is None
    assert p["entry"] == 11.51 and p["stop_range"] == 10.79
    assert p["vol_min_minute"] == 1500.0  # 1,5 × 5000 / 5
    assert G.plan_a([bar(ts(9, 30), 13.0, 13.5, 12.8, 13.3)], 10.0)[0] is None   # +30 % : « gap and crap »
    assert G.plan_a([bar(ts(9, 30), 10.2, 10.5, 10.1, 10.3)], 10.0)[0] is None   # +2 %


def _morning_then_flag(flag_range=0.2):
    """Montée de 10 à 12 le matin (pic 12), puis 6 bougies de 5 min en drapeau sous le pic."""
    rows = []
    t = ts(9, 30)
    for i in range(30):  # 9 h 30 → 12 h 00 : montée régulière
        p = 10.0 + i * 2.0 / 29
        rows.append(bar(t + i * 300, p, p + 0.05, p - 0.05, p, 10000))
    for i in range(6):   # 12 h 00 → 12 h 30 : drapeau
        lo = 11.6
        rows.append(bar(t + (30 + i) * 300, lo + 0.1, lo + flag_range, lo, lo + 0.1, 3000))
    return rows


def test_plan_b_flag():
    p, why = G.plan_b(_morning_then_flag(), 9.0)
    assert why is None
    assert p["entry"] == pytest.approx(11.81) and p["tp1"] == pytest.approx(12.05)
    assert p["stop"] < p["entry"] and p["tp2"] >= p["entry"] * 1.10 - 1e-9
    assert G.plan_b(_morning_then_flag(flag_range=1.5), 9.0)[1] in ("pas de consolidation horizontale",
                                                                    "pas de pic du matin au-dessus du drapeau")
    assert "journée" in G.plan_b(_morning_then_flag(), 11.5)[1]   # +3 % seulement


def test_plan_c_vwap_reclaim():
    rows = []
    for i in range(72):  # 9 h 30 → 15 h 25 : séance à 20, creux sous le VWAP à 15 h 00-15 h 10, reprise à 15 h 25
        tod = (datetime.combine(DAY, time(9, 30)) + timedelta(minutes=5 * i)).time()
        p = 20.0 if tod < time(15, 0) else 19.5 if tod <= time(15, 10) else 20.4
        rows.append(bar(ts(tod.hour, tod.minute), p, p + 0.1, p - 0.1, p, 1000))
    daily = [{"h": 22.0}] * 20
    p, why = G.plan_c(rows, daily)
    assert why is None
    assert p["stop"] == pytest.approx(19.39) and p["tp2"] == 22.0
    rows[-1] = rows[-1] | {"c": 19.0}
    assert G.plan_c(rows, daily)[0] is None


def test_rvol_same_time_of_day():
    rows = []
    for k in range(1, 8):  # 7 séances passées à 1 000 par bougie, le jour à 5 000
        d = DAY - timedelta(days=k)
        if d.weekday() < 5:
            rows += [bar(ts(9, 30, d) + i * 300, 1, 1, 1, 1, 1000) for i in range(6)]
    rows += [bar(ts(9, 30) + i * 300, 1, 1, 1, 1, 5000) for i in range(2)]
    assert G.rvol(rows, DAY, time(9, 35)) == pytest.approx(5.0)
    assert G.rvol(rows[-2:], DAY, time(9, 35)) is None  # pas d'historique


# --- Solde ------------------------------------------------------------------------------------------

def _plan_a():
    return {"symbol": "CHRN", "kind": "A", "day": DAY.isoformat(), "entry": 17.51, "stop_range": 16.99,
            "vol_min_minute": 1500.0, "tp_pct": [10.0, 20.0], "actif_des": ts(9, 37), "temoins": ["RAND"]}


def test_fill_a_needs_volume_and_close_above():
    p = _plan_a()
    bars = flat(ts(9, 37), 3, 17.4) + [bar(ts(9, 40), 17.4, 17.6, 17.4, 17.45, 9000),   # mèche : close sous l'entrée
                                       bar(ts(9, 41), 17.45, 17.7, 17.4, 17.6, 1000),    # volume trop faible
                                       bar(ts(9, 42), 17.6, 17.8, 17.5, 17.7, 2000)]
    f = G.fill(p, bars)
    assert f["t"] == ts(9, 43) and f["prix"] == 17.7
    assert f["tp1"] == pytest.approx(19.47) and f["tp2"] == pytest.approx(21.24)
    late = _plan_a() | {"actif_des": ts(10, 31)}
    assert G.fill(late, bars) is None


def test_manage_tp1_then_breakeven_and_worst_case():
    f = {"t": ts(10, 0), "prix": 10.0, "stop": 9.5, "tp1": 11.0, "tp2": 12.0}
    bars = [bar(ts(10, 1), 10.0, 11.1, 9.9, 11.0), bar(ts(10, 2), 10.5, 10.6, 9.95, 10.0)]
    m = G.manage(f, bars, ts(15, 45))
    assert [(leg, px, why) for leg, px, why, _ in m["sorties"]] == [("tp1", 11.0, "tp1"), ("tp2", 10.0, "entree")]
    both = [bar(ts(10, 1), 10.0, 11.2, 9.4, 10.0)]  # stop et objectif dans la même minute : le stop
    assert {why for _, _, why, _ in G.manage(f, both, ts(15, 45))["sorties"]} == {"stop"}
    timed = G.manage(f, flat(ts(10, 1), 400, 10.2), ts(15, 45))
    assert {why for _, _, why, _ in timed["sorties"]} == {"temps"} and not timed["incomplet"]


def test_result_fees_cost_less_than_gross():
    f = {"t": 0, "prix": 10.0, "stop": 9.5, "tp1": 11.0, "tp2": 12.0}
    m = {"sorties": [("tp1", 11.0, "tp1", False), ("tp2", 12.0, "tp2", False)]}
    r = G.result(f, m, 1.15)
    assert r["r_brut"] == pytest.approx(3.0)
    assert r["actions"] == 45 and 2.5 < r["r_frais"] < 3.0
    expensive = {"t": 0, "prix": 100.0, "stop": 80.0, "tp1": 110.0, "tp2": 120.0}
    assert G.result(expensive, m | {"sorties": [("tp1", 110.0, "tp1", False), ("tp2", 120.0, "tp2", False)]},
                    1.15)["hors_mise"]


def test_settle_plan_with_witness_both_variants():
    p = _plan_a()
    me = flat(ts(9, 30), 12, 17.4) + [bar(ts(9, 42), 17.6, 17.8, 17.5, 17.7, 2000)]
    me += [bar(ts(9, 43) + i * 60, 18.0 + i * 0.02, 18.05 + i * 0.02, 17.95 + i * 0.02, 18.0 + i * 0.02)
           for i in range(200)]          # monte jusqu'à ~22 : TP1 puis TP2
    rand = flat(ts(9, 30), 400, 50.0)
    out = G.settle_plan(p, {"CHRN": me, "RAND": rand}, NEXT, 1.15)
    assert out["declenche"] and out["temoin"] == "RAND"
    assert {leg for leg, _, _ in out["j0"]["sorties"]} == {"tp1", "tp2"}
    assert out["j0"]["r_frais"] > 0
    assert out["j0"]["temoin"]["r_brut"] == pytest.approx(0.0, abs=1e-9) or out["j0"]["temoin"]["incomplet"]
    assert "j1" in out


def test_witness_same_geometry():
    f = {"t": ts(10, 0), "prix": 20.0, "stop": 19.0, "tp1": 22.0, "tp2": 24.0}
    w = G.witness_fill(f, [bar(ts(10, 0), 50.0, 50, 50, 50)])
    assert w["stop"] == pytest.approx(47.5) and w["tp1"] == pytest.approx(55.0) and w["tp2"] == pytest.approx(60.0)


# --- Bilan et passages ----------------------------------------------------------------------------------

def _settled(day, r, w):
    return {"day": day, "version": G.VERSION, "kind": "A",
            "solde": {"declenche": True, "j0": {"r_brut": r + 0.1, "r_frais": r, "incomplet": False,
                                                "temoin": {"r_frais": w, "incomplet": False}}}}


def test_summary_and_reading_rule():
    book = [_settled(f"2026-10-{d:02d}", 1.0, 0.0) for d in range(1, 21)]
    s = G.summary(book, "j0")
    assert s["plan"]["n"] == 20 and s["ecart"]["m"] == pytest.approx(1.0)
    assert "lecture à 100" in G.verdict(s["ecart"])
    big = [_settled(f"2026-{m:02d}-{d:02d}", 0.5 + (d % 3) * 0.1, 0.0) for m in range(1, 6) for d in range(1, 21)]
    assert "bat son témoin" in G.verdict(G.summary(big, "j0")["ecart"])
    text = G.bilan_text({"plans": book})
    assert "Challenger Gemini" in text and "Seul Simão décide" in text


def test_due_windows_and_early_close(monkeypatch):
    monkeypatch.setattr(G.alpaca, "configured", lambda: True)
    at = datetime.combine(DAY, time(9, 37), NY).astimezone(timezone.utc)
    st: dict = {}
    assert G.due(at, st) == "A"
    G.mark_done(st, "A", at)
    assert G.due(at, st) is None
    assert G.due(datetime.combine(DAY, time(12, 40), NY), st) == "B"
    assert G.due(datetime.combine(DAY, time(15, 35), NY), st) == "C"
    assert G.due(datetime.combine(date(2026, 11, 27), time(9, 37), NY), {}) is None  # lendemain de Thanksgiving


@pytest.mark.asyncio
async def test_scan_a_end_to_end(monkeypatch, tmp_path):
    monkeypatch.setattr(G, "JOURNAL_PATH", str(tmp_path / "scans.jsonl"))
    monkeypatch.setattr(G.config, "WATCHLIST_STOCKS", [])
    now = datetime.combine(DAY, time(9, 36), NY).astimezone(timezone.utc)

    async def most_actives(top=100):
        return ["CHRN", "BIG", "RA", "RB"]

    async def gainers(top=50):
        return ["CHRN", "BIG"]

    async def snapshots(symbols, feed="iex"):
        return {s: {"price": 11.2, "open": 11.0, "open_day": DAY.isoformat(), "day_volume": 50000} for s in symbols}

    async def bars(symbols, tf, days, now=None, feed="sip"):
        if tf == 1440:
            return {s: [{"t": ts(12, 0, DAY - timedelta(days=k)), "o": 10, "h": 10.5, "l": 9.5, "c": 10.0,
                         "v": 100000} for k in range(40, 0, -1)] for s in symbols}
        out = {}
        for s in symbols:
            rows = []
            for k in range(1, 30):
                d = DAY - timedelta(days=k)
                if d.weekday() < 5:
                    rows += [bar(ts(9, 30, d), 10, 10, 10, 10, 1000)]
            rows.append(bar(ts(9, 30), 11.0, 11.5, 10.8, 11.3, 9000))
            out[s] = sorted(rows, key=lambda r: r["t"])
        return out

    async def profile(symbol):
        return {"name": symbol, "shares": 3.5e6 if symbol == "CHRN" else 80e6}

    async def safe_check(symbol, now=None):
        return {"available": True}

    monkeypatch.setattr(G.alpaca, "most_actives", most_actives)
    monkeypatch.setattr(G.alpaca, "top_gainers", gainers)
    monkeypatch.setattr(G.alpaca, "snapshots", snapshots)
    monkeypatch.setattr(G.alpaca, "bars", bars)
    monkeypatch.setattr(G.finnhub, "profile", profile)
    monkeypatch.setattr(G.C, "safe_check", safe_check)
    monkeypatch.setattr(G.KM, "catalyst_pillar", lambda cat, now: (True, ["actu positive"]))
    r = await G.scan("A", now)
    assert [p["symbol"] for p in r["plans"]] == ["CHRN"]
    p = r["plans"][0]
    assert p["entry"] == 11.51 and p["rvol"] == pytest.approx(9.0)
    assert len(p["temoins"]) == 3 and "CHRN" not in p["temoins"]
    assert "30 M" in r["ecartees"]["BIG"]
    assert "Challenger Gemini" in G.plan_text(p, 1.15, 20.0)


async def test_tick_scheduler_saves_plans_and_sends(monkeypatch, tmp_path):
    from app import scheduler as SCH

    monkeypatch.setattr(G, "STATE_PATH", str(tmp_path / "gemini.json"))
    monkeypatch.setattr(G.alpaca, "configured", lambda: True)
    plan = _plan_a() | {"rvol": 9.0, "actions": 3.5e6, "catalyseur": ["actu positive"], "gap_pct": 11.4,
                        "range_haut": 17.5, "range_bas": 17.0, "version": G.VERSION}
    calls = []

    async def scan(kind, now):
        calls.append(kind)
        return {"plans": [plan]}

    async def rate():
        return 1.15

    sent = []
    monkeypatch.setattr(G, "scan", scan)
    monkeypatch.setattr(SCH, "_usd_per_eur", rate)
    monkeypatch.setattr(SCH, "_size_rule", lambda now: ({}, 20.0))
    monkeypatch.setattr(SCH, "_queue", lambda texts, kind="", **kw: sent.append(texts))
    monkeypatch.setattr(SCH, "_dm_trades", lambda texts: None)
    monkeypatch.setattr(SCH, "_kick_outbox", lambda: None)
    at = datetime.combine(DAY, time(9, 37), NY).astimezone(timezone.utc)
    await SCH.tick_gemini(at)
    await SCH.tick_gemini(at + timedelta(minutes=1))
    assert calls == ["A"] and len(sent) == 1 and "CHRN" in sent[0][0]
    assert [p["symbol"] for p in G.load()["plans"]] == ["CHRN"]
    assert "CHRN" in G.today_text(G.load(), at)


def test_time_exit_without_trade_at_exit_minute_uses_last_close():
    f = {"t": ts(15, 0), "prix": 10.0, "stop": 9.0, "tp1": 11.0, "tp2": 12.0}
    bars = flat(ts(15, 0), 10, 10.3) + [bar(ts(9, 30, NEXT), 9.5, 9.6, 9.4, 9.5)]  # rien après 15 h 10, puis lendemain
    m = G.manage(f, bars, ts(15, 59))
    assert {px for _, px, _, _ in m["sorties"]} == {10.3}
