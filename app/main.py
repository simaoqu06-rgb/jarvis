"""Serveur JARVIS : expose chaque donnée au même format JSON.

Lancer : uvicorn app.main:app --reload
"""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import alerts, needs, scheduler
from app.notify import discord
from app.trading import paper, scanner
from app.trading import proposals as PR
from app.trading import service as trading
from app.trading.settings import settings


@asynccontextmanager
async def lifespan(_app: FastAPI):
    tasks = scheduler.start()
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(title="JARVIS", version="0.2.0", lifespan=lifespan)


def _list(value: str) -> list[str]:
    items = [v for v in value.split(",") if v.strip()]
    if not items:
        raise HTTPException(422, "Liste vide")
    return items


async def _city(city: str) -> tuple[float, float]:
    place = await needs.geocode_city(city)
    if not place.data:
        raise HTTPException(503, place.message)
    return place.data[0]["lat"], place.data[0]["lon"]


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/crypto/prices")
async def crypto_prices(symbols: str = "BTC,ETH"):
    return (await needs.crypto_prices(_list(symbols))).to_dict()


@app.get("/api/crypto/ranking")
async def crypto_ranking(limit: int = Query(20, ge=1, le=100)):
    return (await needs.crypto_ranking(limit)).to_dict()


@app.get("/api/fx")
async def fx(base: str = "EUR", symbols: str = "USD,GBP,CHF,JPY"):
    return (await needs.fx_rates(base, _list(symbols))).to_dict()


@app.get("/api/stocks/quotes")
async def stock_quotes(symbols: str = Query(..., description="ex. AAPL,MSFT (8 au maximum par minute en gratuit)")):
    return (await needs.stock_quotes(_list(symbols))).to_dict()


@app.get("/api/markets/us/status")
async def us_market_status():
    return await needs.us_market_status()


@app.get("/api/markets/us/holidays")
async def us_market_holidays(year: int | None = None):
    return (await needs.us_market_holidays(year)).to_dict()


@app.get("/api/holidays")
async def holidays(country: str = "FR", year: int | None = None):
    return (await needs.holidays(country, year)).to_dict()


@app.get("/api/calendar/economic")
async def economic_calendar():
    return (await needs.economic_calendar()).to_dict()


@app.get("/api/news/market")
async def market_news(limit: int = Query(20, ge=1, le=50)):
    return (await needs.market_news(limit)).to_dict()


@app.get("/api/news/company/{symbol}")
async def company_news(symbol: str, limit: int = Query(10, ge=1, le=50)):
    return (await needs.company_news(symbol, limit)).to_dict()


@app.get("/api/news/tech")
async def tech_news(q: str | None = None, limit: int = Query(20, ge=1, le=50)):
    return (await needs.tech_news(q, limit)).to_dict()


@app.get("/api/news/space")
async def space_news(q: str | None = None, limit: int = Query(20, ge=1, le=50)):
    return (await needs.space_news(q, limit)).to_dict()


@app.get("/api/weather")
async def weather(city: str | None = None, lat: float | None = None, lon: float | None = None):
    if lat is None or lon is None:
        if not city:
            raise HTTPException(422, "Donne une ville (city) ou des coordonnées (lat, lon)")
        lat, lon = await _city(city)
    return (await needs.weather(lat, lon)).to_dict()


@app.get("/api/air-quality")
async def air_quality(city: str | None = None, lat: float | None = None, lon: float | None = None):
    if lat is None or lon is None:
        if not city:
            raise HTTPException(422, "Donne une ville (city) ou des coordonnées (lat, lon)")
        lat, lon = await _city(city)
    return (await needs.air_quality(lat, lon)).to_dict()


@app.get("/api/earthquakes")
async def earthquakes(min_magnitude: float = Query(4.5, ge=0), hours: int = Query(24, ge=1, le=720)):
    return (await needs.earthquakes(min_magnitude, hours)).to_dict()


@app.get("/api/geocode")
async def geocode(city: str):
    return (await needs.geocode_city(city)).to_dict()


@app.get("/api/address/france")
async def address_france(q: str):
    return (await needs.address_france(q)).to_dict()


@app.get("/api/address/world")
async def address_world(q: str):
    return (await needs.address_world(q)).to_dict()


@app.get("/api/wikipedia")
async def wikipedia(topic: str):
    return (await needs.encyclopedia(topic)).to_dict()


@app.get("/api/books")
async def books(q: str):
    return (await needs.books(q)).to_dict()


async def _send(text: str) -> dict:
    try:
        return {"sent": await discord.send(text)}
    except discord.DiscordError as exc:
        raise HTTPException(503, f"Envoi Discord impossible : {exc}")


@app.post("/api/notify/test")
async def notify_test():
    """Envoie un message de test sur le salon Discord."""
    return await _send("✅ JARVIS est bien connecté à ce salon. Les alertes arriveront ici.")


@app.post("/api/notify/brief")
async def notify_brief():
    """Envoie tout de suite le point du matin sur Discord."""
    return await _send(await alerts.morning_brief(datetime.now(timezone.utc)))


@app.get("/api/notify/brief/preview")
async def brief_preview():
    """Montre le point du matin sans l'envoyer."""
    return {"text": await alerts.morning_brief(datetime.now(timezone.utc))}


# --- Trading : plans de trade et suivi (JARVIS propose, Simão passe les ordres) ---

class TradeRequest(BaseModel):
    symbol: str
    market: str = "crypto"          # crypto ou stock
    side: str = "long"              # long ou short
    entry: float | None = None      # vide = dernier cours
    stop: float | None = None       # vide = niveau d'invalidation (dernier creux de swing, 1 à 3 ATR)
    targets: list[float] | None = None  # vide = prochaines résistances (supports pour un short)
    capital: float | None = None    # vide = JARVIS_CAPITAL ; sert à la taille de position
    risk_pct: float | None = None   # vide = JARVIS_RISK_PCT (3 %)
    note: str | None = None


async def _plan(req: TradeRequest) -> dict:
    try:
        return await trading.build_plan(req.symbol, req.market, req.side, req.entry, req.stop, req.targets,
                                        capital=req.capital, risk_pct=req.risk_pct)
    except trading.PlanError as exc:
        raise HTTPException(422, str(exc))


@app.post("/api/trading/plan")
async def trading_plan(req: TradeRequest, discord_post: bool = False):
    """Calcule un plan (entrée, stop, TP1-3, délais estimés) sans l'enregistrer."""
    plan = await _plan(req)
    if discord_post:
        await _send(trading.plan_text(plan))
    return plan | {"text": trading.plan_text(plan)}


@app.post("/api/trading/trades")
async def trading_open(req: TradeRequest):
    """Calcule le plan, l'enregistre et le suit (TP/SL touchés annoncés sur Discord)."""
    trade = scheduler.book.add(await _plan(req), req.note)
    if scheduler.enabled():
        await _send("📌 Nouveau trade suivi\n" + trading.plan_text(trade))
    return trading.summary(trade)


@app.get("/api/trading/trades")
async def trading_list(status: str | None = Query(None, description="open ou closed")):
    trades = [t for t in scheduler.book.trades if status is None or t["status"] == status]
    return [trading.summary(t) for t in reversed(trades)]


@app.get("/api/trading/live")
async def trading_live():
    """Dernier prix et mini-graphe (2 h en 1 min) des trades ouverts ; le cockpit le lit toutes les 10 s."""
    return await trading.live(scheduler.book)


@app.post("/api/trading/trades/{trade_id}/close")
async def trading_close(trade_id: str, price: float | None = None):
    trade = scheduler.book.close(trade_id, price)
    if not trade:
        raise HTTPException(404, "Trade introuvable")
    return trading.summary(trade)


@app.delete("/api/trading/trades/{trade_id}")
async def trading_delete(trade_id: str):
    if not scheduler.book.remove(trade_id):
        raise HTTPException(404, "Trade introuvable")
    return {"deleted": trade_id}


@app.get("/api/trading/scan")
async def trading_scan(discord_post: bool = False):
    """Lance le scanner pré-ouverture maintenant (sans l'enregistrer ni passer d'ordre fictif)."""
    try:
        result = await scanner.scan(save=False)
    except scanner.ScanError as exc:
        raise HTTPException(503, str(exc))
    text = scanner.text(result)
    if discord_post:
        await _send(text)
    return result | {"text": text}


@app.get("/api/trading/settings")
async def trading_settings():
    """Seuils de la note composite, du veto, du scanner et du forward-test (✏️ = changé)."""
    return {"settings": settings.all(), "changed": sorted(settings.overrides)}


@app.put("/api/trading/settings")
async def trading_settings_update(changes: dict):
    """Change des réglages : {"rsi_max": 70} ; une valeur null remet le défaut."""
    try:
        settings.update(changes)
    except KeyError as exc:
        raise HTTPException(422, f"Réglage inconnu : {exc}")
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return await trading_settings()


@app.get("/api/trading/paper")
async def trading_paper():
    """Plans passés sur le compte fictif Alpaca et leur issue."""
    return list(reversed(scheduler.paper_book.entries))


@app.get("/api/trading/paper/stats")
async def trading_paper_stats():
    """Bilan agrégé par critère et réglages à envisager."""
    st = paper.stats(scheduler.paper_book)
    return st | {"text": paper.report_text(st)}


@app.post("/api/trading/paper")
async def trading_paper_submit(req: TradeRequest):
    """Passe un plan sur le compte fictif (en plus de ceux du scanner)."""
    entry = await paper.submit(scheduler.paper_book, await _plan(req), origin="manuel")
    if not entry:
        raise HTTPException(422, "Rien passé : action seulement, clés Alpaca, taille suffisante, pas déjà en cours")
    return entry


@app.get("/api/trading/proposals")
async def trading_proposals(status: str | None = Query(None, description="pending, approved, refused")):
    """Réglages proposés par JARVIS d'après le forward-test."""
    return [p for p in reversed(PR.proposals.items) if status is None or p["status"] == status]


@app.post("/api/trading/proposals/{pid}/{choice}")
async def trading_proposal_decide(pid: int, choice: str):
    """Approuver (approve) ou refuser (refuse) une proposition ; seule l'approbation change le réglage."""
    if choice not in ("approve", "refuse"):
        raise HTTPException(422, "choice doit valoir approve ou refuse")
    try:
        return PR.proposals.decide(pid, choice == "approve")
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))


COCKPIT_HTML = Path(__file__).parent / "web" / "cockpit.html"


# Icônes et manifeste : le cockpit s'installe comme une appli sur l'écran d'accueil du téléphone.
app.mount("/cockpit-static", StaticFiles(directory=COCKPIT_HTML.parent / "static"), name="cockpit-static")


@app.get("/cockpit", response_class=HTMLResponse, include_in_schema=False)
async def cockpit():
    """Le Trading Cockpit : trades suivis, TP/SL touchés, délais, journal (rafraîchi toutes les 30 s)."""
    return HTMLResponse(COCKPIT_HTML.read_text(encoding="utf-8"))
