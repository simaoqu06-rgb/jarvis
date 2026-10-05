from app.sources import hn_firebase
from tests.live.conftest import NEWS, check


async def test_hn_firebase_top():
    check(await hn_firebase.tech_news(None, 3), NEWS, required=("title", "url", "published_at"))
