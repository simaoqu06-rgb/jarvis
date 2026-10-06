"""Finnhub : cours US et actus (clé gratuite FINNHUB_API_KEY).

Les champs utilisés viennent du schéma officiel de la documentation
(https://finnhub.io/docs/api). Ils seront confirmés sur une vraie réponse
par tests/live/test_finnhub.py dès que la clé est disponible.
"""
import asyncio
from datetime import date, datetime, timedelta, timezone

from app.config import secret
from app.core.http import SourceError, get_json, to_float

BASE = "https://finnhub.io/api/v1"
KEY_NAME = "FINNHUB_API_KEY"


def _key() -> str:
    key = secret(KEY_NAME)
    if not key:
        raise SourceError(f"clé {KEY_NAME} absente")
    return key


async def _quote(symbol: str, key: str) -> dict:
    q = await get_json(f"{BASE}/quote", params={"symbol": symbol, "token": key})
    # Un symbole inconnu renvoie des zéros au lieu d'une erreur.
    if not q.get("c"):
        raise SourceError(f"{symbol} : pas de cours")
    return {
        "symbol": symbol,
        "name": None,
        "exchange": None,
        "currency": "USD",
        "price": to_float(q.get("c")),
        "open": to_float(q.get("o")),
        "high": to_float(q.get("h")),
        "low": to_float(q.get("l")),
        "previous_close": to_float(q.get("pc")),
        "change_pct": to_float(q.get("dp")),
        "volume": None,
        "is_market_open": None,
        "as_of": None,
    }


async def stock_quotes(symbols: list[str]) -> list[dict]:
    key = _key()
    return list(await asyncio.gather(*(_quote(s, key) for s in symbols)))


def _news_item(n: dict) -> dict:
    ts = n.get("datetime")
    return {
        "title": n.get("headline"),
        "url": n.get("url"),
        "published_at": datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None,
        "source": n.get("source"),
        "summary": n.get("summary"),
    }


async def market_news(limit: int) -> list[dict]:
    items = await get_json(f"{BASE}/news", params={"category": "general", "token": _key()})
    return [_news_item(n) for n in items[:limit]]


async def company_news(symbol: str, limit: int, days: int = 7) -> list[dict]:
    today = date.today()
    items = await get_json(
        f"{BASE}/company-news",
        params={
            "symbol": symbol,
            "from": (today - timedelta(days=days)).isoformat(),
            "to": today.isoformat(),
            "token": _key(),
        },
    )
    return [_news_item(n) for n in items[:limit]]


async def earnings(symbol: str, start: date, end: date) -> list[dict]:
    """Publications de résultats entre deux dates : date, moment (bmo avant l'ouverture, amc après
    la clôture), BPA estimé et publié."""
    payload = await get_json(
        f"{BASE}/calendar/earnings",
        params={"symbol": symbol, "from": start.isoformat(), "to": end.isoformat(), "token": _key()},
    )
    return [
        {"date": e.get("date"), "hour": e.get("hour") or None,
         "eps_estimate": to_float(e.get("epsEstimate")), "eps_actual": to_float(e.get("epsActual")),
         "revenue_estimate": to_float(e.get("revenueEstimate")), "revenue_actual": to_float(e.get("revenueActual"))}
        for e in payload.get("earningsCalendar") or [] if e.get("date")
    ]


async def profile(symbol: str) -> dict:
    """Nom, secteur et capitalisation (en dollars) d'une société."""
    p = await get_json(f"{BASE}/stock/profile2", params={"symbol": symbol, "token": _key()})
    if not p or not p.get("name"):
        raise SourceError(f"{symbol} : profil inconnu")
    cap = to_float(p.get("marketCapitalization"))
    return {"symbol": symbol, "name": p.get("name"), "industry": p.get("finnhubIndustry"),
            "market_cap": cap * 1e6 if cap is not None else None}
