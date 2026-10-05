"""Wikipédia en français : résumé d'un article (sans clé, licence CC BY-SA)."""
from urllib.parse import quote

from app.core.http import SourceError, get_json

SUMMARY_URL = "https://fr.wikipedia.org/api/rest_v1/page/summary/{title}"
SEARCH_URL = "https://fr.wikipedia.org/w/api.php"


async def _summary(title: str) -> dict:
    page = await get_json(SUMMARY_URL.format(title=quote(title.replace(" ", "_"), safe="")))
    return {
        "title": page["title"],
        "description": page.get("description"),
        "extract": page.get("extract"),
        "url": page["content_urls"]["desktop"]["page"],
    }


async def lookup(topic: str) -> dict:
    """Cherche l'article le plus pertinent, puis renvoie son résumé."""
    found = await get_json(
        SEARCH_URL,
        params={"action": "query", "list": "search", "srsearch": topic, "srlimit": 1, "format": "json"},
    )
    hits = found["query"]["search"]
    if not hits:
        raise SourceError(f"aucun article pour « {topic} »")
    return await _summary(hits[0]["title"])
