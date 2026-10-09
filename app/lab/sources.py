"""Sources de données du labo : OKX (crypto, sans clé) et Alpaca (actions US), avec limites de débit.

OKX : https://www.okx.com/docs-v5/en/ — history-candles (20 requêtes / 2 s, 100 bougies par appel,
plus récentes d'abord : [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]) et instruments SPOT.
Alpaca : app/sources/alpaca.py (200 requêtes / min en gratuit). Uniquement des lectures : la liste
des actifs et les bougies. Aucune route d'ordre n'est appelée par le labo.
"""
import asyncio
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone

import httpx

from app.config import TIMEOUT_SECONDS, USER_AGENT
from app.core.http import SourceError
from app.sources import alpaca

log = logging.getLogger(__name__)
OKX = "https://www.okx.com"
OKX_BARS = {5: "5m", 15: "15m", 60: "1H", 240: "4H", 1440: "1Dutc"}  # 1Dutc : journée UTC (1D = heure de Hong Kong)
OKX_RATE_LIMIT_CODES = {"50011", "50013", "50061"}
RETRIES = 5


class RateLimiter:
    """Au plus `calls` appels par fenêtre glissante de `period` secondes."""

    def __init__(self, calls: int, period: float, clock=time.monotonic, sleep=asyncio.sleep):
        self.calls, self.period, self.clock, self.sleep = calls, period, clock, sleep
        self.stamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            while True:
                now = self.clock()
                while self.stamps and now - self.stamps[0] >= self.period:
                    self.stamps.popleft()
                if len(self.stamps) < self.calls:
                    self.stamps.append(now)
                    return
                await self.sleep(self.period - (now - self.stamps[0]) + 0.01)


def _alpaca_per_minute() -> int:
    try:
        return max(1, int(os.getenv("JARVIS_LAB_ALPACA_PER_MINUTE", "100")))
    except ValueError:
        return 100


# 200 / min en gratuit, partagées avec JARVIS en direct (scanner, forward-test) : le labo en prend la moitié.
ALPACA_LIMIT = RateLimiter(_alpaca_per_minute(), 60.0)
OKX_HISTORY_LIMIT = RateLimiter(18, 2.0)  # 20 / 2 s
OKX_PUBLIC_LIMIT = RateLimiter(18, 2.0)


def retryable(exc: Exception) -> bool:
    text = str(exc)
    return any(k in text for k in ("429", "HTTP 5", "réseau", "délai", "limite")) or \
        any(code in text for code in OKX_RATE_LIMIT_CODES)


async def with_retry(fn, *args, limiter: RateLimiter | None = None, tries: int = RETRIES, base: float = 1.0,
                     sleep=asyncio.sleep, **kwargs):
    """Rappelle `fn` après une limite de débit ou une panne passagère (attente 1, 2, 4, 8 s…)."""
    for attempt in range(tries):
        if limiter:
            await limiter.wait()
        try:
            return await fn(*args, **kwargs)
        except SourceError as exc:
            if attempt == tries - 1 or not retryable(exc):
                raise
            delay = base * 2 ** attempt
            log.info("Labo : %s, nouvel essai dans %.0f s", exc, delay)
            await sleep(delay)
    raise SourceError("nombre d'essais épuisé")  # pragma: no cover


# --- OKX ----------------------------------------------------------------------------------

async def okx_get(path: str, params: dict, transport: httpx.AsyncBaseTransport | None = None):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, headers=headers, transport=transport) as client:
            response = await client.get(f"{OKX}{path}", params=params)
    except httpx.TimeoutException as exc:
        raise SourceError(f"délai de {TIMEOUT_SECONDS:.0f} s dépassé") from exc
    except httpx.HTTPError as exc:
        raise SourceError(f"erreur réseau ({type(exc).__name__})") from exc
    if response.status_code != 200:
        raise SourceError(f"HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise SourceError("réponse qui n'est pas du JSON") from exc
    if str(payload.get("code")) != "0":
        raise SourceError(f"OKX {payload.get('code')} {payload.get('msg', '')}".strip())
    return payload.get("data") or []


async def okx_instruments(transport=None) -> list[dict]:
    """Paires au comptant en cotation (instId, baseCcy, quoteCcy, state)."""
    rows = await with_retry(okx_get, "/api/v5/public/instruments", {"instType": "SPOT"},
                            limiter=OKX_PUBLIC_LIMIT, transport=transport)
    return [{"inst_id": r["instId"], "base": r.get("baseCcy"), "quote": r.get("quoteCcy"), "state": r.get("state")}
            for r in rows]


def okx_row(r: list) -> dict:
    return {"t": int(r[0]) // 1000, "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]),
            "v": float(r[5])}


async def okx_candles(inst_id: str, tf: int, start: int, end: int, transport=None, max_calls: int = 100000) -> list[dict]:
    """Bougies closes de début dans [start, end[ (epoch s), de la plus ancienne à la plus récente.

    `after` = horodatage en ms : OKX renvoie les bougies strictement antérieures, 100 au plus.
    """
    out: dict[int, dict] = {}
    after = end * 1000
    for _ in range(max_calls):
        rows = await with_retry(okx_get, "/api/v5/market/history-candles",
                                {"instId": inst_id, "bar": OKX_BARS[tf], "after": str(after), "limit": "100"},
                                limiter=OKX_HISTORY_LIMIT, transport=transport)
        if not rows:
            break
        for r in rows:
            if len(r) > 8 and str(r[8]) == "0":
                continue  # bougie en cours : jamais stockée
            bar = okx_row(r)
            if start <= bar["t"] < end:
                out[bar["t"]] = bar
        oldest = min(int(r[0]) for r in rows)
        if oldest // 1000 <= start or oldest >= after:
            break
        after = oldest
    return [out[t] for t in sorted(out)]


# --- Bybit (dérivés crypto, lot B) ----------------------------------------------------------
# https://bybit-exchange.github.io/docs/v5/market/history-fund-rate et /open-interest : historiques
# publics sans clé (OKX ne garde que ~3 mois de funding). Données seulement : JARVIS ne trade pas chez Bybit.

BYBIT = "https://api.bybit.com"
BYBIT_LIMIT = RateLimiter(8, 1.0)
BYBIT_PAGE = 200


async def bybit_get(path: str, params: dict, transport: httpx.AsyncBaseTransport | None = None) -> list:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS, headers=headers, transport=transport) as client:
            response = await client.get(f"{BYBIT}{path}", params=params)
    except httpx.TimeoutException as exc:
        raise SourceError(f"délai de {TIMEOUT_SECONDS:.0f} s dépassé") from exc
    except httpx.HTTPError as exc:
        raise SourceError(f"erreur réseau ({type(exc).__name__})") from exc
    if response.status_code != 200:
        raise SourceError(f"HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise SourceError("réponse qui n'est pas du JSON") from exc
    if payload.get("retCode") != 0:
        code = payload.get("retCode")
        raise SourceError(f"Bybit {'429 ' if code in (10006, 10018) else ''}{code} {payload.get('retMsg', '')}".strip())
    return (payload.get("result") or {}).get("list") or []


async def _bybit_series(path: str, params: dict, ts_key: str, value_key: str, start: int, end: int,
                        transport=None, max_calls: int = 50) -> list[tuple[int, float]]:
    """Points (epoch s, valeur) de début dans [start, end[, du plus ancien au plus récent ; Bybit renvoie les
    plus récents d'abord, 200 au plus : on recule `endTime` page par page."""
    out: dict[int, float] = {}
    hi = end * 1000 - 1
    for _ in range(max_calls):
        rows = await with_retry(bybit_get, path, params | {"startTime": str(start * 1000), "endTime": str(hi),
                                                             "limit": str(BYBIT_PAGE)},
                                limiter=BYBIT_LIMIT, transport=transport)
        if not rows:
            break
        stamps = []
        for r in rows:
            t = int(r[ts_key])
            stamps.append(t)
            if start * 1000 <= t < end * 1000:
                out[t // 1000] = float(r[value_key])
        oldest = min(stamps)
        if len(rows) < BYBIT_PAGE or oldest <= start * 1000 or oldest - 1 >= hi:
            break
        hi = oldest - 1
    return sorted(out.items())


async def bybit_funding(symbol: str, start: int, end: int, transport=None) -> list[tuple[int, float]]:
    """Taux de funding réglés (fraction par période, souvent 8 h) du contrat perpétuel USDT `symbol`."""
    return await _bybit_series("/v5/market/funding/history", {"category": "linear", "symbol": symbol},
                               "fundingRateTimestamp", "fundingRate", start, end, transport)


async def bybit_open_interest(symbol: str, start: int, end: int, interval: str = "4h",
                              transport=None) -> list[tuple[int, float]]:
    """Open interest (en contrats) du perpétuel USDT `symbol`, un point par `interval`."""
    return await _bybit_series("/v5/market/open-interest", {"category": "linear", "symbol": symbol,
                                                             "intervalTime": interval},
                               "timestamp", "openInterest", start, end, transport)


# --- Alpaca -------------------------------------------------------------------------------

async def alpaca_assets(include_inactive: bool = True, transport=None) -> list[dict]:
    rows = await with_retry(alpaca.assets, "active", limiter=ALPACA_LIMIT, transport=transport)
    if include_inactive:
        try:
            rows = rows + await with_retry(alpaca.assets, "inactive", limiter=ALPACA_LIMIT, transport=transport)
        except SourceError as exc:
            log.warning("Labo : actifs inactifs Alpaca indisponibles (%s)", exc)
    return rows


async def _alpaca_call(fn, *args, **kwargs):
    return await with_retry(fn, *args, limiter=ALPACA_LIMIT, **kwargs)


async def alpaca_bars(symbols: list[str], tf: int, start: datetime, end: datetime, transport=None,
                      adjustment: str = "split") -> dict[str, list[dict]]:
    """Bougies SIP (historique de plus de 15 min) ; chaque page passe par la limite de débit. `adjustment` :
    « split » (prix ajustés des divisions, par défaut) ou « raw » (prix réellement négociés, déclaration n° 9)."""
    end = min(end, datetime.now(timezone.utc) - alpaca.SIP_DELAY)
    if end <= start:
        return {s: [] for s in symbols}
    return await alpaca.bars_range(symbols, tf, start, end, adjustment=adjustment, transport=transport,
                                   call=_alpaca_call)
