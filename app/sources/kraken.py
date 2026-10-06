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


OHLC_URL = "https://api.kraken.com/0/public/OHLC"


async def candles(symbol: str, interval_min: int, quote: str = "USD") -> list[dict]:
    """Bougies OHLC (720 au maximum), de la plus ancienne à la plus récente.

    La dernière bougie est celle en cours (pas encore close).
    """
    code = _kraken_code(symbol)
    payload = await get_json(OHLC_URL, params={"pair": f"{code}{quote}", "interval": interval_min})
    if payload.get("error"):
        raise SourceError("; ".join(payload["error"]))
    result = payload.get("result") or {}
    rows = next((v for k, v in result.items() if k != "last"), None)
    if not rows:
        raise SourceError(f"paire {symbol}/{quote} absente de la réponse")
    # [heure, ouverture, haut, bas, clôture, vwap, volume, nombre de transactions]
    return [
        {"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]), "v": float(r[6])}
        for r in rows
    ]
