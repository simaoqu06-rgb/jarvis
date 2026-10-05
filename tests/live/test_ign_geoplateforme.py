from app.sources import ign_geoplateforme
from tests.live.conftest import PLACE, check


async def test_ign_adresse():
    places = await ign_geoplateforme.search("8 bd du port amiens", 1)
    check(places, PLACE, required=("label", "lat", "lon", "city", "postcode"))
    assert places[0]["postcode"] == "80000"
