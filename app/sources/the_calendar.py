"""the-calendar.net : jours de fermeture NYSE/Nasdaq (sans clé, CC BY 4.0)."""
from app.core.http import get_json

URL = "https://the-calendar.net/api/finance/market-holidays/{year}.json"


async def us_market_holidays(year: int) -> list[dict]:
    payload = await get_json(URL.format(year=year))
    holidays = []
    for h in payload["holidays"]:
        name = h["name"]
        early = "early close" in name.lower()
        holidays.append({"date": h["date"], "name": name, "closed": not early, "early_close": early})
    return holidays
