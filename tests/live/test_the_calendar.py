from app.sources import the_calendar
from tests.live.conftest import HOLIDAY, check


async def test_the_calendar_fermetures_nyse_2026():
    holidays = await the_calendar.us_market_holidays(2026)
    check(holidays, HOLIDAY, required=("date", "name"))
    assert any(h["date"] == "2026-04-03" and h["closed"] for h in holidays)  # Vendredi saint
