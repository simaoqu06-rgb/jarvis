"""MET Norway : prévisions mondiales (sans clé, User-Agent identifiant obligatoire). Secours météo."""
from app.core.http import get_json, to_float

URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"

SYMBOL_FR = {
    "clearsky": "ciel dégagé", "fair": "plutôt dégagé", "partlycloudy": "partiellement nuageux",
    "cloudy": "couvert", "fog": "brouillard", "lightrain": "pluie faible", "rain": "pluie",
    "heavyrain": "pluie forte", "lightrainshowers": "averses faibles", "rainshowers": "averses",
    "heavyrainshowers": "averses violentes", "lightsnow": "neige faible", "snow": "neige",
    "heavysnow": "neige forte", "sleet": "neige fondue", "rainandthunder": "orage",
    "heavyrainandthunder": "orage", "rainshowersandthunder": "orage",
}


async def weather(lat: float, lon: float) -> dict:
    # MET Norway demande au plus 4 décimales.
    payload = await get_json(URL, params={"lat": round(lat, 4), "lon": round(lon, 4)})
    now = payload["properties"]["timeseries"][0]
    details = now["data"]["instant"]["details"]
    next_hour = now["data"].get("next_1_hours", {})
    symbol = (next_hour.get("summary") or {}).get("symbol_code", "")
    wind_ms = to_float(details.get("wind_speed"))
    return {
        "time": now["time"],
        "temperature_c": to_float(details.get("air_temperature")),
        "apparent_temperature_c": None,
        "humidity_pct": to_float(details.get("relative_humidity")),
        "precipitation_mm": to_float((next_hour.get("details") or {}).get("precipitation_amount")),
        "wind_speed_kmh": round(wind_ms * 3.6, 1) if wind_ms is not None else None,
        "condition": SYMBOL_FR.get(symbol.split("_")[0], symbol or "inconnu"),
    }
