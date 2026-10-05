from app.sources import frankfurter
from tests.live.conftest import FX, check


async def test_frankfurter_taux():
    fx = await frankfurter.fx_rates("EUR", ["USD", "GBP"])
    check(fx, FX)
    assert fx["rates"]["USD"] > 0 and fx["rates"]["GBP"] > 0
