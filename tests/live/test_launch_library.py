from app.sources import launch_library
from tests.live.conftest import NEWS, check


async def test_launch_library_prochains_lancements():
    check(await launch_library.space_news(None, 3), NEWS, required=("title", "url", "published_at"))
