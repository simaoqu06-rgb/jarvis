from app.sources import nominatim
from tests.live.conftest import PLACE, check


async def test_nominatim_adresse_monde():
    check(await nominatim.search("Times Square, New York", 1), PLACE, required=("label", "lat", "lon", "country_code"))
