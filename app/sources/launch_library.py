"""Launch Library 2 (The Space Devs) : prochains lancements (sans clé). Secours des actus spatiales."""
from app.core.http import get_json

URL = "https://ll.thespacedevs.com/2.3.0/launches/upcoming/"


async def space_news(query: str | None, limit: int) -> list[dict]:
    params = {"limit": limit, "mode": "list"}
    if query:
        params["search"] = query
    payload = await get_json(URL, params=params)
    return [
        {
            "title": f"Lancement prévu : {launch.get('name')}",
            "url": launch.get("url"),
            "published_at": launch.get("net"),
            "source": "Launch Library 2",
            "summary": (launch.get("status") or {}).get("name"),
        }
        for launch in payload["results"]
    ]
