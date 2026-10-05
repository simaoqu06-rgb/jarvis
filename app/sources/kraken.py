"""Kraken : prix publics en euros (sans clé). https://docs.kraken.com/rest/"""
from app.core.http import SourceError, get_json, to_float

URL = "https://api.kraken.com/0/public/Ticker"
# Kraken utilise ses propres codes pour certains actifs.
ALIASES = {"BTC": "XBT", "DOGE": "XDG"}


def _kraken_code(symbol: str) -> str:
    return ALIASES.get(symbol, symbol)


async def crypto_prices(symbols: list[str]) -> list[dict]:
    codes = {s: _kraken_code(s) for s in symbols}
    payload = await get_json(URL, params={"pair": ",".join(f"{c}EUR" for c in codes.values())})
    if payload.get("error"):
        raise SourceError("; ".join(payload["error"]))
    result = payload.get("result") or {}
    prices = []
    for symbol, code in codes.items():
        # Les paires historiques sont préfixées (XXBTZEUR), les récentes non (SOLEUR).
        ticker = result.get(f"{code}EUR") or result.get(f"X{code}ZEUR")
        if not ticker:
            raise SourceError(f"paire {symbol}/EUR absente de la réponse")
        vwap_24h = to_float(ticker["p"][1])
        volume_24h = to_float(ticker["v"][1])
        prices.append(
            {
                "symbol": symbol,
                "currency": "EUR",
                "price": to_float(ticker["c"][0]),
                "bid": to_float(ticker["b"][0]),
                "ask": to_float(ticker["a"][0]),
                "high_24h": to_float(ticker["h"][1]),
                "low_24h": to_float(ticker["l"][1]),
                "volume_24h_eur": round(vwap_24h * volume_24h, 2) if vwap_24h and volume_24h else None,
                # Kraken ne donne pas la variation sur 24 h (seulement l'ouverture du jour UTC).
                "change_24h_pct": None,
            }
        )
    return prices
