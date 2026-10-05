from app.sources import currency_api
from tests.live.conftest import FX, check


async def test_currency_api_taux():
    fx = await currency_api.fx_rates("EUR", ["USD", "GBP"])
    check(fx, FX)
    assert fx["rates"]["USD"] > 0 and fx["rates"]["GBP"] > 0
