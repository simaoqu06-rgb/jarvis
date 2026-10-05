"""CoinPaprika : prix en euros et classement (sans clé, ~20 000 appels/mois). Sert de secours."""
import asyncio

from app.core.http import SourceError, get_json, to_float

BASE = "https://api.coinpaprika.com/v1"
_ids: dict[str, str] = {"BTC": "btc-bitcoin", "ETH": "eth-ethereum", "SOL": "sol-solana"}


async def _coin_id(symbol: str) -> str:
    if symbol in _ids:
        return _ids[symbol]
    found = await get_json(f"{BASE}/search", params={"q": symbol, "c": "currencies", "limit": 5})
    for coin in found.get("currencies", []):
        if coin.get("symbol", "").upper() == symbol:
            _ids[symbol] = coin["id"]
            return coin["id"]
    raise SourceError(f"{symbol} introuvable")


async def _price(symbol: str) -> dict:
    ticker = await get_json(f"{BASE}/tickers/{await _coin_id(symbol)}", params={"quotes": "EUR"})
    quote = ticker["quotes"]["EUR"]
    return {
        "symbol": symbol,
        "currency": "EUR",
        "price": to_float(quote.get("price")),
        "bid": None,
        "ask": None,
        "high_24h": None,
        "low_24h": None,
        "volume_24h_eur": to_float(quote.get("volume_24h")),
        "change_24h_pct": to_float(quote.get("percent_change_24h")),
    }


async def crypto_prices(symbols: list[str]) -> list[dict]:
    return list(await asyncio.gather(*(_price(s) for s in symbols)))


async def crypto_ranking(limit: int) -> list[dict]:
    tickers = await get_json(f"{BASE}/tickers", params={"quotes": "USD"})
    ranked = sorted((t for t in tickers if t.get("rank")), key=lambda t: t["rank"])[:limit]
    return [
        {
            "rank": t["rank"],
            "symbol": t["symbol"],
            "name": t["name"],
            "price_usd": to_float(t["quotes"]["USD"].get("price")),
            "change_24h_pct": to_float(t["quotes"]["USD"].get("percent_change_24h")),
            "market_cap_usd": to_float(t["quotes"]["USD"].get("market_cap")),
            "volume_24h_usd": to_float(t["quotes"]["USD"].get("volume_24h")),
        }
        for t in ranked
    ]
