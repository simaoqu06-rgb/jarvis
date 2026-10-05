"""caldays.com : jours fériés de l'année en cours (sans clé, CC BY 4.0). Secours de Nager.Date."""
from app.core.http import SourceError, get_json

URL = "https://caldays.com/api/holidays/{country}"


async def holidays(country: str, year: int) -> list[dict]:
    payload = await get_json(URL.format(country=country.lower()))
    if payload.get("year") != year:
        raise SourceError(f"seule l'année {payload.get('year')} est disponible")
    return [{"date": h["date"], "name": h["name"], "closed": True, "early_close": False} for h in payload["holidays"]]
