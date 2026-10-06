from app.sources import twelvedata
from tests.live.conftest import CANDLE, QUOTE, check, needs_key


async def test_twelvedata_cle_publique_demo():
    # La clé publique « demo » ne donne que AAPL : elle suffit à vérifier les champs.
    check(await twelvedata.stock_quotes(["AAPL"], apikey="demo"), QUOTE, required=("price", "previous_close", "change_pct"))


@needs_key("TWELVEDATA_API_KEY")
async def test_twelvedata_avec_ta_cle():
    check(await twelvedata.stock_quotes(["MSFT", "NVDA"]), QUOTE, required=("price", "previous_close", "change_pct"))


@needs_key("TWELVEDATA_API_KEY")
async def test_twelvedata_bougies():
    rows = await twelvedata.candles("NVDA", 15, outputsize=100)
    check(rows, CANDLE, required=("o", "h", "l", "c"))
    assert len(rows) == 100 and rows[0]["t"] < rows[-1]["t"]
