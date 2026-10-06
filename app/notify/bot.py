"""Commandes Discord : demander un plan, suivre un trade ou lancer le scanner depuis le téléphone.

Le webhook ne sait qu'envoyer ; pour lire les messages il faut un bot (DISCORD_BOT_TOKEN) avec
l'intention « Message Content » activée. Leçon d'OpenClaw (bot ouvert à tous) : le bot ne répond
qu'aux comptes listés dans JARVIS_DISCORD_OWNER_ID et ignore tout le reste sans rien dire.

  !plan NVDA            plan complet (long, action ; « short », « crypto » ou « action » en plus)
  !prends NVDA          calcule le plan et suit le trade (TP/SL annoncés, cockpit)
  !scan                 scanner pré-ouverture tout de suite
  !bilan                bilan du forward-test sur le compte fictif
  !propositions         réglages que JARVIS propose et qui attendent ta réponse
  !oui 3 / !non 3       approuver ou refuser la proposition 3 (ou les boutons sous le message)
  !regle                réglages actuels
  !aide                 cette liste

Les propositions de réglages arrivent en message privé, avec des boutons Approuver / Refuser.
"""
import asyncio
import logging
import os

from app import config
from app.notify.discord import split_message
from app.trading import paper, scanner
from app.trading import proposals as PR
from app.trading import service as trading
from app.trading.settings import settings

log = logging.getLogger(__name__)
TOKEN_NAME = "DISCORD_BOT_TOKEN"
CRYPTO = {"BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "AVAX", "LINK", "DOT", "LTC", "BNB", "TRX", "SUI", "PEPE",
          "SHIB", "XLM", "ATOM", "NEAR", "APT", "ARB", "OP", "INJ", "TIA", "SEI", "HBAR", "UNI", "AAVE", "XMR"}
HELP = __doc__.split("\n\n")[2].strip()
PROPOSAL_PREFIX = "jarvis:prop:"
outbox: asyncio.Queue = asyncio.Queue()  # propositions à envoyer en privé avec leurs boutons


def offer(proposal: dict) -> None:
    """Envoie une proposition au propriétaire dès que le bot est connecté."""
    outbox.put_nowait(proposal)


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
    lines = ["**Réglages du trading** (✏️ = changé après une proposition approuvée)"]
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
            return settings_text()
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

    async def deliver():
        await client.wait_until_ready()
        owner = await client.fetch_user(next(iter(allowed)))
        while True:
            p = await outbox.get()
            try:
                await owner.send(f"🧪 {PR.text(p)}", view=buttons(p["id"]))
            except discord.HTTPException:
                log.exception("Proposition %s non envoyée en privé", p["id"])

    @client.event
    async def on_interaction(interaction):
        custom_id = (interaction.data or {}).get("custom_id", "")
        if not custom_id.startswith(PROPOSAL_PREFIX):
            return
        if interaction.user.id not in allowed:
            await interaction.response.send_message("Réservé au propriétaire.", ephemeral=True)
            return
        pid, choice = custom_id.removeprefix(PROPOSAL_PREFIX).split(":")
        try:
            p = PR.proposals.decide(int(pid), choice == "oui")
            await interaction.response.edit_message(content=f"{PR.text(p)}\n{PR.decision_text(p)}", view=None)
        except (KeyError, ValueError) as exc:
            await interaction.response.edit_message(content=f"⚠️ {exc}", view=None)

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
    try:
        await client.start(config.secret(TOKEN_NAME))
    finally:
        sender.cancel()
        if not client.is_closed():
            await client.close()
