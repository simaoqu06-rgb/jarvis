"""Open-Meteo : météo, qualité de l'air et géocodage (sans clé).

Usage non commercial uniquement : 10 000 appels/jour, 5 000/h, 600/min.
Licence CC BY 4.0 : afficher « Données : Open-Meteo ».
"""
from app.core.http import SourceError, get_json, to_float

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
AIR_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"

# Codes météo WMO utilisés par Open-Meteo.
WMO_FR = {
    0: "ciel dégagé", 1: "plutôt dégagé", 2: "partiellement nuageux", 3: "couvert",
    45: "brouillard", 48: "brouillard givrant",
    51: "bruine légère", 53: "bruine", 55: "bruine forte", 56: "bruine verglaçante", 57: "bruine verglaçante forte",
    61: "pluie faible", 63: "pluie", 65: "pluie forte", 66: "pluie verglaçante", 67: "pluie verglaçante forte",
    71: "neige faible", 73: "neige", 75: "neige forte", 77: "grains de neige",
    80: "averses faibles", 81: "averses", 82: "averses violentes", 85: "averses de neige", 86: "fortes averses de neige",
    95: "orage", 96: "orage avec grêle", 99: "orage avec forte grêle",
}


async def weather(lat: float, lon: float) -> dict:
    payload = await get_json(
        FORECAST_URL,
        params={
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m",
            "timezone": "UTC",
        },
    )
    c = payload["current"]
    return {
        "time": c["time"] + "Z",
        "temperature_c": to_float(c.get("temperature_2m")),
        "apparent_temperature_c": to_float(c.get("apparent_temperature")),
        "humidity_pct": to_float(c.get("relative_humidity_2m")),
        "precipitation_mm": to_float(c.get("precipitation")),
        "wind_speed_kmh": to_float(c.get("wind_speed_10m")),
        "condition": WMO_FR.get(c.get("weather_code"), "inconnu"),
    }


async def air_quality(lat: float, lon: float) -> dict:
    payload = await get_json(
        AIR_URL,
        params={
            "latitude": lat,
            "longitude": lon,
            "current": "european_aqi,pm2_5,pm10,ozone,nitrogen_dioxide",
            "timezone": "UTC",
        },
    )
    c = payload["current"]
    return {
        "time": c["time"] + "Z",
        "aqi": to_float(c.get("european_aqi")),
        "aqi_scale": "européen (EAQI)",
        "pm2_5": to_float(c.get("pm2_5")),
        "pm10": to_float(c.get("pm10")),
        "o3": to_float(c.get("ozone")),
        "no2": to_float(c.get("nitrogen_dioxide")),
    }


async def geocode(name: str, limit: int = 5) -> list[dict]:
    payload = await get_json(GEOCODING_URL, params={"name": name, "count": limit, "language": "fr"})
    results = payload.get("results") or []
    if not results:
        raise SourceError(f"aucune ville trouvée pour « {name} »")
    return [
        {
            "label": ", ".join(p for p in (r.get("name"), r.get("admin1"), r.get("country")) if p),
            "lat": r["latitude"],
            "lon": r["longitude"],
            "city": r.get("name"),
            "postcode": None,
            "country_code": r.get("country_code"),
            "timezone": r.get("timezone"),
        }
        for r in results
    ]
