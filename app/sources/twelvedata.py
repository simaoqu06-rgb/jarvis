"""Twelve Data : cours des actions US (clé gratuite TWELVEDATA_API_KEY, 8 requêtes/min)."""
import asyncio
from datetime import datetime, timezone

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
