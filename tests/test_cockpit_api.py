"""API du nouveau cockpit (/api/cockpit/*, app/web/cockpit_api.py) : sécurité, espaces, blocs position, séances."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import scheduler
from app.sources import kraken, twelvedata
from app.trading import calendar as CAL
from app.trading import paper
from app.trading import reco as R
from app.trading import service as T
from app.web import cockpit_api as API

NOW = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc)  # mercredi, 11 h New York, séance ouverte


@pytest.fixture
def client(monkeypatch, tmp_path):
    from app import main

    monkeypatch.setattr(scheduler, "book", T.Book(None))
    monkeypatch.setattr(scheduler, "reco_book", R.RecoBook(None))
    monkeypatch.setattr(scheduler, "paper_book", paper.PaperBook(None))
    monkeypatch.setattr(scheduler, "confirm_state", {})
    monkeypatch.setenv("JARVIS_LAB_DB", str(tmp_path / "lab.db"))  # pas de base : le labo le dit, sans exception
    monkeypatch.setattr(API, "_lab_cache", {"at": 0.0, "data": None})
    monkeypatch.setattr(API, "_text_cache", {})
    monkeypatch.setattr(API, "_agenda_cache", {})
    monkeypatch.setattr(API, "_agenda_inflight", {})

    async def no_twelvedata(*a, **kw):
        raise AssertionError("la page ne doit jamais déclencher un appel Twelve Data")

    async def fake_kraken(symbol, interval, quote="USD"):
        t = int(NOW.timestamp()) // 60 * 60
        return [{"t": t - 60 * i, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0 + i, "v": 1.0} for i in range(5, 0, -1)]

    monkeypatch.setattr(twelvedata, "candles", no_twelvedata)
    monkeypatch.setattr(twelvedata, "_spend", no_twelvedata)
    monkeypatch.setattr(kraken, "candles", fake_kraken)
    T._candle_cache.clear()
    return TestClient(main.app)


ROUTES = ["/api/cockpit/state", "/api/cockpit/instruments", "/api/cockpit/instrument/NVDA",
          "/api/cockpit/positions/charts", "/api/cockpit/reco", "/api/cockpit/lab", "/api/cockpit/journal",
          "/api/cockpit/health", "/api/cockpit/diagnostics", "/api/cockpit/texte/grade"]


def test_toutes_les_routes_repondent_sur_data_vide(client):
    for url in ROUTES:
        r = client.get(url)
        assert r.status_code == 200, (url, r.text)


def test_en_tetes_de_securite_sur_le_cockpit_seulement(client):
    r = client.get("/api/cockpit/state")
    assert r.headers["content-security-policy"].startswith("default-src 'self'; script-src 'self'")
    assert r.headers["x-frame-options"] == "DENY" and r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer" and r.headers["x-content-type-options"] == "nosniff"
    for url in ("/api/health", "/cockpit/ancien", "/cockpit/labo", "/docs"):
        assert "content-security-policy" not in client.get(url).headers, url  # styles et scripts en ligne, CDN
    assert client.get("/cockpit").headers["content-security-policy"].startswith("default-src 'self'")


def test_espace_symbole_et_intervalle_invalides(client):
    for url in ("/api/cockpit/state?space=reel", "/api/cockpit/instruments?space=x", "/api/cockpit/journal?space=",
                "/api/cockpit/positions/charts?space=live", "/api/cockpit/instrument/NVDA?space=x",
                "/api/cockpit/instrument/NVDA?tf=2m", "/api/cockpit/positions/charts?tf=4h",
                "/api/cockpit/instrument/NV$DA", "/api/cockpit/instrument/ABCDEFGHIJKLM",
                "/api/cockpit/instrument/NVDA?market=forex"):
        assert client.get(url).status_code == 400, url


def test_diagnostic_sans_secret(client, monkeypatch):
    monkeypatch.setenv("TWELVEDATA_API_KEY", "CLE-TRES-SECRETE-42")
    monkeypatch.setenv("JARVIS_CAPITAL", "98765.4")
    monkeypatch.setenv("JARVIS_LAB_API_KEY", "autre-secret-17")
    r = client.get("/api/cockpit/diagnostics")
    assert "attachment" in r.headers["content-disposition"]
    assert "CLE-TRES-SECRETE-42" not in r.text and "98765.4" not in r.text and "autre-secret-17" not in r.text
    body = r.json()
    assert body["cles_presentes"]["TWELVEDATA_API_KEY"] is True and "JARVIS_CAPITAL" in body["variables_jarvis_definies"]


def test_erreurs_recentes_masquent_les_cles():
    import logging

    logging.getLogger("app.test").warning("échec https://x.example/q?apikey=ABC123&symbol=NVDA")
    assert any("apikey=***" in e["message"] and "ABC123" not in e["message"] for e in API.recent_errors.items)


def trade(**over):
    t = {"id": "t1", "symbol": "NVDA", "market": "stock", "side": "long", "status": "open", "entry": 176.90,
         "stop": 171.60, "stop_current": 171.60, "targets_hit": 0, "events": [], "last_price": 180.0,
         "last_check": int(NOW.timestamp()) - 30, "exit_signals": [], "late_warned": [], "sortie_unique": 1,
         "created_at": (NOW - timedelta(hours=1)).isoformat(),
         "targets": [{"name": "TP1", "price": 182.0, "r_multiple": 0.96, "eta_text": "vers 16:10", "eta_median": None,
                      "eta_latest": (NOW - timedelta(minutes=5)).isoformat()},
                     {"name": "TP2", "price": 189.0, "r_multiple": 2.28, "eta_text": "vers 17:40", "eta_median": None,
                      "eta_latest": None},
                     {"name": "TP3", "price": 195.0, "r_multiple": 3.42, "eta_text": "", "eta_median": None,
                      "eta_latest": None}],
            "sizing": {"quantity": 4, "risk_amount_eur": 20.0, "eurusd": 1.16},
            "score": {"go": True}}
    return t | over


def test_bloc_position_long_en_sortie_unique(monkeypatch):
    monkeypatch.setattr(API.CAL, "session_open", lambda now: True)
    p = API.position_block(trade(), "trades", NOW)
    assert p["stop"] == {"price": 171.60, "initial": 171.60, "kind": "fixed",
                         "source": "IBKR, posé par toi · suivi par JARVIS"}
    assert [t["role"] for t in p["targets"]] == ["repere", "exit", "repere"]
    assert p["targets"][1]["pct"] == round((189.0 - 176.90) / 176.90 * 100, 2)
    assert p["targets"][0]["late"] is True and p["targets"][1]["late"] is False
    assert p["r_now"] == round((180.0 - 176.90) / (176.90 - 171.60), 2)
    assert p["unrealized"] == {"r": p["r_now"], "usd": 12.4, "eur": round(12.4 / 1.16, 2)}
    assert p["verdict"] == "GO" and p["qty"] == 4


def test_bloc_position_short_point_mort_et_taille_inconnue(monkeypatch):
    monkeypatch.setattr(API.CAL, "session_open", lambda now: True)
    t = trade(side="short", entry=100.0, stop=104.0, stop_current=100.0, last_price=98.0, sizing=None,
              sortie_unique=None, targets_hit=2, score={"go": False},
              events=[{"kind": "tp1", "price": 96.0, "at": NOW.isoformat()},
                      {"kind": "tp2", "price": 92.0, "at": NOW.isoformat()}],
              targets=[{"name": "TP1", "price": 96.0}, {"name": "TP2", "price": 92.0}, {"name": "TP3", "price": 88.0}])
    p = API.position_block(t, "trades", NOW)
    assert p["stop"]["kind"] == "breakeven" and p["r_now"] == 0.5 and p["move_pct"] == 2.0
    assert p["unrealized"] == {"r": 0.5, "usd": None, "eur": None}  # quantité inconnue : jamais 0 €
    assert [t["role"] for t in p["targets"]] == ["exit", "exit", "exit"] and p["targets"][1]["hit_at"]
    assert p["verdict"] == "NO GO" and p["against_jarvis"] is True


def test_cotation_perimee_pas_de_latent(monkeypatch):
    monkeypatch.setattr(API.CAL, "session_open", lambda now: True)
    p = API.position_block(trade(last_check=int(NOW.timestamp()) - 600), "trades", NOW)
    assert p["freshness"]["stale"] and p["unrealized"] is None and p["r_now"] is not None
    monkeypatch.setattr(API.CAL, "session_open", lambda now: False)
    p = API.position_block(trade(last_check=int(NOW.timestamp()) - 6000), "trades", NOW)
    assert p["freshness"]["etat"] == "cloture" and p["unrealized"] is not None


def test_les_deux_espaces_sont_separes(client):
    scheduler.book.trades.append(trade())
    r = R.RecoBook(None)
    reco = {"id": "go-x", "kind": "go_confirme", "symbol": "AMD", "market": "stock", "side": "long", "day": "2026-10-28",
            "sent_at": NOW.isoformat(), "status": "ouvert", "activation_ts": 0,
            "message": {"entry": 100.0, "stop": 98.0, "targets": [101.0, 104.0, 106.0], "qty": 11, "risk_eur": 20.0,
                        "eurusd": 1.16, "sortie_unique": 1, "valid_until": NOW.isoformat()},
            "trade": trade(id="go-x", symbol="AMD", entry=100.0, stop=98.0, stop_current=98.0, last_price=101.0,
                           targets=[{"name": "TP1", "price": 101.0}, {"name": "TP2", "price": 104.0},
                                    {"name": "TP3", "price": 106.0}], sizing={"quantity": 11})}
    r.recos.append(reco)
    scheduler.reco_book = r
    paper_ = client.get("/api/cockpit/state?space=paper").json()["positions_open"]
    mine = client.get("/api/cockpit/state?space=trades").json()["positions_open"]
    assert [p["symbol"] for p in paper_] == ["AMD"] and [p["symbol"] for p in mine] == ["NVDA"]
    assert paper_[0]["verdict"] == "GO" and paper_[0]["stop"]["source"].startswith("message Discord")
    assert paper_[0]["ibkr_orders"] == [{"quantity": 11, "entry": 100.0, "stop": 98.0, "name": "TP2",
                                         "take_profit": 104.0}]
    assert client.get("/api/cockpit/reco").json()["recos"][0]["status_fr"] == "déclenchée"


def test_aucun_appel_twelve_data_depuis_la_page(client, monkeypatch):
    """Action en position sans bougie en cache et sans Alpaca : pas de prix plutôt qu'un crédit dépensé."""
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    scheduler.book.trades.append(trade())
    for url in ("/api/cockpit/instrument/NVDA?tf=1m", "/api/cockpit/positions/charts?space=trades",
                "/api/cockpit/instruments?space=trades", "/api/cockpit/state?space=trades"):
        assert client.get(url).status_code == 200, url
    out = client.get("/api/cockpit/instrument/NVDA?space=trades").json()
    assert out["bars"] == [] and "Twelve Data" in out["manque"] and out["positions"][0]["id"] == "t1"
    T._candle_cache[("NVDA", "stock", 15, "")] = (0.0, [{"t": 1, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 9}])
    assert client.get("/api/cockpit/instrument/NVDA?space=trades").json()["bars"] == [[1, 1, 2, 0.5, 1.5, 9]]


def ny(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=CAL.NEW_YORK).astimezone(timezone.utc)


def test_seances_ferie_week_end_demi_seance_et_decalage_horaire():
    s = API.market_sessions(ny(2026, 11, 26, 12, 0))["nyse"]  # Thanksgiving
    assert s["etat"] == "ferme" and "Thanksgiving" in s["raison_fermeture"]
    assert s["prochaine_ouverture"] == ny(2026, 11, 27, 9, 30).isoformat()
    s = API.market_sessions(ny(2026, 10, 31, 12, 0))["nyse"]  # samedi
    assert s["etat"] == "ferme" and s["raison_fermeture"] == "week-end"
    s = API.market_sessions(ny(2026, 11, 27, 12, 30))["nyse"]  # lendemain de Thanksgiving : 13 h
    assert s["etat"] == "seance" and s["demi_seance"] and s["prochain_changement"] == ny(2026, 11, 27, 13, 0).isoformat()
    s = API.market_sessions(ny(2026, 11, 27, 14, 0))["nyse"]
    assert s["etat"] == "apres_bourse" and s["prochain_changement"] == ny(2026, 11, 27, 17, 0).isoformat()
    s = API.market_sessions(ny(2026, 10, 28, 9, 0))["nyse"]  # semaine du décalage : ouverture à 14 h 30 à Paris
    opening = datetime.fromisoformat(s["prochaine_ouverture"]).astimezone(T.PARIS)
    assert s["etat"] == "premarche" and (opening.hour, opening.minute) == (14, 30) and s["decalage_inhabituel"]
    s = API.market_sessions(ny(2026, 11, 4, 9, 0))["nyse"]
    opening = datetime.fromisoformat(s["prochaine_ouverture"]).astimezone(T.PARIS)
    assert (opening.hour, opening.minute) == (15, 30) and not s["decalage_inhabituel"]


def test_boucles_du_planificateur(monkeypatch):
    monkeypatch.setattr(scheduler, "last_runs", {API.TRACKING_LOOP: {"at": (NOW - timedelta(seconds=400)).isoformat(),
                                                                     "ok": True, "every_s": 60}})
    out = API.loops(NOW)
    assert out["suivi"]["stale"] and out["suivi"]["age_s"] == 400


def test_risque_porte_les_baremes_ibkr(client, monkeypatch):
    from app.trading import killmode as K

    monkeypatch.setattr(K, "IBKR_PLAN", "degressif")
    ibkr = client.get("/api/cockpit/state").json()["risk"]["ibkr"]
    assert ibkr["bareme"] == "degressif" and set(ibkr["baremes"]) == set(K.IBKR_PLANS)
    fixe = ibkr["baremes"]["fixe"]
    assert fixe["minimum_usd"] == K.IBKR_PLANS["fixe"]["min"] and fixe["plancher_aller_retour_usd"] == 2 * fixe["minimum_usd"]
    assert fixe["plancher_r"] == round(2 * fixe["minimum_usd"] / (T.rule_eur() * K.EURUSD_DEFAULT), 3)


# --- Textes des commandes Discord ---------------------------------------------------------------------------


def test_texte_liste_blanche_et_meme_texte_que_discord(client, monkeypatch):
    from app.notify import bot

    assert client.get("/api/cockpit/texte/inconnu").status_code == 400
    assert client.get("/api/cockpit/texte/..%2Fsecrets").status_code in (400, 404)
    monkeypatch.setattr(bot, "grade_text", lambda: "🏅 **Grade** : F partout")
    body = client.get("/api/cockpit/texte/grade").json()
    assert body["texte"] == "🏅 **Grade** : F partout" and body["commande"] == "!grade" and body["erreur"] is None
    monkeypatch.setattr(bot, "grade_text", lambda: "autre")
    assert client.get("/api/cockpit/texte/grade").json()["texte"] == "🏅 **Grade** : F partout"  # cache de 2 min


def test_texte_illisible_rend_l_erreur_sans_casser_la_vue(client, monkeypatch):
    from app.notify import bot

    def boom():
        raise RuntimeError("no such table: rates")

    monkeypatch.setattr(bot, "rates_text", boom)
    r = client.get("/api/cockpit/texte/rates")
    assert r.status_code == 200 and r.json()["texte"] is None and "no such table" in r.json()["erreur"]


def test_chaque_module_de_texte_repond_sur_data_vide(client):
    for module in API.TEXT_MODULES:
        r = client.get(f"/api/cockpit/texte/{module}")
        assert r.status_code == 200, module
        assert r.json()["texte"] or r.json()["erreur"], module


# --- Agenda --------------------------------------------------------------------------------------------------


@pytest.fixture
def agenda_sources(monkeypatch):
    """Calendrier économique, résultats et 8-K factices, datés autour de maintenant (la route lit l'heure réelle)."""
    from app import config, needs
    from app.core.result import Result
    from app.sources import edgar, finnhub

    now = datetime.now(timezone.utc)
    day = now.astimezone(API.NEW_YORK).date()
    calls = {"submissions": 0}
    monkeypatch.setattr(config, "WATCHLIST_STOCKS", ["AMD", "KO", "ZZZZ"])
    monkeypatch.setenv("FINNHUB_API_KEY", "finnhub-secret-99")
    monkeypatch.setenv("SEC_USER_AGENT", "Test test@example.com")

    async def calendar():
        ev = [(now + timedelta(hours=1), "Décision de la Fed", "US", "High"),
              (now + timedelta(hours=2), "Ventes au détail", "US", "Low"),          # impact faible : filtré
              (now + timedelta(hours=3), "Indice Tankan", "JP", "High"),           # pays hors filtre
              (now + timedelta(days=9), "Emploi", "US", "High")]                   # au-delà de 7 jours
        return Result("economic_calendar", [{"datetime_utc": at.isoformat(), "title": t, "country": c, "impact": i,
                                             "forecast": None, "previous": None, "actual": None, "status": None}
                                            for at, t, c, i in ev], "Helious", "Source : Helious", now.isoformat())

    async def earnings(start, end):
        assert (start, end) == (day, day + timedelta(days=API.AGENDA_DAYS - 1))
        return [{"symbol": s, "date": day.isoformat(), "hour": h, "quarter": 3, "year": 2026, "eps_estimate": 1.0,
                 "eps_actual": None, "revenue_estimate": None, "revenue_actual": None}
                for s, h in [("AMD", "amc"), ("KO", "bmo"), ("MSFT", "amc"), ("XYZ", "")]]

    async def tickers():
        return {"AMD": 2488, "KO": 21344}

    async def submissions(cik):
        calls["submissions"] += 1
        recent = [{"accession": "0000002488-26-000101", "form": "8-K", "items": ["2.02", "9.99"],
                   "accepted_ts": int(now.timestamp()), "filing_date": day.isoformat(), "suspect": True},
                  {"accession": "0000002488-26-000090", "form": "10-Q", "items": [],
                   "accepted_ts": int(now.timestamp()), "filing_date": day.isoformat(), "suspect": False},
                  {"accession": "0000002488-26-000050", "form": "8-K", "items": ["5.02"],
                   "accepted_ts": int((now - timedelta(days=9)).timestamp()),
                   "filing_date": (day - timedelta(days=9)).isoformat(), "suspect": False}]
        return {"recent": recent if cik == 2488 else []}

    monkeypatch.setattr(needs, "economic_calendar", calendar)
    monkeypatch.setattr(finnhub, "earnings_calendar", earnings)
    monkeypatch.setattr(edgar, "tickers", tickers)
    monkeypatch.setattr(edgar, "submissions", submissions)
    return calls


def test_agenda_filtre_les_sources_et_ne_montre_aucune_cle(client, agenda_sources):
    r = client.get("/api/cockpit/agenda")
    assert r.status_code == 200 and "finnhub-secret-99" not in r.text
    out = r.json()
    assert out["valeurs_suivies"] == ["AMD", "KO", "ZZZZ"] and out["aujourd_hui"] and "nyse" in out["seances"]
    assert [e["titre"] for e in out["economique"]["evenements"]] == ["Décision de la Fed"]
    res = out["resultats"]
    assert [s["symbol"] for s in res["suivies"]] == ["AMD", "KO"] and res["suivies"][0]["trimestre"] == "T3 2026"
    jour = next(iter(res["jours"].values()))
    assert jour["total"] == 4 and jour["apres_cloture"] == ["AMD", "MSFT"] and jour["heure_inconnue"] == ["XYZ"]
    sec = out["sec8k"]
    assert [(d["symbol"], d["form"]) for d in sec["depots"]] == [("AMD", "8-K")]  # ni 10-Q ni 8-K de 9 jours
    depot = sec["depots"][0]
    assert depot["heure_douteuse"] and [i["label"] for i in depot["items"]] == ["résultats", "item 9.99"]
    assert depot["lien"].startswith("https://www.sec.gov/") and sec["sans_cik"] == ["ZZZZ"]
    assert sec["lues"] == ["AMD", "KO"] and sec["erreurs"] == sec["trop_lentes"] == sec["hors_plafond"] == []
    assert out["economique"]["couvre_jusqu_au"] is not None
    client.get("/api/cockpit/agenda")
    assert agenda_sources["submissions"] == 2  # AMD et KO une fois chacun : le second passage lit le cache


def test_agenda_sans_cles_et_source_en_panne(client, agenda_sources, monkeypatch):
    from app import needs

    async def down():
        raise ConnectionError("Helious injoignable")

    monkeypatch.delenv("FINNHUB_API_KEY")
    monkeypatch.delenv("SEC_USER_AGENT")
    monkeypatch.setattr(needs, "economic_calendar", down)
    out = client.get("/api/cockpit/agenda").json()
    assert "FINNHUB_API_KEY" in out["resultats"]["manque"] and out["resultats"]["suivies"] == []
    assert "SEC_USER_AGENT" in out["sec8k"]["manque"] and out["sec8k"]["depots"] == []
    assert "Helious injoignable" in out["economique"]["erreur"] and out["aujourd_hui"]


def test_agenda_calendrier_sans_valeur_connue_est_une_erreur(client, agenda_sources, monkeypatch):
    """needs.resolve ne lève jamais : sans source ni valeur gardée, il rend data=None. Inconnu ≠ « rien de notable »."""
    from app import needs
    from app.core.result import Result

    async def unknown():
        return Result("economic_calendar", None, None, None, None, stale=True,
                      message="Donnée indisponible : aucune valeur connue")

    monkeypatch.setattr(needs, "economic_calendar", unknown)
    eco = client.get("/api/cockpit/agenda").json()["economique"]
    assert "aucune valeur connue" in eco["erreur"] and "evenements" not in eco


def test_agenda_positions_d_abord_plans_du_jour_seulement(client, agenda_sources, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "WATCHLIST_STOCKS", ["KO", "W1", "W2"])
    monkeypatch.setattr(API, "_held_symbols", lambda space=None: {"AMD": "stock", "BTC": "crypto"})
    monkeypatch.setattr(API, "plans_today", lambda now: {"du_jour": False, "picks": [{"symbol": "OLD"}]})
    assert API.agenda_symbols(NOW) == ["AMD", "KO", "W1", "W2"]  # pas le scan d'une séance passée
    monkeypatch.setattr(API, "plans_today", lambda now: {"du_jour": True, "picks": [{"symbol": "PLAN"}]})
    assert API.agenda_symbols(NOW) == ["AMD", "PLAN", "KO", "W1", "W2"]
    monkeypatch.setattr(API, "SEC_MAX_SYMBOLS", 2)
    sec = client.get("/api/cockpit/agenda").json()["sec8k"]
    assert sec["valeurs"] == ["AMD", "PLAN"] and sec["hors_plafond"] == ["KO", "W1", "W2"] and sec["lues"] == ["AMD"]
    assert sec["sans_cik"] == ["PLAN"]


async def test_8k_valeurs_lues_separees_des_erreurs_et_des_lentes(agenda_sources, monkeypatch):
    import asyncio

    from app.sources import edgar

    async def tickers():
        return {"AMD": 1, "KO": 2, "SLOW": 3}

    async def submissions(cik):
        if cik == 2:
            raise ConnectionError("HTTP 403")
        if cik == 3:
            await asyncio.sleep(5)
        return {"recent": []}

    monkeypatch.setattr(edgar, "tickers", tickers)
    monkeypatch.setattr(edgar, "submissions", submissions)
    monkeypatch.setattr(API, "SEC_BUDGET_S", 0.2)
    sec = await API._sec_filings(NOW, ["AMD", "KO", "SLOW"])
    assert sec["lues"] == ["AMD"] and sec["erreurs"] == ["KO : ConnectionError"] and sec["trop_lentes"] == ["SLOW"]
    assert sec["depots"] == []  # aucun 8-K chez AMD ; KO et SLOW non vérifiées, pas « sans 8-K »
    API._agenda_inflight["sec:3"].cancel()


async def test_cache_de_l_agenda_partage_les_lectures_et_oublie_les_perimees(monkeypatch):
    import asyncio

    monkeypatch.setattr(API, "_agenda_cache", {"vieux": (0.0, "périmé")})
    monkeypatch.setattr(API, "_agenda_inflight", {})
    calls = []

    async def fetch():
        calls.append(1)
        await asyncio.sleep(0.05)
        return "valeur"

    out = await asyncio.gather(*(API._cached("clé", 60, fetch) for _ in range(3)))
    assert out == ["valeur"] * 3 and len(calls) == 1  # une seule lecture pour trois requêtes
    assert "vieux" not in API._agenda_cache and "clé" not in API._agenda_inflight


def test_confirmations_d_une_seance_passee_ne_sont_pas_celles_du_jour(client, monkeypatch):
    monkeypatch.setattr(scheduler, "confirm_state", {"day": "2026-10-23", "gos": {"NVDA": {"status": "attente"}}})
    c = client.get("/api/cockpit/journal").json()["confirmations"]
    assert c["du_jour"] is False and c["jour"] == "2026-10-23"
