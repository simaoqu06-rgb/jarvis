from app.sources import helious
from tests.live.conftest import CALENDAR, NEWS, check


async def test_helious_actus():
    check(await helious.market_news(3), NEWS, required=("title", "url", "published_at"))


async def test_helious_calendrier():
    check(await helious.economic_calendar(), CALENDAR, required=("datetime_utc", "title", "country", "impact"))
