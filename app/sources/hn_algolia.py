"""Hacker News via Algolia : recherche d'articles tech par mot-clé (sans clé)."""
from app.core.http import get_json

SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
FRONT_URL = "https://hn.algolia.com/api/v1/search"


async def tech_news(query: str | None, limit: int) -> list[dict]:
    if query:
        payload = await get_json(SEARCH_URL, params={"tags": "story", "query": query, "hitsPerPage": limit})
    else:
        payload = await get_json(FRONT_URL, params={"tags": "front_page", "hitsPerPage": limit})
    return [
        {
            "title": hit.get("title"),
            "url": hit.get("url") or f"https://news.ycombinator.com/item?id={hit['objectID']}",
            "published_at": hit.get("created_at"),
            "source": "Hacker News",
            "summary": None,
        }
        for hit in payload["hits"]
    ]
