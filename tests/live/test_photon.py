from app.sources import photon
from tests.live.conftest import PLACE, check


async def test_photon_adresse_monde():
    check(await photon.search("Lyon", 1), PLACE, required=("label", "lat", "lon", "country_code"))
