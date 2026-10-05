"""Open Library : recherche de livres (sans clé)."""
from app.core.http import SourceError, get_json

URL = "https://openlibrary.org/search.json"


def _isbn13(isbns: list[str] | None) -> str | None:
    for isbn in isbns or []:
        if len(isbn) == 13:
            return isbn
    return (isbns or [None])[0]


async def books(query: str, limit: int = 5) -> list[dict]:
    payload = await get_json(
        URL,
        params={"q": query, "limit": limit, "fields": "key,title,author_name,first_publish_year,isbn"},
    )
    docs = payload.get("docs") or []
    if not docs:
        raise SourceError(f"aucun livre pour « {query} »")
    return [
        {
            "title": d.get("title"),
            "authors": d.get("author_name") or [],
            "first_publish_year": d.get("first_publish_year"),
            "isbn": _isbn13(d.get("isbn")),
            "url": f"https://openlibrary.org{d['key']}",
        }
        for d in docs
    ]
