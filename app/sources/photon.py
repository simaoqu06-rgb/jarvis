"""Photon (komoot, données OpenStreetMap) : adresses du monde, sans clé. Secours de Nominatim."""
from app.core.http import SourceError, get_json

URL = "https://photon.komoot.io/api/"


async def search(query: str, limit: int = 5) -> list[dict]:
    payload = await get_json(URL, params={"q": query, "limit": limit})
    features = payload.get("features") or []
    if not features:
        raise SourceError(f"aucune adresse trouvée pour « {query} »")
    places = []
    for f in features:
        p = f["properties"]
        label_parts = [p.get("name"), p.get("street"), p.get("postcode"), p.get("city"), p.get("state"), p.get("country")]
        places.append(
            {
                "label": ", ".join(dict.fromkeys(x for x in label_parts if x)),
                "lat": f["geometry"]["coordinates"][1],
                "lon": f["geometry"]["coordinates"][0],
                "city": p.get("city") or (p.get("name") if p.get("type") == "city" else None),
                "postcode": p.get("postcode"),
                "country_code": p.get("countrycode"),
                "timezone": None,
            }
        )
    return places
