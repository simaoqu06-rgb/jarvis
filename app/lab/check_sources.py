"""Vérifie les sources du labo depuis le serveur : `python -m app.lab.check_sources`.

Rapport court en français : profondeur des bougies de 5 min SIP d'Alpaca (SPY), liste des actifs,
profondeur de l'historique OKX (BTC-USDT et un petit altcoin), profondeur des actus Finnhub (AAPL),
place libre sur le volume des données et taille estimée de la base pour le rejeu configuré.
Uniquement des lectures ; aucun ordre.
"""
import asyncio
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app.config import secret
from app.core.http import SourceError, get_json
from app.lab import collector as CO
from app.lab import sources as SRC
from app.lab.db import db_path
from app.lab.worker import replay_sessions
from app.sources import alpaca, finnhub

BYTES_PER_ROW = 70  # clé (marché, symbole, tf, ts) + 6 réels, index compris, mesure approximative
SMALL_ALTS = ("ZIL-USDT", "ICX-USDT", "LRC-USDT", "SUSHI-USDT")


def _years_ago(now: datetime, years: float) -> datetime:
    return now - timedelta(days=round(365.25 * years))


async def alpaca_depth(now: datetime, transport=None) -> list[str]:
    lines = []
    if not alpaca.configured():
        return ["Alpaca : clés ALPACA_API_KEY / ALPACA_SECRET_KEY absentes, rien vérifié"]
    for years in (2, 3, 5, 8):
        start = _years_ago(now, years)
        try:
            rows = (await SRC.alpaca_bars(["SPY"], 5, start, start + timedelta(days=5), transport)).get("SPY") or []
            lines.append(f"Alpaca SIP 5 min SPY il y a {years} ans : "
                         + (f"oui ({len(rows)} bougies, première le "
                            f"{datetime.fromtimestamp(rows[0]['t'], timezone.utc):%d/%m/%Y})" if rows else "non"))
        except SourceError as exc:
            lines.append(f"Alpaca SIP 5 min SPY il y a {years} ans : erreur {exc}")
    try:
        assets = await SRC.alpaca_assets(include_inactive=False, transport=transport)
        lines.append(f"Alpaca actifs : {len(assets)} actions actives, {len(CO.stock_symbols(assets))} retenues "
                     f"(NYSE, NASDAQ, AMEX, ARCA, BATS, tickers simples)")
    except SourceError as exc:
        lines.append(f"Alpaca actifs : erreur {exc}")
    return lines


async def _okx_has(inst_id: str, moment: datetime, transport=None) -> bool:
    after = int((moment + timedelta(days=1)).timestamp() * 1000)
    rows = await SRC.with_retry(SRC.okx_get, "/api/v5/market/history-candles",
                                {"instId": inst_id, "bar": "5m", "after": str(after), "limit": "1"},
                                limiter=SRC.OKX_HISTORY_LIMIT, transport=transport)
    return bool(rows) and abs(int(rows[0][0]) / 1000 - moment.timestamp()) < 3 * 86400


async def okx_depth(now: datetime, transport=None) -> list[str]:
    lines = []
    try:
        instruments = await SRC.okx_instruments(transport)
    except SourceError as exc:
        return [f"OKX instruments : erreur {exc}"]
    pairs = CO.crypto_pairs(instruments)
    lines.append(f"OKX : {len(pairs)} paires USDT retenues sur {len(instruments)} paires au comptant")
    alt = next((p for p in SMALL_ALTS if p in pairs), pairs[-1] if pairs else None)
    for inst in ("BTC-USDT", alt):
        if not inst:
            continue
        found = []
        for years in (1, 2, 3):
            label = f"{years} an{'s' if years > 1 else ''}"
            try:
                ok = await _okx_has(inst, _years_ago(now, years), transport)
                found.append(f"{label} {'oui' if ok else 'non'}")
            except SourceError as exc:
                found.append(f"{label} erreur {exc}")
        lines.append(f"OKX 5 min {inst} : " + ", ".join(found))
    return lines


async def bybit_depth(now: datetime, transport=None) -> list[str]:
    """Lot B : funding et open interest du perpétuel BTCUSDT chez Bybit, il y a 1 et 2 ans."""
    found = []
    for years in (1, 2):
        t = int(_years_ago(now, years).timestamp())
        try:
            funding = await SRC.bybit_funding("BTCUSDT", t, t + 3 * 86400, transport)
            oi = await SRC.bybit_open_interest("BTCUSDT", t, t + 3 * 86400, transport=transport)
            found.append(f"{years} an{'s' if years > 1 else ''} : funding {len(funding)} points, open interest "
                         f"{len(oi)} points")
        except SourceError as exc:
            found.append(f"{years} an{'s' if years > 1 else ''} : erreur {exc}")
    return ["Bybit dérivés BTCUSDT (sur 3 jours) : " + " ; ".join(found)]


async def finnhub_depth(now: datetime) -> list[str]:
    key = secret(finnhub.KEY_NAME)
    if not key:
        return ["Finnhub : clé FINNHUB_API_KEY absente, rien vérifié"]
    found = []
    for months in (6, 12, 18):
        day = (now - timedelta(days=round(30.4 * months))).date()
        try:
            rows = await get_json(f"{finnhub.BASE}/company-news",
                                  params={"symbol": "AAPL", "from": day.isoformat(),
                                          "to": (day + timedelta(days=7)).isoformat(), "token": key})
            found.append(f"{months} mois : {len(rows or [])} actus")
        except SourceError as exc:
            found.append(f"{months} mois : erreur {exc}")
    return ["Finnhub actus AAPL (semaine) il y a " + ", ".join(found)]


def size_estimate(sessions: int) -> tuple[float, float]:
    """Taille estimée de la base (Go) : fourchette basse (univers stable) et haute (forte rotation)."""
    days = sessions + CO.STOCK_HISTORY_SESSIONS
    cal_days = round(sessions * 365 / 252) + CO.CRYPTO_HISTORY_DAYS
    daily = 8000 * (sessions + 310) + 600 * (cal_days + CO.DAILY_HISTORY_DAYS)
    low = 300 * days * 78 + 60 * cal_days * 288 + daily
    high = 1500 * days * 78 + 120 * cal_days * 288 + daily
    return low * BYTES_PER_ROW / 1e9, high * BYTES_PER_ROW / 1e9


async def report(now: datetime | None = None, transport=None) -> str:
    now = now or datetime.now(timezone.utc)
    lines = ["Vérification des sources du labo"]
    lines += await alpaca_depth(now, transport)
    lines += await okx_depth(now, transport)
    lines += await bybit_depth(now, transport)
    lines += await finnhub_depth(now)
    folder = Path(db_path()).resolve().parent
    folder.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(folder).free / 1e9
    low, high = size_estimate(replay_sessions())
    lines.append(f"Disque : {free:.1f} Go libres sur {folder}")
    lines.append(f"Base estimée pour {replay_sessions()} séances de rejeu : {low:.1f} à {high:.1f} Go "
                 f"(depuis le {CO.sessions_back(date.today(), replay_sessions()):%d/%m/%Y})")
    return "\n".join(lines)


def main() -> None:
    print(asyncio.run(report()))


if __name__ == "__main__":
    main()
