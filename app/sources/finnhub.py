"""Finnhub : cours US et actus (clé gratuite FINNHUB_API_KEY).

Les champs utilisés viennent du schéma officiel de la documentation
(https://finnhub.io/docs/api). Ils seront confirmés sur une vraie réponse
par tests/live/test_finnhub.py dès que la clé est disponible.
"""
import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

from app.config import secret
from app.core.http import SourceError, get_json, to_float

BASE = "https://finnhub.io/api/v1"
KEY_NAME = "FINNHUB_API_KEY"
log = logging.getLogger(__name__)


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


def _failed(symbol: str, error: Exception) -> dict:
    """Cours manquant pour un symbole : même forme, valeurs vides, et l'erreur en clair."""
    return {"symbol": symbol, "name": None, "exchange": None, "currency": "USD", "price": None, "open": None,
            "high": None, "low": None, "previous_close": None, "change_pct": None, "volume": None,
            "is_market_open": None, "as_of": None, "error": str(error)}


async def stock_quotes(symbols: list[str]) -> list[dict]:
    key = _key()
    results = await asyncio.gather(*(_quote(s, key) for s in symbols), return_exceptions=True)
    quotes, errors = [], []
    for symbol, r in zip(symbols, results):
        if isinstance(r, SourceError):  # un ticker inconnu ne fait pas tomber les autres
            errors.append(str(r))
            quotes.append(_failed(symbol, r))
        elif isinstance(r, BaseException):
            raise r
        else:
            quotes.append(r)
    if symbols and len(errors) == len(symbols):
        raise SourceError(" ; ".join(errors))
    if errors:
        log.warning("Finnhub : cours manquants (%s)", " ; ".join(errors))
    return quotes


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
    if not isinstance(items, list):  # objet d'erreur ({"error": …}) au lieu de la liste
        raise SourceError("réponse inattendue de news")
    return [_news_item(n) for n in items[:limit] if isinstance(n, dict)]


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
    if not isinstance(items, list):
        raise SourceError(f"{symbol} : réponse inattendue de company-news")
    return [_news_item(n) for n in items[:limit] if isinstance(n, dict)]


async def company_news_between(symbol: str, start: date, end: date) -> list[dict]:
    """Actus d'une société entre deux dates (incluses), avec l'heure de publication en epoch
    (`published_ts`) ; la formule gratuite remonte environ un an. Utilisé par le labo (fiches catalyseurs)."""
    items = await get_json(
        f"{BASE}/company-news",
        params={"symbol": symbol, "from": start.isoformat(), "to": end.isoformat(), "token": _key()},
    )
    if not isinstance(items, list):  # objet d'erreur ({"error": …}) au lieu de la liste
        raise SourceError(f"{symbol} : réponse inattendue de company-news")
    return [_news_item(n) | {"published_ts": n.get("datetime")} for n in items
            if isinstance(n, dict) and isinstance(n.get("datetime"), int) and n.get("datetime") > 0]


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


async def earnings_calendar(start: date, end: date) -> list[dict]:
    """Toutes les publications de résultats entre deux dates, toutes sociétés : même source que `earnings`,
    avec le symbole. Utilisé par le labo (archive des gappers, déclaration n° 8)."""
    payload = await get_json(
        f"{BASE}/calendar/earnings", params={"from": start.isoformat(), "to": end.isoformat(), "token": _key()},
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("earningsCalendar"), list):
        raise SourceError("réponse inattendue du calendrier des résultats")
    return [
        {"symbol": e.get("symbol"), "date": e.get("date"), "hour": e.get("hour") or None,
         "quarter": e.get("quarter"), "year": e.get("year"),
         "eps_estimate": to_float(e.get("epsEstimate")), "eps_actual": to_float(e.get("epsActual")),
         "revenue_estimate": to_float(e.get("revenueEstimate")), "revenue_actual": to_float(e.get("revenueActual"))}
        for e in payload["earningsCalendar"] if isinstance(e, dict) and e.get("date") and e.get("symbol")
    ]


async def profile(symbol: str) -> dict:
    """Nom, secteur, capitalisation et nombre d'actions (en dollars et en titres) d'une société."""
    p = await get_json(f"{BASE}/stock/profile2", params={"symbol": symbol, "token": _key()})
    if not p or not p.get("name"):
        raise SourceError(f"{symbol} : profil inconnu")
    cap, shares = to_float(p.get("marketCapitalization")), to_float(p.get("shareOutstanding"))
    return {"symbol": symbol, "name": p.get("name"), "industry": p.get("finnhubIndustry"),
            "market_cap": cap * 1e6 if cap is not None else None,
            "shares": shares * 1e6 if shares is not None else None,
            "exchange": p.get("exchange"), "ipo": p.get("ipo"), "url": p.get("weburl")}


async def basic_financials(symbol: str) -> dict:
    """Chiffres clés de la formule gratuite (clés de Finnhub telles quelles : 52WeekHigh, beta,
    revenueGrowthTTMYoy, grossMarginTTM, netProfitMarginTTM, peExclExtraTTM, psTTM…)."""
    p = await get_json(f"{BASE}/stock/metric", params={"symbol": symbol, "metric": "all", "token": _key()})
    metric = p.get("metric") if isinstance(p, dict) else None
    if not isinstance(metric, dict) or not metric:
        raise SourceError(f"{symbol} : chiffres clés indisponibles")
    return metric


async def recommendations(symbol: str) -> list[dict]:
    """Avis des analystes par mois (le plus récent d'abord) : nombre d'achats forts, achats, neutres, ventes."""
    items = await get_json(f"{BASE}/stock/recommendation", params={"symbol": symbol, "token": _key()})
    if not isinstance(items, list):
        raise SourceError(f"{symbol} : réponse inattendue de recommendation")
    keys = (("strong_buy", "strongBuy"), ("buy", "buy"), ("hold", "hold"), ("sell", "sell"), ("strong_sell", "strongSell"))
    out = [{"period": r.get("period")} | {k: int(to_float(r.get(src)) or 0) for k, src in keys}
           for r in items if isinstance(r, dict) and r.get("period")]
    return sorted(out, key=lambda r: r["period"], reverse=True)


async def insider_transactions(symbol: str, start: date, end: date) -> list[dict]:
    """Transactions des dirigeants (formulaires 4) : nom, variation du nombre d'actions, date, code SEC
    (S vente en bourse, P achat en bourse, M levée d'options, A attribution, F retenue fiscale…) et prix."""
    p = await get_json(f"{BASE}/stock/insider-transactions",
                       params={"symbol": symbol, "from": start.isoformat(), "to": end.isoformat(), "token": _key()})
    rows = p.get("data") if isinstance(p, dict) else None
    if not isinstance(rows, list):
        raise SourceError(f"{symbol} : réponse inattendue de insider-transactions")
    return [{"name": r.get("name"), "change": to_float(r.get("change")), "date": r.get("transactionDate"),
             "code": r.get("transactionCode"), "price": to_float(r.get("transactionPrice"))}
            for r in rows if isinstance(r, dict)]


async def filings(symbol: str, start: date, end: date) -> list[dict]:
    """Dépôts à la SEC entre deux dates : formulaire (8-K, S-3, 424B5…), date et lien."""
    items = await get_json(f"{BASE}/stock/filings",
                           params={"symbol": symbol, "from": start.isoformat(), "to": end.isoformat(), "token": _key()})
    if not isinstance(items, list):
        raise SourceError(f"{symbol} : réponse inattendue de filings")
    return [{"form": f.get("form"), "filed": (f.get("filedDate") or "")[:10], "url": f.get("reportUrl") or f.get("filingUrl")}
            for f in items if isinstance(f, dict) and f.get("form")]


async def earnings_surprises(symbol: str) -> list[dict]:
    """Derniers trimestres publiés (4 en formule gratuite) : BPA publié, attendu et écart en %."""
    items = await get_json(f"{BASE}/stock/earnings", params={"symbol": symbol, "token": _key()})
    if not isinstance(items, list):
        raise SourceError(f"{symbol} : réponse inattendue de earnings")
    return [{"period": e.get("period"), "actual": to_float(e.get("actual")), "estimate": to_float(e.get("estimate")),
             "surprise_pct": to_float(e.get("surprisePercent"))} for e in items if isinstance(e, dict) and e.get("period")]
