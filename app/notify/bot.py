"""Commandes Discord : demander un plan, suivre un trade ou lancer le scanner depuis le téléphone.

Le webhook ne sait qu'envoyer ; pour lire les messages il faut un bot (DISCORD_BOT_TOKEN) avec
l'intention « Message Content » activée. Leçon d'OpenClaw (bot ouvert à tous) : le bot ne répond
qu'aux comptes listés dans JARVIS_DISCORD_OWNER_ID et ignore tout le reste sans rien dire.

  !plan NVDA            plan complet (long, action ; « short », « crypto » ou « action » en plus)
  !avis NVDA            avis de JARVIS : long, short ou attendre, pourquoi, et ce que le labo a mesuré
  !prends NVDA          calcule le plan et suit le trade (TP/SL annoncés, cockpit) ; `!prends NVDA 132.5`
                        = ton prix d'entrée réel, `stop 128` en plus pour ton stop, `tp 135 138 142` pour tes objectifs ;
                        `!prends NVDA 132.5 go` = le GO confirmé du jour, avec son stop et ses objectifs
  !sortie NVDA 131.2    ta sortie réelle : clôture le trade suivi à ce prix, ou corrige le prix de sa dernière
                        sortie s'il est déjà terminé (stop exécuté plus bas, objectif touché sans remplissage)
  !rouvre NVDA 132.5    le stop a été touché mais tu tiens toujours la position : rouvre le trade
                        (même syntaxe que !prends, ex. `!rouvre MRVL 281 stop 273.9 tp 286.8 290.5 299.5`)
  !positions            que faire des trades suivis avant la clôture : sortir, alléger ou garder
  !position NVDA 132.5  même conseil pour une position prise hors de JARVIS (« short », `stop 128` en plus)
  !scan                 scanner pré-ouverture tout de suite
  !confirmations        GO du scanner confirmés ou pas après l'ouverture, et ce que ça rapporte
  !bilan                bilan du forward-test sur le compte fictif
  !desaccords           trades pris contre l'avis de JARVIS (plans NO GO) comparés aux autres
  !propositions         réglages que JARVIS propose et qui attendent ta réponse
  !oui 3 / !non 3       approuver ou refuser la proposition 3 (ou les boutons sous le message)
  !regle                réglages actuels
  !labo                 état du labo (rejeu, espérance, méthode en place, challenger, propositions)
  !lecons               carnet des leçons du labo (confirmées, à confirmer, démenties)
  !grade                grade de JARVIS (F- à S+), de quoi il est fait et le risque qu'il permet
  !strategies           labo v2 : stratégies à déclencheur contre v1 et leur témoin aléatoire
  !autopsie             où les trades du labo échouent (élan contre le hasard, stops, gagnants qui frôlent le stop) ;
                        `!autopsie crypto` ou `!autopsie actions`
  !potentiel            potentiel +10 : part des séances qui touchent +8 % selon le gap, types de catalyseurs
  !kill                 JARVIS Killmode, désactivé par défaut : `!kill on`, `!kill NVDA`, `!kill scan`, `!kill bilan`
  !aide                 cette liste

Les alertes des trades suivis (TP, stop, sortie anticipée, objectif en retard, conseil avant la clôture)
arrivent aussi en message privé, copie de celles du salon qui ne mentionne personne (JARVIS_TRADE_DM=off
pour couper la copie).
Les propositions de réglages arrivent en message privé, avec des boutons Approuver / Refuser.
Les règles validées par le labo arrivent groupées dans un seul message privé entre 8 h et 9 h
(app/lab/morning.py ; JARVIS_LAB_DM=on, sinon rodage : gardées sans envoi), avec un bouton Approuver
et un bouton Refuser par règle ; les mêmes `!oui N` / `!non N` marchent.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone

from app import config
from app.lab import db as lab_db
from app.lab import audit as lab_audit
from app.lab import autopsie as lab_autopsie
from app.lab import grade as lab_grade
from app.lab import lessons as lab_lessons
from app.lab import morning as lab_morning
from app.lab import status as lab_status
from app.lab import potentiel as lab_potentiel
from app.lab import strat_stats as lab_strategies
from app.notify.discord import split_message
from app.trading import advice, paper, scanner
from app.trading import proposals as PR
from app.trading import service as trading
from app.trading.settings import settings
from app.trading import killmode  # JARVIS Killmode (!kill), éteint par défaut

log = logging.getLogger(__name__)
TOKEN_NAME = "DISCORD_BOT_TOKEN"
CRYPTO = {"BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "AVAX", "LINK", "DOT", "LTC", "BNB", "TRX", "SUI", "PEPE",
          "SHIB", "XLM", "ATOM", "NEAR", "APT", "ARB", "OP", "INJ", "TIA", "SEI", "HBAR", "UNI", "AAVE", "XMR"}
HELP = __doc__.split("\n\n")[2].strip()
PROPOSAL_PREFIX = "jarvis:prop:"
outbox: asyncio.Queue = asyncio.Queue()  # propositions à envoyer en privé avec leurs boutons
delivered: set[tuple[int, int]] = set()  # (proposition, propriétaire) déjà reçues en privé
RETRY_FIRST, RETRY_MAX = 5.0, 600.0  # attente entre deux essais d'envoi en privé (secondes)
MAX_ATTEMPTS = 12  # ensuite on abandonne l'envoi privé : la proposition reste dans !propositions et le cockpit
LAB_TICK = 30.0  # secondes entre deux passages de l'envoi du labo (message du matin, annonces)
lab_texts: asyncio.Queue = asyncio.Queue()  # annonces du labo (méthode en place, challenger retiré)
trade_texts: asyncio.Queue = asyncio.Queue()  # alertes des trades suivis (TP, stop, sortie anticipée, clôture)


def offer_lab_text(text: str) -> None:
    """Annonce du labo à envoyer en privé au propriétaire (au plus une fois)."""
    lab_texts.put_nowait(text)


def trade_dm_on() -> bool:
    return os.getenv("JARVIS_TRADE_DM", "on").strip().lower() not in ("off", "0", "false", "non", "faux")


def offer_trade_text(text: str) -> None:
    """Copie en privé d'une alerte d'un trade suivi : le salon ne mentionne personne, le message privé
    sonne. Envoyée au plus une fois, dès que le bot est connecté ; l'original reste sur le salon."""
    trade_texts.put_nowait(text)


async def send_private(text: str, owner_ids: set[int], fetch_user, users: dict) -> bool:
    """Envoie un texte en privé à chaque propriétaire (découpé à 2000 caractères) ; False si un envoi échoue."""
    ok = True
    for oid in sorted(owner_ids):
        try:
            users[oid] = users.get(oid) or await fetch_user(oid)
            for chunk in split_message(text):
                await users[oid].send(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            users.pop(oid, None)  # un compte introuvable sera recherché à nouveau
            log.warning("Alerte de trade non envoyée en privé au compte %s : %s (%s)", oid, exc, text)
            ok = False
    return ok


def offer(proposal: dict) -> None:
    """Envoie une proposition au propriétaire dès que le bot est connecté."""
    outbox.put_nowait(proposal)


def queue_pending() -> int:
    """Remet en file les propositions en attente gardées sur disque (data/trading_proposals.json).

    Appelé à chaque connexion du bot : rien n'est perdu si le serveur a redémarré ou si un envoi
    a échoué. Celles déjà reçues par tous les propriétaires sont ignorées à l'envoi.
    """
    pending = [p for p in PR.proposals.pending() if p.get("kind") != "labo"]  # labo : message du matin groupé
    for p in pending:
        outbox.put_nowait(p)
    return len(pending)


async def deliver_once(p: dict, owner_ids: set[int], fetch_user, view_for, users: dict | None = None) -> bool:
    """Envoie la proposition en privé à chaque propriétaire qui ne l'a pas encore reçue.

    Renvoie True quand tous l'ont reçue (ou qu'elle n'est plus en attente), False s'il faut réessayer.
    """
    current = PR.proposals.get(p["id"])
    if current is not None and current["status"] != "pending":
        return True  # déjà décidée (cockpit, !oui/!non) : plus rien à envoyer
    if p.get("kind") == "labo":
        return True  # envoyée dans le message du matin groupé (app/lab/morning.py)
    users = {} if users is None else users
    ok = True
    for oid in sorted(owner_ids):
        if (p["id"], oid) in delivered:
            continue
        try:
            user = users.get(oid) or await fetch_user(oid)
            users[oid] = user
            await user.send(f"🧪 {PR.text(p)}", view=view_for(p["id"]))
            delivered.add((p["id"], oid))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Proposition %s non envoyée en privé au compte %s", p["id"], oid)
            ok = False
    return ok


def owners() -> set[int]:
    return {int(x) for x in os.getenv("JARVIS_DISCORD_OWNER_ID", "").replace(" ", "").split(",") if x.isdigit()}


def enabled() -> bool:
    return bool(config.secret(TOKEN_NAME)) and bool(owners())


def _price(word: str) -> float | None:
    try:
        x = float(word.replace(",", "."))
    except ValueError:
        return None
    return x if x > 0 else None


def parse_trade(args: list[str]) -> dict:
    """« NVDA short », « eth », « SOL crypto long », « NVDA 132.5 stop 128 tp 135 138 » → symbole, marché,
    sens, prix d'entrée, stop et objectifs (None quand ils ne sont pas donnés)."""
    if not args:
        raise ValueError("donne un ticker, ex. `!plan NVDA`")
    words = [a.lower() for a in args[1:]]
    entry = stop = None
    targets: list[float] = []
    in_tp = False
    for i, w in enumerate(words):
        if w == "tp":
            in_tp = True
        elif in_tp and _price(w) is not None:
            targets.append(_price(w))
        elif w == "stop" and i + 1 < len(words):
            in_tp = False
            stop = _price(words[i + 1])
            if stop is None:
                raise ValueError(f"stop illisible : `{words[i + 1]}`")
        elif _price(w) is not None and (i == 0 or words[i - 1] != "stop") and entry is None:
            entry = _price(w)
        else:
            in_tp = False
    if "tp" in words and not targets:
        raise ValueError("donne les objectifs après `tp`, ex. `tp 286.8 290.5 299.5`")
    symbol = args[0].upper()
    if symbol.endswith("USD") and len(symbol) > 4:  # « BTCUSD » → BTC
        symbol = symbol[:-3]
    if "crypto" in words:
        market = "crypto"
    elif "action" in words or "stock" in words:
        market = "stock"
    else:
        market = "crypto" if symbol in CRYPTO | set(config.WATCHLIST_CRYPTO) else "stock"
    side = "short" if "short" in words else "long"
    go = {"go": True} if "go" in words else {}  # « go » : le GO confirmé du jour (`!prends … go`)
    # « kill » : un KILL de Killmode pris, mesuré par son journal et pas par `!desaccords`
    return {"symbol": symbol, "market": market, "side": side, "entry": entry, "stop": stop,
            "targets": targets or None} | ({"killmode": True} if "kill" in words else {}) | go


GO_STATUS_FR = {"attente": "en attente du repli", "repli": "repli vu, reprise du VWAP pas encore confirmée",
                "annulé": "annulé", "expiré": "expiré à 11 h 30 New York"}


def confirmed_go(symbol: str, state: dict, now: datetime | None = None) -> tuple[dict | None, str]:
    """Le GO du scanner confirmé aujourd'hui (New York) sur `symbol`, d'après l'état des confirmations, ou None
    et la raison en clair."""
    day = (now or datetime.now(timezone.utc)).astimezone(trading.NEW_YORK).date().isoformat()
    if state.get("day") != day:
        return None, "pas de GO du scanner aujourd'hui"
    go = (state.get("gos") or {}).get(symbol.upper())
    if go is None:
        return None, f"{symbol.upper()} n'est pas dans les GO du scanner du jour"
    if go.get("status") != "confirmé":
        return None, f"son GO est {GO_STATUS_FR.get(go.get('status'), go.get('status'))}"
    return go, ""


def settings_text() -> str:
    current = settings.all()
    lines = ["**Réglages du trading** (✏️ = changé après une proposition approuvée)"]
    for key, value in current.items():
        mark = " ✏️" if key in settings.overrides else ""
        lines.append(f"`{key}` = {value}{mark}")
    return "\n".join(lines)


def grade_text() -> str:
    from app import scheduler

    conn = lab_db.open_readonly()
    hist, indicatif = [], []
    if conn is not None:
        try:
            hist = lab_grade.history(conn, limit=5)
            indicatif = lab_autopsie.indicatif_lines((lab_autopsie.cached(conn) or {}).get("grade_indicatif"))
        finally:
            conn.close()
    extra = lab_grade.populations_text(indicatif, lab_grade.reels(scheduler.book.trades))
    return lab_grade.text(lab_grade.current(), hist) + ("\n" + extra if extra else "")


def autopsie_text(args: list[str]) -> str:
    conn = lab_db.open_readonly()
    if conn is None:
        return "🩺 Base du labo absente : le service jarvis-lab n'a pas encore démarré."
    words = {a.lower() for a in args}
    market = "crypto" if "crypto" in words else "stock" if words & {"actions", "action", "stock"} else None
    try:
        return lab_autopsie.text(lab_autopsie.cached(conn), market)
    finally:
        conn.close()


def potentiel_text() -> str:
    conn = lab_db.open_readonly()
    if conn is None:
        return "🎯 Base du labo absente : le service jarvis-lab n'a pas encore démarré."
    try:
        return lab_potentiel.text(lab_potentiel.cached(conn))
    finally:
        conn.close()


def strategies_text() -> str:
    conn = lab_db.open_readonly()
    if conn is None:
        return "🧪 Base du labo absente : le service jarvis-lab n'a pas encore démarré."
    try:
        audit = lab_audit.text(lab_audit.cached(conn))  # lot 2 (C2) : pour audit, jamais pour décider
        return lab_strategies.text(lab_strategies.cached(conn)) + (f"\n\n{audit}" if audit else "")
    finally:
        conn.close()


def lessons_text() -> str:
    conn = lab_db.open_readonly()
    if conn is None:
        return "📒 Base du labo absente : le service jarvis-lab n'a pas encore démarré."
    try:
        return lab_lessons.text(conn)
    finally:
        conn.close()


async def handle(content: str, lock: asyncio.Lock | None = None) -> str | None:
    """Réponse à une commande (None si ce n'est pas une commande JARVIS)."""
    if not content.startswith("!"):
        return None
    parts = content[1:].split()
    if not parts:
        return None
    cmd, args = parts[0].lower(), parts[1:]
    try:
        if cmd in ("aide", "help"):
            return HELP
        if cmd == "plan":
            req = parse_trade(args)
            return trading.plan_text(await trading.build_plan(req["symbol"], req["market"], req["side"],
                                                              req["entry"], req["stop"]))
        if cmd == "avis":
            req = parse_trade(args)
            return advice.opinion_text(await advice.opinion(req["symbol"], req["market"]))
        if cmd == "positions":
            from app import scheduler
            trades = scheduler.book.open_trades()
            if not trades:
                return ("Aucun trade suivi. Pour une position prise hors de JARVIS : `!position NVDA 132.5` "
                        "(ton prix d'entrée), ou `!prends NVDA 132.5` pour qu'il la suive.\n"
                        + advice.stats_line(advice.advice_stats(scheduler.book.trades)))
            advices = await advice.advise_book(trades)
            async with lock or asyncio.Lock():
                by_id = {t["id"]: t for t in scheduler.book.trades}
                for a in advices:
                    if "error" not in a and a["id"] in by_id:
                        advice.record(by_id[a["id"]], a, datetime.now(timezone.utc))
                scheduler.book.save()
            return advice.closing_text(advices, advice.advice_stats(scheduler.book.trades),
                                       title="🧭 **Tes positions**")
        if cmd == "position":
            req = parse_trade(args)
            if req["entry"] is None:
                return "Donne ton prix d'entrée, ex. `!position NVDA 132.5` (`short`, `stop 128` en plus si besoin)."
            a = await advice.advise_position(req["symbol"], req["market"], req["side"], req["entry"], req["stop"])
            return (advice.advice_text(a) + f"\n-# Plan recalculé sur ton entrée {trading._num(req['entry'])} : stop "
                    f"{trading._num(a['plan']['stop'])}, objectifs {', '.join(trading._num(t['price']) for t in a['plan']['targets'])}. "
                    f"`!prends {a['symbol']} {req['entry']:g}{' short' if req['side'] == 'short' else ''}` pour que JARVIS la suive.")
        if cmd == "prends":
            req = parse_trade(args)
            kill = killmode.kill_today(req["symbol"]) if req.get("killmode") else None
            if kill:  # stop et objectifs du KILL du jour quand ils manquent : la gestion que son journal mesure
                req["stop"] = req["stop"] if req["stop"] is not None else kill["stop"]
                req["targets"] = req["targets"] or kill["targets"]
            go = None
            if req.get("go"):  # stop et objectifs du GO confirmé du jour, pas recalculés : ceux que mesure `!confirmations`
                from app import scheduler  # import tardif : scheduler importe ce module
                go, why = confirmed_go(req["symbol"], scheduler.confirm_state)
                if go is None:
                    price = f"{req['entry']:g}" if req["entry"] is not None else "<ton prix>"
                    return (f"Pas de GO confirmé aujourd'hui pour {req['symbol']} ({why}) : rien n'est suivi. "
                            f"`!prends {req['symbol']} {price}` sans `go` pour le suivre sur un plan recalculé.")
                if req["side"] != "long":
                    return f"Le GO du scanner sur {req['symbol']} est à l'achat : `!prends {req['symbol']} <ton prix> go`."
                req["entry"] = req["entry"] if req["entry"] is not None else go.get("trigger")
                req["stop"] = req["stop"] if req["stop"] is not None else go["stop"]
                req["targets"] = req["targets"] or go["targets"]
            plan = await trading.build_plan(req["symbol"], req["market"], req["side"], req["entry"], req["stop"],
                                            req["targets"])
            if go:
                plan["policy"] = "go_confirme"
            if req.get("killmode"):
                plan["killmode"] = True
                plan["sizing"] = killmode.taken_sizing(plan)  # 20 € hors grade, compté dans le risque ouvert
                plan.pop("sizing_regle", None)
            async with lock or asyncio.Lock():
                from app import scheduler  # import tardif : scheduler importe ce module
                scheduler.book.add(plan, "via Killmode" if req.get("killmode") else
                                   "via GO confirmé" if go else "via Discord")
            head = trading.disagreement_text(plan) + "\n" if trading.against_jarvis(plan) else ""
            if go:
                head += (f"✅ GO confirmé du jour : stop {trading._num(plan['stop'])}, objectifs "
                         f"{' · '.join(trading._num(t['price']) for t in plan['targets'])}. Stop remonté à l'entrée "
                         "après TP2 (ta règle), pas de sortie au temps.\n")
                if plan.get("score") and not plan["score"]["go"]:
                    head += ("-# La note recalculée à ton entrée est NO GO ; le trade compte quand même avec JARVIS, "
                             "comme le GO confirmé du scanner.\n")
            return head + "📌 Trade suivi\n" + trading.plan_text(plan)
        if cmd == "sortie":
            if not args:
                return "Donne le symbole et ton prix de sortie réel, ex. `!sortie NVDA 131.2`."
            req = parse_trade(args)
            if req["entry"] is None:
                return f"Donne ton prix de sortie réel, ex. `!sortie {req['symbol']} 131.2`."
            async with lock or asyncio.Lock():
                from app import scheduler
                res = trading.record_exit(scheduler.book, req["symbol"], req["entry"])
            if res is None:
                return f"Aucun trade {req['symbol']} suivi : rien à clôturer ni à corriger."
            return trading.exit_text(res)
        if cmd == "rouvre":
            req = parse_trade(args)
            if req["entry"] is None:
                return "Donne ton prix d'entrée, ex. `!rouvre MRVL 281 stop 273.9 tp 286.8 290.5 299.5`."
            plan = await trading.build_plan(req["symbol"], req["market"], req["side"], req["entry"], req["stop"],
                                            req["targets"])
            async with lock or asyncio.Lock():
                from app import scheduler
                old = trading.reopen(scheduler.book, req["symbol"], plan)
            if old is None:
                return (f"Aucun trade {req['symbol']} terminé par le stop à rouvrir. "
                        f"`!prends {req['symbol']} {req['entry']:g}` pour en suivre un nouveau.")
            return (f"♻️ Trade {req['symbol']} rouvert : le stop {trading._num(old['stop_current'])} n'a pas été "
                    "exécuté chez ton courtier, l'ancienne sortie est effacée du journal.\n" + trading.plan_text(plan))
        if cmd == "scan":
            return scanner.text(await scanner.scan(save=False))
        if cmd == "bilan":
            from app import scheduler
            return paper.report_text(paper.stats(scheduler.paper_book))
        if cmd in ("confirmations", "confirmation"):
            from app import scheduler
            from app.trading import confirm as CONFIRM

            return CONFIRM.report_text(CONFIRM.read_log(), scheduler.confirm_state)
        if cmd in ("desaccords", "désaccords"):
            from app import scheduler
            slopes = await asyncio.to_thread(trading.note_slopes)
            return trading.disagreements_text(trading.disagreements(scheduler.book, slopes))
        if cmd == "regle":
            return settings_text()
        if cmd == "labo":
            text = lab_status.text(await asyncio.to_thread(lab_status.status))
            return text + "\n" + lab_morning.status_line(lab_morning.shared_state(), PR.proposals)
        if cmd in ("lecons", "leçons"):
            return await asyncio.to_thread(lessons_text)
        if cmd == "grade":
            return await asyncio.to_thread(grade_text)
        if cmd in ("strategies", "stratégies", "strats"):
            return await asyncio.to_thread(strategies_text)
        if cmd == "autopsie":
            return await asyncio.to_thread(autopsie_text, args)
        if cmd in ("potentiel", "potentiel10", "plus10"):
            return await asyncio.to_thread(potentiel_text)
        if cmd == "kill":
            if args and args[0].upper().removesuffix("USD") in CRYPTO:
                return "🔪 Killmode ne traite que les actions US (la fiche société et le catalyseur en font partie)."
            return await killmode.command(args)
        if cmd == "propositions":
            pending = PR.proposals.pending()
            if not pending:
                return "Aucune proposition en attente."
            return "\n".join(PR.text(p) for p in pending) + "\n-# `!oui N` ou `!non N`"
        if cmd in ("oui", "non"):
            if not args or not args[0].isdigit():
                return f"Donne le numéro, ex. `!{cmd} 3`"
            return PR.decision_text(PR.proposals.decide(int(args[0]), cmd == "oui"))
    except (ValueError, KeyError) as exc:
        return f"⚠️ {exc}"
    except (trading.PlanError, scanner.ScanError) as exc:
        return f"⚠️ {exc}"
    except killmode.KillError as exc:
        return f"⚠️ {exc}"
    return None


async def run(lock: asyncio.Lock | None = None) -> None:
    """Connecte le bot (tâche de fond lancée au démarrage du serveur)."""
    import discord

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())
    allowed = owners()

    def buttons(pid: int):
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="Approuver", style=discord.ButtonStyle.success,
                                        custom_id=f"{PROPOSAL_PREFIX}{pid}:oui"))
        view.add_item(discord.ui.Button(label="Refuser", style=discord.ButtonStyle.danger,
                                        custom_id=f"{PROPOSAL_PREFIX}{pid}:non"))
        return view

    def lab_buttons(pids: list[int]):
        """Message du matin : un bouton Approuver et un bouton Refuser par règle (une ligne par règle)."""
        view = discord.ui.View(timeout=None)
        for row, pid in enumerate(pids[:5]):
            view.add_item(discord.ui.Button(label=f"Approuver {pid}", style=discord.ButtonStyle.success,
                                            custom_id=f"{PROPOSAL_PREFIX}{pid}:oui", row=row))
            view.add_item(discord.ui.Button(label=f"Refuser {pid}", style=discord.ButtonStyle.danger,
                                            custom_id=f"{PROPOSAL_PREFIX}{pid}:non", row=row))
        return view

    async def deliver_lab():
        """Message du matin du labo (fenêtre 8 h - 9 h, au plus un par jour) et annonces du worker."""
        await client.wait_until_ready()
        users: dict = {}

        async def fetch(oid):
            users[oid] = users.get(oid) or await client.fetch_user(oid)
            return users[oid]

        while True:
            try:
                await lab_morning.send_due(lab_morning.shared_state(), PR.proposals, datetime.now(timezone.utc),
                                           allowed, fetch, lab_buttons)
                while not lab_texts.empty():
                    text = lab_texts.get_nowait()
                    for oid in sorted(allowed):
                        try:
                            await (await fetch(oid)).send(text)
                        except Exception as exc:  # au plus une fois : l'annonce reste dans les journaux
                            log.warning("Annonce du labo non envoyée au compte %s : %s (%s)", oid, exc, text)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Envoi du labo en échec")
                users.clear()
            await asyncio.sleep(LAB_TICK)

    async def deliver_trades():
        """Alertes des trades suivis : copie en privé dès qu'elle arrive (au plus une fois)."""
        await client.wait_until_ready()
        users: dict = {}
        while True:
            text = await trade_texts.get()
            await send_private(text, allowed, client.fetch_user, users)

    async def deliver():
        await client.wait_until_ready()
        queue_pending()
        users: dict = {}
        attempts: dict[int, int] = {}
        delay = RETRY_FIRST
        while True:
            p = await outbox.get()
            try:
                done = await deliver_once(p, allowed, client.fetch_user, buttons, users)
            except asyncio.CancelledError:
                outbox.put_nowait(p)  # reconnexion : la proposition repart au prochain passage
                raise
            except Exception:
                log.exception("Envoi de la proposition %s en échec", p.get("id"))
                done = False
            if done:
                attempts.pop(p["id"], None)
                delay = RETRY_FIRST
                continue
            attempts[p["id"]] = attempts.get(p["id"], 0) + 1
            if attempts[p["id"]] >= MAX_ATTEMPTS:
                log.error("Proposition %s : envoi privé abandonné après %s essais (voir !propositions ou le cockpit)",
                          p["id"], MAX_ATTEMPTS)
                continue
            users.clear()  # un compte introuvable sera recherché à nouveau
            outbox.put_nowait(p)  # remise en file, nouvel essai après une attente croissante
            log.warning("Proposition %s : nouvel essai dans %.0f s", p["id"], delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RETRY_MAX)

    @client.event
    async def on_interaction(interaction):
        custom_id = (interaction.data or {}).get("custom_id", "")
        if not custom_id.startswith(PROPOSAL_PREFIX):
            return
        if interaction.user.id not in allowed:
            await interaction.response.send_message("Réservé au propriétaire.", ephemeral=True)
            return
        pid, choice = custom_id.removeprefix(PROPOSAL_PREFIX).split(":")
        current = PR.proposals.get(int(pid)) if pid.isdigit() else None
        if current is not None and current.get("kind") == "labo":
            await on_lab_choice(interaction, int(pid), choice == "oui")
            return
        try:
            p = PR.proposals.decide(int(pid), choice == "oui")
            await interaction.response.edit_message(content=f"{PR.text(p)}\n{PR.decision_text(p)}", view=None)
        except (KeyError, ValueError) as exc:
            await interaction.response.edit_message(content=f"⚠️ {exc}", view=None)

    async def on_lab_choice(interaction, pid: int, approve: bool):
        """Message groupé : la décision s'ajoute sous le texte, les boutons des autres règles restent."""
        content = (interaction.message.content if interaction.message else "") or ""
        try:
            p = PR.proposals.decide(pid, approve)
            line = PR.decision_text(p)
        except (KeyError, ValueError) as exc:
            p, line = PR.proposals.get(pid), f"⚠️ {exc}"
        group = (p or {}).get("groupe")
        digest = lab_morning.shared_state().day(group) if group else None
        others = [i for i in (digest or {}).get("propositions", [])
                  if (q := PR.proposals.get(i)) and q["status"] == "pending"]
        await interaction.response.edit_message(content=f"{content}\n{line}"[-2000:],
                                                view=lab_buttons(others) if others else None)

    @client.event
    async def on_message(message):
        if message.author.bot or message.author.id not in allowed:
            return
        if not message.content.startswith("!"):
            return
        async with message.channel.typing():
            try:
                answer = await handle(message.content.strip(), lock)
            except Exception:
                log.exception("Commande Discord en échec : %s", message.content)
                answer = "⚠️ Erreur inattendue, détail dans les journaux du serveur."
        if answer:
            for chunk in split_message(answer):
                await message.channel.send(chunk)

    sender = asyncio.create_task(deliver())
    sender.add_done_callback(
        lambda t: t.cancelled() or not t.exception() or log.error("Envoi des propositions arrêté", exc_info=t.exception())
    )
    lab_sender = asyncio.create_task(deliver_lab())
    lab_sender.add_done_callback(
        lambda t: t.cancelled() or not t.exception() or log.error("Envoi du labo arrêté", exc_info=t.exception())
    )
    trade_sender = asyncio.create_task(deliver_trades())
    trade_sender.add_done_callback(
        lambda t: t.cancelled() or not t.exception() or log.error("Envoi privé des alertes de trade arrêté",
                                                                  exc_info=t.exception())
    )
    try:
        await client.start(config.secret(TOKEN_NAME))
    finally:
        sender.cancel()
        lab_sender.cancel()
        trade_sender.cancel()
        if not client.is_closed():
            await client.close()
