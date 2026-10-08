"""Alpaca : données de marché US et compte de trading fictif (paper), clés ALPACA_API_KEY et ALPACA_SECRET_KEY.

Docs : https://docs.alpaca.markets/reference (Market Data API v2, screener v1beta1, Trading API v2).
Avec le plan gratuit, le flux consolidé (SIP) est disponible avec 15 minutes de retard : on demande
donc les bougies jusqu'à « maintenant - 16 min » ; en cas de refus, on retombe sur le flux IEX.
Seul le compte fictif est utilisé (ALPACA_PAPER doit rester à true) : JARVIS ne passe jamais d'ordre réel.
"""
import os
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from app.config import TIMEOUT_SECONDS, USER_AGENT, secret
from app.core.http import SourceError

DATA = "https://data.alpaca.markets"
KEY_NAME, SECRET_NAME = "ALPACA_API_KEY", "ALPACA_SECRET_KEY"
SIP_DELAY = timedelta(minutes=16)
NEW_YORK = ZoneInfo("America/New_York")
TIMEFRAMES = {1: "1Min", 5: "5Min", 15: "15Min", 60: "1Hour", 240: "4Hour", 1440: "1Day"}


def configured() -> bool:
    return bool(secret(KEY_NAME) and secret(SECRET_NAME))


def _trading_base() -> str:
    # Garde-fou : uniquement le compte fictif, quel que soit le réglage.
    if os.getenv("ALPACA_PAPER", "true").lower() != "true":
        raise SourceError("ALPACA_PAPER doit valoir true : JARVIS ne trade que sur le compte fictif")
    return "https://paper-api.alpaca.markets"


async def _request(method: str, url: str, params: dict | None = None, body: dict | None = None,
                   transport: httpx.AsyncBaseTransport | None = None):
    if not configured():
        raise SourceError(f"clés {KEY_NAME} / {SECRET_NAME} absentes")
    headers = {"APCA-API-KEY-ID": secret(KEY_NAME), "APCA-API-SECRET-KEY": secret(SECRET_NAME),
               "User-Agent": USER_AGENT, "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, headers=headers, transport=transport) as client:
            response = await client.request(method, url, params=params, json=body)
    except httpx.TimeoutException as exc:
        raise SourceError(f"délai de {TIMEOUT_SECONDS:.0f} s dépassé") from exc
    except httpx.HTTPError as exc:
        raise SourceError(f"erreur réseau ({type(exc).__name__})") from exc
    if response.status_code == 204:
        return None
    if response.status_code not in (200, 207):
        try:
            message = response.json().get("message", "")
        except ValueError:
            message = response.text[:200]
        raise SourceError(f"HTTP {response.status_code} {message}".strip())
    return response.json()


def _bar(b: dict) -> dict:
    t = datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
    return {"t": int(t.timestamp()), "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": float(b.get("v") or 0)}


async def bars(symbols: list[str], interval_min: int, days: int, now: datetime | None = None,
               feed: str = "sip") -> dict[str, list[dict]]:
    """Bougies OHLCV de plusieurs actions d'un coup, de la plus ancienne à la plus récente."""
    now = now or datetime.now(timezone.utc)
    end = now - SIP_DELAY if feed == "sip" else now
    params = {"symbols": ",".join(symbols), "timeframe": TIMEFRAMES[interval_min], "limit": 10000,
              "start": (now - timedelta(days=days)).isoformat(timespec="seconds"),
              "end": end.isoformat(timespec="seconds"), "adjustment": "split", "feed": feed}
    out: dict[str, list[dict]] = {s: [] for s in symbols}
    try:
        for _ in range(50):
            payload = await _request("GET", f"{DATA}/v2/stocks/bars", params)
            for symbol, rows in (payload.get("bars") or {}).items():
                out.setdefault(symbol, []).extend(_bar(b) for b in rows)
            token = payload.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
    except SourceError as exc:
        if feed == "sip" and "403" in str(exc):
            return await bars(symbols, interval_min, days, now, feed="iex")
        raise
    return {s: sorted(rows, key=lambda r: r["t"]) for s, rows in out.items()}


async def bars_range(symbols: list[str], interval_min: int, start: datetime, end: datetime, feed: str = "sip",
                     adjustment: str = "split", transport: httpx.AsyncBaseTransport | None = None,
                     max_pages: int = 500, call=None) -> dict[str, list[dict]]:
    """Bougies de plusieurs actions entre deux instants (pagination page_token / next_page_token).

    Pour l'historique (labo) : `end` doit être antérieur à maintenant - 16 min avec le flux SIP gratuit.
    `call(fn, *args, **kwargs)` enveloppe chaque requête (limite de débit, nouvelles tentatives).
    """
    async def direct(fn, *args, **kwargs):
        return await fn(*args, **kwargs)

    call = call or direct
    params = {"symbols": ",".join(symbols), "timeframe": TIMEFRAMES[interval_min], "limit": 10000,
              "start": start.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
              "end": end.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
              "adjustment": adjustment, "feed": feed, "sort": "asc"}
    out: dict[str, list[dict]] = {s: [] for s in symbols}
    for _ in range(max_pages):
        payload = await call(_request, "GET", f"{DATA}/v2/stocks/bars", dict(params), transport=transport) or {}
        for symbol, rows in (payload.get("bars") or {}).items():
            out.setdefault(symbol, []).extend(_bar(b) for b in rows)
        token = payload.get("next_page_token")
        if not token:
            break
        params["page_token"] = token
    else:
        raise SourceError(f"pagination Alpaca interrompue après {max_pages} pages")
    return {s: sorted(rows, key=lambda r: r["t"]) for s, rows in out.items()}


async def assets(status: str = "active", transport: httpx.AsyncBaseTransport | None = None) -> list[dict]:
    """Liste des actions US (lecture seule, compte fictif) : symbole, place, négociable, attributs."""
    payload = await _request("GET", f"{_trading_base()}/v2/assets",
                             {"status": status, "asset_class": "us_equity"}, transport=transport)
    return payload or []


def regular_hours(rows: list[dict]) -> list[dict]:
    """Garde les bougies de la séance régulière (9 h 30 - 16 h à New York), comme Twelve Data :
    VWAP et points pivots se calculent sur la séance, pas sur la pré-ouverture."""
    out = []
    for r in rows:
        local = datetime.fromtimestamp(r["t"], NEW_YORK).time()
        if time(9, 30) <= local < time(16, 0):
            out.append(r)
    return out


async def candles(symbol: str, interval_min: int, days: int | None = None) -> list[dict]:
    """Bougies d'une action, même forme que twelvedata.candles."""
    days = days or {1: 2, 5: 10, 15: 280, 60: 120, 240: 400, 1440: 450}.get(interval_min, 30)
    rows = (await bars([symbol], interval_min, days)).get(symbol) or []
    if interval_min < 240:
        rows = regular_hours(rows)
    if not rows:
        raise SourceError(f"{symbol} : aucune bougie Alpaca")
    return rows


async def snapshots(symbols: list[str], feed: str = "iex") -> dict[str, dict]:
    """Dernier échange (pré-ouverture comprise sur IEX), bougie du jour et de la veille."""
    payload = await _request("GET", f"{DATA}/v2/stocks/snapshots", {"symbols": ",".join(symbols), "feed": feed})
    out = {}
    for symbol, s in (payload or {}).items():
        if not s:
            continue
        prev = s.get("prevDailyBar") or {}
        daily = s.get("dailyBar") or {}
        trade = s.get("latestTrade") or {}
        out[symbol] = {"price": trade.get("p") or daily.get("c"), "prev_close": prev.get("c"),
                       "prev_volume": prev.get("v"), "day_volume": daily.get("v"), "as_of": trade.get("t"),
                       "open": daily.get("o"), "open_day": _ny_day(daily.get("t"))}
    return out


def _ny_day(stamp: str | None) -> str | None:
    """Jour de New York d'un horodatage Alpaca (la bougie du jour reste celle de la veille avant le 1er échange)."""
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(NEW_YORK).date().isoformat()
    except ValueError:
        return None


async def most_actives(top: int = 50) -> list[str]:
    payload = await _request("GET", f"{DATA}/v1beta1/screener/stocks/most-actives", {"by": "volume", "top": top})
    return [r["symbol"] for r in payload.get("most_actives") or []]


async def top_gainers(top: int = 50) -> list[str]:
    payload = await _request("GET", f"{DATA}/v1beta1/screener/stocks/movers", {"top": top})
    return [r["symbol"] for r in payload.get("gainers") or []]


# --- Compte fictif ------------------------------------------------------------------------

async def account() -> dict:
    return await _request("GET", f"{_trading_base()}/v2/account")


async def clock() -> dict:
    return await _request("GET", f"{_trading_base()}/v2/clock")


def round_price(price: float) -> float:
    """Pas de cotation US : 2 décimales à partir de 1 $, 4 en dessous (sous-penny refusé sinon)."""
    return round(price, 2) if price >= 1 else round(price, 4)


def _fmt(price: float) -> str:
    return f"{price:.2f}" if price >= 1 else f"{price:.4f}"


def bracket_prices(entry: float, take_profit: float, stop: float) -> tuple[float, float, float]:
    """Prix arrondis au pas de cotation ; refuse un plan que l'arrondi rendrait absurde."""
    e, tp, sl = round_price(entry), round_price(take_profit), round_price(stop)
    if e == sl or e == tp:
        raise SourceError(f"arrondi au pas de cotation : entrée {e} confondue avec le stop ({sl}) ou le TP ({tp})")
    return e, tp, sl


async def submit_bracket(symbol: str, qty: int, side: str, entry: float, take_profit: float, stop: float,
                         client_order_id: str) -> dict:
    """Ordre à cours limité à l'entrée, avec son TP et son stop attachés (ordre « bracket »)."""
    entry, take_profit, stop = bracket_prices(entry, take_profit, stop)
    body = {"symbol": symbol, "qty": str(qty), "side": "buy" if side == "long" else "sell",
            "type": "limit", "limit_price": _fmt(entry), "time_in_force": "gtc",
            "order_class": "bracket", "client_order_id": client_order_id,
            "take_profit": {"limit_price": _fmt(take_profit)}, "stop_loss": {"stop_price": _fmt(stop)}}
    return await _request("POST", f"{_trading_base()}/v2/orders", body=body)


async def order(order_id: str) -> dict:
    return await _request("GET", f"{_trading_base()}/v2/orders/{order_id}", {"nested": "true"})


async def cancel_order(order_id: str) -> None:
    await _request("DELETE", f"{_trading_base()}/v2/orders/{order_id}")


async def close_position(symbol: str, qty: int | None = None) -> dict | None:
    params = {"qty": str(qty)} if qty else None
    return await _request("DELETE", f"{_trading_base()}/v2/positions/{symbol}", params)
