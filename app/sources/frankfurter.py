"""Frankfurter : taux de change officiels (BCE), publiés une fois par jour, sans clé."""
from app.core.http import get_json, to_float

URL = "https://api.frankfurter.dev/v1/latest"


async def fx_rates(base: str, symbols: list[str]) -> dict:
    payload = await get_json(URL, params={"base": base, "symbols": ",".join(symbols)})
    return {
        "base": payload["base"],
        "date": payload["date"],
        "rates": {code: to_float(rate) for code, rate in payload["rates"].items()},
    }
