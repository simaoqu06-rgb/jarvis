from app.sources import coinlore
from tests.live.conftest import RANKING, check


async def test_coinlore_classement():
    ranking = await coinlore.crypto_ranking(5)
    check(ranking, RANKING, required=("rank", "symbol", "price_usd", "change_24h_pct", "market_cap_usd"))
    assert len(ranking) == 5
