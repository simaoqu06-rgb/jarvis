"""Tests hors ligne des indicateurs EmperorBTC (RSI, VWAP, pivots, bougies, divergences, checklist)."""
from datetime import datetime, timezone

import httpx
import pytest

from app.notify import discord
from app.trading import indicators as I
from app.trading import signals as S

T0 = int(datetime(2026, 10, 5, tzinfo=timezone.utc).timestamp())


def bar(t, o, h, l, c, v=100.0):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}


def test_rsi_tendance():
    assert I.rsi_series([100.0 + i for i in range(30)])[-1] == 100.0
    assert I.rsi_series([100.0 - i for i in range(30)])[-1] < 1


def test_vwap_de_la_seance():
    bars = [bar(T0 - 3600, 50, 50, 50, 50, 1000),  # veille : ignorée
            bar(T0, 10, 10, 10, 10, 1), bar(T0 + 3600, 20, 20, 20, 20, 3)]
    assert I.vwap(bars, "crypto") == pytest.approx(17.5)


def test_pivots_standard():
    p = I.pivots({"h": 110, "l": 90, "c": 100})
    assert p["P"] == pytest.approx(100) and p["R1"] == pytest.approx(110) and p["S1"] == pytest.approx(90)


def test_marteau_et_engulfing():
    assert I.is_hammer(bar(0, 10, 10.05, 7, 10))
    assert not I.is_hammer(bar(0, 10, 12, 9.9, 10.1))
    assert I.engulfing(bar(0, 10, 10.2, 9, 9.2), bar(1, 9.1, 10.6, 9, 10.5)) == "haussier"


def test_divergence_haussiere():
    # Deux creux : le second plus bas en prix, mais atteint après une baisse plus lente (RSI plus haut).
    closes = [100 - i * 2 for i in range(20)]           # forte baisse
    closes += [62, 66, 70, 72, 70]                       # rebond
    closes += [69, 68, 67.5, 67, 61.5, 64, 67, 70]      # creux plus bas, baisse lente
    bars = [bar(T0 + i * 3600, c, c + 0.5, c - 0.5, c) for i, c in enumerate(closes)]
    assert I.rsi_divergence(bars) == "haussière"


def test_anomalie_weis():
    bars = [bar(T0 + i * 3600, 100 + i, 100.5 + i, 99.5 + i, 100.4 + i, 100) for i in range(20)]
    bars.append(bar(T0 + 20 * 3600, 120, 124, 119.8, 120.2, 400))  # gros volume, petit corps, longue mèche haute
    assert I.weis_anomaly(bars)


def test_checklist_tendance_haussiere():
    hourly = [bar(T0 + i * 3600, 100 + i * 0.1, 100.3 + i * 0.1, 99.8 + i * 0.1, 100.1 + i * 0.1, 100 + i)
              for i in range(120)]
    daily = [bar(T0 - (300 - i) * 86400, 50 + i * 0.1, 51 + i * 0.1, 49 + i * 0.1, 50 + i * 0.1) for i in range(300)]
    c = S.checklist(hourly, daily, daily, "crypto", "long", atr_value=0.5)
    items = {i["key"]: i for i in c["items"]}
    assert items["trend_daily"]["ok"] and items["ema200_4h"]["ok"] and items["vwap"]["ok"]
    assert S.checklist(hourly, daily, daily, "crypto", "short", atr_value=0.5)["verdict"] == "Contre la tendance de fond"


async def test_discord_upsert_reecrit_puis_reposte():
    calls = []

    def handler(request: httpx.Request):
        calls.append((request.method, request.url.path))
        if request.method == "PATCH":
            return httpx.Response(404 if "gone" in request.url.path else 200, json={"id": "abc"})
        return httpx.Response(200, json={"id": "new"})

    transport = httpx.MockTransport(handler)
    url = "https://discord.com/api/webhooks/1/tok"
    assert await discord.upsert("x", "abc", url, transport) == "abc"
    assert await discord.upsert("x", "gone", url, transport) == "new"
    assert await discord.upsert("x", None, url, transport) == "new"
    assert calls[0] == ("PATCH", "/api/webhooks/1/tok/messages/abc")
