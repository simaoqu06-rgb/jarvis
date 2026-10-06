"""Twelve Data : cours des actions US (clé gratuite TWELVEDATA_API_KEY, 8 requêtes/min)."""
import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.config import secret
from app.core.http import SourceError, get_json, to_float

URL = "https://api.twelvedata.com/quote"
KEY_NAME = "TWELVEDATA_API_KEY"


async def _quote(symbol: str, apikey: str) -> dict:
    q = await get_json(URL, params={"symbol": symbol, "apikey": apikey})
    if q.get("status") == "error":
        raise SourceError(f"{symbol} : {q.get('message', 'erreur')}")
    timestamp = q.get("last_quote_at") or q.get("timestamp")
    return {
        "symbol": q["symbol"],
        "name": q.get("name"),
        "exchange": q.get("exchange"),
        "currency": q.get("currency"),
        "price": to_float(q.get("close")),
        "open": to_float(q.get("open")),
        "high": to_float(q.get("high")),
        "low": to_float(q.get("low")),
        "previous_close": to_float(q.get("previous_close")),
        "change_pct": to_float(q.get("percent_change")),
        "volume": to_float(q.get("volume")),
        "is_market_open": q.get("is_market_open"),
        "as_of": datetime.fromtimestamp(timestamp, timezone.utc).isoformat() if timestamp else None,
    }


async def stock_quotes(symbols: list[str], apikey: str | None = None) -> list[dict]:
    key = apikey or secret(KEY_NAME)
    if not key:
        raise SourceError(f"clé {KEY_NAME} absente")
    return list(await asyncio.gather(*(_quote(s, key) for s in symbols)))


SERIES_URL = "https://api.twelvedata.com/time_series"


async def candles(symbol: str, interval_min: int, outputsize: int = 5000, apikey: str | None = None) -> list[dict]:
    """Bougies OHLC (5 000 au maximum, heures de séance seulement), de la plus ancienne à la plus récente."""
    key = apikey or secret(KEY_NAME)
    if not key:
        raise SourceError(f"clé {KEY_NAME} absente")
    interval = f"{interval_min}min" if interval_min < 60 else f"{interval_min // 60}h"
    payload = await get_json(
        SERIES_URL, params={"symbol": symbol, "interval": interval, "outputsize": outputsize, "apikey": key}
    )
    if payload.get("status") == "error":
        raise SourceError(f"{symbol} : {payload.get('message', 'erreur')}")
    tz = ZoneInfo(payload["meta"]["exchange_timezone"])
    rows = [
        {
            "t": int(datetime.fromisoformat(v["datetime"]).replace(tzinfo=tz).timestamp()),
            "o": float(v["open"]), "h": float(v["high"]), "l": float(v["low"]), "c": float(v["close"]),
            "v": to_float(v.get("volume")) or 0.0,
        }
        for v in payload.get("values", [])
    ]
    if not rows:
        raise SourceError(f"{symbol} : aucune bougie")
    return sorted(rows, key=lambda r: r["t"])
