from app.sources import wikidata
from tests.live.conftest import ARTICLE, check


async def test_wikidata_entite():
    check(await wikidata.lookup("Tesla"), ARTICLE, required=("title", "url"))
