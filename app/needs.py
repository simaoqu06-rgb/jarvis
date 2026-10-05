"""Chaque donnée de JARVIS : sa source principale, sa source de secours et la durée du cache.

Toutes les fonctions renvoient un Result (voir app/core/result.py) au même format.
"""
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from app.core.fallback import Source, resolve
from app.core.result import Result
from app.sources import (
    caldays, coinlore, coinpaprika, currency_api, emsc, finnhub, frankfurter, helious,
    hn_algolia, hn_firebase, ign_geoplateforme, kraken, launch_library, met_norway,
    nager_date, nominatim, open_library, open_meteo, photon, spaceflight_news,
    the_calendar, twelvedata, usgs, wikidata, wikipedia,
)

MINUTE = 60
HOUR = 3600
DAY = 86400

OPEN_METEO = "Données : Open-Meteo (CC BY 4.0)"
OSM = "© OpenStreetMap"
HELIOUS = "Source : Helious (https://helious.io)"


def _symbols(symbols: list[str]) -> list[str]:
    return sorted({s.strip().upper() for s in symbols if s.strip()})


async def crypto_prices(symbols: list[str]) -> Result:
    symbols = _symbols(symbols)
    return await resolve(
        "crypto_prices", f"crypto_prices:{','.join(symbols)}",
        [
            Source("Kraken", kraken.crypto_prices, "Données : Kraken"),
            Source("CoinPaprika", coinpaprika.crypto_prices, "Données : CoinPaprika"),
        ],
        ttl=MINUTE, symbols=symbols,
    )


async def crypto_ranking(limit: int = 20) -> Result:
    return await resolve(
        "crypto_ranking", f"crypto_ranking:{limit}",
        [
            Source("CoinLore", coinlore.crypto_ranking, "Données : CoinLore"),
            Source("CoinPaprika", coinpaprika.crypto_ranking, "Données : CoinPaprika"),
        ],
        ttl=MINUTE, limit=limit,
    )


async def fx_rates(base: str = "EUR", symbols: list[str] | None = None) -> Result:
    base = base.upper()
    symbols = _symbols(symbols or ["USD", "GBP", "CHF", "JPY"])
    # Taux officiels publiés une fois par jour : une heure de cache suffit.
    return await resolve(
        "fx_rates", f"fx:{base}:{','.join(symbols)}",
        [
            Source("Frankfurter", frankfurter.fx_rates, "Taux : Frankfurter (BCE)"),
            Source("Currency-api", currency_api.fx_rates, "Taux : fawazahmed0/currency-api"),
        ],
        ttl=HOUR, base=base, symbols=symbols,
    )


async def stock_quotes(symbols: list[str]) -> Result:
    symbols = _symbols(symbols)
    return await resolve(
        "stock_quotes", f"stocks:{','.join(symbols)}",
        [
            Source("Twelve Data", twelvedata.stock_quotes, "Données : Twelve Data", "TWELVEDATA_API_KEY"),
            Source("Finnhub", finnhub.stock_quotes, "Données : Finnhub", "FINNHUB_API_KEY"),
        ],
        ttl=MINUTE, symbols=symbols,
    )


async def market_news(limit: int = 20) -> Result:
    return await resolve(
        "market_news", f"market_news:{limit}",
        [
            Source("Helious", helious.market_news, HELIOUS),
            Source("Finnhub", finnhub.market_news, "Données : Finnhub", "FINNHUB_API_KEY"),
        ],
        ttl=5 * MINUTE, limit=limit,
    )


async def company_news(symbol: str, limit: int = 10) -> Result:
    symbol = symbol.strip().upper()
    # MarketAux (actus par ticker) sera ajouté quand sa clé et une vraie réponse seront disponibles.
    return await resolve(
        "company_news", f"company_news:{symbol}:{limit}",
        [Source("Finnhub", finnhub.company_news, "Données : Finnhub", "FINNHUB_API_KEY")],
        ttl=30 * MINUTE, symbol=symbol, limit=limit,
    )


async def tech_news(query: str | None = None, limit: int = 20) -> Result:
    return await resolve(
        "tech_news", f"tech_news:{query or ''}:{limit}",
        [
            Source("Hacker News (Algolia)", hn_algolia.tech_news, "Source : Hacker News"),
            Source("Hacker News", hn_firebase.tech_news, "Source : Hacker News"),
        ],
        ttl=5 * MINUTE, query=query, limit=limit,
    )


async def space_news(query: str | None = None, limit: int = 20) -> Result:
    return await resolve(
        "space_news", f"space_news:{query or ''}:{limit}",
        [
            Source("Spaceflight News", spaceflight_news.space_news, "Source : Spaceflight News API"),
            Source("Launch Library 2", launch_library.space_news, "Source : The Space Devs"),
        ],
        ttl=5 * MINUTE, query=query, limit=limit,
    )


async def economic_calendar() -> Result:
    # Pas de secours sans clé : FRED sera ajouté quand sa clé sera disponible.
    return await resolve(
        "economic_calendar", "economic_calendar",
        [Source("Helious", helious.economic_calendar, "Source : Helious (https://helious.io/calendar)")],
        ttl=HOUR,
    )


async def us_market_holidays(year: int | None = None) -> Result:
    year = year or date.today().year
    return await resolve(
        "us_market_holidays", f"us_market_holidays:{year}",
        [
            Source("the-calendar.net", the_calendar.us_market_holidays, "Data by the-calendar.net (CC BY 4.0)"),
            # Secours approximatif : fériés fédéraux, pas exactement les fermetures de la Bourse.
            Source("Nager.Date (fériés fédéraux US)", nager_date.us_market_holidays, "Source : Nager.Date"),
        ],
        ttl=DAY, year=year,
    )


async def holidays(country: str = "FR", year: int | None = None) -> Result:
    year = year or date.today().year
    country = country.upper()
    return await resolve(
        "holidays", f"holidays:{country}:{year}",
        [
            Source("Nager.Date", nager_date.holidays, "Source : Nager.Date"),
            Source("caldays", caldays.holidays, "Source : caldays.com (CC BY 4.0)"),
        ],
        ttl=DAY, country=country, year=year,
    )


NEW_YORK = ZoneInfo("America/New_York")
OPEN, CLOSE, EARLY_CLOSE = time(9, 30), time(16, 0), time(13, 0)


async def us_market_status(now: datetime | None = None) -> dict:
    """Bourse de New York ouverte ou fermée maintenant (heures régulières 9 h 30 à 16 h, heure de New York)."""
    now_ny = (now or datetime.now(timezone.utc)).astimezone(NEW_YORK)
    calendar = await us_market_holidays(now_ny.year)
    today = now_ny.date().isoformat()
    holiday = next((h for h in (calendar.data or []) if h["date"] == today), None)
    close = EARLY_CLOSE if holiday and holiday["early_close"] else CLOSE
    if now_ny.weekday() >= 5:
        is_open, reason = False, "week-end"
    elif holiday and holiday["closed"]:
        is_open, reason = False, f"jour férié : {holiday['name']}"
    else:
        is_open = OPEN <= now_ny.time() < close
        reason = "séance en cours" if is_open else "hors des heures de séance"
        if holiday and holiday["early_close"]:
            reason += " (fermeture anticipée à 13 h)"
    return {
        "exchange": "NYSE/Nasdaq",
        "is_open": is_open,
        "reason": reason,
        "local_time_new_york": now_ny.isoformat(timespec="minutes"),
        "calendar": calendar.to_dict() | {"data": None},
    }


async def weather(lat: float, lon: float) -> Result:
    return await resolve(
        "weather", f"weather:{lat:.2f},{lon:.2f}",
        [
            Source("Open-Meteo", open_meteo.weather, OPEN_METEO),
            Source("MET Norway", met_norway.weather, "Données : MET Norway"),
        ],
        ttl=10 * MINUTE, lat=lat, lon=lon,
    )


async def air_quality(lat: float, lon: float) -> Result:
    # Secours AQICN à ajouter quand sa clé (gratuite) et une vraie réponse seront disponibles.
    return await resolve(
        "air_quality", f"air:{lat:.2f},{lon:.2f}",
        [Source("Open-Meteo", open_meteo.air_quality, OPEN_METEO)],
        ttl=10 * MINUTE, lat=lat, lon=lon,
    )


async def earthquakes(min_magnitude: float = 4.5, hours: int = 24) -> Result:
    return await resolve(
        "earthquakes", f"earthquakes:{min_magnitude}:{hours}",
        [
            Source("USGS", usgs.earthquakes, "Source : USGS"),
            Source("EMSC", emsc.earthquakes, "Source : EMSC"),
        ],
        ttl=5 * MINUTE, min_magnitude=min_magnitude, hours=hours,
    )


async def geocode_city(name: str) -> Result:
    async def nominatim_city(name: str) -> list[dict]:
        return await nominatim.search(name)

    return await resolve(
        "geocode_city", f"city:{name.strip().lower()}",
        [
            Source("Open-Meteo Geocoding", open_meteo.geocode, OPEN_METEO),
            Source("Nominatim", nominatim_city, OSM),
        ],
        ttl=DAY, name=name,
    )


async def address_france(query: str) -> Result:
    async def nominatim_fr(query: str) -> list[dict]:
        return await nominatim.search(query, country_codes="fr")

    return await resolve(
        "address_france", f"address_fr:{query.strip().lower()}",
        [
            Source("IGN Géoplateforme", ign_geoplateforme.search, "Source : IGN Géoplateforme (BAN)"),
            Source("Nominatim", nominatim_fr, OSM),
        ],
        ttl=DAY, query=query,
    )


async def address_world(query: str) -> Result:
    return await resolve(
        "address_world", f"address_world:{query.strip().lower()}",
        [
            Source("Nominatim", nominatim.search, OSM),
            Source("Photon", photon.search, OSM + " (Photon)"),
        ],
        ttl=DAY, query=query,
    )


async def encyclopedia(topic: str) -> Result:
    return await resolve(
        "encyclopedia", f"wiki:{topic.strip().lower()}",
        [
            Source("Wikipédia", wikipedia.lookup, "Source : Wikipédia (CC BY-SA)"),
            Source("Wikidata", wikidata.lookup, "Source : Wikidata"),
        ],
        ttl=DAY, topic=topic,
    )


async def books(query: str) -> Result:
    # Secours Google Books à ajouter avec sa clé gratuite (quota sans clé épuisé lors des tests).
    return await resolve(
        "books", f"books:{query.strip().lower()}",
        [Source("Open Library", open_library.books, "Source : Open Library")],
        ttl=DAY, query=query,
    )
