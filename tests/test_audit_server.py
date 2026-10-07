"""Tests hors ligne des corrections de l'audit serveur : repli sur erreur inattendue, cours manquants
de Twelve Data, trade enregistré malgré Discord en panne, suivi sans webhook, point du matin sans
valeurs, file d'envoi pleine pendant un envoi, palier d'alerte à 0."""
import asyncio
import importlib
from datetime import datetime, timezone

import pytest

from app import alerts, config, needs, scheduler
from app.core import fallback
from app.core.cache import Store
from app.core.fallback import Source, resolve
from app.core.http import SourceError
from app.core.outbox import Outbox
from app.core.result import Result
from app.sources import finnhub, twelvedata

NOW = datetime(2026, 10, 7, 13, 0, tzinfo=timezone.utc)


def result(need, data, source="Test"):
    return Result(need, data, source, f"Source : {source}", "2026-10-07T12:59:00+00:00")


@pytest.fixture(autouse=True)
def fresh_store(tmp_path, monkeypatch):
    monkeypatch.setattr(fallback, "store", Store(str(tmp_path / "last.json")))


# --- 1. Repli : une erreur de parseur passe à la source suivante ---------------------------------

async def test_erreur_inattendue_passe_au_secours():
    async def cassee(**_):
        return {}["price"]  # KeyError dans le parseur

    async def ok(**_):
        return {"value": 1}

    res = await resolve("x", "audit1", [Source("A", cassee), Source("B", ok)], ttl=60)
    assert res.source == "B" and "KeyError" in res.errors[0]


async def test_finnhub_news_objet_d_erreur(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "k")

    async def get_json(url, params=None, headers=None):
        return {"error": "API limit reached"}

    monkeypatch.setattr(finnhub, "get_json", get_json)
    with pytest.raises(SourceError):
        await finnhub.market_news(5)
    with pytest.raises(SourceError):
        await finnhub.company_news("AAPL", 5)


async def test_check_news_une_action_en_echec_n_arrete_pas_les_autres(monkeypatch):
    monkeypatch.setattr(config, "WATCHLIST_STOCKS", ["AAPL", "MSFT"])

    async def market_news(limit):
        return result("market_news", [])

    async def company_news(symbol, limit):
        if symbol == "AAPL":
            raise RuntimeError("boum")
        return result("company_news", [{"title": "MSFT publie ses résultats", "url": "https://x/1"}])

    monkeypatch.setattr(needs, "market_news", market_news)
    monkeypatch.setattr(needs, "company_news", company_news)
    state = alerts.State(None)
    state.data["news"] = []  # pas le premier lancement
    messages = await alerts.check_news(state)
    assert len(messages) == 1 and "MSFT" in messages[0]


# --- 2. Twelve Data : cours vides complétés par Finnhub ------------------------------------------

async def test_cours_manquants_completes_par_finnhub(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "k")

    async def td(symbols):
        return [{"symbol": "AAPL", "price": 10.0}, {"symbol": "MSFT", "price": None, "error": "limite"}]

    async def fh(symbols):
        assert symbols == ["MSFT"]
        return [{"symbol": "MSFT", "price": 20.0}]

    monkeypatch.setattr(twelvedata, "stock_quotes", td)
    monkeypatch.setattr(finnhub, "stock_quotes", fh)
    quotes = await needs._twelvedata_quotes(["AAPL", "MSFT"])
    assert [q["price"] for q in quotes] == [10.0, 20.0] and quotes[1]["source"] == "Finnhub"


# --- 3. Trade enregistré même si Discord est en panne --------------------------------------------

async def test_trade_enregistre_malgre_discord_en_panne(monkeypatch):
    main = importlib.import_module("app.main")
    added, kicked = [], []

    class Book:
        def add(self, plan, note):
            return {"id": "t1", **plan}

    async def plan(req):
        return {"symbol": "AAPL"}

    monkeypatch.setattr(main, "_plan", plan)
    monkeypatch.setattr(scheduler, "book", Book())
    monkeypatch.setattr(scheduler, "enabled", lambda: True)
    monkeypatch.setattr(scheduler.outbox, "add", lambda text, kind="alerte", **kw: added.append(text))
    monkeypatch.setattr(scheduler, "_kick_outbox", lambda: kicked.append(1))
    monkeypatch.setattr(main.trading, "against_jarvis", lambda t: False)
    monkeypatch.setattr(main.trading, "plan_text", lambda t: "plan")
    monkeypatch.setattr(main.trading, "summary", lambda t: {"id": t["id"]})
    out = await main.trading_open(main.TradeRequest(symbol="AAPL"))
    assert out == {"id": "t1"} and added and kicked  # mis en file, jamais d'erreur 503


# --- 4. Sans webhook, le suivi des trades tourne quand même --------------------------------------

def test_suivi_lance_sans_notifications(monkeypatch):
    monkeypatch.setattr(scheduler, "enabled", lambda: False)
    monkeypatch.setattr(scheduler.bot, "enabled", lambda: False)
    started = []

    async def fake_loop(name, job, every, locked=True):
        started.append(job)

    monkeypatch.setattr(scheduler, "_loop", fake_loop)

    async def run():
        tasks = scheduler.start()
        await asyncio.gather(*tasks)

    asyncio.run(run())
    assert {scheduler.tick_trades, scheduler.tick_paper, scheduler.tick_scan} <= set(started)
    assert scheduler.tick_news not in started


def test_rien_en_file_sans_notifications(monkeypatch):
    monkeypatch.setattr(scheduler, "enabled", lambda: False)
    box = Outbox(None)
    monkeypatch.setattr(scheduler, "outbox", box)
    scheduler._queue(["TP1 touché"], "trade")
    scheduler._kick_outbox()  # sans boucle ni webhook : ne fait rien
    assert box.items == []


# --- 5. Point du matin : météo ou air sans valeur -------------------------------------------------

async def test_point_du_matin_air_sans_indice(monkeypatch):
    monkeypatch.setattr(config, "WATCHLIST_CRYPTO", [])
    monkeypatch.setattr(config, "WATCHLIST_STOCKS", [])
    monkeypatch.setattr(config, "HOME_CITY", "Paris")

    async def status(now):
        return {"is_open": False, "reason": "week-end"}

    async def simple(*a, **k):
        return result("x", None)

    async def geocode(city):
        return result("geocode", [{"lat": 48.85, "lon": 2.35}])

    async def weather(lat, lon):
        return result("weather", {"condition": "nuageux", "temperature_c": None, "wind_speed_kmh": None})

    async def air(lat, lon):
        return result("air", {"aqi": None, "pm2_5": None})

    monkeypatch.setattr(needs, "us_market_status", status)
    monkeypatch.setattr(needs, "economic_calendar", simple)
    monkeypatch.setattr(needs, "fx_rates", simple)
    monkeypatch.setattr(needs, "geocode_city", geocode)
    monkeypatch.setattr(needs, "weather", weather)
    monkeypatch.setattr(needs, "air_quality", air)
    text = await alerts.morning_brief(NOW)
    assert "indice européen n/d" in text and "n/d °C" in text


# --- 6. File d'envoi : messages abandonnés pendant un envoi en cours -----------------------------

async def test_file_pleine_pendant_un_envoi(monkeypatch):
    from app.core import outbox as outbox_mod

    monkeypatch.setattr(outbox_mod, "MAX_ITEMS", 2)
    box = Outbox(None)
    box.add(["a", "b"])
    items = box.items

    async def send(text):
        box.add(["c", "d"])  # la file déborde pendant l'envoi : « a » et « b » en sortent

    done = await box.flush(send)
    assert box.items is items and done >= 1  # liste coupée sur place, pas de ValueError


# --- 7. Palier d'alerte à 0 ----------------------------------------------------------------------

async def test_palier_a_zero_sans_division_par_zero(monkeypatch):
    monkeypatch.setattr(config, "MOVE_ALERT_PCT", 0.0)
    monkeypatch.setattr(config, "WATCHLIST_CRYPTO", ["BTC"])
    monkeypatch.setattr(config, "WATCHLIST_STOCKS", [])

    async def ranking(limit):
        return result("crypto_ranking", [{"symbol": "BTC", "price_usd": 60000.0, "change_24h_pct": 6.0}])

    monkeypatch.setattr(needs, "crypto_ranking", ranking)
    messages = await alerts.check_price_moves(alerts.State(None), NOW)
    assert len(messages) == 1  # palier par défaut (5 %)
