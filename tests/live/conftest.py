import pytest

from app.config import secret

# Champs que JARVIS utilise pour chaque type de donnée : s'ils disparaissent, le test échoue.
CANDLE = {"t", "o", "h", "l", "c", "v"}
PRICE = {"symbol", "currency", "price", "bid", "ask", "high_24h", "low_24h", "volume_24h_eur", "change_24h_pct"}
RANKING = {"rank", "symbol", "name", "price_usd", "change_24h_pct", "market_cap_usd", "volume_24h_usd"}
FX = {"base", "date", "rates"}
QUOTE = {"symbol", "name", "exchange", "currency", "price", "open", "high", "low", "previous_close",
         "change_pct", "volume", "is_market_open", "as_of"}
NEWS = {"title", "url", "published_at", "source", "summary"}
CALENDAR = {"datetime_utc", "title", "country", "impact", "forecast", "previous", "actual", "status"}
HOLIDAY = {"date", "name", "closed", "early_close"}
WEATHER = {"time", "temperature_c", "apparent_temperature_c", "humidity_pct", "precipitation_mm",
           "wind_speed_kmh", "condition"}
AIR = {"time", "aqi", "aqi_scale", "pm2_5", "pm10", "o3", "no2"}
QUAKE = {"id", "time", "magnitude", "place", "lat", "lon", "depth_km", "tsunami", "url"}
PLACE = {"label", "lat", "lon", "city", "postcode", "country_code", "timezone"}
ARTICLE = {"title", "description", "extract", "url"}
BOOK = {"title", "authors", "first_publish_year", "isbn", "url"}


def check(items, fields, required=()):
    """Vérifie la forme normalisée et que les champs indispensables ont une valeur."""
    items = items if isinstance(items, list) else [items]
    assert items, "réponse vide"
    for item in items:
        assert set(item) == fields, f"champs inattendus : {set(item) ^ fields}"
        for name in required:
            assert item[name] is not None, f"{name} vide dans {item}"


def needs_key(name):
    return pytest.mark.skipif(not secret(name), reason=f"{name} absente")


def pytest_collection_modifyitems(items):
    for item in items:
        if "live" in item.path.parts:  # ce hook voit tous les tests, pas seulement ceux de ce dossier
            item.add_marker(pytest.mark.live)
