"""Nager.Date : jours fériés par pays (sans clé, pas de limite annoncée)."""
from app.core.http import get_json

URL = "https://date.nager.at/api/v3/PublicHolidays/{year}/{country}"


async def holidays(country: str, year: int) -> list[dict]:
    payload = await get_json(URL.format(year=year, country=country.upper()))
    return [
        {"date": h["date"], "name": h.get("localName") or h.get("name"), "closed": True, "early_close": False}
        for h in payload
    ]


async def us_market_holidays(year: int) -> list[dict]:
    """Approximation : fériés fédéraux US. Ils ne correspondent pas exactement aux
    fermetures de la Bourse (Vendredi saint fermé, Columbus Day ouvert...)."""
    return await holidays("US", year)
