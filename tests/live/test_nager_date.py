from app.sources import nager_date
from tests.live.conftest import HOLIDAY, check


async def test_nager_date_france():
    holidays = await nager_date.holidays("FR", 2026)
    check(holidays, HOLIDAY, required=("date", "name"))
    assert any(h["date"] == "2026-07-14" for h in holidays)
