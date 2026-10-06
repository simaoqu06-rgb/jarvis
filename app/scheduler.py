"""Boucles de fond qui envoient les notifications Discord pendant que le serveur tourne.

Démarrées seulement si DISCORD_WEBHOOK_URL est définie (et JARVIS_NOTIFICATIONS n'est pas « off »).
"""
import asyncio
import logging
import os
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from app import alerts, config
from app.notify import discord
from app.trading import service as trading

log = logging.getLogger(__name__)
NEW_YORK = ZoneInfo("America/New_York")
BRIEF_TIME_NY = time(9, 0)  # 30 min avant l'ouverture, soit 15 h à Paris la plupart de l'année
MINUTE = 60

state = alerts.State(config.ALERTS_STATE_PATH)
book = trading.Book()
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


JOBS = [
    ("suivi des trades", tick_trades, 2 * MINUTE),
    ("point du matin", tick_brief, MINUTE),
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


def start() -> list[asyncio.Task]:
    if not enabled():
        log.info("Notifications Discord désactivées (DISCORD_WEBHOOK_URL absente ou JARVIS_NOTIFICATIONS=off)")
        return []
    return [asyncio.create_task(_loop(name, job, every)) for name, job, every in JOBS]
