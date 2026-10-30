"""Calendrier économique (Helious) et actus de marché : crédits Helious comptés au mois (29/10/2026).

Sans clé : 50 appels par mois partagés par l'adresse du serveur ; les actus (4 par heure) les vidaient en une
demi-journée. Les actus passent par Finnhub seul, le calendrier est relu toutes les 8 h avec la clé, 24 h sans, et plus
aucun appel ne part pendant 30 min après un refus.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app import needs
from app.core import fallback
from app.core.cache import Store
from app.core.http import SourceError
from app.core.result import Result
from app.sources import finnhub, helious

EVENT = {"datetime_utc": "2026-10-30T12:30:00+00:00", "title": "US: PCE", "country": "US", "impact": "High",
         "forecast": "0.3%", "previous": "0.2%", "actual": None, "status": "upcoming"}


@pytest.fixture(autouse=True)
def isole(monkeypatch, tmp_path):
    monkeypatch.setattr(fallback, "store", Store(str(tmp_path / "last_known.json")))
    monkeypatch.setattr(helious, "_paused_until", 0.0)
    monkeypatch.delenv("HELIOUS_API_KEY", raising=False)
    monkeypatch.setenv("FINNHUB_API_KEY", "fh-cle")


@pytest.fixture
def appels(monkeypatch):
    """Faux get_json de Helious : note chaque appel (url, paramètres, en-têtes) et renvoie un calendrier."""
    calls, box = [], {"error": None, "payload": None}

    async def fake(url, params=None, headers=None, refusal=None):
        calls.append({"url": url, "params": params, "headers": headers})
        if box["error"]:
            raise SourceError(box["error"])
        payload = box["payload"] or {"data": {"events": [dict(EVENT)]}}
        refused = refusal(payload) if refusal else None
        if refused:
            raise SourceError(refused)
        return payload

    monkeypatch.setattr(helious, "get_json", fake)
    return calls, box


def kept(age: timedelta) -> None:
    got = datetime.now(timezone.utc) - age
    fallback.store.put("economic_calendar", Result("economic_calendar", [dict(EVENT)], "Helious", "Source : Helious",
                                                   got.isoformat(timespec="seconds")))
    fallback.store._fresh.clear()  # reçu il y a `age` : seule la copie sur disque compte, pas le cache du processus


async def test_toute_la_semaine_en_un_appel_sans_cle(appels):
    calls, _ = appels
    assert await helious.economic_calendar() == [EVENT]
    assert calls == [{"url": "https://api.helious.io/v1/calendar",
                      "params": {"days_back": 1, "days_ahead": 8, "limit": 300}, "headers": None}]


async def test_cle_envoyee_en_bearer(appels, monkeypatch):
    calls, _ = appels
    monkeypatch.setenv("HELIOUS_API_KEY", "hel_test")
    await helious.economic_calendar()
    assert calls[0]["headers"] == {"Authorization": "Bearer hel_test"}


async def test_pause_apres_un_refus_429(appels, monkeypatch):
    calls, box = appels
    box["error"] = "HTTP 429"
    with pytest.raises(SourceError, match="^HTTP 429$"):
        await helious.economic_calendar()
    box["error"] = None
    with pytest.raises(SourceError, match=r"^HTTP 429 \(refus de Helious, prochain essai dans 30 min\)"):
        await helious.economic_calendar()  # aucun appel pendant la pause
    assert len(calls) == 1
    monkeypatch.setattr(helious, "_paused_until", 0.0)  # pause finie
    assert await helious.economic_calendar() == [EVENT] and len(calls) == 2


async def test_reponse_200_sans_calendrier_est_un_echec_et_met_en_pause(appels):
    calls, box = appels
    box["payload"] = {"data": {"items": []}}  # forme du flux d'actus, pas du calendrier : un crédit facturé pour rien
    with pytest.raises(SourceError, match="^réponse inattendue de Helious"):
        await helious.economic_calendar()
    with pytest.raises(SourceError, match="^HTTP 429 \\(refus de Helious"):
        await helious.economic_calendar()
    assert len(calls) == 1


async def test_pas_de_pause_apres_une_autre_erreur(appels):
    calls, box = appels
    box["error"] = "HTTP 500"
    for _ in range(2):
        with pytest.raises(SourceError, match="^HTTP 500$"):
            await helious.economic_calendar()
    assert len(calls) == 2


def test_duree_de_vie_du_calendrier_selon_la_cle(monkeypatch):
    assert needs.calendar_ttl() == 24 * 3600  # sans clé : ≈ 31 appels par mois sur les 50 partagés
    monkeypatch.setenv("HELIOUS_API_KEY", "hel_test")
    assert needs.calendar_ttl() == 8 * 3600   # avec la clé : ≈ 93 appels par mois sur 100


async def test_calendrier_garde_sur_disque_sert_apres_un_redemarrage(appels, tmp_path, monkeypatch):
    calls, _ = appels
    kept(timedelta(hours=20))
    fallback.store.flush()
    monkeypatch.setattr(fallback, "store", Store(str(tmp_path / "last_known.json")))  # redémarrage : cache vide
    res = await needs.economic_calendar()
    assert calls == [] and res.source == "Helious" and not res.stale and res.data == [EVENT]


async def test_calendrier_relu_apres_sa_duree_de_vie(appels, monkeypatch):
    calls, _ = appels
    kept(timedelta(hours=9))
    await needs.economic_calendar()
    assert calls == []  # sans clé : 24 h
    monkeypatch.setenv("HELIOUS_API_KEY", "hel_test")
    res = await needs.economic_calendar()
    assert len(calls) == 1 and not res.stale  # avec la clé : 8 h
    on_disk = Store(fallback.store._path and str(fallback.store._path)).last_known("economic_calendar")
    assert on_disk.updated_at == res.updated_at  # écrit sur disque tout de suite
    await needs.economic_calendar()
    assert len(calls) == 1  # la nouvelle lecture sert à son tour


async def test_refus_donne_la_derniere_version_connue(appels):
    calls, box = appels
    kept(timedelta(days=3))
    box["error"] = "HTTP 429"
    res = await needs.economic_calendar()
    assert res.stale and res.data == [EVENT] and "Helious : HTTP 429" in res.errors
    await needs.economic_calendar()
    assert len(calls) == 1  # pause : le cockpit (rafraîchi chaque minute) ne relance pas Helious


async def test_actus_de_marche_par_finnhub_seul(monkeypatch):
    async def news(limit):
        return [{"title": "Fed", "url": "https://finnhub.io/n/1", "published_at": None, "source": "Reuters",
                 "summary": None}]

    monkeypatch.setattr(finnhub, "market_news", news)
    assert not hasattr(helious, "market_news")
    res = await needs.market_news(20)
    assert res.source == "Finnhub" and res.data[0]["title"] == "Fed"


async def test_actus_sans_cle_finnhub(monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY")
    res = await needs.market_news(7)
    assert res.data is None and res.errors == ["Finnhub : clé FINNHUB_API_KEY absente"]
