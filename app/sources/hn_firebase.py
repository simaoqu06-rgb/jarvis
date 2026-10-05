"""Hacker News, API officielle (Firebase) : top des articles (sans clé). Secours."""
import asyncio
from datetime import datetime, timezone

from app.core.http import get_json

BASE = "https://hacker-news.firebaseio.com/v0"


async def _item(item_id: int) -> dict:
    item = await get_json(f"{BASE}/item/{item_id}.json")
    return {
        "title": item.get("title"),
        "url": item.get("url") or f"https://news.ycombinator.com/item?id={item_id}",
        "published_at": datetime.fromtimestamp(item["time"], timezone.utc).isoformat() if item.get("time") else None,
        "source": "Hacker News",
        "summary": None,
    }


async def tech_news(query: str | None, limit: int) -> list[dict]:
    ids = await get_json(f"{BASE}/topstories.json")
    items = await asyncio.gather(*(_item(i) for i in ids[: limit * 3 if query else limit]))
    if query:
        items = [i for i in items if query.lower() in (i["title"] or "").lower()]
    return items[:limit]
