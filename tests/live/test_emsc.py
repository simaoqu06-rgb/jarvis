from app.sources import emsc
from tests.live.conftest import QUAKE, check


async def test_emsc_seismes():
    check(await emsc.earthquakes(4.5, 24 * 7), QUAKE, required=("id", "time", "magnitude", "place", "lat", "lon"))
