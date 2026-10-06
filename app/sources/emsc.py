"""EMSC (centre sismologique euro-méditerranéen) : séismes mondiaux (sans clé). Secours de l'USGS."""
from datetime import datetime, timedelta, timezone

from app.core.http import get_json, to_float

URL = "https://www.seismicportal.eu/fdsnws/event/1/query"


async def earthquakes(min_magnitude: float, hours: int) -> list[dict]:
    start = datetime.now(timezone.utc) - timedelta(hours=hours)
    payload = await get_json(
        URL,
        params={
            "format": "json",
            "start": start.strftime("%Y-%m-%dT%H:%M:%S"),
            "minmag": min_magnitude,
            "orderby": "time",
        },
    )
    return [
        {
            "id": f["properties"].get("unid") or f["id"],
            "time": f["properties"]["time"],
            "magnitude": to_float(f["properties"].get("mag")),
            "place": f["properties"].get("flynn_region"),
            "lat": to_float(f["properties"].get("lat")),
            "lon": to_float(f["properties"].get("lon")),
            # Profondeur parfois absente chez l'EMSC : None (affichée « n/d »), jamais d'erreur.
            "depth_km": to_float(f["properties"].get("depth")),
            "tsunami": None,
            "url": None,
        }
        for f in payload["features"]
    ]
