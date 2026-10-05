from app.sources import hn_algolia
from tests.live.conftest import NEWS, check


async def test_hn_algolia_recherche():
    check(await hn_algolia.tech_news("nvidia", 3), NEWS, required=("title", "url", "published_at"))


async def test_hn_algolia_une():
    check(await hn_algolia.tech_news(None, 3), NEWS, required=("title", "url", "published_at"))
