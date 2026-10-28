"""Twelve Data : cours des actions US (clé gratuite TWELVEDATA_API_KEY, 8 requêtes/min)."""
import asyncio
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.config import secret
from app.core.http import SourceError, get_json, to_float

URL = "https://api.twelvedata.com/quote"
KEY_NAME = "TWELVEDATA_API_KEY"
log = logging.getLogger(__name__)
# Offre gratuite : 8 crédits par minute (1 crédit par symbole et par appel). Plan payant : à relever.
PER_MINUTE = max(1, int(os.getenv("TWELVEDATA_CREDITS_PER_MINUTE", "8") or 8))
_calls: deque[float] = deque()  # heures (monotones) des appels de la dernière minute
# Crédits dépensés par ce processus et par jour de New York (cockpit : quota du jour, 800 en gratuit). Le worker du
# labo a son propre compteur, invisible ici.
DAILY_CREDITS = int(os.getenv("TWELVEDATA_CREDITS_PER_DAY", "800") or 800)
credits: dict = {"day": None, "n": 0}


def count_credit(now: datetime | None = None) -> None:
    day = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/New_York")).date().isoformat()
    if credits["day"] != day:
        credits.update(day=day, n=0)
    credits["n"] += 1


def credits_today(now: datetime | None = None) -> dict:
    day = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/New_York")).date().isoformat()
    return {"jour_new_york": day, "depenses": credits["n"] if credits["day"] == day else 0, "par_jour": DAILY_CREDITS,
            "par_minute": PER_MINUTE, "portee": "processus web seulement (le worker du labo compte à part)"}


MAX_WAIT = 2.0  # au-delà, on n'attend pas : la source de secours prend le relais


async def _spend() -> None:
    """Réserve un crédit pour rester sous PER_MINUTE appels sur 60 s glissantes.

    Si le prochain crédit libre est à plus de MAX_WAIT secondes, lève SourceError plutôt que
    d'attendre : JARVIS bascule aussitôt sur la source de secours au lieu de bloquer jusqu'à une minute.
    """
    while True:
        now = time.monotonic()
        while _calls and now - _calls[0] >= 60:
            _calls.popleft()
        if len(_calls) < PER_MINUTE:
            _calls.append(now)
            count_credit()
            return
        wait = 60 - (now - _calls[0]) + 0.1
        if wait > MAX_WAIT:
            raise SourceError(f"limite de {PER_MINUTE} appels par minute atteinte (libre dans {wait:.0f} s)")
        await asyncio.sleep(wait)


def _failed(symbol: str, error: Exception) -> dict:
    """Cours manquant pour un symbole : même forme, valeurs vides, et l'erreur en clair."""
    return {"symbol": symbol, "name": None, "exchange": None, "currency": None, "price": None, "open": None,
            "high": None, "low": None, "previous_close": None, "change_pct": None, "volume": None,
            "is_market_open": None, "as_of": None, "error": str(error)}


async def _quote(symbol: str, apikey: str) -> dict:
    await _spend()
    q = await get_json(URL, params={"symbol": symbol, "apikey": apikey})
    if q.get("status") == "error":
        raise SourceError(f"{symbol} : {q.get('message', 'erreur')}")
    timestamp = q.get("last_quote_at") or q.get("timestamp")
    return {
        "symbol": q["symbol"],
        "name": q.get("name"),
        "exchange": q.get("exchange"),
        "currency": q.get("currency"),
        "price": to_float(q.get("close")),
        "open": to_float(q.get("open")),
        "high": to_float(q.get("high")),
        "low": to_float(q.get("low")),
        "previous_close": to_float(q.get("previous_close")),
        "change_pct": to_float(q.get("percent_change")),
        "volume": to_float(q.get("volume")),
        "is_market_open": q.get("is_market_open"),
        "as_of": datetime.fromtimestamp(timestamp, timezone.utc).isoformat() if timestamp else None,
    }


async def stock_quotes(symbols: list[str], apikey: str | None = None) -> list[dict]:
    key = apikey or secret(KEY_NAME)
    if not key:
        raise SourceError(f"clé {KEY_NAME} absente")
    quotes: list[dict] = []
    errors: list[str] = []
    # Par paquets de PER_MINUTE : un ticker en erreur ne fait pas tomber les autres.
    for i in range(0, len(symbols), PER_MINUTE):
        chunk = symbols[i:i + PER_MINUTE]
        results = await asyncio.gather(*(_quote(s, key) for s in chunk), return_exceptions=True)
        for symbol, r in zip(chunk, results):
            if isinstance(r, SourceError):
                errors.append(str(r))
                quotes.append(_failed(symbol, r))
            elif isinstance(r, BaseException):
                raise r
            else:
                quotes.append(r)
    if symbols and len(errors) == len(symbols):
        raise SourceError(" ; ".join(errors))  # tout a échoué : on passe à la source de secours
    if errors:
        log.warning("Twelve Data : cours manquants (%s)", " ; ".join(errors))
    return quotes


SERIES_URL = "https://api.twelvedata.com/time_series"


async def candles(symbol: str, interval_min: int, outputsize: int = 5000, apikey: str | None = None) -> list[dict]:
    """Bougies OHLC (5 000 au maximum, heures de séance seulement), de la plus ancienne à la plus récente."""
    key = apikey or secret(KEY_NAME)
    if not key:
        raise SourceError(f"clé {KEY_NAME} absente")
    await _spend()
    if interval_min >= 1440:
        interval = "1day"
    else:
        interval = f"{interval_min}min" if interval_min < 60 else f"{interval_min // 60}h"
    payload = await get_json(
        SERIES_URL, params={"symbol": symbol, "interval": interval, "outputsize": outputsize, "apikey": key}
    )
    if payload.get("status") == "error":
        raise SourceError(f"{symbol} : {payload.get('message', 'erreur')}")
    tz = ZoneInfo(payload["meta"]["exchange_timezone"])
    rows = [
        {
            "t": int(datetime.fromisoformat(v["datetime"]).replace(tzinfo=tz).timestamp()),
            "o": float(v["open"]), "h": float(v["high"]), "l": float(v["low"]), "c": float(v["close"]),
            "v": to_float(v.get("volume")) or 0.0,
        }
        for v in payload.get("values", [])
    ]
    if not rows:
        raise SourceError(f"{symbol} : aucune bougie")
    return sorted(rows, key=lambda r: r["t"])
