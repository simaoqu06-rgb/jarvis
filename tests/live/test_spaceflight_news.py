from app.sources import spaceflight_news
from tests.live.conftest import NEWS, check


async def test_spaceflight_news_recherche():
    check(await spaceflight_news.space_news("Rocket Lab", 3), NEWS, required=("title", "url", "published_at", "source"))
