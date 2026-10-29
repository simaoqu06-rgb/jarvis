"""Boucles de fond qui envoient les notifications Discord pendant que le serveur tourne.

Le suivi des trades (TP/SL), le compte fictif et le scanner tournent toujours ; les alertes (point du
matin, cours, séismes, actus) et tous les envois sur le salon ne partent que si DISCORD_WEBHOOK_URL est
définie (et JARVIS_NOTIFICATIONS n'est pas « off »). Les alertes des trades suivis partent aussi en
message privé par le bot (JARVIS_TRADE_DM, activé par défaut), webhook ou pas.
"""
import asyncio
import logging
import os
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app import alerts, config
from app.core.outbox import Outbox
from app.lab import grade as lab_grade
from app.lab import method as lab_method
from app.lab import morning as lab_morning
from app.lab import routines_watch as RW
from app.notify import bot, discord
from app.sources import alpaca, twelvedata
from app.trading import calendar as CAL
from app.trading import advice, paper, scanner
from app.trading import gemini as GEMINI
from app.trading import plus10 as PLUS10
from app.trading import visibilite as VIS
from app.trading import confirm as CONFIRM
from app.trading import reco as RECO
from app.trading import proposals as PR
from app.trading import research
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
trading.taken_trades = lambda: book.trades  # le grade tient compte du risque déjà ouvert et du jour
paper_book = paper.PaperBook()
confirm_state = CONFIRM.load()
reco_book = RECO.RecoBook()  # « si j'avais suivi JARVIS » : GO confirmés et KILL envoyés, suivis tels qu'envoyés
last_runs: dict[str, dict] = {}  # dernier passage de chaque boucle (cockpit : /api/cockpit/state et /health)
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


def _dm_trades(texts: list[str] | str) -> None:
    """Copie en privé des alertes des trades suivis : le salon ne mentionne personne, le message privé
    sonne. Ne dépend pas du webhook (JARVIS_TRADE_DM=off pour la couper)."""
    if bot.enabled() and bot.trade_dm_on():
        for text in [texts] if isinstance(texts, str) else texts:
            bot.offer_trade_text(text)


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


async def tick_disk() -> None:
    await _post_all(alerts.check_disk(state, datetime.now(timezone.utc)), "disque")


def routine_seen(kind: str, value: str) -> None:
    """Dépôt d'une routine Claude vu par le serveur (`soir` : soirée de l'avis du council), pour tick_routines."""
    seen = state.data.setdefault("routines", {})
    if (seen.get(kind) or "") < value:
        seen[kind] = value


async def tick_routines(now: datetime | None = None) -> None:
    """Prévient une fois sur Discord quand l'équipe du soir ou la recherche avant l'ouverture n'a rien déposé
    (routines_watch ; audits de GPT et Gemini, 25/10/2026)."""
    now = now or datetime.now(timezone.utc)
    seen = state.data.setdefault("routines", {})
    if "soir" not in seen:  # mise en service : la soirée d'hier est supposée vue (pas de fausse alerte)
        seen["soir"] = (now.astimezone(RW.PARIS).date() - timedelta(days=1)).isoformat()
    messages = []
    hit = RW.soir_alert(now, seen.get("soir"), seen.get("alerte_soir"))
    if hit:
        seen["alerte_soir"] = hit[0]
        messages.append(hit[1])
    view = research.scan_view(research.last_scan(), now)
    scan = view.get("scan") or {}
    verdicts = len(research.ResearchBook().day(view["jour"]))
    hit = RW.recherche_alert(now, scan.get("jour"), len(scan.get("picks") or []), verdicts,
                             seen.get("alerte_recherche"))
    if hit:
        seen["alerte_recherche"] = hit[0]
        messages.append(hit[1])
    if messages:
        _queue(messages, "alerte")
        _kick_outbox()


async def tick_trades() -> None:
    messages = await trading.check_trades(book, save=False)
    _queue(messages, "trade")  # sur disque avant l'état des trades : un TP/SL ne se perd plus
    _dm_trades(messages)
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


async def tick_closing(now: datetime | None = None) -> None:
    """Avant la clôture US (30 min par défaut) : sortir, alléger ou garder chaque action suivie encore
    ouverte. Une fois par séance ; les conseils sont gardés sur les trades pour être mesurés.
    Calculé hors du verrou global (bougies, catalyseur), qui n'est pris que pour écrire."""
    now = now or datetime.now(timezone.utc)
    stocks = [t for t in book.open_trades() if t["market"] == "stock"]
    # Un KILL pris se vend à la clôture (la règle que mesure son journal) : pas de conseil garder / alléger.
    kills = [t for t in stocks if t.get("killmode")]
    trades = [t for t in stocks if not t.get("killmode")]
    if not stocks or not advice.closing_due(now, state.data.get("closing_advice_date")):
        return
    today = now.astimezone(NEW_YORK).date().isoformat()
    async with _lock:
        state.data["closing_advice_date"] = today  # une seule tentative : un échec ne doit pas répéter le message
        state.save()
    _, close = CAL.session_bounds(now.astimezone(NEW_YORK).date())
    if kills:
        last = (close - timedelta(minutes=1)).astimezone(NEW_YORK)
        text = ("🔪 **Killmode** : vers " + f"{last.hour} h {last.minute:02d}" + " New York, annule les TP et SL de "
                + ", ".join(sorted({t["symbol"] for t in kills})) + " puis vends le reste au marché en un ordre, avant "
                f"la clôture ({close.astimezone(NEW_YORK).hour} h New York) : un KILL ne passe pas la nuit, c'est la "
                "règle que mesure son journal (vendu à la dernière minute de la séance).")
        async with _lock:
            _queue(text, "clôture des KILL", expires_at=close)
            _dm_trades(text)
            _kick_outbox()
    if not trades:
        return
    advices = await advice.advise_book(trades, now)
    async with _lock:
        by_id = {t["id"]: t for t in book.trades}
        for a in advices:
            if "error" not in a and a["id"] in by_id and by_id[a["id"]]["status"] == "open":
                advice.record(by_id[a["id"]], a, now)
        book.save()
        text = advice.closing_text(advices, advice.advice_stats(book.trades), (close - now).total_seconds() / 60)
        _queue(text, "conseil de clôture", expires_at=close)
        _dm_trades(text)
        _kick_outbox()


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
    text = scanner.text(result)
    try:  # n° 9 : liste d'attente « potentiel +10 », mesurée, jamais un GO ; son échec ne retient pas le scanner
        text += "\n\n" + PLUS10.waitlist_text(await PLUS10.waitlist(now))
    except Exception as exc:
        log.warning("Potentiel +10 : liste d'attente en échec : %s", exc)
    async with _lock:
        _queue(text, "scanner", expires_at=bounds[1] if bounds else None)
        state.data["scan_date"] = today
        state.save()
        confirm_state.clear()  # GO du jour, relus après l'ouverture par tick_confirm
        CONFIRM.register(confirm_state, result, today)
        CONFIRM.save(confirm_state)
        for pick in result["picks"]:
            try:
                await paper.submit(paper_book, pick["plan"], pick["features"])
            except Exception:
                log.exception("Forward-test : envoi de %s en échec", pick["symbol"])
        _kick_outbox()


async def _usd_per_eur() -> float | None:
    try:
        from app import needs  # import tardif : needs tire tout le serveur de données

        return (await needs.fx_rates("EUR", ["USD"])).data["rates"]["USD"]
    except Exception as exc:
        log.warning("Taux EUR/USD indisponible pour la taille des GO confirmés : %s", exc)
        return None


def _size_rule(now: datetime) -> tuple[dict, float | None]:
    """Grade du moment (actions, trades suivis) et risque en € des tailles des GO confirmés et du potentiel +10 :
    la règle unique (risque du grade, 20 € au plus) ; ta règle de 20 € quand le grade ne propose rien (le message
    le dit dans la ligne d'ordre) ; None, pas de taille, quand ce 0 % vient de l'arrêt de la journée ou du budget de
    risque ouvert plein (confirm.rule_size, JARVIS_MP_TAILLE_APRES_ARRET). Un grade illisible vaut 0 % (fail-closed)."""
    try:
        g = lab_grade.risk_now("stock", book.trades, now)
    except Exception as exc:
        log.warning("Grade illisible pour la taille des GO (%s: %s) : 0 %%", type(exc).__name__, exc)
        g = lab_grade.unreadable(exc)
    return g, CONFIRM.rule_size(g, trading.recommended_eur(g["risque_propose"]), trading.rule_eur())


async def _bars_5min(symbol: str) -> list[dict] | None:
    try:
        return await twelvedata.candles(symbol, 5, outputsize=100)
    except Exception as exc:
        log.warning("Contrôle des GO : bougies 5 min de %s indisponibles : %s", symbol, exc)
        return None


async def tick_confirm(now: datetime | None = None) -> None:
    """GO du scanner relus toutes les 5 min, de 15 min après l'ouverture à 11 h 30 New York :
    confirmé (ordre exact en MP), annulé ou expiré ; puis mesure brut/confirmé après la clôture.
    Bougies lues hors du verrou global, qui n'est pris que pour écrire."""
    now = now or datetime.now(timezone.utc)
    if not confirm_state.get("gos") or confirm_state.get("closed"):
        return
    day = datetime.fromisoformat(confirm_state["day"]).date()
    bounds = CAL.session_bounds(day)
    if not bounds or now < bounds[0] + CONFIRM.WAIT:
        return
    day_open, close = bounds
    if now >= close + timedelta(minutes=2):
        bars = {s: CONFIRM.session_bars(b, day_open, now) for s in confirm_state["gos"]
                if (b := await _bars_5min(s))}
        rate = await _usd_per_eur()  # change du jour pour confirme_r20 (1,15 si la source ne répond pas)
        async with _lock:
            CONFIRM.close_day(confirm_state, bars, rate=rate)
            CONFIRM.save(confirm_state)
        return
    pending = [s for s, g in confirm_state["gos"].items() if g["status"] in CONFIRM.PENDING]
    if not pending:
        return
    if CONFIRM.expire_due(now, day_open):
        async with _lock:
            for s in pending:
                confirm_state["gos"][s]["status"] = "expiré"
            CONFIRM.save(confirm_state)
            text = CONFIRM.expired_text(pending)
            _queue(text, "scanner", expires_at=close)
            _dm_trades(text)
            _kick_outbox()
        return
    last_closed = int(now.timestamp()) // CONFIRM.BAR * CONFIRM.BAR - CONFIRM.BAR
    if all((confirm_state["gos"][s]["last_bar"] or 0) >= last_closed for s in pending):
        return  # pas de nouvelle bougie 5 min close : rien à relire (un crédit Twelve Data par lecture)
    texts, rate, size = [], None, None
    for s in pending:
        raw = await _bars_5min(s)
        if not raw:
            continue
        go = confirm_state["gos"][s]
        ev = CONFIRM.evaluate(go, CONFIRM.session_bars(raw, day_open, now), day_open)
        if ev:
            go["events"].append(ev)
            if ev["kind"] == "confirmé" and rate is None:
                rate = await _usd_per_eur()
            if ev["kind"] == "confirmé" and size is None:
                size = _size_rule(now)  # une fois par passage
            g, risk_eur = size or (None, trading.rule_eur())
            texts.append(CONFIRM.event_text(s, go, ev, now, risk_eur, rate, g))
            if ev["kind"] == "confirmé":
                _reco_go(s, go, ev, now, risk_eur, rate, g)
    async with _lock:
        CONFIRM.save(confirm_state)
        if texts:
            _queue(texts, "scanner", expires_at=close)
            _dm_trades(texts)
            _kick_outbox()


def _reco_go(symbol: str, go: dict, ev: dict, now: datetime, risk_eur: float | None, rate: float | None,
             g: dict | None) -> None:
    """Le GO confirmé entre au journal des recommandations avec la taille de son MP (confirm.event_text) ; un échec
    ne retient jamais le message."""
    try:
        risk_usd = risk_eur * rate if rate and risk_eur else None
        qty = CONFIRM.shares(ev["trigger"], go["stop"], risk_usd)
        note = None if qty else ("pas de taille : " + (CONFIRM.portfolio_stop(g) or "aucun risque proposé")
                                 if risk_eur is None else "pas de taille : une action dépasse le risque"
                                 if qty == 0 else "pas de taille : change EUR/USD indisponible")
        reco_book.add(RECO.go_from_confirm(symbol, go, ev, confirm_state["day"], now, qty, risk_eur, rate, note))
    except Exception:
        log.exception("Journal des recommandations : GO confirmé %s pas noté", symbol)


async def tick_reco(now: datetime | None = None) -> None:
    """Journal des recommandations : reprend les KILL notés, fait avancer les GO confirmés et les KILL sur les bougies
    5 min (déclenchement, objectifs, stop, vente à la clôture d'un KILL). Rien n'est envoyé. Lectures hors du verrou."""
    now = now or datetime.now(timezone.utc)
    rate = await _usd_per_eur() if any(_new_kills()) else None
    await RECO.tick(reco_book, now, lock=_lock, usd_per_eur=rate)


def _new_kills():
    """KILL du jour pas encore au journal (le change n'est lu que pour eux)."""
    from app.trading import killmode as K  # import tardif : killmode tire le labo

    since = datetime.fromisoformat(reco_book.since).timestamp()
    return (c for c in K.calls() if K._group_of(c) == "kill" and c.get("at", 0) >= since
            and not reco_book.get(f"kill-{c['id']}"))


async def tick_plus10(now: datetime | None = None) -> None:
    """n° 9 : liste « potentiel +10 » confirmée au gap d'ouverture (9 h 46 New York) avec les niveaux des essais du
    labo, puis le range de 30 min de Rebond (10 h 01) ; sur le salon et en MP. Une fois par passage et par séance."""
    now = now or datetime.now(timezone.utc)
    kind = PLUS10.due(now)
    if not kind:
        return
    rate = await _usd_per_eur()
    g, risk_eur = _size_rule(now)
    try:
        result = await PLUS10.confirmed(kind, now, risk_eur * rate if rate and risk_eur else None)
    except Exception as exc:
        log.warning("Potentiel +10 : passage %s en échec : %s", kind, exc)
        return  # nouvelle tentative à la minute suivante, dans la fenêtre du passage
    PLUS10.mark_done(kind, now)
    bounds = CAL.session_bounds(now.astimezone(NEW_YORK).date())
    text = PLUS10.confirmed_text(result | {"eurusd": rate, "risk_eur": risk_eur, "grade": g})
    async with _lock:
        _queue(text, "scanner", expires_at=bounds[1] if bounds else None)
        _dm_trades(text)
        _kick_outbox()


async def tick_gemini(now: datetime | None = None) -> None:
    """Challenger « méthode Gemini » (25 octobre 2026) : passages A (9 h 36), B (12 h 36) et C (15 h 31) New York,
    plans sur le salon et en MP ; solde après la clôture du lendemain contre le témoin aléatoire (rien n'est envoyé)."""
    now = now or datetime.now(timezone.utc)
    st = GEMINI.load()
    kind = GEMINI.due(now, st)
    if kind:
        try:
            result = await GEMINI.scan(kind, now)
        except Exception as exc:
            log.warning("Challenger Gemini : passage %s en échec : %s", kind, exc)
            return  # nouvelle tentative à la minute suivante, dans la fenêtre du passage
        st = GEMINI.load()
        GEMINI.mark_done(st, kind, now)
        st.setdefault("plans", []).extend(result["plans"])
        GEMINI.save(st)
        if result["plans"]:
            rate = await _usd_per_eur()
            _, risk_eur = _size_rule(now)
            texts = [GEMINI.plan_text(p, rate, risk_eur) for p in result["plans"]]
            bounds = CAL.session_bounds(now.astimezone(NEW_YORK).date())
            async with _lock:
                _queue(texts, "scanner", expires_at=bounds[1] if bounds else None)
                _dm_trades(texts)
                _kick_outbox()
        return
    if time(16, 20) <= now.astimezone(NEW_YORK).time() < time(18, 0):
        await GEMINI.settle_due(now, st)


async def tick_visibilite(now: datetime | None = None) -> None:
    """Audit anti-fuite : actus Finnhub des gappers vues à 9 h 46 New York, relues à 11 h (rien n'est envoyé)."""
    now = now or datetime.now(timezone.utc)
    kind = VIS.due(now)
    if not kind:
        return
    try:
        await VIS.run(kind, now)
    except Exception as exc:
        log.warning("Visibilité Finnhub : passage %s en échec : %s", kind, exc)
        VIS.mark_done(kind, now)  # une mesure manquée ne se rattrape pas : pas de nouvel essai chaque minute


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
        warnings = await paper.check(paper_book, now)
        if warnings:
            _queue(warnings, "forward-test")
            _kick_outbox()
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
    # Grade : une hausse de lettre attend l'accord de Simão (en privé, boutons Approuver / Refuser).
    for p in lab_grade.sync_proposals(proposals):
        if bot.enabled():
            bot.offer(p)
        elif enabled():
            outbox.add(f"🏅 {PR.text(p)}\n-# À approuver ou refuser dans le cockpit.", "labo", key=f"grade:{p['id']}")
            _kick_outbox()
    # Méthode : le challenger fait nettement mieux à un regard fixe ; il n'est mis en place qu'avec l'accord de Simão.
    for p in lab_method.sync_proposals(proposals):
        if bot.enabled():
            bot.offer(p)
        elif enabled():
            outbox.add(f"🧪 {PR.text(p)}\n-# À approuver ou refuser dans le cockpit.", "labo", key=f"promotion:{p['id']}")
            _kick_outbox()
    return digest


ATTENTION_LOOP = "page À faire"


async def tick_attention(now: datetime | None = None) -> None:
    """Rubrique « À faire » du cockpit : relit l'état (aucun appel réseau) et tient à jour data/attention.json.
    N'envoie rien sur Discord."""
    from app.web import attention  # import tardif : la page importe le planificateur

    await attention.run(now)


async def tick_version() -> None:
    """Version qui tourne contre `main` sur GitHub (dépôt public) : PR fusionnées pas encore déployées (page À
    faire). Une requête toutes les 30 min, ou dès que le commit du disque change (git pull)."""
    from app.core import version

    await version.tick()


# (nom, job, période, verrou global pendant tout le job ; None : ni verrou ni sauvegarde de l'état des alertes)
# Toujours lancés, webhook ou pas : seul l'envoi de leurs messages dépend de enabled().
CORE_JOBS = [
    ("suivi des trades et cockpit Discord", tick_trades, MINUTE, True),
    ("scanner pré-ouverture", tick_scan, MINUTE, False),  # prend le verrou lui-même, seulement pour l'état
    ("conseil avant la clôture", tick_closing, MINUTE, False),  # idem
    ("forward-test sur le compte fictif", tick_paper, 2 * MINUTE, True),
    ("contrôle des GO après l'ouverture", tick_confirm, MINUTE, False),  # prend le verrou lui-même pour écrire
    ("potentiel +10 confirmé", tick_plus10, MINUTE, False),  # n° 9 ; prend le verrou lui-même pour envoyer
    ("challenger Gemini", tick_gemini, MINUTE, False),  # prend le verrou lui-même pour envoyer
    ("visibilité des actus Finnhub", tick_visibilite, MINUTE, False),  # mesure seule, rien n'est envoyé
    ("journal des recommandations", tick_reco, MINUTE, False),  # Papier du cockpit ; prend le verrou pour écrire
    # Rubrique « À faire » du cockpit : lecture seule, sans le verrou (ne doit jamais attendre derrière un job bloqué)
    (ATTENTION_LOOP, tick_attention, MINUTE, None),
    ("version déployée", tick_version, MINUTE, None),  # GitHub (30 min) : PR fusionnées pas encore déployées
]
# Seulement avec les notifications actives.
JOBS = [
    ("file d'envoi Discord", tick_outbox, MINUTE, False),  # l'envoi a son propre verrou
    ("point du matin", tick_brief, MINUTE, True),
    ("mouvements de cours", tick_moves, 5 * MINUTE, True),
    ("séismes", tick_quakes, 5 * MINUTE, True),
    ("actus", tick_news, 15 * MINUTE, True),
    ("disque du serveur", tick_disk, 30 * MINUTE, True),
    ("veille des routines Claude", tick_routines, 5 * MINUTE, True),
]


async def _run(job, locked: bool | None) -> None:
    if locked is None:  # ni verrou ni état des alertes : ne peut jamais attendre derrière un job bloqué
        await job()
    elif locked:
        async with _lock:
            await job()
            state.save()
    else:
        await job()
        async with _lock:
            state.save()


async def _loop(name: str, job, every: int, locked: bool | None = True) -> None:
    while True:
        started = datetime.now(timezone.utc)
        try:
            await _run(job, locked)
            last_runs[name] = {"at": started.isoformat(timespec="seconds"), "ok": True, "every_s": every,
                               "duree_s": round((datetime.now(timezone.utc) - started).total_seconds(), 1),
                               "echecs_suite": 0}
        except Exception as exc:  # une alerte en échec ne doit jamais arrêter les autres
            log.exception("Notification « %s » en échec", name)
            failed = (last_runs.get(name) or {}).get("echecs_suite", 0) + 1  # page À faire : échecs d'affilée
            last_runs[name] = {"at": started.isoformat(timespec="seconds"), "ok": False, "every_s": every,
                               "erreur": type(exc).__name__, "echecs_suite": failed}
        await asyncio.sleep(every)


async def _bot() -> None:
    """Bot des commandes Discord ; se reconnecte après une coupure."""
    while True:
        try:
            await bot.run(_lock)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Bot Discord déconnecté, nouvelle tentative dans 1 min")
            bot.note_error(exc)  # page À faire : jeton refusé, connexion perdue
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
