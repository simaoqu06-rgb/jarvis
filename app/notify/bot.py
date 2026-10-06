"""Commandes Discord : demander un plan, suivre un trade ou lancer le scanner depuis le téléphone.

Le webhook ne sait qu'envoyer ; pour lire les messages il faut un bot (DISCORD_BOT_TOKEN) avec
l'intention « Message Content » activée. Leçon d'OpenClaw (bot ouvert à tous) : le bot ne répond
qu'aux comptes listés dans JARVIS_DISCORD_OWNER_ID et ignore tout le reste sans rien dire.

  !plan NVDA            plan complet (long, action ; « short », « crypto » ou « action » en plus)
  !prends NVDA          calcule le plan et suit le trade (TP/SL annoncés, cockpit)
  !scan                 scanner pré-ouverture tout de suite
  !bilan                bilan du forward-test sur le compte fictif
  !regle                réglages actuels ; « !regle rsi_max 70 » pour en changer un (« defaut » pour revenir)
  !aide                 cette liste
"""
import asyncio
import logging
import os

from app import config
from app.notify.discord import split_message
from app.trading import paper, scanner
from app.trading import service as trading
from app.trading.settings import DEFAULTS, settings

log = logging.getLogger(__name__)
TOKEN_NAME = "DISCORD_BOT_TOKEN"
CRYPTO = {"BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "AVAX", "LINK", "DOT", "LTC", "BNB", "TRX", "SUI", "PEPE",
          "SHIB", "XLM", "ATOM", "NEAR", "APT", "ARB", "OP", "INJ", "TIA", "SEI", "HBAR", "UNI", "AAVE", "XMR"}
HELP = __doc__.split("\n\n", 2)[2].strip()


def owners() -> set[int]:
    return {int(x) for x in os.getenv("JARVIS_DISCORD_OWNER_ID", "").replace(" ", "").split(",") if x.isdigit()}


def enabled() -> bool:
    return bool(config.secret(TOKEN_NAME)) and bool(owners())


def parse_trade(args: list[str]) -> dict:
    """« NVDA short », « eth », « SOL crypto long » → symbole, marché, sens."""
    if not args:
        raise ValueError("donne un ticker, ex. `!plan NVDA`")
    words = [a.lower() for a in args[1:]]
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
    return {"symbol": symbol, "market": market, "side": side}


def settings_text() -> str:
    current = settings.all()
    lines = ["**Réglages du trading** (`!regle cle valeur`, `!regle cle defaut`)"]
    for key, value in current.items():
        mark = " ✏️" if key in settings.overrides else ""
        lines.append(f"`{key}` = {value}{mark}")
    return "\n".join(lines)


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
            return trading.plan_text(await trading.build_plan(req["symbol"], req["market"], req["side"]))
        if cmd == "prends":
            req = parse_trade(args)
            plan = await trading.build_plan(req["symbol"], req["market"], req["side"])
            async with lock or asyncio.Lock():
                from app import scheduler  # import tardif : scheduler importe ce module
                scheduler.book.add(plan, "via Discord")
            return "📌 Trade suivi\n" + trading.plan_text(plan)
        if cmd == "scan":
            return scanner.text(await scanner.scan(save=False))
        if cmd == "bilan":
            from app import scheduler
            return paper.report_text(paper.stats(scheduler.paper_book))
        if cmd == "regle":
            if not args:
                return settings_text()
            if len(args) < 2:
                return f"`{args[0]}` = {settings.get(args[0])}" if args[0] in DEFAULTS else f"Réglage inconnu : `{args[0]}`"
            key, raw = args[0], " ".join(args[1:])
            if key not in DEFAULTS:
                return f"Réglage inconnu : `{key}`"
            if isinstance(DEFAULTS[key], dict):
                return f"`{key}` se change par la route PUT /api/trading/settings"
            if raw.lower() in ("defaut", "défaut", "default"):
                value = None
            elif isinstance(DEFAULTS[key], list):
                value = [x.strip().upper() for x in raw.split(",") if x.strip()]
            else:
                value = raw
            settings.update({key: value})
            return f"✏️ `{key}` = {settings.get(key)}"
    except (ValueError, KeyError) as exc:
        return f"⚠️ {exc}"
    except (trading.PlanError, scanner.ScanError) as exc:
        return f"⚠️ {exc}"
    return None


async def run(lock: asyncio.Lock | None = None) -> None:
    """Connecte le bot (tâche de fond lancée au démarrage du serveur)."""
    import discord

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())
    allowed = owners()

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

    try:
        await client.start(config.secret(TOKEN_NAME))
    finally:
        if not client.is_closed():
            await client.close()
