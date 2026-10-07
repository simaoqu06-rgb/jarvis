"""Serveur JARVIS : expose chaque donnée au même format JSON.

Lancer : uvicorn app.main:app --reload
"""
import asyncio
import hmac
import ipaddress
import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import alerts, config, needs, scheduler
from app.lab import brief as lab_brief
from app.lab import catalysts as lab_catalysts
from app.lab import db as lab_db
from app.lab import grade as lab_grade
from app.lab import lessons as lab_lessons
from app.lab import method as lab_method
from app.lab import morning as lab_morning
from app.lab import page as lab_page
from app.lab import stats as lab_stats
from app.lab import strat_stats as lab_strategies
from app.lab import status as lab_status
from app.lab import summary as lab_summary
from app.lab import validator as lab_validator
from app.notify import discord
from app.trading import paper, scanner
from app.trading import proposals as PR
from app.trading import service as trading
from app.core import fallback
from app.core.http import SourceError
from app.trading.settings import settings


def configure_logging() -> None:
    """Journaux INFO visibles dans `docker compose logs`, avec l'heure.

    httpx et httpcore restent en WARNING : leurs lignes INFO contiennent les URL, donc des clés d'API.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s : %(message)s")
    logging.getLogger().setLevel(logging.INFO)  # même si un autre outil a déjà posé un handler
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


configure_logging()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    configure_logging()
    tasks = scheduler.start()
    yield
    for task in tasks:
        task.cancel()
    fallback.store.flush()  # dernières valeurs pas encore écrites sur disque


app = FastAPI(title="JARVIS", version="0.2.0", lifespan=lifespan)
STATE_CHANGING = {"POST", "PUT", "PATCH", "DELETE"}


def cross_origin(request: Request) -> bool:
    """Requête venue d'un autre site (CSRF) : un site visité pendant que le téléphone est sur Tailscale
    ne doit pas pouvoir approuver une proposition ou clôturer un trade à la place de Simão.

    Sec-Fetch-Site (posé par le navigateur, impossible à falsifier depuis une page) : cross-site refusé,
    same-origin accepté même si un proxy (tailscale serve) a réécrit l'en-tête Host. Sinon, l'Origin
    (ou à défaut le Referer) doit correspondre à Host, X-Forwarded-Host ou JARVIS_ALLOWED_ORIGINS.
    Sans aucun de ces en-têtes (curl, appel serveur), la requête est acceptée.
    """
    fetch_site = request.headers.get("sec-fetch-site", "").lower()
    if fetch_site == "cross-site":
        return True
    if fetch_site in ("same-origin", "none"):
        return False
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return False
    netloc = urlsplit(origin).netloc.lower()
    allowed = {request.headers.get("host", "").lower(), request.headers.get("x-forwarded-host", "").lower()}
    allowed |= {urlsplit(o.strip()).netloc.lower() or o.strip().lower()
                for o in os.getenv("JARVIS_ALLOWED_ORIGINS", "").split(",") if o.strip()}
    return not netloc or netloc not in allowed - {""}


@app.middleware("http")
async def reject_cross_site(request: Request, call_next):
    if request.method in STATE_CHANGING and cross_origin(request):
        return JSONResponse({"detail": "Requête d'un autre site refusée"}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def internet_only_lab(request: Request, call_next):
    """Défense en profondeur derrière Tailscale Funnel (qui ne publie que /api/lab) : une requête venue
    d'Internet n'atteint rien d'autre que /api/lab/… (ni le cockpit, ni les propositions, ni /docs)."""
    path = request.url.path
    if not (path == "/api/lab" or path.startswith("/api/lab/")) and public_request(request):
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    return await call_next(request)


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
    risk_pct: float | None = None   # vide = risque proposé par le grade de JARVIS
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
    plan = await _plan(req)  # calculé hors du verrou
    async with scheduler._lock:
        trade = scheduler.book.add(plan, req.note)
    if scheduler.enabled():
        # Par la file d'envoi persistante : le trade est déjà enregistré, une panne Discord ne doit pas
        # renvoyer une erreur (le client réessaierait et doublerait le trade).
        head = trading.disagreement_text(trade) + "\n" if trading.against_jarvis(trade) else ""
        scheduler.outbox.add(head + "📌 Nouveau trade suivi\n" + trading.plan_text(trade), "trade")
        scheduler._kick_outbox()
    return trading.summary(trade)


@app.get("/api/trading/trades")
async def trading_list(status: str | None = Query(None, description="open ou closed")):
    trades = [t for t in scheduler.book.trades if status is None or t["status"] == status]
    return [trading.summary(t) for t in reversed(trades)]


@app.get("/api/trading/disagreements")
async def trading_disagreements():
    """Trades pris avec et contre l'avis de JARVIS (plans NO GO) : qui avait raison."""
    return trading.disagreements(scheduler.book)


@app.get("/api/trading/live")
async def trading_live():
    """Dernier prix et mini-graphe (2 h en 1 min) des trades ouverts ; le cockpit le lit toutes les 10 s."""
    return await trading.live(scheduler.book)


@app.post("/api/trading/trades/{trade_id}/close")
async def trading_close(trade_id: str, price: float | None = None):
    async with scheduler._lock:
        trade = scheduler.book.close(trade_id, price)
    if not trade:
        raise HTTPException(404, "Trade introuvable")
    return trading.summary(trade)


@app.delete("/api/trading/trades/{trade_id}")
async def trading_delete(trade_id: str):
    async with scheduler._lock:
        removed = scheduler.book.remove(trade_id)
    if not removed:
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
        async with scheduler._lock:
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
    plan = await _plan(req)  # calculé hors du verrou
    try:
        async with scheduler._lock:  # pas deux positions fictives sur la même action
            entry = await paper.submit(scheduler.paper_book, plan, origin="manuel")
    except SourceError as exc:
        raise HTTPException(503, f"Compte fictif Alpaca : {exc}")
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
        async with scheduler._lock:
            return PR.proposals.decide(pid, choice == "approve")
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))


# --- Labo : accès en lecture pour la routine du soir (clé JARVIS_LAB_API_KEY) ---------------
# Lecture seule sur data/lab.db (le worker jarvis-lab est seul à écrire) ; une règle soumise est
# déposée dans une file de fichiers que le worker lit. Rien ici ne change la méthode en direct.
# Labo 3 : tout ce que la routine du soir utilise est sous /api/lab/ (seul chemin ouvert par Tailscale
# Funnel, sur le port 443 ; le cockpit est sur 4443, privé) et demande la clé ; /api/lab/status aussi dès que la requête vient d'Internet.
LAB_KEY_NAME = "JARVIS_LAB_API_KEY"
PRIVATE_NETS = [ipaddress.ip_network(n) for n in ("127.0.0.0/8", "::1/128", "100.64.0.0/10", "fd7a:115c:a1e0::/48",
                                                   "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]


FUNNEL_PORTS = {"8443", "10000"}


def public_request(request: Request) -> bool:
    """Requête venue d'Internet par Tailscale Funnel : en-tête Tailscale-Funnel-Request, adresse
    demandée sur un port de Funnel (8443, 10000), ou adresse d'origine (X-Forwarded-For posé par
    tailscale serve/funnel) hors du réseau Tailscale et du serveur. Prudence plutôt qu'exactitude."""
    if request.headers.get("tailscale-funnel-request"):
        return True
    for host in (request.headers.get("host", ""), request.headers.get("x-forwarded-host", "")):
        if host.rsplit(":", 1)[-1] in FUNNEL_PORTS and ":" in host:
            return True  # anciens ports de Funnel ; sur 443, l'en-tête Tailscale-Funnel-Request suffit
    forwarded = request.headers.get("x-forwarded-for", "")
    for part in [x.strip() for x in forwarded.split(",") if x.strip()]:
        try:
            ip = ipaddress.ip_address(part)
        except ValueError:
            return True
        if not any(ip in net for net in PRIVATE_NETS):
            return True
    return False


@app.get("/api/lab/status")
def lab_state(request: Request):
    """Labo de performance : avancement du rejeu, trades simulés par marché et origine, taille de la base.
    Lecture seule (la base est écrite par le service jarvis-lab). Sans clé depuis le serveur et le
    réseau Tailscale ; avec la clé depuis Internet (Funnel)."""
    if public_request(request):
        lab_key(request)
    st = lab_status.status()
    return st | {"text": lab_status.text(st)}


def lab_key(request: Request) -> None:
    """Clé dans l'en-tête X-Lab-Key (ou Authorization: Bearer …), comparée en temps constant.
    Sans clé configurée, ces routes sont désactivées (503)."""
    expected = config.secret(LAB_KEY_NAME)
    if not expected:
        raise HTTPException(503, "Accès au labo désactivé : JARVIS_LAB_API_KEY absente")
    given = request.headers.get("x-lab-key", "")
    auth = request.headers.get("authorization", "")
    if not given and auth.lower().startswith("bearer "):
        given = auth[7:].strip()
    if not hmac.compare_digest(given.encode(), expected.encode()):
        raise HTTPException(401, "Clé du labo invalide")


@app.exception_handler(sqlite3.Error)
async def lab_db_error(_request: Request, exc: sqlite3.Error):
    """Base du labo illisible ou d'un ancien schéma (worker pas encore redémarré) : 503, pas 500."""
    logging.getLogger("app.main").warning("Base du labo illisible : %s", exc)
    return JSONResponse({"detail": "Base du labo illisible (worker pas encore redémarré ?)"}, status_code=503)


def _lab_conn():
    conn = lab_db.open_readonly()
    if conn is None:
        raise HTTPException(503, "Base du labo absente (le service jarvis-lab n'a pas encore démarré)")
    return conn


def _day(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise HTTPException(422, f"{name} : date AAAA-MM-JJ attendue")


@app.get("/api/lab/summary", dependencies=[Depends(lab_key)])
def lab_summary_get(day: str | None = None):
    """Dernier résumé du soir (ou celui d'un jour) : JSON compact et texte."""
    conn = _lab_conn()
    try:
        out = lab_summary.latest(conn, _day(day, "day"))
    finally:
        conn.close()
    if not out:
        raise HTTPException(404, "Aucun résumé pour l'instant")
    return out


@app.get("/api/lab/stats", dependencies=[Depends(lab_key)])
def lab_stats_get(market: str | None = Query(None, pattern="^(stock|crypto)$"),
                  origin: str | None = Query(None, pattern="^(replay|live)$"),
                  version: str | None = Query(None, max_length=40), since: str | None = None,
                  until: str | None = None):
    """Statistiques : sans filtre, celles du cache (version en cours, calculées par le worker) ;
    avec des filtres, calculées à la demande."""
    filters = {"market": market, "origin": origin, "version": version,
               "since": _day(since, "since"), "until": _day(until, "until")}
    conn = _lab_conn()
    try:
        if not any(filters.values()):
            cached = lab_stats.cached(conn)
            if cached:
                return cached
            filters["version"] = lab_validator.current_version(conn)
        return lab_stats.compute(conn, **{k: v for k, v in filters.items() if v})
    finally:
        conn.close()


@app.get("/api/lab/catalysts", dependencies=[Depends(lab_key)])
def lab_catalysts_get(day: str | None = None, symbol: str | None = Query(None, max_length=20),
                      market: str | None = Query(None, pattern="^(stock|crypto)$"),
                      kind: str | None = Query(None, pattern="^(mouvement|contre_exemple)$"),
                      since: str | None = None, until: str | None = None, a_classer: bool = False,
                      limit: int = Query(100, ge=1, le=500)):
    """Fiches catalyseurs (mouvements de ±8 % et contre-exemples) avec leurs actus."""
    conn = _lab_conn()
    try:
        # a_classer : fiches classables seulement (actus trouvées), comme le brief.
        return lab_catalysts.query(conn, _day(day, "day"), symbol.upper() if symbol else None, market, kind,
                                   _day(since, "since"), _day(until, "until"), a_classer, limit,
                                   news_status="ok" if a_classer else None)
    finally:
        conn.close()


@app.get("/api/lab/rules", dependencies=[Depends(lab_key)])
def lab_rules_get(status: str | None = Query(None, pattern="^(en_attente|validee|rejetee|invalide)$"),
                  limit: int = Query(200, ge=1, le=500)):
    """Règles soumises, leur verdict et leurs mesures ; compteur d'essais."""
    conn = _lab_conn()
    try:
        return {"compteurs": lab_validator.counts(conn), "seuils": lab_validator.config(conn),
                "regles": lab_validator.list_rules(conn, status, limit)}
    finally:
        conn.close()


LAB_RULE_MAX_BYTES = 16_384


async def _lab_rule_body(request: Request, expect: type = dict):
    """Corps JSON d'une règle, lu APRÈS la vérification de la clé (un paramètre Body serait lu par
    FastAPI avant les dépendances, donc sans clé) et plafonné à LAB_RULE_MAX_BYTES."""
    declared = request.headers.get("content-length")
    if declared and (not declared.isdigit() or int(declared) > LAB_RULE_MAX_BYTES):
        raise HTTPException(413, f"Règle trop longue (au plus {LAB_RULE_MAX_BYTES} octets)")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > LAB_RULE_MAX_BYTES:
            raise HTTPException(413, f"Règle trop longue (au plus {LAB_RULE_MAX_BYTES} octets)")
        chunks.append(chunk)
    try:
        rule = json.loads(b"".join(chunks) or b"null")
    except (ValueError, RecursionError):
        raise HTTPException(422, "JSON illisible")
    if not isinstance(rule, expect):
        raise HTTPException(422, "Objet JSON attendu" if expect is dict else "Liste JSON attendue")
    return rule


@app.post("/api/lab/rules", dependencies=[Depends(lab_key)])
async def lab_rules_submit(request: Request):
    """Soumet une règle de filtre à tester : elle est seulement mise en file, le worker la valide.
    Une règle déjà testée renvoie son verdict gardé. Ne change jamais la méthode en direct.
    Champs facultatifs « pourquoi » (≤ 1 000 caractères) et « council » (≤ 2 000) gardés avec la règle ;
    au plus 3 nouvelles règles par soirée (heure de Paris, de midi à midi), au-delà : 429."""
    rule = await _lab_rule_body(request)
    return await asyncio.to_thread(_lab_rule_enqueue, rule)


def _lab_rule_enqueue(rule: dict, now: datetime | None = None):
    now = now or datetime.now(timezone.utc)
    for key, limit in lab_validator.NOTE_KEYS.items():
        value = rule.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > limit):
            raise HTTPException(422, f"« {key} » : texte de {limit} caractères au plus")
    try:
        canon = lab_validator.parse(rule)
    except lab_validator.RuleError as exc:
        raise HTTPException(422, str(exc))
    h = lab_validator.rule_hash(canon)
    conn = lab_db.open_readonly()
    if conn is not None:
        try:
            known = lab_validator.get_rule(conn, h)
        finally:
            conn.close()
        if known:
            return known | {"deja_connue": True}
    db_file = lab_db.db_path()
    try:
        left = lab_brief.take_quota(db_file, h, now)
    except lab_brief.QuotaFull as exc:
        raise HTTPException(429, str(exc))
    try:
        out = lab_validator.enqueue(db_file, rule, lab_brief.soiree(now))
    except lab_validator.RuleError as exc:
        lab_brief.release_quota(db_file, h, now)
        raise HTTPException(429, str(exc))
    return out | {"regles_restantes_ce_soir": left}


LAB_TYPES_MAX = 200


@app.post("/api/lab/catalysts/types", dependencies=[Depends(lab_key)])
async def lab_catalyst_types(request: Request):
    """Classe des fiches catalyseurs : [{"id": 12, "type": "resultats"}, …] (types : GET /api/lab/brief,
    « types_permis » ; null remet « à classer »). Mis en file, enregistré par le worker."""
    items = await _lab_rule_body(request, expect=list)
    if not items or len(items) > LAB_TYPES_MAX:
        raise HTTPException(422, f"Liste de 1 à {LAB_TYPES_MAX} fiches attendue")
    clean = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), int) or isinstance(item.get("id"), bool):
            raise HTTPException(422, "Chaque élément : {\"id\": entier, \"type\": …}")
        kind = item.get("type")
        if kind is not None and kind not in lab_catalysts.TYPES:
            raise HTTPException(422, f"type inconnu : {kind} (permis : {', '.join(lab_catalysts.TYPES)})")
        clean.append({"id": item["id"], "type": kind})
    await asyncio.to_thread(lab_method.enqueue_types, lab_db.db_path(), clean)
    return {"en_file": len(clean)}


@app.get("/api/lab/lessons", dependencies=[Depends(lab_key)])
def lab_lessons_get(status: str | None = Query(None, pattern="^(a_confirmer|confirmee|dementie|invalide|retiree)$"),
                    limit: int = Query(200, ge=1, le=500)):
    """Carnet des leçons : compteurs et leçons avec leurs mesures (confirmées d'abord)."""
    conn = _lab_conn()
    try:
        return {"compteurs": lab_lessons.counts(conn), "lecons": lab_lessons.list_lessons(conn, status, limit)}
    finally:
        conn.close()


@app.post("/api/lab/lessons", dependencies=[Depends(lab_key)])
async def lab_lessons_submit(request: Request):
    """Dépose une leçon (ou un retrait : {"retirer": 12, "raison": …}) ; mise en file, mesurée par le
    worker. Une leçon ne filtre aucun trade : confirmée, elle ajuste la note des plans en direct."""
    raw = await _lab_rule_body(request)
    try:
        return await asyncio.to_thread(lab_lessons.enqueue, lab_db.db_path(), raw,
                                       lab_brief.soiree(datetime.now(timezone.utc)))
    except lab_lessons.LessonError as exc:
        raise HTTPException(429 if "file pleine" in str(exc) else 422, str(exc))


@app.get("/api/lab/grade", dependencies=[Depends(lab_key)])
def lab_grade_get(limit: int = Query(20, ge=1, le=200)):
    """Grade de JARVIS par marché (officiel, mesuré, critères, risque permis, hausse en attente) et
    historique des changements."""
    conn = _lab_conn()
    try:
        return lab_grade.payload(conn) | {"historique": lab_grade.history(conn, limit=limit)}
    finally:
        conn.close()


@app.get("/api/lab/strategies", dependencies=[Depends(lab_key)])
def lab_strategies_get():
    """Labo v2 : tableau des stratégies à déclencheur (v1 en référence), sorties, robustesse, écart au
    témoin aléatoire, et diagnostic de v1. Hors bloc scellé. Cache du worker, sinon calcul direct."""
    conn = _lab_conn()
    try:
        return lab_strategies.cached(conn) or lab_strategies.report(conn)
    finally:
        conn.close()


@app.post("/api/lab/grade/avis", dependencies=[Depends(lab_key)])
async def lab_grade_avis(request: Request):
    """Avis du council du soir sur le grade : {"marche", "ajustement": -1|0|1, "motifs", "voix"}.
    Mis en file, appliqué par le worker : une baisse tout de suite, une hausse de lettre seulement avec
    l'accord de Simão ; un avis par marché et par soirée."""
    raw = await _lab_rule_body(request)
    try:
        return await asyncio.to_thread(lab_grade.enqueue_avis, lab_db.db_path(), raw,
                                       lab_brief.soiree(datetime.now(timezone.utc)))
    except lab_grade.AvisError as exc:
        raise HTTPException(422, str(exc))


@app.get("/api/lab/brief", dependencies=[Depends(lab_key)])
def lab_brief_get():
    """Tout ce qu'il faut à la routine du soir en un appel (< ~20 Ko)."""
    conn = _lab_conn()
    try:
        return lab_brief.build(conn, datetime.now(timezone.utc), lab_db.db_path(),
                               lab_morning.shared_state().latest(), lab_morning.dm_on())
    finally:
        conn.close()


@app.get("/api/lab/matin", dependencies=[Depends(lab_key)])
def lab_morning_get(limit: int = Query(7, ge=1, le=60)):
    """Derniers messages du matin du labo (envoyés ou gardés pendant le rodage) et propositions du labo."""
    st = lab_morning.shared_state()
    days = sorted(st.data["jours"].values(), key=lambda d: d["jour"], reverse=True)[:limit]
    return {"message_prive": "activé" if lab_morning.dm_on() else "rodage", "jours": days,
            "propositions": [p for p in reversed(PR.proposals.items) if p.get("kind") == "labo"][:30]}


COCKPIT_HTML = Path(__file__).parent / "web" / "cockpit.html"


# Icônes et manifeste : le cockpit s'installe comme une appli sur l'écran d'accueil du téléphone.
app.mount("/cockpit-static", StaticFiles(directory=COCKPIT_HTML.parent / "static"), name="cockpit-static")


@app.get("/cockpit/labo", response_class=HTMLResponse, include_in_schema=False)
def cockpit_lab(request: Request):
    """Page Labo du cockpit (lecture seule, rendue côté serveur)."""
    if public_request(request):
        raise HTTPException(404)
    st = lab_status.status()
    rules: list = []
    conn = lab_db.open_readonly()
    if conn is not None:
        try:
            rules = lab_validator.list_rules(conn, limit=20)
        except sqlite3.Error:
            rules = []
        finally:
            conn.close()
    proposals = [p for p in reversed(PR.proposals.items) if p.get("kind") in ("labo", "grade")]
    return HTMLResponse(lab_page.render(st, proposals, rules, lab_morning.shared_state().latest(), lab_morning.dm_on(),
                                        lab_grade.current()))


@app.get("/cockpit", response_class=HTMLResponse, include_in_schema=False)
async def cockpit():
    """Le Trading Cockpit : trades suivis, TP/SL touchés, délais, journal (rafraîchi toutes les 30 s)."""
    return HTMLResponse(COCKPIT_HTML.read_text(encoding="utf-8"))
