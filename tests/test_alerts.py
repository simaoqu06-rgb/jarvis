"""Tests hors ligne des alertes : pas de doublon, paliers de variation, premier lancement silencieux."""
from datetime import datetime, timezone

import pytest

from app import alerts, config, needs
from app.core.result import Result

NOW = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)


def result(need, data):
    return Result(need, data, "Test", "Données : Test", "2026-10-05T13:59:00+00:00")


@pytest.fixture
def state():
    return alerts.State(None)


@pytest.fixture(autouse=True)
def watchlists(monkeypatch):
    monkeypatch.setattr(config, "WATCHLIST_CRYPTO", ["BTC"])
    monkeypatch.setattr(config, "WATCHLIST_STOCKS", [])
    monkeypatch.setattr(config, "MOVE_ALERT_PCT", 5.0)


def fake_ranking(monkeypatch, change):
    async def ranking(limit=20):
        return result("crypto_ranking", [{"rank": 1, "symbol": "BTC", "name": "Bitcoin", "price_usd": 80000.0,
                                          "change_24h_pct": change, "market_cap_usd": 1.0, "volume_24h_usd": 1.0}])
    monkeypatch.setattr(needs, "crypto_ranking", ranking)


async def test_pas_d_alerte_sous_le_seuil(monkeypatch, state):
    fake_ranking(monkeypatch, -4.9)
    assert await alerts.check_price_moves(state, NOW) == []


async def test_une_alerte_par_palier(monkeypatch, state):
    fake_ranking(monkeypatch, -5.2)
    first = await alerts.check_price_moves(state, NOW)
    assert len(first) == 1 and "BTC" in first[0] and "-5.20 %" in first[0]
    assert await alerts.check_price_moves(state, NOW) == []  # même palier : pas de doublon
    fake_ranking(monkeypatch, -10.4)
    assert len(await alerts.check_price_moves(state, NOW)) == 1  # palier suivant


async def test_seismes_premier_lancement_silencieux(monkeypatch, state):
    quakes = [{"id": "a", "time": "2026-10-05T10:00:00+00:00", "magnitude": 6.4, "place": "Japon",
               "lat": 0, "lon": 0, "depth_km": 10.0, "tsunami": False, "url": "https://example.org/a"}]

    async def earthquakes(min_magnitude, hours):
        return result("earthquakes", list(quakes))

    monkeypatch.setattr(needs, "earthquakes", earthquakes)
    assert await alerts.check_earthquakes(state) == []
    quakes.insert(0, dict(quakes[0], id="b", magnitude=7.1, place="Chili", tsunami=True))
    messages = await alerts.check_earthquakes(state)
    assert len(messages) == 1 and "M7.1" in messages[0] and "tsunami" in messages[0]
    assert await alerts.check_earthquakes(state) == []


async def test_actus_sans_doublon(monkeypatch, state):
    items = [{"title": "Titre 1", "url": "https://helious.io/news", "published_at": None, "source": "Helious", "summary": None}]

    async def market_news(limit=20):
        return result("market_news", list(items))

    monkeypatch.setattr(needs, "market_news", market_news)
    assert await alerts.check_news(state) == []
    items.append(dict(items[0], title="Titre 2"))  # même lien, autre titre : c'est une nouvelle actu
    messages = await alerts.check_news(state)
    assert len(messages) == 1 and "Titre 2" in messages[0]


async def test_point_du_matin_signale_les_valeurs_anciennes(monkeypatch):
    monkeypatch.setattr(config, "HOME_CITY", "")

    async def status(now=None):
        return {"is_open": False, "reason": "week-end"}

    async def calendar():
        return result("economic_calendar", [])

    async def prices(symbols):
        stale = result("crypto_prices", [{"symbol": "BTC", "currency": "EUR", "price": 70000.0, "bid": None,
                                          "ask": None, "high_24h": None, "low_24h": None,
                                          "volume_24h_eur": None, "change_24h_pct": 1.5}])
        stale.stale = True
        return stale

    async def fx(base, symbols):
        return result("fx_rates", {"base": "EUR", "date": "2026-10-05", "rates": {"USD": 1.12}})

    monkeypatch.setattr(needs, "us_market_status", status)
    monkeypatch.setattr(needs, "economic_calendar", calendar)
    monkeypatch.setattr(needs, "crypto_prices", prices)
    fake_ranking(monkeypatch, 1.0)
    monkeypatch.setattr(needs, "fx_rates", fx)
    text = await alerts.morning_brief(NOW)
    assert "fermée (week-end)" in text and "70 000.00 €" in text and "1 € = 1.1200 USD" in text
    assert "Valeurs non à jour" in text and "Données : Test" in text


@pytest.mark.parametrize("last_event, stale, expected", [
    ("2026-10-02T12:30:00+00:00", True, "**Agenda économique du jour** : inconnu (le dernier calendrier reçu du 01/10 "
                                         "15:00 ne couvre pas aujourd'hui)."),
    ("2026-10-09T12:30:00+00:00", True, "**Agenda économique du jour** : rien d'important pour les États-Unis et "
                                         "l'Europe."),
    ("2026-10-02T12:30:00+00:00", False, "**Agenda économique du jour** : rien d'important pour les États-Unis et "
                                          "l'Europe."),
])
async def test_point_du_matin_agenda_inconnu_si_le_calendrier_garde_ne_couvre_pas_le_jour(monkeypatch, last_event,
                                                                                          stale, expected):
    # Helious refuse (quota du mois) : le calendrier gardé du 01/10 s'arrête le 02/10, le point du 05/10 ne sait rien
    monkeypatch.setattr(config, "WATCHLIST_CRYPTO", [])
    monkeypatch.setattr(config, "HOME_CITY", "")

    async def status(now=None):
        return {"is_open": True, "reason": "séance"}

    async def calendar():
        r = Result("economic_calendar", [{"datetime_utc": last_event, "title": "US: CPI", "country": "US",
                                          "impact": "Low"}], "Helious", "Source : Helious", "2026-10-01T13:00:00+00:00")
        r.stale = stale
        return r

    async def fx(base, symbols):
        return result("fx_rates", {"base": "EUR", "date": "2026-10-05", "rates": {"USD": 1.12}})

    monkeypatch.setattr(needs, "us_market_status", status)
    monkeypatch.setattr(needs, "economic_calendar", calendar)
    monkeypatch.setattr(needs, "fx_rates", fx)
    assert expected in (await alerts.morning_brief(NOW)).splitlines()


async def test_point_du_matin_sans_calendrier(monkeypatch):
    monkeypatch.setattr(config, "WATCHLIST_CRYPTO", [])
    monkeypatch.setattr(config, "HOME_CITY", "")

    async def status(now=None):
        return {"is_open": True, "reason": "séance"}

    async def calendar():
        return Result("economic_calendar", None, None, None, None, stale=True)

    async def fx(base, symbols):
        return result("fx_rates", {"base": "EUR", "date": "2026-10-05", "rates": {"USD": 1.12}})

    monkeypatch.setattr(needs, "us_market_status", status)
    monkeypatch.setattr(needs, "economic_calendar", calendar)
    monkeypatch.setattr(needs, "fx_rates", fx)
    text = await alerts.morning_brief(NOW)
    assert "**Agenda économique du jour** : inconnu (aucun calendrier reçu)." in text.splitlines()
