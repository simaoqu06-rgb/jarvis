"""Spaceflight News API : actus spatiales, recherche par société (sans clé)."""
from app.core.http import get_json

URL = "https://api.spaceflightnewsapi.net/v4/articles/"


async def space_news(query: str | None, limit: int) -> list[dict]:
    params = {"limit": limit}
    if query:
        params["search"] = query
    payload = await get_json(URL, params=params)
    return [
        {
            "title": a.get("title"),
            "url": a.get("url"),
            "published_at": a.get("published_at"),
            "source": a.get("news_site"),
            "summary": a.get("summary"),
        }
        for a in payload["results"]
    ]
