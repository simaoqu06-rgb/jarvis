"""Nominatim (OpenStreetMap) : adresses du monde entier, sans clé.

Règles d'usage : 1 requête/s au maximum, User-Agent identifiant, mention « © OpenStreetMap ».
"""
import asyncio
import time

from app.core.http import SourceError, get_json

URL = "https://nominatim.openstreetmap.org/search"
_lock = asyncio.Lock()
_last_call = 0.0


async def search(query: str, limit: int = 5, country_codes: str | None = None) -> list[dict]:
    global _last_call
    params = {"q": query, "format": "jsonv2", "limit": limit, "addressdetails": 1}
    if country_codes:
        params["countrycodes"] = country_codes
    async with _lock:  # garantit au plus un appel par seconde
        wait = 1.0 - (time.monotonic() - _last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            payload = await get_json(URL, params=params)
        finally:
            _last_call = time.monotonic()
    if not payload:
        raise SourceError(f"aucune adresse trouvée pour « {query} »")
    places = []
    for p in payload:
        address = p.get("address") or {}
        places.append(
            {
                "label": p["display_name"],
                "lat": float(p["lat"]),
                "lon": float(p["lon"]),
                "city": address.get("city") or address.get("town") or address.get("village"),
                "postcode": address.get("postcode"),
                "country_code": (address.get("country_code") or "").upper() or None,
                "timezone": None,
            }
        )
    return places
