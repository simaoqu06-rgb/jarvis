"""Worker du labo : `python -m app.lab.worker`, service docker séparé (jamais dans le serveur web).

Toutes les ~10 min :
1. en direct : chaque séance US terminée depuis 30 min est journalisée (origin = live), et les
   créneaux crypto dont l'horizon de 24 h est passé ;
2. rejeu historique, par morceaux : gros morceaux la nuit (22 h - 8 h à Paris) et le week-end,
   un seul jour à la fois pendant la séance US pour que le serveur reste réactif.
3. Labo 2, chaque étape isolée (une erreur n'arrête pas les suivantes), battement entre deux :
   régimes de marché, mouvements du jour et fiches catalyseurs (base seule), actus Finnhub des fiches
   (jamais pendant la séance US), file des règles soumises puis validation, statistiques en cache,
   résumé du soir (après 22 h 30 à Paris). Budgets par période dans LAB2_BUDGET : gros la nuit et
   le week-end, minimes pendant la séance.
4. Labo 3 (étape « methode ») : décisions de Simão et types de catalyseurs déposés par le serveur,
   challenger, bascule automatique ou retrait, fichier du champion lu par JARVIS en direct.
5. Carnet des leçons (étape « lecons », lessons.py) : leçons déposées par la routine du soir,
   re-mesurées quand les trades changent, fichier des leçons confirmées lu par JARVIS en direct.
6. Grade de JARVIS (étape « grade », grade.py) : avis du council et décisions de Simão, grade de
   chaque marché recalculé quand les trades changent, fichier lab_grade.json lu en direct.
Le rejeu couvre JARVIS_LAB_REPLAY_DAYS séances (504 ≈ 2 ans) avant le premier lancement du worker ;
le direct prend le relais à partir de ce jour-là. JARVIS_LAB_ENABLED=off met le worker en veille.
"""
import asyncio
import logging
import os
import time as clock
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.lab import catalysts as CAT
from app.lab import collector as CO
from app.lab import generator as G
from app.lab import grade as GRADE
from app.lab import heartbeat
from app.lab import lessons as L
from app.lab import method as M
from app.lab import regime as REG
from app.lab import runner as R
from app.lab import stats as STATS
from app.lab import summary as SUM
from app.lab import validator as V
from app.lab.db import LabDB, now_iso
from app.sources import alpaca

log = logging.getLogger("app.lab.worker")
PARIS = ZoneInfo("Europe/Paris")
TICK_SECONDS = 600
CHUNK_DAYS = {"nuit": 20, "jour": 4, "seance": 1}
# Durée maximale du rejeu par marché et par passage (secondes) : un jour d'actions coûte 2 à 5 min ;
# sans borne, 20 jours la nuit retenaient la crypto et le Labo 2 plus d'une heure, et une mise à
# jour du serveur coupait le passage avant qu'ils ne tournent.
REPLAY_SECONDS = {"nuit": 900, "jour": 300, "seance": 120}
MAX_LIVE_DAYS_PER_TICK = 5
KEY_WARNING_SECONDS = 3600
# Labo 2 : jours de mouvements calculés, appels Finnhub, règles validées et âge maximal des
# statistiques en cache (secondes), par période.
LAB2_BUDGET = {
    "nuit": {"moves_days": 30, "news_calls": 40, "rules": 3, "stats_age": 3600},
    "jour": {"moves_days": 5, "news_calls": 10, "rules": 1, "stats_age": 3 * 3600},
    "seance": {"moves_days": 2, "news_calls": 0, "rules": 0, "stats_age": 6 * 3600},
}
_key_warned_at: float | None = None


def configure_logging() -> None:
    """Comme app/main.py : INFO avec l'heure, httpx et httpcore en WARNING (leurs URL contiennent des clés)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s : %(message)s")
    logging.getLogger().setLevel(logging.INFO)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def enabled() -> bool:
    return os.getenv("JARVIS_LAB_ENABLED", "on").strip().lower() not in ("0", "off", "false", "non", "faux")


def replay_sessions() -> int:
    try:
        return max(1, int(os.getenv("JARVIS_LAB_REPLAY_DAYS", "504")))
    except ValueError:
        return 504


# --- Décisions (fonctions pures) -----------------------------------------------------------

def period(now: datetime) -> str:
    """« nuit » : 22 h - 8 h à Paris ou week-end ; « seance » : Bourse de New York ouverte ; sinon « jour »."""
    local = now.astimezone(PARIS)
    if local.weekday() >= 5 or local.hour >= 22 or local.hour < 8:
        return "nuit"
    if CO.CAL.session_open(now):
        return "seance"
    return "jour"


def replay_seconds(now: datetime, summary_last_day: str | None) -> float:
    """Durée permise au rejeu d'un marché à ce passage : REPLAY_SECONDS de la période, sans déborder
    sur l'heure du résumé du soir (22 h 30 à Paris) tant qu'il n'est pas fait, pour qu'il soit prêt
    avant la routine du soir (22 h 52)."""
    box = float(REPLAY_SECONDS[period(now)])
    local = now.astimezone(PARIS)
    target = datetime.combine(local.date(), SUM.SUMMARY_AT, PARIS)
    if summary_last_day != local.date().isoformat() and local < target:
        box = min(box, (target - local).total_seconds())
    return box


def replay_budget(now: datetime) -> int:
    """Nombre de jours de rejeu par passage et par marché."""
    return CHUNK_DAYS[period(now)]


def replay_bounds(live_start: date, sessions: int) -> tuple[date, date]:
    """Du jour de la `sessions`-ième séance avant le premier lancement à la veille de ce lancement."""
    return CO.sessions_back(live_start, sessions), live_start - timedelta(days=1)


def live_days_due(market: str, last_done: date | None, live_start: date, now: datetime) -> list[date]:
    """Jours à journaliser en direct : actions, chaque séance close depuis 30 min ; crypto, chaque
    jour dont au moins un créneau a atteint son horizon (le jour en cours sera complété plus tard)."""
    day = last_done + timedelta(days=1) if last_done else live_start
    out = []
    while day <= now.date() and len(out) < MAX_LIVE_DAYS_PER_TICK:
        if market == "stock":
            if CO.session_ts(day):
                if not R.day_ready("stock", day, now):
                    break
                out.append(day)
        else:
            if G.crypto_moments(day)[0] + 86400 > now.timestamp():
                break
            out.append(day)
        day += timedelta(days=1)
    return out


def stock_pause(now: datetime, collect: bool = True) -> str | None:
    """Raison de laisser les actions de côté à ce passage (None = on y va).

    Pendant la séance régulière, la limite Alpaca (200 requêtes/min) sert à JARVIS en direct
    (scanner, forward-test) : le labo n'y touche pas, la crypto continue. Sans clés Alpaca, la
    crypto tourne seule."""
    if CO.CAL.session_open(now):
        return "séance US en cours"
    if collect and not alpaca.configured():
        return "clés Alpaca absentes"
    return None


def warn_missing_keys(now_mono: float | None = None) -> bool:
    """Une seule ligne d'avertissement par heure (pas de pile d'appels). True si elle est écrite."""
    global _key_warned_at
    now_mono = clock.monotonic() if now_mono is None else now_mono
    if _key_warned_at is not None and now_mono - _key_warned_at < KEY_WARNING_SECONDS:
        return False
    _key_warned_at = now_mono
    log.warning("Labo : clés Alpaca absentes (ALPACA_API_KEY / ALPACA_SECRET_KEY), actions en pause ; la crypto continue")
    return True


def crypto_windows_due(day: date, now: datetime, done_until: int | None) -> bool:
    """Un nouveau créneau crypto du jour a-t-il atteint son horizon depuis le dernier passage ?"""
    due = [m for m in G.crypto_moments(day) if m + 86400 <= now.timestamp()]
    return bool(due) and (done_until is None or due[-1] > done_until)


def _history_start(market: str, day: date, stock_sessions: int, crypto_days: int) -> int:
    if market == "stock":
        return CO.session_ts(CO.sessions_back(day, stock_sessions))[0]
    return CO.utc_midnight(day) - crypto_days * 86400


def prune_cutoff(db: LabDB, market: str, now: datetime) -> int | None:
    """Plus ancienne bougie de 5 min encore utile : fenêtre du prochain jour rejoué (120 séances ou
    40 jours), fenêtre du direct, et historique des jours dont les mouvements restent à calculer.
    None : rien à effacer de sûr."""
    today = now.astimezone(CO.NEW_YORK).date() if market == "stock" else now.astimezone(timezone.utc).date()
    cuts = [_history_start(market, today, CO.STOCK_HISTORY_SESSIONS + 2, CO.CRYPTO_HISTORY_DAYS + 2)]
    job = f"replay:{market}"
    start, end, last = db.get(job, "start"), db.get(job, "end"), db.get(job, "last_day")
    if start and not (last and last >= end):
        cursor = date.fromisoformat(last) if last else date.fromisoformat(start)
        cuts.append(_history_start(market, cursor, CO.STOCK_HISTORY_SESSIONS + 1, CO.CRYPTO_HISTORY_DAYS + 1))
    row = db.conn.execute(
        "SELECT MIN(u.day) FROM universe u WHERE u.market = ? AND NOT EXISTS "
        "(SELECT 1 FROM progress p WHERE p.job = ? AND p.key = u.day)", (market, f"moves:{market}")).fetchone()
    if row and row[0]:
        cuts.append(_history_start(market, date.fromisoformat(row[0]), CAT.RVOL_SESSIONS + 2, CAT.RVOL_SESSIONS + 2))
    return min(cuts)


def summary_first(now: datetime, summary_last_day: str | None) -> bool:
    """Le résumé du soir est attendu à ce passage : le Labo 2 passe avant le rejeu."""
    return SUM.due(now, summary_last_day) is not None


def market_order(db: LabDB, markets: list[str]) -> list[str]:
    """Le rejeu alterne le marché qui passe en premier : un marché lent (journalier de tout l'univers
    au premier passage, collecte) n'affame plus l'autre."""
    if len(markets) < 2:
        return markets
    first = db.get("replay", "first")
    order = markets[1:] + markets[:1] if first == markets[0] else list(markets)
    db.set("replay", "first", order[0])
    return order


# --- Boucle -------------------------------------------------------------------------------

async def tick(db: LabDB, now: datetime | None = None, collect: bool = True, transport=None) -> dict:
    now = now or datetime.now(timezone.utc)
    live_start = db.get("lab", "live_start")
    if not live_start:
        live_start = now.astimezone(CO.NEW_YORK).date().isoformat()
        db.set("lab", "live_start", live_start)
    live_start = date.fromisoformat(live_start)
    R.method(db)
    report: dict = {"live": {}, "replay": {}, "paused": {}}
    pause = stock_pause(now, collect)
    if pause:
        report["paused"]["stock"] = pause
        if pause.startswith("clés"):
            warn_missing_keys()
    markets = [m for m in R.MARKETS if not (m == "stock" and pause)]
    for market in markets:
        try:
            last = db.get(f"live:{market}", "last_day")
            for day in live_days_due(market, date.fromisoformat(last) if last else None, live_start, now):
                if market == "crypto":
                    key = day.isoformat()
                    if not crypto_windows_due(day, now, db.get("live:crypto", f"windows:{key}")):
                        continue
                n = await R.run_live_day(day, db, (market,), collect, now, transport)
                if market == "crypto":
                    due = [m for m in G.crypto_moments(day) if m + 86400 <= now.timestamp()]
                    db.set("live:crypto", f"windows:{day.isoformat()}", due[-1])
                report["live"][f"{market}:{day}"] = n.get(market, 0)
                heartbeat.beat()
        except Exception as exc:
            log.warning("Labo : direct %s en échec : %s: %s", market, type(exc).__name__, exc)
    start, end = replay_bounds(live_start, replay_sessions())
    budget = replay_budget(now)
    started = clock.monotonic()

    def current() -> datetime:  # `now` avancé du temps déjà passé dans ce passage
        return now + timedelta(seconds=clock.monotonic() - started)

    # Résumé du soir attendu : le Labo 2 (qui le construit) passe avant le rejeu, sinon 2 × 15 min de
    # rejeu le repoussaient après la routine de 22 h 52, voire après minuit (résumé perdu).
    if summary_first(now, db.get("summary", "last_day")):
        report["labo2"] = await lab2(db, current(), collect)
    for market in market_order(db, markets):
        try:
            report["replay"][market] = await R.replay(
                start, end, market, db, collect, budget, now, transport,
                max_seconds=replay_seconds(current(), db.get("summary", "last_day")))
        except Exception as exc:
            log.warning("Labo : rejeu %s en échec : %s: %s", market, type(exc).__name__, exc)
        heartbeat.beat()
    if "labo2" not in report:
        report["labo2"] = await lab2(db, current(), collect)
    report["menage"] = {}
    for market in R.MARKETS:
        try:
            cutoff = prune_cutoff(db, market, current())
            if cutoff is not None:
                report["menage"][market] = CO.prune_intraday(db, market, cutoff)
        except Exception as exc:
            log.warning("Labo : ménage %s en échec : %s: %s", market, type(exc).__name__, exc)
    heartbeat.beat()
    return report


def trades_signature(db: LabDB) -> list:
    row = db.conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM trades").fetchone()
    return [row[0], row[1]]


def refresh_stats(db: LabDB, now: datetime, max_age: float, force: bool = False) -> dict | None:
    """Recalcule les statistiques de la version en cours si les trades ont changé et que le cache a
    plus de `max_age` secondes (ou `force`). Renvoie les statistiques à jour, ou None si inchangées."""
    sig = trades_signature(db)
    last = db.get("stats", "signature")
    at = db.get("stats", "at", 0) or 0
    if not force and (sig == last or now.timestamp() - at < max_age):
        return None
    version = V.current_version(db.conn)
    data = STATS.compute(db.conn, version=version) if version else STATS.compute(db.conn)
    with db.transaction():
        STATS.store(db.conn, "courant", data, sig[0], now_iso())
        db.set("stats", "signature", sig)
        db.set("stats", "at", now.timestamp())
    return data


async def lab2(db: LabDB, now: datetime, collect: bool = True) -> dict:
    """Étapes du Labo 2, chacune isolée, dans le budget de la période."""
    budget = LAB2_BUDGET[period(now)]
    report: dict = {}
    fresh: dict = {}

    async def step(name: str, fn):
        try:
            result = fn()
            if asyncio.iscoroutine(result):
                result = await result
            report[name] = result if not isinstance(result, (list, dict)) or name != "stats" else "à jour"
            return result
        except Exception as exc:
            log.warning("Labo : étape %s en échec : %s: %s", name, type(exc).__name__, exc)
            report[name] = f"échec : {type(exc).__name__}"
            return None
        finally:
            heartbeat.beat()

    await step("regimes", lambda: sum(REG.refresh(db, m) for m in R.MARKETS))
    await step("mouvements", lambda: CAT.backfill_moves(db, now, budget["moves_days"]))
    if collect and not CO.CAL.session_open(now):
        await step("actus", lambda: CAT.fetch_news(db, now, budget["news_calls"]))
    await step("file_regles", lambda: V.ingest_queue(db))
    await step("regles", lambda: len(V.process_pending(db, budget["rules"])))
    await step("methode", lambda: M.step(db, now))
    await step("lecons", lambda: L.step(db, now, budget["stats_age"]))
    await step("grade", lambda: GRADE.step(db, now))
    summary_day = SUM.due(now, db.get("summary", "last_day"))
    stats = await step("stats", lambda: refresh_stats(db, now, budget["stats_age"], force=bool(summary_day)))
    if stats:
        fresh["stats"] = stats
    if summary_day:
        def build_summary():
            data = SUM.build(db.conn, summary_day, fresh.get("stats") or STATS.cached(db.conn))
            with db.transaction():
                SUM.store(db, data, SUM.text(data))
                db.set("summary", "last_day", summary_day)
            return SUM.size(data)
        await step("resume", build_summary)
    return report


async def loop() -> None:
    """Boucle principale ; le battement n'est rafraîchi qu'après chaque étape terminée (lot de
    bougies, jour rejoué, passage complet) : une boucle bloquée cesse de battre."""
    db = LabDB()
    heartbeat.enabled = True
    heartbeat.beat()
    log.info("Labo démarré : base %s, rejeu de %d séances", db.path, replay_sessions())
    while True:
        started = clock.monotonic()
        try:
            report = await tick(db)
            log.info("Labo : passage terminé en %.0f s (%s)", clock.monotonic() - started, report)
        except Exception as exc:
            log.warning("Labo : passage en échec : %s: %s", type(exc).__name__, exc)
        heartbeat.beat()
        await asyncio.sleep(max(60.0, TICK_SECONDS - (clock.monotonic() - started)))


def main() -> None:
    configure_logging()
    if not enabled():
        log.info("Labo désactivé (JARVIS_LAB_ENABLED=off) : en veille")
        heartbeat.enabled = True
        while True:
            heartbeat.beat()
            clock.sleep(TICK_SECONDS)
    asyncio.run(loop())


if __name__ == "__main__":
    main()
