from app.sources import wikipedia
from tests.live.conftest import ARTICLE, check


async def test_wikipedia_resume():
    article = await wikipedia.lookup("Nvidia")
    check(article, ARTICLE, required=("title", "extract", "url"))
    assert article["url"].startswith("https://fr.wikipedia.org/")
