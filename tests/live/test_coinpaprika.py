from app.sources import coinpaprika
from tests.live.conftest import PRICE, RANKING, check


async def test_coinpaprika_prix_eur():
    check(await coinpaprika.crypto_prices(["BTC", "SOL"]), PRICE, required=("price", "change_24h_pct"))


async def test_coinpaprika_classement():
    ranking = await coinpaprika.crypto_ranking(5)
    check(ranking, RANKING, required=("rank", "symbol", "price_usd", "market_cap_usd"))
    assert ranking[0]["rank"] == 1
