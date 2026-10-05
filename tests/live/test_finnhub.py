from app.sources import finnhub
from tests.live.conftest import NEWS, QUOTE, check, needs_key

pytestmark = needs_key("FINNHUB_API_KEY")


async def test_finnhub_cours():
    check(await finnhub.stock_quotes(["AAPL"]), QUOTE, required=("price", "previous_close", "change_pct"))


async def test_finnhub_actus_generales():
    check(await finnhub.market_news(5), NEWS, required=("title", "url", "published_at"))


async def test_finnhub_actus_societe():
    check(await finnhub.company_news("AAPL", 5), NEWS, required=("title", "url", "published_at"))
