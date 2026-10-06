"""Collecte et agrégation du labo (hors ligne : transports httpx simulés)."""
import json
from datetime import date, datetime, timezone

import httpx
import pytest

from app.core.http import SourceError
from app.lab import collector as CO
from app.lab import sources as SRC
from app.lab.db import LabDB
from app.sources import alpaca

DAY = date(2026, 10, 6)
OPEN, CLOSE = CO.session_ts(DAY)


def five(start: int, n: int, price: float = 100.0) -> list[dict]:
    return [{"t": start + 300 * i, "o": price + i, "h": price + i + 0.5, "l": price + i - 0.5, "c": price + i + 0.2,
             "v": 10.0} for i in range(n)]


def test_agregation_actions_15_min_seance_seulement():
    pre = five(OPEN - 3600, 12)  # pré-ouverture : ignorée
    bars = pre + five(OPEN, 78)
    out = CO.aggregate(bars, 15, "stock")
    assert len(out) == 26 and out[0]["t"] == OPEN and out[-1]["t"] == CLOSE - 900
    first = out[0]
    assert (first["o"], first["h"], first["l"], first["c"], first["v"]) == (100.0, 102.5, 99.5, 102.2, 30.0)


def test_agregation_actions_1h_et_4h_alignees_sur_l_ouverture():
    bars = five(OPEN, 78)
    h1 = CO.aggregate(bars, 60, "stock")
    assert [datetime.fromtimestamp(b["t"], CO.NEW_YORK).strftime("%H:%M") for b in h1] == \
        ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]
    assert h1[-1]["v"] == 60.0  # 15 h 30 - 16 h : 6 bougies
    h4 = CO.aggregate(bars, 240, "stock")
    assert [datetime.fromtimestamp(b["t"], CO.NEW_YORK).strftime("%H:%M") for b in h4] == ["09:30", "13:30"]
    assert h4[0]["v"] == 480.0 and h4[1]["v"] == 300.0


def test_agregation_jour_court_et_journalier():
    short = date(2026, 11, 27)
    o, c = CO.session_ts(short)
    bars = five(o, 42) + five(c, 6)  # après 13 h : hors séance
    assert len(CO.aggregate(bars, 240, "stock")) == 1
    two_days = five(OPEN, 78) + bars
    daily = CO.aggregate(two_days, 1440, "stock")
    assert [b["t"] for b in daily] == [CO.ny_midnight(DAY), CO.ny_midnight(short)]
    assert daily[1]["v"] == 420.0 and daily[0]["c"] == five(OPEN, 78)[-1]["c"]


def test_agregation_crypto_24_7_alignee_utc():
    start = CO.utc_midnight(DAY) - 3600
    bars = five(start, 12 * 30)  # 30 h
    h1 = CO.aggregate(bars, 60, "crypto")
    assert len(h1) == 30 and all(b["t"] % 3600 == 0 for b in h1)
    h4 = CO.aggregate(bars, 240, "crypto")
    assert h4[0]["t"] == CO.utc_midnight(DAY) - 4 * 3600 and h4[0]["v"] == 120.0
    daily = CO.aggregate(bars, 1440, "crypto")
    assert [b["t"] for b in daily] == [CO.utc_midnight(DAY) + k * 86400 for k in (-1, 0, 1)]
    assert [b["v"] for b in daily] == [120.0, 2880.0, 600.0]  # 1 h, 24 h, 5 h


def test_bougies_closes_et_bougie_en_cours():
    bars = five(OPEN, 12)
    moment = OPEN + 30 * 60
    assert len(CO.known_before(bars, 5, moment)) == 6
    h1 = CO.aggregate(bars, 60, "crypto")
    assert all(b["t"] + 3600 <= OPEN + 3600 for b in CO.known_before(h1, 60, OPEN + 3600))
    cur = CO.in_progress(bars, OPEN, moment, last_close=1.0)
    assert cur["t"] == OPEN and cur["c"] == bars[5]["c"] and cur["v"] == 60.0
    empty = CO.in_progress(bars, moment, moment, last_close=7.0)
    assert (empty["o"], empty["h"], empty["c"], empty["v"]) == (7.0, 7.0, 7.0, 0.0)


def test_heures_de_seance_et_seances_precedentes():
    bars = five(OPEN - 600, 4) + five(CLOSE - 300, 3)
    kept = CO.regular_hours(bars)
    assert [b["t"] for b in kept] == [OPEN, OPEN + 300, CLOSE - 300]
    assert CO.previous_trading_day(date(2026, 10, 5)) == date(2026, 10, 2)  # lundi → vendredi
    assert CO.sessions_back(date(2026, 11, 27), 1) == date(2026, 11, 25)    # Thanksgiving sauté


def test_filtre_des_actions_sans_taille_ni_liquidite():
    assets = [
        {"symbol": "AAPL", "exchange": "NASDAQ", "tradable": True, "status": "active", "class": "us_equity"},
        {"symbol": "TINY", "exchange": "AMEX", "tradable": True, "status": "active"},
        {"symbol": "BRK.B", "exchange": "NYSE", "tradable": True, "status": "active"},
        {"symbol": "OTCX", "exchange": "OTC", "tradable": True, "status": "active"},
        {"symbol": "HALT", "exchange": "NYSE", "tradable": False, "status": "active"},
        {"symbol": "GONE", "exchange": "NYSE", "tradable": False, "status": "inactive"},
        {"symbol": "SPYX", "exchange": "ARCA", "tradable": True, "status": "active", "attributes": ["etf"]},
        {"symbol": "SPY", "exchange": "ARCA", "tradable": True, "status": "active", "attributes": []},
    ]
    assert CO.stock_symbols(assets) == ["AAPL", "GONE", "SPY", "TINY"]


def test_filtre_des_paires_crypto():
    inst = [{"inst_id": f"{b}-{q}", "base": b, "quote": q, "state": "live"}
            for b, q in (("BTC", "USDT"), ("USDC", "USDT"), ("WBTC", "USDT"), ("ETH", "USDC"), ("BTC3L", "USDT"),
                         ("JUP", "USDT"))]
    inst.append({"inst_id": "OLD-USDT", "base": "OLD", "quote": "USDT", "state": "suspend"})
    assert CO.crypto_pairs(inst) == ["BTC-USDT", "JUP-USDT"]


def test_couverture_ne_redemande_que_le_manque():
    assert CO._missing(None, 10, 20) == [(10, 20)]
    assert CO._missing([12, 18], 10, 20) == [(10, 12), (18, 20)]
    assert CO._missing([5, 25], 10, 20) == []
    assert CO._extend([12, 18], 10, 15) == [10, 18]


def okx_transport(pages: list[list[list]], seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        data = pages.pop(0) if pages else []
        return httpx.Response(200, json={"code": "0", "msg": "", "data": data})
    return httpx.MockTransport(handler)


def okx_row(t: int, confirm: str = "1") -> list:
    return [str(t * 1000), "1", "2", "0.5", "1.5", "10", "15", "15", confirm]


async def test_okx_pagination_plus_recentes_d_abord():
    base = 1_700_000_000 - 1_700_000_000 % 300
    page1 = [okx_row(base + 300 * i, "0" if i == 5 else "1") for i in (5, 4, 3)]
    page2 = [okx_row(base + 300 * i) for i in (2, 1, 0)]
    seen: list = []
    rows = await SRC.okx_candles("BTC-USDT", 5, base + 300, base + 1800, transport=okx_transport([page1, page2], seen))
    assert [r["t"] for r in rows] == [base + 300 * i for i in (1, 2, 3, 4)]  # en cours et hors plage écartées
    assert seen[0]["after"] == str((base + 1800) * 1000) and seen[1]["after"] == str((base + 900) * 1000)
    assert seen[0]["bar"] == "5m" and rows[0]["v"] == 10.0


async def test_okx_erreur_de_debit_retentee(monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(200, json={"code": "50011", "msg": "Too Many Requests", "data": []})
        return httpx.Response(200, json={"code": "0", "data": [{"instId": "BTC-USDT", "baseCcy": "BTC",
                                                                "quoteCcy": "USDT", "state": "live"}]})

    async def no_sleep(_):
        return None

    monkeypatch.setattr(SRC.asyncio, "sleep", no_sleep)
    rows = await SRC.with_retry(SRC.okx_get, "/x", {}, transport=httpx.MockTransport(handler), sleep=no_sleep)
    assert rows[0]["instId"] == "BTC-USDT" and len(calls) == 2


async def test_erreur_definitive_non_retentee():
    calls = []

    async def fail():
        calls.append(1)
        raise SourceError("HTTP 404")

    with pytest.raises(SourceError):
        await SRC.with_retry(fail)
    assert len(calls) == 1


async def test_limite_de_debit_glissante():
    now = [0.0]
    waits = []

    async def fake_sleep(s):
        waits.append(s)
        now[0] += s

    lim = SRC.RateLimiter(2, 1.0, clock=lambda: now[0], sleep=fake_sleep)
    for _ in range(3):
        await lim.wait()
    assert len(waits) == 1 and waits[0] == pytest.approx(1.01)


async def test_alpaca_pagination_et_actifs(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    seen = []

    def handler(request):
        seen.append((request.url.path, dict(request.url.params)))
        if request.url.path.endswith("/v2/assets"):
            return httpx.Response(200, json=[{"symbol": "AAPL", "exchange": "NASDAQ", "tradable": True}])
        if "page_token" not in request.url.params:
            return httpx.Response(200, json={"bars": {"SPY": [{"t": "2024-01-02T14:30:00Z", "o": 1, "h": 2, "l": 0.5,
                                                               "c": 1.5, "v": 100}]}, "next_page_token": "abc"})
        return httpx.Response(200, json={"bars": {"SPY": [{"t": "2024-01-02T14:35:00Z", "o": 1.5, "h": 2, "l": 1,
                                                           "c": 1.8, "v": 50}]}, "next_page_token": None})

    transport = httpx.MockTransport(handler)
    start = datetime(2024, 1, 2, tzinfo=timezone.utc)
    rows = await alpaca.bars_range(["SPY"], 5, start, datetime(2024, 1, 3, tzinfo=timezone.utc), transport=transport)
    assert [r["c"] for r in rows["SPY"]] == [1.5, 1.8]
    assert seen[0][1]["feed"] == "sip" and seen[0][1]["timeframe"] == "5Min" and seen[1][1]["page_token"] == "abc"
    assets = await alpaca.assets(transport=transport)
    assert assets[0]["symbol"] == "AAPL" and seen[-1][0] == "/v2/assets"
    assert all("orders" not in path for path, _ in seen)  # jamais de route d'ordre


async def test_collecte_5_min_reprend_la_couverture(monkeypatch, tmp_path):
    db = LabDB(str(tmp_path / "lab.db"))
    calls = []

    async def fake_bars(symbols, tf, start, end, transport=None):
        calls.append((tuple(symbols), int(start.timestamp()), int(end.timestamp())))
        a = int(start.timestamp())
        return {s: [b for b in five(OPEN - 3600, 30) if a <= b["t"] < int(end.timestamp())] for s in symbols}

    monkeypatch.setattr(SRC, "alpaca_bars", fake_bars)
    now = datetime.fromtimestamp(CLOSE + 7200, timezone.utc)
    n = await CO.ensure_intraday(db, "stock", ["AAA", "BBB"], OPEN - 3600, CLOSE, now)
    assert n == 2 * 18 and len(calls) == 1  # pré-ouverture filtrée, une requête pour les deux
    await CO.ensure_intraday(db, "stock", ["AAA", "BBB"], OPEN - 3600, CLOSE, now)
    assert len(calls) == 1  # déjà couvert : aucun appel
    assert db.get("cov5:stock", "AAA") == [OPEN - 3600, CLOSE]
    assert len(db.get_bars("stock", "AAA", 5, 0, 2**40)) == 18


def test_base_wal_et_avancement(tmp_path):
    db = LabDB(str(tmp_path / "lab.db"))
    assert db.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    db.set("job", "k", {"a": 1})
    assert db.get("job", "k") == {"a": 1} and db.get("job", "absent", 3) == 3
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.set("job", "k", 2)
            raise RuntimeError("coupure")
    assert db.get("job", "k") == {"a": 1}  # tout ou rien
    assert json.loads(json.dumps(db.keys("job"))) == {"k": {"a": 1}}
