from app.sources import open_meteo
from tests.live.conftest import AIR, PLACE, WEATHER, check

PARIS = (48.8566, 2.3522)


async def test_open_meteo_meteo():
    check(await open_meteo.weather(*PARIS), WEATHER,
          required=("time", "temperature_c", "humidity_pct", "wind_speed_kmh", "condition"))


async def test_open_meteo_air():
    check(await open_meteo.air_quality(*PARIS), AIR, required=("time", "aqi", "pm2_5", "pm10"))


async def test_open_meteo_geocodage():
    places = await open_meteo.geocode("Lyon", 1)
    check(places, PLACE, required=("label", "lat", "lon", "country_code", "timezone"))
    assert places[0]["country_code"] == "FR"
