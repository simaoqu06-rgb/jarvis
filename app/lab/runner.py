"""Rejeu historique et journées en direct du labo, reprenables et idempotents.

- replay(start_day, end_day, market) : jour par jour, chaque jour dans une transaction (trades,
  univers et avancement ensemble) ; une coupure reprend au jour suivant le dernier validé.
- run_live_day(day) : la séance qui vient de finir (actions) et les créneaux crypto du jour dont
  l'horizon est passé. Rejouer un jour remplace ses trades : jamais de doublon.
La collecte des données (réseau) est séparée du calcul : `collect=False` travaille sur la base seule.

Données manquantes (avis du 10 octobre, I4) : un symbole dont un lot a échoué à la source fait échouer le jour
(nouvel essai au passage suivant) au lieu de le calculer en silence sur un univers amputé. Au 3e essai, le jour se
calcule sans lui et le note (`missing_symbols` dans day:<D>) : un symbole durablement en panne (paire retirée
d'OKX, ticker refusé dans un lot Alpaca) sauterait sinon tous les jours du rejeu. Sauf si plus de la moitié de
l'univers manque (panne de la source) : le jour est alors sauté et compté (skipped:<D>). Les créneaux crypto
d'un jour du direct pas encore complet se calculent sans les absents (le jour est refait une fois complet,
avec la même règle). Chaque jour garde aussi
les valeurs de l'univers sans trade et pourquoi (`absent` : historique<100, séance vide, plan impossible).
"""
import asyncio
import logging
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from app.core.http import SourceError
from app.lab import collector as CO
from app.lab import generator as G
from app.lab import heartbeat
from app.lab import strategies as ST
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
                             now: datetime | None = None, transport=None, failed_out: set | None = None) -> None:
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
                          last_day - timedelta(days=1), now, transport, failed_out=failed_out)


async def prepare_day(db: LabDB, market: str, day: date, now: datetime | None = None, transport=None,
                      missing_out: set | None = None) -> list[dict]:
    """Collecte ce qu'il faut pour le jour (journalier de tous les symboles, 5 min de l'univers),
    calcule l'univers et le renvoie. Seuls les manques sont demandés.

    Un lot en échec à la source lève SourceError (I4). Avec `missing_out` (dernier essai), ses symboles y sont
    notés et le jour se calcule sans eux, sauf panne de la source : plus de la moitié de l'univers sans ses
    bougies de 5 min, ou univers vide."""
    now = now or datetime.now(timezone.utc)
    failed: set = set()
    await ensure_daily_range(db, market, day, day, now, transport, failed_out=failed)
    universe = U.build(db, market, day, save=False)
    names = [u["symbol"] for u in universe]
    start, end = CO.stock_window(day) if market == "stock" else CO.crypto_window(day)
    await CO.ensure_intraday(db, market, names, start, end, now, transport, failed_out=failed)
    index = G.INDEX["stock"]
    if market == "stock" and index not in names and CO.session_ts(day):
        # Labo v2, déclaration n° 6 : séance de SPY en 5 min (sortie sur avalement baissier de l'indice).
        await CO.ensure_intraday(db, market, [index], *CO.session_ts(day), now, transport, failed_out=failed)
    if failed:
        lost = failed & set(names)
        if missing_out is None or not names or 2 * len(lost) > len(names):
            raise SourceError(f"{len(failed)} symboles sans données : {', '.join(sorted(failed)[:5])}")
        missing_out.update(failed)
    return universe


MAX_DAY_ATTEMPTS = 3


def last_try(db: LabDB, job: str, day: date) -> bool:
    """Le prochain essai du jour est le dernier avant qu'il ne soit sauté (I4 : calculé sans les symboles manquants)."""
    return (db.get(job, f"attempts:{day.isoformat()}", 0) or 0) + 1 >= MAX_DAY_ATTEMPTS


def run_day(db: LabDB, market: str, day: date, origin: str, universe: list[dict] | None = None,
            now: datetime | None = None, progress: tuple[str, str] | None = None, missing: set | None = None) -> int:
    """Calcule et journalise un jour (sans réseau). Renvoie le nombre de trades écrits.

    Un symbole en erreur est sauté (une ligne de journal) et compté dans le bilan du jour,
    gardé dans progress (job du rejeu ou du direct, clé « day:AAAA-MM-JJ »), avec les valeurs de l'univers
    sans trade et leur motif (`absent`) et les symboles que la source n'a pas donnés (`missing`, I4)."""
    version, s = method(db)
    if universe is None:
        universe = U.build(db, market, day, save=False)
    errors: list[dict] = []
    strat: list[dict] = []
    absent: list[dict] = []
    now_ts = int(now.timestamp()) if now else None
    if market == "stock":
        moments, rows = G.stock_day(db, day, s, universe, errors, strat, absent)
    else:
        moments, rows = G.crypto_day(db, day, s, universe, now_ts, errors, strat, absent)
    job = progress[0] if progress else f"{origin}:{market}"
    with db.transaction():
        db.replace_universe(market, day.isoformat(), universe)
        db.replace_trades(origin, version, market, moments, rows)
        # Labo v2 : lignes des stratégies, une fois toutes les sorties du jour connues. Le premier jour
        # calculé ici borne le rattrapage des jours traités avant le labo v2 (rattrapage.py).
        if market == "stock" or ST.crypto_complete(day, now_ts):
            db.replace_strat(origin, market, day.isoformat(), strat)
            key, job_v2 = f"inline_from:{origin}", ST.progress_job(market)
            if not db.get(job_v2, key):
                db.set(job_v2, key, day.isoformat())
        info = {"trades": len(rows), "errors": len(errors), "error_symbols": [e["symbol"] for e in errors[:10]],
                "absent": len(absent), "absent_motifs": dict(Counter(a["motif"] for a in absent)),
                "absent_symbols": absent[:10]}
        if missing:
            info |= {"missing": len(missing), "missing_symbols": sorted(missing)[:10]}
        db.set(job, f"day:{day.isoformat()}", info)
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
                 max_days: int | None = None, now: datetime | None = None, transport=None,
                 max_seconds: float | None = None) -> int:
    """Rejoue les jours de start_day à end_day, en reprenant après le dernier jour validé.
    `max_days` et `max_seconds` bornent l'appel (au moins un jour est tenté) : le reste du passage
    du worker (crypto, Labo 2, leçons) ne doit pas attendre des heures derrière le rejeu.
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
        heartbeat.beat(f"rejeu {market} : journalier de l'univers")
        await ensure_daily_range(db, market, days[0], days[-1], now, transport)
    done = 0
    started = time.monotonic()
    for day in days:
        if max_days is not None and done >= max_days:
            break
        if max_seconds is not None and done and time.monotonic() - started >= max_seconds:
            break
        heartbeat.beat(f"rejeu {market} : {day}")
        try:
            missing = set() if last_try(db, job, day) else None
            universe = await prepare_day(db, market, day, now, transport, missing) if collect else None
            n = run_day(db, market, day, "replay", universe, progress=(job, "last_day"), missing=missing)
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
            # Jour pas encore complet : calculé sans les symboles en échec (il sera refait une fois complet) ;
            # complet : nouvel essai au passage suivant, calculé sans eux au dernier (I4).
            missing = set() if not complete or last_try(db, progress[0], day) else None
            universe = await prepare_day(db, market, day, now, transport, missing) if collect else None
            out[market] = run_day(db, market, day, "live", universe, now=now, progress=progress, missing=missing)
        except Exception as exc:
            if complete:
                day_failed(db, f"live:{market}", day, exc)  # 3 échecs : le jour est sauté
            else:
                log.warning("Labo : direct %s %s en échec : %s", market, day, exc)
            out[market] = 0
        heartbeat.beat()
    return out
