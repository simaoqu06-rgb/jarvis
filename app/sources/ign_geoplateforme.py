"""IGN Géoplateforme : adresses françaises (Base Adresse Nationale), sans clé, 50 requêtes/s."""
from app.core.http import SourceError, get_json

URL = "https://data.geopf.fr/geocodage/search"


async def search(query: str, limit: int = 5) -> list[dict]:
    payload = await get_json(URL, params={"q": query, "limit": limit})
    features = payload.get("features") or []
    if not features:
        raise SourceError(f"aucune adresse trouvée pour « {query} »")
    return [
        {
            "label": f["properties"]["label"],
            "lat": f["geometry"]["coordinates"][1],
            "lon": f["geometry"]["coordinates"][0],
            "city": f["properties"].get("city"),
            "postcode": f["properties"].get("postcode"),
            "country_code": "FR",
            "timezone": None,
        }
        for f in features
    ]
