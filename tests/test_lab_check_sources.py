"""Rapport de vérification des sources (hors ligne : OKX simulé, sans clés)."""
from datetime import datetime, timezone

import httpx

from app.lab import check_sources as CS


async def test_rapport_sans_cles_avec_okx_simule(monkeypatch, tmp_path):
    for name in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "FINNHUB_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("JARVIS_LAB_DB", str(tmp_path / "lab.db"))
    now = datetime(2026, 10, 6, tzinfo=timezone.utc)

    def handler(request):
        if request.url.path.endswith("/instruments"):
            data = [{"instId": f"{b}-USDT", "baseCcy": b, "quoteCcy": "USDT", "state": "live"}
                    for b in ("BTC", "ZIL", "USDC")]
            return httpx.Response(200, json={"code": "0", "data": data})
        after = int(request.url.params["after"])
        ts = after - 86400 * 1000
        old = ts < (now.timestamp() - 2.5 * 365 * 86400) * 1000  # rien avant 2 ans et demi
        return httpx.Response(200, json={"code": "0", "data": [] if old else [[str(ts), "1", "1", "1", "1", "1", "1", "1", "1"]]})

    text = await CS.report(now, transport=httpx.MockTransport(handler))
    assert "clés ALPACA_API_KEY" in text and "FINNHUB_API_KEY absente" in text
    assert "OKX : 2 paires USDT retenues sur 3" in text
    assert "OKX 5 min BTC-USDT : 1 an oui, 2 ans oui, 3 ans non" in text and "ZIL-USDT" in text
    assert "Go libres" in text and "Base estimée pour" in text


def test_taille_estimee_croit_avec_le_rejeu():
    low, high = CS.size_estimate(504)
    assert 0 < low < high and CS.size_estimate(252)[1] < high
