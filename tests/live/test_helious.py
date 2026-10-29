from app.sources import helious
from tests.live.conftest import CALENDAR, check


async def test_helious_calendrier():
    check(await helious.economic_calendar(), CALENDAR, required=("datetime_utc", "title", "country", "impact"))
