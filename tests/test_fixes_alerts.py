"""Tests hors ligne des corrections : doublons de séismes et d'actus entre sources, alertes crypto
sur 24 h glissantes, découpage Discord, propositions en privé, cours par symbole."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import alerts, config, needs
from app.core.http import SourceError
from app.core.result import Result
from app.notify import bot, discord
from app.sources import finnhub, twelvedata

NOW = datetime(2026, 10, 5, 21, 30, tzinfo=timezone.utc)  # 23 h 30 à Paris
URL = "https://discord.com/api/webhooks/1/test"


def result(need, data, source="Test"):
    return Result(need, data, source, f"Source : {source}", "2026-10-05T13:59:00+00:00")


@pytest.fixture
def state():
    return alerts.State(None)


@pytest.fixture(autouse=True)
def watchlists(monkeypatch):
    monkeypatch.setattr(config, "WATCHLIST_CRYPTO", ["BTC"])
    monkeypatch.setattr(config, "WATCHLIST_STOCKS", [])
    monkeypatch.setattr(config, "MOVE_ALERT_PCT", 5.0)


# --- 1. Séismes ---------------------------------------------------------------------------------

USGS_QUAKE = {"id": "us7000abcd", "time": "2026-10-05T10:00:03+00:00", "magnitude": 6.4, "place": "Honshu, Japon",
              "lat": 38.30, "lon": 142.40, "depth_km": 24.0, "tsunami": False, "url": "https://usgs.example/a"}
EMSC_QUAKE = {"id": "20261005_0000123", "time": "2026-10-05T10:00:05.1Z", "magnitude": 6.3,
              "place": "NEAR EAST COAST OF HONSHU", "lat": 38.45, "lon": 142.55, "depth_km": None,
              "tsunami": None, "url": None}


def fake_quakes(monkeypatch, box):
    async def earthquakes(min_magnitude, hours):
        return result("earthquakes", list(box["data"]), box["source"])
    monkeypatch.setattr(needs, "earthquakes", earthquakes)


async def test_seisme_pas_de_doublon_usgs_puis_emsc(monkeypatch, state):
    box = {"data": [], "source": "USGS"}
    fake_quakes(monkeypatch, box)
    assert await alerts.check_earthquakes(state) == []  # premier lancement
    box["data"] = [USGS_QUAKE]
    assert len(await alerts.check_earthquakes(state)) == 1
    box.update(data=[EMSC_QUAKE], source="EMSC")  # l'USGS tombe : même séisme, autre identifiant
    assert await alerts.check_earthquakes(state) == []
    box.update(data=[USGS_QUAKE], source="USGS")
    assert await alerts.check_earthquakes(state) == []


async def test_seisme_emsc_sans_profondeur_est_annonce(monkeypatch, state):
    box = {"data": [], "source": "EMSC"}
    fake_quakes(monkeypatch, box)
    await alerts.check_earthquakes(state)
    box["data"] = [EMSC_QUAKE]
    messages = await alerts.check_earthquakes(state)
    assert len(messages) == 1 and "profondeur n/d" in messages[0] and "M6.3" in messages[0]
    assert EMSC_QUAKE["id"] in state.seen("quakes")


async def test_seisme_retenu_seulement_apres_le_message(monkeypatch, state):
    box = {"data": [], "source": "USGS"}
    fake_quakes(monkeypatch, box)
    await alerts.check_earthquakes(state)
    box["data"] = [USGS_QUAKE]

    def boom(q, r):
        raise RuntimeError("format")

    monkeypatch.setattr(alerts, "_quake_message", boom)
    assert await alerts.check_earthquakes(state) == []
    assert USGS_QUAKE["id"] not in state.seen("quakes")  # pas perdu : réessayé au prochain passage
    monkeypatch.undo()
    fake_quakes(monkeypatch, box)
    monkeypatch.setattr(config, "QUAKE_MIN_MAGNITUDE", 6.0)
    assert len(await alerts.check_earthquakes(state)) == 1


async def test_seismes_distincts_restent_distincts(monkeypatch, state):
    box = {"data": [], "source": "USGS"}
    fake_quakes(monkeypatch, box)
    await alerts.check_earthquakes(state)
    far = dict(USGS_QUAKE, id="us2", lat=-33.0, lon=-71.0, place="Chili")
    box["data"] = [far, USGS_QUAKE]
    assert len(await alerts.check_earthquakes(state)) == 2


# --- 2. Variations crypto sur 24 h --------------------------------------------------------------

def fake_ranking(monkeypatch, change):
    async def ranking(limit=20):
        return result("crypto_ranking", [{"rank": 1, "symbol": "BTC", "name": "Bitcoin", "price_usd": 80000.0,
                                          "change_24h_pct": change, "market_cap_usd": 1.0, "volume_24h_usd": 1.0}])
    monkeypatch.setattr(needs, "crypto_ranking", ranking)


async def test_crypto_pas_de_realerte_a_minuit_paris(monkeypatch, state):
    fake_ranking(monkeypatch, -6.0)
    first = await alerts.check_price_moves(state, NOW)
    assert len(first) == 1 and "sur 24 h" in first[0] and "journée" not in first[0]
    # Minuit passé à Paris, même variation sur 24 h glissantes : pas de nouvelle alerte.
    assert await alerts.check_price_moves(state, NOW + timedelta(hours=1)) == []
    fake_ranking(monkeypatch, -11.0)  # la baisse s'aggrave d'un palier : nouvelle alerte
    assert len(await alerts.check_price_moves(state, NOW + timedelta(hours=2))) == 1
    fake_ranking(monkeypatch, 5.5)  # autre sens : alerte séparée
    assert len(await alerts.check_price_moves(state, NOW + timedelta(hours=3))) == 1
    fake_ranking(monkeypatch, -6.0)  # plus de 24 h après la dernière alerte baissière
    assert len(await alerts.check_price_moves(state, NOW + timedelta(hours=27))) == 1


async def test_crypto_ancien_format_d_etat_ignore(monkeypatch, state):
    state.data["moves"] = {"2026-10-05:BTC:-": 1}
    fake_ranking(monkeypatch, -6.0)
    assert len(await alerts.check_price_moves(state, NOW)) == 1


# --- 3. Actus -----------------------------------------------------------------------------------

def fake_news(monkeypatch, box):
    async def market_news(limit=20):
        return result("market_news", None if box["items"] is None else list(box["items"]), box["source"])
    monkeypatch.setattr(needs, "market_news", market_news)


async def test_actus_pas_de_doublon_entre_sources(monkeypatch, state):
    box = {"items": [{"title": "Fed holds rates steady, signals two cuts in 2027", "url": "https://helious.io/n/1",
                      "published_at": None, "source": "Helious", "summary": None}], "source": "Helious"}
    fake_news(monkeypatch, box)
    assert await alerts.check_news(state) == []
    box.update(source="Finnhub", items=[{"title": "Fed Holds Rates Steady; Signals Two Cuts in 2027",
                                         "url": "https://finnhub.io/api/news?id=42", "published_at": None,
                                         "source": "Reuters", "summary": None}])
    assert await alerts.check_news(state) == []
    box["items"].append({"title": "Nvidia unveils new chip", "url": "https://finnhub.io/api/news?id=43",
                         "published_at": None, "source": "Reuters", "summary": None})
    messages = await alerts.check_news(state)
    assert len(messages) == 1 and "Nvidia" in messages[0]


async def test_actus_premier_lancement_garde_si_tout_en_panne(monkeypatch, state):
    box = {"items": None, "source": None}
    fake_news(monkeypatch, box)
    assert await alerts.check_news(state) == []
    assert "news" not in state.data  # le premier lancement n'est pas consommé
    box.update(items=[{"title": "Ancienne actu", "url": "https://x.example/1", "published_at": None,
                       "source": "X", "summary": None}], source="Helious")
    assert await alerts.check_news(state) == []  # vrai premier lancement : mémorisé sans envoi


# --- 4. Discord ---------------------------------------------------------------------------------

def transport(*responses):
    calls, queue = [], list(responses)

    def handler(request):
        calls.append(request)
        return queue.pop(0)

    return httpx.MockTransport(handler), calls


def test_decoupe_sans_morceau_vide():
    text = "a" * 1990 + "\n" + "\n" * 30 + " " * 50 + "\n" + "b" * 10
    chunks = discord.split_message(text)
    assert chunks and all(c.strip() for c in chunks) and all(len(c) <= 2000 for c in chunks)
    assert discord.split_message("\n\n   \n") == []


async def test_429_sans_json(monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr(discord.asyncio, "sleep", no_sleep)
    mock, calls = transport(httpx.Response(429, text="<html>trop de requêtes</html>"), httpx.Response(200, json={}))
    assert await discord.send("x", URL, mock) == 1 and len(calls) == 2


async def test_envoi_partiel_indique_ou_reprendre():
    mock, calls = transport(httpx.Response(200, json={}), httpx.Response(500, text="panne"))
    with pytest.raises(discord.DiscordError) as err:
        await discord.send_parts(["un", "deux", "trois"], URL, mock)
    assert err.value.sent_count == 1
    mock, calls = transport(httpx.Response(200, json={}), httpx.Response(200, json={}))
    assert await discord.send_parts(["un", "deux", "trois"][err.value.sent_count:], URL, mock) == 2


async def test_cockpit_coupe_par_trades_entiers():
    header = "🎯 **Trading Cockpit JARVIS** · mis à jour 21:30"
    trades = [f"🟢 **SYM{i}** 100.00 (+1.00 %, +0.50 R) · entrée 99 · stop 95\n" + "\n".join(
        f"   TP{j} 110.00 (+10.00 %) · ⏱️ ~2 h" for j in range(1, 4)) for i in range(40)]
    text = "\n\n".join([header, *trades])
    mock, calls = transport(httpx.Response(200, json={"id": "9"}))
    assert await discord.upsert(text, None, URL, mock) == "9"
    import json
    content = json.loads(calls[0].content)["content"]
    assert len(content) <= 2000 and content.endswith("trades (voir le cockpit web)")
    kept = content.split("\n\n")[1:-1]
    assert all(block in trades for block in kept)  # aucun trade coupé au milieu
    assert f"… +{40 - len(kept)} trades" in content


# --- 5. Propositions en privé -------------------------------------------------------------------

class FakeUser:
    def __init__(self, fail=0):
        self.fail, self.sent = fail, []

    async def send(self, text, view=None):
        if self.fail:
            self.fail -= 1
            raise RuntimeError("HTTP 503")
        self.sent.append(text)


async def test_proposition_envoyee_a_chaque_proprietaire_et_reessayee(monkeypatch):
    monkeypatch.setattr(bot, "delivered", set())
    monkeypatch.setattr(bot.PR.proposals, "get", lambda pid: None)
    users = {1: FakeUser(), 2: FakeUser(fail=1)}
    lookups = []

    async def fetch_user(oid):
        lookups.append(oid)
        if oid == 2 and lookups.count(2) == 1:
            raise RuntimeError("fetch_user en échec")
        return users[oid]

    p = {"id": 7, "key": "rsi_max", "current": 75, "value": 65, "why": "test"}
    assert await bot.deliver_once(p, {1, 2}, fetch_user, lambda pid: None) is False  # fetch_user échoue
    assert len(users[1].sent) == 1
    assert await bot.deliver_once(p, {1, 2}, fetch_user, lambda pid: None) is False  # envoi échoue
    assert await bot.deliver_once(p, {1, 2}, fetch_user, lambda pid: None) is True
    assert len(users[1].sent) == 1 and len(users[2].sent) == 1  # personne ne la reçoit deux fois


async def test_propositions_en_attente_relues_au_demarrage(monkeypatch):
    monkeypatch.setattr(bot, "outbox", bot.asyncio.Queue())
    pending = [{"id": 3, "status": "pending"}, {"id": 4, "status": "pending"}]
    monkeypatch.setattr(bot.PR.proposals, "pending", lambda: pending)
    assert bot.queue_pending() == 2 and bot.outbox.qsize() == 2


# --- 7. Cours par symbole -----------------------------------------------------------------------

async def test_twelvedata_un_ticker_en_erreur_n_arrete_pas_les_autres(monkeypatch):
    monkeypatch.setattr(twelvedata, "_calls", twelvedata.deque())

    async def get_json(url, params=None, headers=None):
        if params["symbol"] == "ZZZZ":
            return {"status": "error", "message": "symbole inconnu"}
        return {"symbol": params["symbol"], "close": "10", "percent_change": "1.5"}

    monkeypatch.setattr(twelvedata, "get_json", get_json)
    quotes = await twelvedata.stock_quotes(["AAPL", "ZZZZ", "MSFT"], apikey="k")
    assert [q["symbol"] for q in quotes] == ["AAPL", "ZZZZ", "MSFT"]
    assert quotes[0]["price"] == 10.0 and quotes[1]["price"] is None and "inconnu" in quotes[1]["error"]
    with pytest.raises(SourceError):
        await twelvedata.stock_quotes(["ZZZZ"], apikey="k")


async def test_twelvedata_respecte_8_par_minute(monkeypatch):
    monkeypatch.setattr(twelvedata, "_calls", twelvedata.deque())
    clock = {"t": 1000.0}
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)
        clock["t"] += seconds

    async def get_json(url, params=None, headers=None):
        return {"symbol": params["symbol"], "close": "1"}

    monkeypatch.setattr(twelvedata.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(twelvedata.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(twelvedata, "get_json", get_json)
    monkeypatch.setattr(twelvedata, "PER_MINUTE", 8)
    quotes = await twelvedata.stock_quotes([f"S{i}" for i in range(10)], apikey="k")
    # 8 cours dans la minute, les 2 suivants en erreur (source de secours) au lieu d'attendre 60 s
    assert len(quotes) == 10 and not waits
    assert [q["price"] is not None for q in quotes].count(True) == 8
    assert all("limite" in q["error"] for q in quotes[8:])


async def test_finnhub_un_ticker_en_erreur_n_arrete_pas_les_autres(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "k")

    async def get_json(url, params=None, headers=None):
        return {"c": 0} if params["symbol"] == "ZZZZ" else {"c": 5, "dp": 2.0}

    monkeypatch.setattr(finnhub, "get_json", get_json)
    quotes = await finnhub.stock_quotes(["AAPL", "ZZZZ"])
    assert quotes[0]["price"] == 5.0 and quotes[1]["price"] is None and quotes[1]["error"]
