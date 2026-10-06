from app.sources import kraken
from tests.live.conftest import CANDLE, PRICE, check


async def test_kraken_prix_eur():
    prices = await kraken.crypto_prices(["BTC", "ETH"])
    check(prices, PRICE, required=("price", "bid", "ask", "high_24h", "low_24h", "volume_24h_eur"))
    assert [p["symbol"] for p in prices] == ["BTC", "ETH"]


async def test_kraken_bougies():
    rows = await kraken.candles("BTC", 60)
    check(rows, CANDLE, required=("o", "h", "l", "c"))
    assert len(rows) > 500 and rows[0]["t"] < rows[-1]["t"]
