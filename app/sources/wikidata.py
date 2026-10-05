"""Wikidata : description courte d'une entité (lecture sans clé, CC0). Secours de Wikipédia."""
from app.core.http import SourceError, get_json

URL = "https://www.wikidata.org/w/api.php"


async def lookup(topic: str) -> dict:
    payload = await get_json(
        URL,
        params={"action": "wbsearchentities", "search": topic, "language": "fr", "uselang": "fr", "format": "json", "limit": 1},
    )
    hits = payload.get("search") or []
    if not hits:
        raise SourceError(f"aucune entité pour « {topic} »")
    hit = hits[0]
    display = hit.get("display") or {}
    return {
        "title": (display.get("label") or {}).get("value") or hit["id"],
        "description": (display.get("description") or {}).get("value"),
        "extract": None,
        "url": hit["concepturi"],
    }
