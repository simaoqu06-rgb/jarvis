from app.sources import met_norway
from tests.live.conftest import WEATHER, check


async def test_met_norway_meteo():
    check(await met_norway.weather(48.8566, 2.3522), WEATHER,
          required=("time", "temperature_c", "humidity_pct", "wind_speed_kmh", "condition"))
