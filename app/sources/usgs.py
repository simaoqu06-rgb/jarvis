"""USGS : séismes en temps réel dans le monde (sans clé, domaine public)."""
from datetime import datetime, timedelta, timezone

from app.core.http import get_json

URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"


async def earthquakes(min_magnitude: float, hours: int) -> list[dict]:
    start = datetime.now(timezone.utc) - timedelta(hours=hours)
    payload = await get_json(
        URL,
        params={
            "format": "geojson",
            "starttime": start.strftime("%Y-%m-%dT%H:%M:%S"),
            "minmagnitude": min_magnitude,
            "orderby": "time",
        },
    )
    quakes = []
    for f in payload["features"]:
        p = f["properties"]
        lon, lat, depth = f["geometry"]["coordinates"]
        quakes.append(
            {
                "id": f["id"],
                "time": datetime.fromtimestamp(p["time"] / 1000, timezone.utc).isoformat(),
                "magnitude": p.get("mag"),
                "place": p.get("place"),
                "lat": lat,
                "lon": lon,
                "depth_km": depth,
                "tsunami": bool(p.get("tsunami")),
                "url": p.get("url"),
            }
        )
    return quakes
