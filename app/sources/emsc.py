"""EMSC (centre sismologique euro-méditerranéen) : séismes mondiaux (sans clé). Secours de l'USGS."""
from datetime import datetime, timedelta, timezone

from app.core.http import get_json

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
            "magnitude": f["properties"].get("mag"),
            "place": f["properties"].get("flynn_region"),
            "lat": f["properties"].get("lat"),
            "lon": f["properties"].get("lon"),
            "depth_km": f["properties"].get("depth"),
            "tsunami": None,
            "url": None,
        }
        for f in payload["features"]
    ]
