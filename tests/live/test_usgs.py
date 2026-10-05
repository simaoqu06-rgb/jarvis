from app.sources import usgs
from tests.live.conftest import QUAKE, check


async def test_usgs_seismes():
    # Sur 7 jours, il y a toujours des séismes de magnitude 4,5 ou plus.
    check(await usgs.earthquakes(4.5, 24 * 7), QUAKE, required=("id", "time", "magnitude", "lat", "lon", "url"))
