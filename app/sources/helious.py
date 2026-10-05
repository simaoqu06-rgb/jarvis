"""Helious : actus de marché (retard de 60 min sans clé) et calendrier économique.

Sans clé : 30 appels/min partagés. Mention « Helious » + lien demandée.
"""
from app.core.http import get_json

BASE = "https://api.helious.io/v1"
NEWS_PAGE = "https://helious.io/news"  # lien donné par le flux lui-même (champ source.url)


async def market_news(limit: int) -> list[dict]:
    payload = await get_json(f"{BASE}/feed", params={"limit": limit})
    return [
        {
            "title": item.get("title"),
            "url": item.get("url") or NEWS_PAGE,  # certains suivis n'ont pas de lien propre
            "published_at": item.get("created_at"),
            "source": "Helious",
            "summary": (item.get("text") or "")[:500] or None,
        }
        for item in payload["data"]["items"]
    ]


async def economic_calendar() -> list[dict]:
    payload = await get_json(f"{BASE}/calendar")
    return [
        {
            "datetime_utc": e.get("datetime_utc"),
            "title": e.get("title"),
            "country": e.get("country"),
            "impact": e.get("impact"),
            "forecast": e.get("forecast"),
            "previous": e.get("previous"),
            "actual": e.get("actual"),
            "status": e.get("status"),
        }
        for e in payload["data"]["events"]
    ]
