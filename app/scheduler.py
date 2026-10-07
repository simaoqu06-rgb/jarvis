"""Boucles de fond qui envoient les notifications Discord pendant que le serveur tourne.

Le suivi des trades (TP/SL), le compte fictif et le scanner tournent toujours ; les alertes (point du
matin, cours, séismes, actus) et tous les envois ne partent que si DISCORD_WEBHOOK_URL est définie
(et JARVIS_NOTIFICATIONS n'est pas « off »).
"""
import asyncio
import logging
import os
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app import alerts, config
from app.core.outbox import Outbox
from app.lab import morning as lab_morning
from app.notify import bot, discord
from app.sources import alpaca
from app.trading import calendar as CAL
from app.trading import paper, scanner
from app.trading import proposals as PR
from app.trading.proposals import proposals
from app.trading import service as trading
from app.trading.settings import settings

log = logging.getLogger(__name__)
NEW_YORK = ZoneInfo("America/New_York")
BRIEF_TIME_NY = time(9, 0)  # 30 min avant l'ouverture, soit 15 h à Paris la plupart de l'année
BRIEF_DEADLINE_NY = time(12, 0)  # au-delà, le « point du matin » n'a plus de sens (serveur redémarré le soir)
SCAN_WINDOW = timedelta(minutes=30)
MAX_ATTEMPTS = {"brief": 5, "scan": 3, "report": 5}  # tentatives par jour (par semaine pour le bilan)
MINUTE = 60

state = alerts.State(config.ALERTS_STATE_PATH)
book = trading.Book()
paper_book = paper.PaperBook()
outbox = Outbox(config.OUTBOX_PATH, discord.split_message)
_background: set[asyncio.Task] = set()
_lock = asyncio.Lock()  # un seul job modifie l'état à la fois


def enabled() -> bool:
    return bool(config.secret(discord.KEY_NAME)) and os.getenv("JARVIS_NOTIFICATIONS", "on").lower() != "off"


async def _send_part(text: str) -> None:
    await discord.send(text)


def _queue(texts: list[str] | str, kind: str = "alerte", **kw) -> None:
    """Met en file seulement si les envois sont actifs (sinon la file grossirait sans jamais partir)."""
    if enabled():
        outbox.add(texts, kind, **kw)


def _kick_outbox() -> None:
    """Lance l'envoi en tâche de fond : les attentes de Discord (limites, pannes) ne bloquent jamais le verrou global."""
    if not enabled():
        return
    task = asyncio.get_running_loop().create_task(outbox.flush(_send_part))
    _background.add(task)  # garde une référence tant que la tâche tourne
    task.add_done_callback(_background.discard)


async def _post_all(messages: list[str], kind: str = "alerte", expires_at: datetime | None = None) -> None:
    """Met les messages en file (sur disque) puis envoie tout ce qui attend.

    Un message en échec reste en file et repart au passage suivant ; il n'empêche pas les autres.
    """
    if messages:
        _queue(messages, kind, expires_at=expires_at)
    _kick_outbox()


def _attempts(name: str, period: str) -> int:
    rec = state.data.get(f"{name}_attempts") or {}
    return rec.get("n", 0) if rec.get("period") == period else 0


def _count_attempt(name: str, period: str) -> int:
    n = _attempts(name, period) + 1
    state.data[f"{name}_attempts"] = {"period": period, "n": n}
    return n


def brief_due(now: datetime) -> bool:
    """Tous les jours entre 9 h et midi (New York) ; pas de point du matin envoyé l'après-midi."""
    now_ny = now.astimezone(NEW_YORK)
    today = now_ny.date().isoformat()
    return (BRIEF_TIME_NY <= now_ny.time() < BRIEF_DEADLINE_NY and state.data.get("brief_date") != today
            and _attempts("brief", today) < MAX_ATTEMPTS["brief"])


async def run_brief(now: datetime) -> None:
    """Le texte est mis en file avec sa date limite : en cas d'échec Discord, l'envoi reprend au
    morceau en échec (jamais de répétition des premiers morceaux)."""
    now_ny = now.astimezone(NEW_YORK)
    today = now_ny.date().isoformat()
    _count_attempt("brief", today)
    text = await alerts.morning_brief(now)
    outbox.add(text, "point du matin", expires_at=datetime.combine(now_ny.date(), BRIEF_DEADLINE_NY, NEW_YORK),
               key=f"brief:{today}")
    state.data["brief_date"] = today
    state.save()
    _kick_outbox()


async def tick_brief() -> None:
    now = datetime.now(timezone.utc)
    if brief_due(now):
        await run_brief(now)


async def tick_outbox() -> None:
    await outbox.flush(_send_part)


async def tick_moves() -> None:
    await _post_all(await alerts.check_price_moves(state))


async def tick_quakes() -> None:
    await _post_all(await alerts.check_earthquakes(state))


async def tick_news() -> None:
    await _post_all(await alerts.check_news(state))


async def tick_trades() -> None:
    messages = await trading.check_trades(book, save=False)
    _queue(messages, "trade")  # sur disque avant l'état des trades : un TP/SL ne se perd plus
    book.save()
    _kick_outbox()
    if enabled() and os.getenv("JARVIS_DISCORD_COCKPIT", "on").lower() != "off" and (book.trades or state.data.get("cockpit_message_id")):
        # Message « cockpit » réécrit à chaque passage : à épingler une fois dans le salon.
        try:
            state.data["cockpit_message_id"] = await discord.upsert(
                trading.cockpit_text(book), state.data.get("cockpit_message_id")
            )
        except discord.DiscordError as exc:
            log.warning("Cockpit Discord pas mis à jour : %s", exc)


def scan_due(now: datetime) -> bool:
    """À l'heure prévue, une fois par jour de séance ; pas de rattrapage plus de 30 min après (serveur
    redémarré en pleine séance : les entrées de pré-ouverture n'auraient plus de sens)."""
    now_ny = now.astimezone(NEW_YORK)
    today = now_ny.date().isoformat()
    hour, minute = (int(x) for x in str(settings["scan_time_ny"]).split(":"))
    planned = datetime.combine(now_ny.date(), time(hour, minute), NEW_YORK)
    return (settings["scan_enabled"] and alpaca.configured() and timedelta(0) <= now_ny - planned < SCAN_WINDOW
            and CAL.is_trading_day(now_ny.date()) and state.data.get("scan_date") != today
            and _attempts("scan", today) < MAX_ATTEMPTS["scan"])


async def tick_scan() -> None:
    """Scanner avant l'ouverture : message Discord, puis chaque valeur retenue part sur le compte fictif.

    Tourne sans le verrou global (les plans se calculent pendant que le suivi continue) ; le verrou
    n'est pris que pour toucher au compte fictif et à l'état. En cas d'échec, nouvelle tentative à la
    minute suivante dans la fenêtre, au plus MAX_ATTEMPTS["scan"] fois.
    """
    now = datetime.now(timezone.utc)
    if not scan_due(now):
        return
    now_ny = now.astimezone(NEW_YORK)
    today = now_ny.date().isoformat()
    n = _count_attempt("scan", today)
    try:
        result = await scanner.scan(now)
    except Exception as exc:
        log.warning("Scanner pré-ouverture en échec (tentative %d/%d) : %s", n, MAX_ATTEMPTS["scan"], exc)
        if n >= MAX_ATTEMPTS["scan"]:
            async with _lock:
                state.data["scan_date"] = today
                await _post_all([f"🔭 Scanner pré-ouverture impossible : {exc}"], "scanner")
        return
    bounds = CAL.session_bounds(now_ny.date())
    async with _lock:
        _queue(scanner.text(result), "scanner", expires_at=bounds[1] if bounds else None)
        state.data["scan_date"] = today
        state.save()
        for pick in result["picks"]:
            try:
                await paper.submit(paper_book, pick["plan"], pick["features"])
            except Exception:
                log.exception("Forward-test : envoi de %s en échec", pick["symbol"])
        _kick_outbox()


def report_due(now: datetime) -> bool:
    now_ny = now.astimezone(NEW_YORK)
    week = now_ny.strftime("%G-W%V")
    return (now_ny.weekday() == 4 and now_ny.time() >= time(16, 30) and state.data.get("paper_report_week") != week
            and _attempts("report", week) < MAX_ATTEMPTS["report"])


async def tick_paper() -> None:
    """Relit les ordres fictifs ; le vendredi soir, envoie le bilan agrégé (jamais trade par trade)."""
    if not alpaca.configured():
        return
    now = datetime.now(timezone.utc)
    if paper_book.active():
        await paper.check(paper_book, now)
    if report_due(now) and paper_book.entries:
        week = now.astimezone(NEW_YORK).strftime("%G-W%V")
        _count_attempt("report", week)
        st = paper.stats(paper_book)
        messages = [paper.report_text(st)]
        for sg in st["suggestions"]:
            p = sg["key"] and proposals.propose(sg["key"], sg["value"], sg["why"])
            if not p:
                continue
            if bot.enabled():
                bot.offer(p)  # en privé, avec les boutons Approuver / Refuser
            else:
                messages.append(f"🧪 {PR.text(p)}\n-# À approuver ou refuser dans le cockpit.")
        _queue(messages, "bilan du forward-test")
        state.data["paper_report_week"] = week  # seulement une fois le bilan calculé et mis en file
        state.save()
        _kick_outbox()


async def tick_lab(now: datetime | None = None) -> dict | None:
    """Labo 3 : message du matin (8 h - 9 h à Paris, au plus un par jour) et annonces du worker.

    Tourne même sans webhook (le rodage garde le message sans l'envoyer). L'envoi privé est fait par
    le bot (bot.deliver_lab) ; sans bot, les annonces partent sur le salon (webhook) si possible."""
    now = now or datetime.now(timezone.utc)
    st = lab_morning.shared_state()
    digest = lab_morning.tick(now, proposals, st, bot.enabled())
    for ev in lab_morning.pull_events(st, now):
        log.info("Labo : %s", ev["texte"])
        if bot.enabled():
            bot.offer_lab_text(ev["texte"])
        elif enabled():
            outbox.add(ev["texte"], "labo", key=f"labo:{ev['id']}")
            _kick_outbox()
    return digest


# (nom, job, période, verrou global pendant tout le job)
# Toujours lancés, webhook ou pas : seul l'envoi de leurs messages dépend de enabled().
CORE_JOBS = [
    ("suivi des trades et cockpit Discord", tick_trades, MINUTE, True),
    ("scanner pré-ouverture", tick_scan, MINUTE, False),  # prend le verrou lui-même, seulement pour l'état
    ("forward-test sur le compte fictif", tick_paper, 2 * MINUTE, True),
]
# Seulement avec les notifications actives.
JOBS = [
    ("file d'envoi Discord", tick_outbox, MINUTE, False),  # l'envoi a son propre verrou
    ("point du matin", tick_brief, MINUTE, True),
    ("mouvements de cours", tick_moves, 5 * MINUTE, True),
    ("séismes", tick_quakes, 5 * MINUTE, True),
    ("actus", tick_news, 15 * MINUTE, True),
]


async def _run(job, locked: bool) -> None:
    if locked:
        async with _lock:
            await job()
            state.save()
    else:
        await job()
        async with _lock:
            state.save()


async def _loop(name: str, job, every: int, locked: bool = True) -> None:
    while True:
        try:
            await _run(job, locked)
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
    # Labo 3 : message du matin et annonces, avec ou sans webhook (rodage : journalisé seulement).
    tasks.append(asyncio.create_task(_loop("labo : message du matin", tick_lab, MINUTE, True)))
    if bot.enabled():
        tasks.append(asyncio.create_task(_bot()))
    else:
        log.info("Commandes Discord désactivées (DISCORD_BOT_TOKEN ou JARVIS_DISCORD_OWNER_ID absent)")
    tasks += [asyncio.create_task(_loop(name, job, every, locked)) for name, job, every, locked in CORE_JOBS]
    if not enabled():
        log.info("Notifications Discord désactivées (DISCORD_WEBHOOK_URL absente ou JARVIS_NOTIFICATIONS=off) : "
                 "suivi des trades, compte fictif et scanner tournent quand même, sans envoi")
        return tasks
    return tasks + [asyncio.create_task(_loop(name, job, every, locked)) for name, job, every, locked in JOBS]
