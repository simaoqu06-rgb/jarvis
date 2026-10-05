"""CoinLore : classement des cryptos (sans clé, ~1 appel/s recommandé)."""
from app.core.http import get_json, to_float

URL = "https://api.coinlore.net/api/tickers/"


async def crypto_ranking(limit: int) -> list[dict]:
    payload = await get_json(URL, params={"start": 0, "limit": limit})
    return [
        {
            "rank": int(c["rank"]),
            "symbol": c["symbol"],
            "name": c["name"],
            "price_usd": to_float(c.get("price_usd")),
            "change_24h_pct": to_float(c.get("percent_change_24h")),
            "market_cap_usd": to_float(c.get("market_cap_usd")),
            "volume_24h_usd": to_float(c.get("volume24")),
        }
        for c in payload["data"]
    ]
