from app.sources import alpaca
from tests.live.conftest import CANDLE, needs_key

pytestmark = [needs_key("ALPACA_API_KEY"), needs_key("ALPACA_SECRET_KEY")]


async def test_alpaca_bougies_plusieurs_actions():
    rows = await alpaca.bars(["AAPL", "MSFT"], 1440, days=30)
    assert rows["AAPL"] and set(rows["AAPL"][0]) == CANDLE


async def test_alpaca_snapshots():
    snap = (await alpaca.snapshots(["AAPL"]))["AAPL"]
    assert snap["price"] and snap["prev_close"]


async def test_alpaca_screener():
    assert await alpaca.most_actives(10)
    assert await alpaca.top_gainers(10)


async def test_alpaca_compte_fictif():
    assert (await alpaca.account())["status"] == "ACTIVE"
