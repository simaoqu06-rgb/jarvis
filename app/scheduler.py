"""Boucles de fond qui envoient les notifications Discord pendant que le serveur tourne.

Démarrées seulement si DISCORD_WEBHOOK_URL est définie (et JARVIS_NOTIFICATIONS n'est pas « off »).
"""
import asyncio
import logging
import os
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app import alerts, config
from app.notify import bot, discord
from app.sources import alpaca
from app.trading import paper, scanner
from app.trading import proposals as PR
from app.trading.proposals import proposals
from app.trading import service as trading
from app.trading.settings import settings

log = logging.getLogger(__name__)
NEW_YORK = ZoneInfo("America/New_York")
BRIEF_TIME_NY = time(9, 0)  # 30 min avant l'ouverture, soit 15 h à Paris la plupart de l'année
MINUTE = 60

state = alerts.State(config.ALERTS_STATE_PATH)
book = trading.Book()
paper_book = paper.PaperBook()
_lock = asyncio.Lock()  # un seul job modifie l'état à la fois


def enabled() -> bool:
    return bool(config.secret(discord.KEY_NAME)) and os.getenv("JARVIS_NOTIFICATIONS", "on").lower() != "off"


async def _post_all(messages: list[str]) -> None:
    for message in messages:
        await discord.send(message)


def brief_due(now: datetime) -> bool:
    now_ny = now.astimezone(NEW_YORK)
    return now_ny.time() >= BRIEF_TIME_NY and state.data.get("brief_date") != now_ny.date().isoformat()


async def run_brief(now: datetime) -> None:
    await discord.send(await alerts.morning_brief(now))
    state.data["brief_date"] = now.astimezone(NEW_YORK).date().isoformat()


async def tick_brief() -> None:
    now = datetime.now(timezone.utc)
    if brief_due(now):
        await run_brief(now)


async def tick_moves() -> None:
    await _post_all(await alerts.check_price_moves(state))


async def tick_quakes() -> None:
    await _post_all(await alerts.check_earthquakes(state))


async def tick_news() -> None:
    await _post_all(await alerts.check_news(state))


async def tick_trades() -> None:
    await _post_all(await trading.check_trades(book))
    if os.getenv("JARVIS_DISCORD_COCKPIT", "on").lower() != "off" and (book.trades or state.data.get("cockpit_message_id")):
        # Message « cockpit » réécrit à chaque passage : à épingler une fois dans le salon.
        state.data["cockpit_message_id"] = await discord.upsert(
            trading.cockpit_text(book), state.data.get("cockpit_message_id")
        )


async def nyse_trading_day(now: datetime) -> bool:
    local = now.astimezone(NEW_YORK)
    closed, _ = await trading._nyse_calendar(local.year)
    return local.weekday() < 5 and local.date().isoformat() not in closed


def scan_due(now: datetime) -> bool:
    """À l'heure prévue, une fois par jour ; pas de rattrapage plus de 30 min après (serveur redémarré
    en pleine séance : les entrées de pré-ouverture n'auraient plus de sens)."""
    now_ny = now.astimezone(NEW_YORK)
    hour, minute = (int(x) for x in str(settings["scan_time_ny"]).split(":"))
    planned = datetime.combine(now_ny.date(), time(hour, minute), NEW_YORK)
    return (settings["scan_enabled"] and alpaca.configured() and timedelta(0) <= now_ny - planned < timedelta(minutes=30)
            and state.data.get("scan_date") != now_ny.date().isoformat())


async def tick_scan() -> None:
    """Scanner avant l'ouverture : message Discord, puis chaque valeur retenue part sur le compte fictif."""
    now = datetime.now(timezone.utc)
    if not scan_due(now):
        return
    state.data["scan_date"] = now.astimezone(NEW_YORK).date().isoformat()
    if not await nyse_trading_day(now):
        return
    try:
        result = await scanner.scan(now)
    except scanner.ScanError as exc:
        await discord.send(f"🔭 Scanner pré-ouverture impossible : {exc}")
        return
    await discord.send(scanner.text(result))
    for pick in result["picks"]:
        try:
            await paper.submit(paper_book, pick["plan"], pick["features"])
        except Exception:
            log.exception("Forward-test : envoi de %s en échec", pick["symbol"])


def report_due(now: datetime) -> bool:
    now_ny = now.astimezone(NEW_YORK)
    week = now_ny.strftime("%G-W%V")
    return now_ny.weekday() == 4 and now_ny.time() >= time(16, 30) and state.data.get("paper_report_week") != week


async def tick_paper() -> None:
    """Relit les ordres fictifs ; le vendredi soir, envoie le bilan agrégé (jamais trade par trade)."""
    if not alpaca.configured():
        return
    now = datetime.now(timezone.utc)
    if paper_book.active():
        await paper.check(paper_book, now)
    if report_due(now) and paper_book.entries:
        state.data["paper_report_week"] = now.astimezone(NEW_YORK).strftime("%G-W%V")
        st = paper.stats(paper_book)
        await discord.send(paper.report_text(st))
        for sg in st["suggestions"]:
            p = sg["key"] and proposals.propose(sg["key"], sg["value"], sg["why"])
            if not p:
                continue
            if bot.enabled():
                bot.offer(p)  # en privé, avec les boutons Approuver / Refuser
            else:
                await discord.send(f"🧪 {PR.text(p)}\n-# À approuver ou refuser dans le cockpit.")


JOBS = [
    ("suivi des trades et cockpit Discord", tick_trades, MINUTE),
    ("point du matin", tick_brief, MINUTE),
    ("scanner pré-ouverture", tick_scan, MINUTE),
    ("forward-test sur le compte fictif", tick_paper, 2 * MINUTE),
    ("mouvements de cours", tick_moves, 5 * MINUTE),
    ("séismes", tick_quakes, 5 * MINUTE),
    ("actus", tick_news, 15 * MINUTE),
]


async def _loop(name: str, job, every: int) -> None:
    while True:
        try:
            async with _lock:
                await job()
                state.save()
        except Exception:  # une alerte en échec ne doit jamais arrêter les autres
            log.exception("Notification « %s » en échec", name)
        await asyncio.sleep(every)


async def _bot() -> None:
    """Bot des commandes Discord ; se reconnecte après une coupure."""
    while True:
        try:
            await bot.run(_lock)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Bot Discord déconnecté, nouvelle tentative dans 1 min")
        await asyncio.sleep(MINUTE)


def start() -> list[asyncio.Task]:
    tasks = []
    if bot.enabled():
        tasks.append(asyncio.create_task(_bot()))
    else:
        log.info("Commandes Discord désactivées (DISCORD_BOT_TOKEN ou JARVIS_DISCORD_OWNER_ID absent)")
    if not enabled():
        log.info("Notifications Discord désactivées (DISCORD_WEBHOOK_URL absente ou JARVIS_NOTIFICATIONS=off)")
        return tasks
    return tasks + [asyncio.create_task(_loop(name, job, every)) for name, job, every in JOBS]
