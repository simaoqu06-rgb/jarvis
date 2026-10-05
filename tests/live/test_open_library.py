from app.sources import open_library
from tests.live.conftest import BOOK, check


async def test_open_library_recherche():
    books = await open_library.books("the intelligent investor", 2)
    check(books, BOOK, required=("title", "url"))
    assert "Benjamin Graham" in books[0]["authors"]
