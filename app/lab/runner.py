"""Rejeu historique et journées en direct du labo, reprenables et idempotents.

- replay(start_day, end_day, market) : jour par jour, chaque jour dans une transaction (trades,
  univers et avancement ensemble) ; une coupure reprend au jour suivant le dernier validé.
- run_live_day(day) : la séance qui vient de finir (actions) et les créneaux crypto du jour dont
  l'horizon est passé. Rejouer un jour remplace ses trades : jamais de doublon.
La collecte des données (réseau) est séparée du calcul : `collect=False` travaille sur la base seule.
"""
import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

from app.lab import collector as CO
from app.lab import generator as G
from app.lab import heartbeat
from app.lab import universe as U
from app.lab.db import LabDB
from app.trading import service as T
from app.trading.settings import settings

log = logging.getLogger(__name__)
MARKETS = ("stock", "crypto")


def settings_snapshot() -> dict:
    """Réglages figés dans la version de méthode : note, veto et paramètres du labo."""
    return {"trading": settings.all(), "plan": {k: dict(v) for k, v in T.SETTINGS.items()},
            "lab": {"timing_lookback": G.TIMING_LOOKBACK, "plan_bars": G.PLAN_BARS,
                    "crypto_hours_utc": list(G.CRYPTO_HOURS_UTC), "stock_top_dollar": U.STOCK_TOP_DOLLAR,
                    "stock_top_movers": U.STOCK_TOP_MOVERS, "crypto_top": U.CRYPTO_TOP}}


def method(db: LabDB) -> tuple[str, dict]:
    """Version de méthode en cours et ses réglages de note (dict utilisable par score.evaluate)."""
    version, snap = db.method_version(settings_snapshot())
    return version, snap["trading"]


def market_days(market: str, start: date, end: date) -> list[date]:
    if market == "stock":
        return CO.trading_days(start, end)
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def day_ready(market: str, day: date, now: datetime) -> bool:
    """Toutes les données du jour existent : séance close depuis 30 min (actions), horizon du
    dernier créneau passé (crypto : 20 h UTC + 24 h)."""
    if market == "stock":
        bounds = CO.session_ts(day)
        return bool(bounds) and now.timestamp() >= bounds[1] + 30 * 60
    return now.timestamp() >= G.crypto_moments(day)[-1] + 86400


async def ensure_daily_range(db: LabDB, market: str, first_day: date, last_day: date,
                             now: datetime | None = None, transport=None) -> None:
    """Bougies journalières de tous les symboles, de 450 jours avant `first_day` à `last_day` - 1."""
    now = now or datetime.now(timezone.utc)
    if market == "stock":
        symbols = await CO.stock_universe_symbols(db, now.date(), transport)
    else:
        symbols = await CO.crypto_universe_symbols(db, now.date(), transport)
    # Indice de référence (SPY, BTC-USDT) toujours collecté pour le régime de marché (Labo 2) ; s'il
    # n'est pas dans la liste des actifs (ETF signalé par Alpaca), il reste hors de l'univers du jour.
    index = G.INDEX[market]
    db.set("lab", f"index_only:{market}", None if index in symbols else index)
    if index not in symbols:
        symbols = [*symbols, index]
    await CO.ensure_daily(db, market, symbols, first_day - timedelta(days=CO.DAILY_HISTORY_DAYS),
                          last_day - timedelta(days=1), now, transport)


async def prepare_day(db: LabDB, market: str, day: date, now: datetime | None = None, transport=None) -> list[dict]:
    """Collecte ce qu'il faut pour le jour (journalier de tous les symboles, 5 min de l'univers),
    calcule l'univers et le renvoie. Seuls les manques sont demandés."""
    now = now or datetime.now(timezone.utc)
    await ensure_daily_range(db, market, day, day, now, transport)
    universe = U.build(db, market, day, save=False)
    names = [u["symbol"] for u in universe]
    start, end = CO.stock_window(day) if market == "stock" else CO.crypto_window(day)
    await CO.ensure_intraday(db, market, names, start, end, now, transport)
    return universe


MAX_DAY_ATTEMPTS = 3


def run_day(db: LabDB, market: str, day: date, origin: str, universe: list[dict] | None = None,
            now: datetime | None = None, progress: tuple[str, str] | None = None) -> int:
    """Calcule et journalise un jour (sans réseau). Renvoie le nombre de trades écrits.

    Un symbole en erreur est sauté (une ligne de journal) et compté dans le bilan du jour,
    gardé dans progress (job du rejeu ou du direct, clé « day:AAAA-MM-JJ »)."""
    version, s = method(db)
    if universe is None:
        universe = U.build(db, market, day, save=False)
    errors: list[dict] = []
    if market == "stock":
        moments, rows = G.stock_day(db, day, s, universe, errors)
    else:
        moments, rows = G.crypto_day(db, day, s, universe, int(now.timestamp()) if now else None, errors)
    job = progress[0] if progress else f"{origin}:{market}"
    with db.transaction():
        db.replace_universe(market, day.isoformat(), universe)
        db.replace_trades(origin, version, market, moments, rows)
        db.set(job, f"day:{day.isoformat()}", {"trades": len(rows), "errors": len(errors),
                                               "error_symbols": [e["symbol"] for e in errors[:10]]})
        if progress:
            db.set(job, progress[1], day.isoformat())
    return len(rows)


def day_failed(db: LabDB, job: str, day: date, exc: Exception) -> bool:
    """Compte un échec du jour entier ; au 3e, le jour est sauté (raison gardée) pour ne pas
    bloquer la suite. Renvoie True si le jour est désormais sauté."""
    key = day.isoformat()
    n = (db.get(job, f"attempts:{key}", 0) or 0) + 1
    reason = f"{type(exc).__name__}: {exc}"[:300]
    db.set(job, f"attempts:{key}", n)
    if n < MAX_DAY_ATTEMPTS:
        log.warning("Labo : %s %s en échec (essai %d/%d) : %s", job, key, n, MAX_DAY_ATTEMPTS, reason)
        return False
    log.error("Labo : %s %s sauté après %d échecs : %s", job, key, n, reason)
    with db.transaction():
        db.set(job, f"skipped:{key}", reason)
        db.set(job, "last_day", key)
    return True


async def replay(start_day: date, end_day: date, market: str, db: LabDB | None = None, collect: bool = True,
                 max_days: int | None = None, now: datetime | None = None, transport=None) -> int:
    """Rejoue les jours de start_day à end_day, en reprenant après le dernier jour validé.
    Renvoie le nombre de jours traités pendant cet appel."""
    db = db or LabDB()
    now = now or datetime.now(timezone.utc)
    job = f"replay:{market}"
    db.set(job, "start", start_day.isoformat())
    db.set(job, "end", end_day.isoformat())
    db.set(job, "days_total", len(market_days(market, start_day, end_day)))
    last = db.get(job, "last_day")
    first = max(start_day, date.fromisoformat(last) + timedelta(days=1)) if last else start_day
    days = [d for d in market_days(market, first, end_day) if day_ready(market, d, now)]
    if collect and days:
        # Journalier de tout l'univers en une passe pour toute la plage (pas une passe par jour).
        await ensure_daily_range(db, market, days[0], days[-1], now, transport)
    done = 0
    for day in days:
        if max_days is not None and done >= max_days:
            break
        try:
            universe = await prepare_day(db, market, day, now, transport) if collect else None
            n = run_day(db, market, day, "replay", universe, progress=(job, "last_day"))
        except Exception as exc:  # données manquantes, source en panne… : 3 essais puis on saute
            if day_failed(db, job, day, exc):
                done += 1
                continue
            break  # nouvel essai au prochain passage
        log.info("Labo : rejeu %s %s, %d trades", market, day, n)
        done += 1
        heartbeat.beat()
        await asyncio.sleep(0)
    return done


async def run_live_day(day: date, db: LabDB | None = None, markets=MARKETS, collect: bool = True,
                       now: datetime | None = None, transport=None) -> dict:
    """Journée en direct : séance d'actions terminée, créneaux crypto dont l'horizon est passé."""
    db = db or LabDB()
    now = now or datetime.now(timezone.utc)
    out = {}
    for market in markets:
        if market == "stock" and not (CO.session_ts(day) and day_ready("stock", day, now)):
            continue
        complete = day_ready(market, day, now)
        progress = ("live:" + market, "last_day") if complete else None
        try:
            universe = await prepare_day(db, market, day, now, transport) if collect else None
            out[market] = run_day(db, market, day, "live", universe, now=now, progress=progress)
        except Exception as exc:
            if complete:
                day_failed(db, f"live:{market}", day, exc)  # 3 échecs : le jour est sauté
            else:
                log.warning("Labo : direct %s %s en échec : %s", market, day, exc)
            out[market] = 0
        heartbeat.beat()
    return out
