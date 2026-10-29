"""Rubrique « À faire » du cockpit (app/web/attention.py) : règles, synchronisation, routes et instrumentations."""
import asyncio
import json
import os
import sqlite3
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import alerts, scheduler
from app.core import outbox as OB
from app.core import sources_health, version
from app.core.http import SourceError
from app.notify import bot, discord
from app.sources import twelvedata
from app.trading import paper
from app.trading import proposals as PR
from app.trading import reco as R
from app.trading import research
from app.trading import service as T
from app.web import attention as A
from app.web import cockpit_actions as ACT
from app.web import cockpit_api as API
from tests.test_cockpit_api import trade

NOW = datetime(2026, 10, 28, 15, 0, tzinfo=timezone.utc)  # mercredi : 11 h à New York (séance ouverte), 16 h à Paris
KEYS = {"TWELVEDATA_API_KEY": "td-cle", "FINNHUB_API_KEY": "fh-cle", "ALPACA_API_KEY": "al-cle",
        "ALPACA_SECRET_KEY": "al-secret", "JARVIS_LAB_API_KEY": "lab-cle", "SEC_USER_AGENT": "Simao test@example.fr",
        "DISCORD_WEBHOOK_URL": "https://discord.example/api/webhooks/1/abc", "DISCORD_BOT_TOKEN": "bot-jeton",
        "JARVIS_DISCORD_OWNER_ID": "42"}
Usage = namedtuple("Usage", "total used free")  # comme shutil.disk_usage
HEALTHY_RISK = {"r_du_jour_net": 0.0, "arret_jour_r": -3.0, "risque_ouvert_pct": 0.0, "risque_ouvert_max_pct": 3.0,
                "grade_age_h": 1.0, "grade_max_age_h": 6.0}


def iso(d: datetime) -> str:
    return d.isoformat(timespec="seconds")


def up_to_date(**over) -> dict:
    sha = "a" * 40
    return {"sur_le_disque": sha, "branche": "main", "erreur_disque": None, "code": "identique",
            "empreinte_image": "e1", "empreinte_disque": "e1", "depot": "simaoqu06-rgb/jarvis", "base_verifiee": sha,
            "verifie_le": iso(NOW - timedelta(minutes=5)), "statut": "identical", "commits_en_retard": 0,
            "pr_en_retard": [], "code_touche": False, "erreur": None} | over


def make_lab(path: Path, garde: dict | None = None, events: list[tuple] = (), promoted: dict | None = None) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE progress (job TEXT NOT NULL, key TEXT NOT NULL, value TEXT, PRIMARY KEY (job, key))")
    conn.execute("CREATE TABLE lab_events (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, ts INTEGER NOT NULL, "
                 "kind TEXT NOT NULL, version TEXT, text TEXT NOT NULL, data TEXT)")
    conn.execute("CREATE TABLE method_versions (version TEXT PRIMARY KEY, created_at TEXT NOT NULL, settings TEXT "
                 "NOT NULL)")
    for v, by in (promoted or {}).items():
        conn.execute("INSERT INTO method_versions VALUES (?, ?, ?)", (v, iso(NOW), json.dumps({"promu_par": by})))
    if garde:
        conn.execute("INSERT INTO progress VALUES ('lab', 'garde_v1', ?)", (json.dumps(garde),))
    for e in events:
        conn.execute("INSERT INTO lab_events (id, created_at, ts, kind, version, text) VALUES (?, ?, ?, ?, ?, ?)", e)
    conn.commit()
    conn.close()


def beat(folder: Path, at: datetime) -> None:
    p = folder / "lab.heartbeat"
    p.write_text(json.dumps({"le": iso(at), "etape": "direct"}), encoding="utf-8")
    os.utime(p, (at.timestamp(), at.timestamp()))


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Un JARVIS en bonne santé, démarré depuis une heure : aucune règle ne doit rien dire."""
    for k, v in KEYS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("JARVIS_NOTIFICATIONS", raising=False)
    monkeypatch.delenv("JARVIS_SSH_CIBLE", raising=False)
    monkeypatch.setattr(scheduler, "book", T.Book(None))
    monkeypatch.setattr(scheduler, "reco_book", R.RecoBook(None))
    monkeypatch.setattr(scheduler, "paper_book", paper.PaperBook(None))
    monkeypatch.setattr(scheduler, "confirm_state", {})
    monkeypatch.setattr(scheduler, "state", alerts.State(None))
    monkeypatch.setattr(scheduler, "outbox", OB.Outbox(None))
    monkeypatch.setattr(PR, "proposals", PR.Proposals(None))
    monkeypatch.setattr(sources_health, "_state", {})
    monkeypatch.setattr(API.recent_errors, "items", type(API.recent_errors.items)(maxlen=50))
    monkeypatch.setattr(twelvedata, "credits", {"day": None, "n": 0})
    monkeypatch.setattr(bot, "status", {"connecte": True, "depuis": iso(NOW - timedelta(hours=1)),
                                        "derniere_erreur": None, "erreur_type": None, "erreur_le": None})
    monkeypatch.setattr(A, "STARTED", NOW - timedelta(hours=1))
    monkeypatch.setattr(A, "_rebuild", {"since": None})
    monkeypatch.setattr(A, "book", A.AttentionBook(str(tmp_path / "attention.json")))
    monkeypatch.setattr(version, "snapshot", lambda: up_to_date())
    monkeypatch.setattr(API, "risk", lambda now: dict(HEALTHY_RISK))
    monkeypatch.setattr("shutil.disk_usage", lambda folder: Usage(100e9, 30e9, 70e9))
    monkeypatch.setattr(research, "last_scan", lambda path=None: None)
    monkeypatch.setattr(ACT, "ACTIONS_LOG", str(tmp_path / "actions.jsonl"))
    db = tmp_path / "lab.db"
    make_lab(db)
    monkeypatch.setenv("JARVIS_LAB_DB", str(db))
    beat(tmp_path, NOW - timedelta(minutes=2))
    monkeypatch.setattr(scheduler, "running", {})
    monkeypatch.setattr(scheduler, "last_runs", {
        name: {"at": iso(NOW - timedelta(seconds=20)), "ok": True, "every_s": every, "echecs_suite": 0}
        for name, every in A.expected_loops()})
    T._candle_cache.clear()

    async def no_network(*a, **kw):
        raise AssertionError("la page À faire ne fait aucun appel réseau")

    monkeypatch.setattr(twelvedata, "candles", no_network)
    monkeypatch.setattr(twelvedata, "_spend", no_network)
    monkeypatch.setattr(discord, "send", no_network)
    monkeypatch.setattr(httpx.AsyncClient, "send", no_network)
    return tmp_path


def run(now: datetime = NOW) -> list[dict]:
    return asyncio.run(A.run(now))


def keys(found: list[dict]) -> set[str]:
    return {f["key"] for f in found}


def one(found: list[dict], key: str) -> dict:
    hits = [f for f in found if f["key"] == key]
    assert len(hits) == 1, (key, keys(found))
    return hits[0]


def source(name: str, fails: int, first_ago_s: int, last_ago_s: int = 30, code: int | None = 500,
           err: str = "HTTP 500") -> None:
    sources_health._state[name] = {
        "ok": 10, "erreurs": fails, "dernier_succes": iso(NOW - timedelta(seconds=first_ago_s + 60)),
        "derniere_erreur": iso(NOW - timedelta(seconds=last_ago_s)), "erreur": err, "code": code,
        "dernier_resultat": "erreur", "echecs_suite": fails, "premier_echec": iso(NOW - timedelta(seconds=first_ago_s)),
        "derniere_tentative": iso(NOW - timedelta(seconds=last_ago_s))}


# --- Base saine, forme des constats ---------------------------------------------------------------------

def test_base_saine_aucun_constat(env):
    assert run() == []
    v = A.view("all", NOW + timedelta(seconds=30))
    assert v["items"] == [] and v["counts"]["total"] == {"action": 0, "probleme": 0, "info": 0}
    assert json.loads((env / "attention.json").read_text(encoding="utf-8"))["last_check_at"] == iso(NOW)


def test_un_constat_ne_propose_que_la_liste_blanche():
    with pytest.raises(ValueError):
        A.item("x", "action", "commun", "t", "d", [A.act("Fermer", "close_trade")])
    with pytest.raises(ValueError):
        A.item("x", "action", "commun", "t", "d", [A.go("Ailleurs", "reglages")])
    with pytest.raises(ValueError):
        A.item("x", "action", "commun", "t", "d", [{"text": "deux", "cmd": "ls", "go": "sante"}])
    with pytest.raises(ValueError):
        A.item("x", "urgent", "commun", "t", "d")
    with pytest.raises(ValueError):
        A.item("x", "info", "mes-trades", "t", "d")
    f = A.item("x", "info", "commun", "t", "d", evidence={"erreur": "GET /quote?apikey=SECRET123 refusé",
                                                           "jeton": "Bearer abc.def"})
    assert "SECRET123" not in json.dumps(f) and "abc.def" not in json.dumps(f)
    assert f["steps"] == [{"text": "Rien à faire de ton côté : JARVIS reprend tout seul."}]


def test_commande_ssh_vers_le_serveur_de_deploy(env, monkeypatch):
    assert A.ssh_steps()[0]["cmd"] == "ssh root@2.31.19.106"
    monkeypatch.setenv("JARVIS_SSH_CIBLE", "root@mon-serveur")
    assert A.ssh_steps()[0]["cmd"] == "ssh root@mon-serveur"
    monkeypatch.setenv("JARVIS_SSH_CIBLE", "root@x; rm -rf /")
    assert A.ssh_steps()[0]["cmd"] == "ssh root@2.31.19.106"


# --- JARVIS, labo, serveur --------------------------------------------------------------------------------

def test_worker_du_labo_arrete(env):
    (env / "lab.heartbeat").unlink()
    found = run()
    f = one(found, "lab:down")
    assert f["kind"] == "action" and f["space"] == "commun"
    assert [s.get("cmd") for s in f["steps"] if s.get("cmd")][-1] == "docker compose up -d jarvis-lab"
    assert "grade:stale" not in keys(found)  # lab:down le dit déjà


def test_worker_du_labo_bloque(env):
    beat(env, NOW - timedelta(hours=1))
    f = one(run(), "lab:down")
    assert f["title"] == "Le worker du labo ne tourne plus" and f["evidence"]["âge (s)"] == 3600


def test_base_du_labo_introuvable(env):
    (env / "lab.db").unlink()
    assert one(run(), "lab:base")["kind"] == "probleme"


def test_garde_du_labo_1(env):
    (env / "lab.db").unlink()
    make_lab(env / "lab.db", garde={"etat": "change", "attendue": "abc", "calculee": "def", "le": iso(NOW)})
    f = one(run(), "lab:v1_pause")
    assert "abc" in f["detail"] and "def" in f["detail"]


def test_decisions_du_council_et_veto_sur_la_derniere(env):
    (env / "lab.db").unlink()
    ts = int((NOW - timedelta(hours=2)).timestamp())
    make_lab(env / "lab.db", events=[
        (1, iso(NOW - timedelta(hours=5)), ts - 3600, "council", None, "**Essai** de `rebond`"),
        (2, iso(NOW - timedelta(hours=2)), ts, "bascule", "v7", "Méthode v7 en place"),
        (3, iso(NOW - timedelta(days=5)), ts - 5 * 86400, "bascule", "v6", "trop vieux"),
    ], promoted={"v7": "council", "v6": "simao"})
    found = run()
    assert {"council:decision:1", "council:decision:2"} <= keys(found) and "council:decision:3" not in keys(found)
    assert one(found, "council:decision:2")["title"] == "Le council a mis en place la méthode v7"
    assert [s.get("action") for s in one(found, "council:decision:2")["steps"] if s.get("action")] == ["veto"]
    assert not [s for s in one(found, "council:decision:1")["steps"] if s.get("action")]
    assert one(found, "council:decision:1")["detail"] == "Essai de rebond"
    # Après un veto, plus de bouton
    conn = sqlite3.connect(env / "lab.db")
    conn.execute("INSERT INTO lab_events (id, created_at, ts, kind, version, text) VALUES (4, ?, ?, 'veto', NULL, 'x')",
                 (iso(NOW), ts + 60))
    conn.commit()
    conn.close()
    found = run()
    assert not [s for s in one(found, "council:decision:2")["steps"] if s.get("action")]


def test_promotion_approuvee_par_simao_sans_veto(env):
    (env / "lab.db").unlink()
    ts = int((NOW - timedelta(hours=2)).timestamp())
    make_lab(env / "lab.db", events=[
        (1, iso(NOW - timedelta(hours=5)), ts - 3600, "council", None, "Essai de rebond"),
        (2, iso(NOW - timedelta(hours=2)), ts, "bascule", "v7", "Méthode v7 en place (promotion approuvée par Simão)"),
    ], promoted={"v7": "simao"})
    found = run()
    assert one(found, "council:decision:2")["title"] == "Méthode v7 en place (promotion que tu as approuvée)"
    assert not [s for f in found for s in f["steps"] if s.get("action")]  # le veto viserait autre chose


def test_boucle_du_suivi_bloquee_avec_un_trade_ouvert(env):
    scheduler.book.trades.append(trade(market="crypto", symbol="BTC"))
    scheduler.last_runs[API.TRACKING_LOOP]["at"] = iso(NOW - timedelta(minutes=10))
    f = one(run(), "loop:stuck:suivi-des-trades-et-cockpit-discord")
    assert f["kind"] == "action" and f["space"] == "trades"
    assert "docker compose restart jarvis" in [s.get("cmd") for s in f["steps"]]


def test_boucle_bloquee_sans_trade_et_boucle_qui_echoue(env):
    scheduler.last_runs[API.TRACKING_LOOP]["at"] = iso(NOW - timedelta(minutes=10))
    scheduler.last_runs["scanner pré-ouverture"] |= {"ok": False, "erreur": "KeyError", "echecs_suite": 3}
    found = run()
    assert one(found, "loop:stuck:suivi-des-trades-et-cockpit-discord")["kind"] == "probleme"
    f = one(found, "loop:failing:scanner-pre-ouverture")
    assert "KeyError" in f["detail"] and f["evidence"]["échecs d'affilée"] == 3
    scheduler.last_runs["scanner pré-ouverture"] |= {"echecs_suite": 2}
    assert "loop:failing:scanner-pre-ouverture" not in keys(run())


def test_passage_long_n_est_pas_un_blocage(env, monkeypatch):
    name = "scanner pré-ouverture"
    scheduler.last_runs[name] |= {"at": iso(NOW - timedelta(minutes=4)), "duree_s": 150.0}  # fini il y a 1 min 30
    monkeypatch.setattr(scheduler, "running", {API.TRACKING_LOOP: iso(NOW - timedelta(minutes=10))})
    scheduler.last_runs[API.TRACKING_LOOP]["at"] = iso(NOW - timedelta(minutes=11))
    assert run() == []  # passage en cours depuis 10 min : on attend
    scheduler.running[API.TRACKING_LOOP] = iso(NOW - timedelta(minutes=20))
    f = one(run(), "loop:stuck:suivi-des-trades-et-cockpit-discord")
    assert f["detail"].startswith("Un passage dure depuis 20 min")


def test_delai_de_grace_garde_les_constats_ouverts(env, monkeypatch):
    scheduler.last_runs[API.TRACKING_LOOP]["at"] = iso(NOW - timedelta(minutes=10))
    run()
    assert any(r["key"].startswith("loop:stuck") for r in A.book.open_rows())
    monkeypatch.setattr(A, "STARTED", NOW + timedelta(minutes=1))  # redémarrage : last_runs vide
    scheduler.last_runs.clear()
    later = NOW + timedelta(minutes=2)
    assert not [f for f in run(later) if f["key"].startswith("loop:")]
    assert any(r["key"].startswith("loop:stuck") for r in A.book.open_rows())  # ni fermé ni rouvert


def test_erreurs_repetees_hors_boucles(env):
    for i in range(3):
        API.recent_errors.items.append({"at": iso(NOW - timedelta(minutes=i)), "niveau": "ERROR",
                                        "module": "app.sources.finnhub", "message": f"panne {i}"})
        API.recent_errors.items.append({"at": iso(NOW - timedelta(minutes=i)), "niveau": "ERROR",
                                        "module": "app.scheduler", "message": "boucle"})
    API.recent_errors.items.append({"at": iso(NOW), "niveau": "WARNING", "module": "app.sources.kraken",
                                    "message": "lent"})
    found = run()
    assert keys(found) == {"errors:app-sources-finnhub"}


def test_disque(env, monkeypatch):
    monkeypatch.setattr("shutil.disk_usage", lambda folder: Usage(100e9, 80e9, 20e9))
    assert one(run(), "disk")["kind"] == "probleme"
    monkeypatch.setattr("shutil.disk_usage", lambda folder: Usage(100e9, 95e9, 5e9))
    f = one(run(), "disk")
    assert f["kind"] == "action" and "docker compose down -v" in " ".join(s["text"] for s in f["steps"])


# --- Sources, clés, quota ---------------------------------------------------------------------------------

def test_source_seule_en_panne(env):
    source("Finnhub", 4, first_ago_s=600)
    f = one(run(), "source:finnhub")
    assert f["kind"] == "probleme" and "catalyseur" in f["detail"]


def test_source_sans_constat_pendant_la_grace_ou_hors_appels(env):
    source("Finnhub", 4, first_ago_s=120)          # en panne depuis 2 min : trop tôt
    source("Twelve Data", 9, first_ago_s=8000, last_ago_s=7200)  # plus appelée depuis 2 h (hors séance)
    source("Kraken", 2, first_ago_s=900)            # 2 échecs seulement
    assert run() == []


def test_trois_sources_en_panne_un_seul_constat_reseau(env):
    for name in ("Finnhub", "Kraken", "SEC EDGAR"):
        source(name, 5, first_ago_s=900, err="erreur réseau (ConnectError)", code=None)
    found = run()
    assert keys(found) == {"internet"}


def test_refus_403_de_la_sec_ou_d_alpaca_n_est_pas_une_cle(env):
    source("SEC EDGAR", 5, first_ago_s=900, code=403, err="HTTP 403")
    source("Alpaca (données)", 5, first_ago_s=900, code=403, err="HTTP 403")
    found = run()
    assert keys(found) == {"source:sec-edgar", "source:alpaca-donnees"}


def test_cle_twelve_data_refusee_dans_une_reponse_200(env):
    text = twelvedata.refusal({"code": 401, "message": "**apikey** parameter is incorrect: apikey=td-cle",
                               "status": "error"})
    assert text.startswith("HTTP 401")
    for _ in range(2):
        sources_health.fail("https://api.twelvedata.com/quote?apikey=td-cle", SourceError(text), None)
    found = run()
    f = one(found, "auth:twelvedata-api-key")
    assert f["kind"] == "action" and "td-cle" not in json.dumps(found)
    assert "nano .env" in [s.get("cmd") for s in f["steps"]]
    assert "docker compose up -d --force-recreate" in [s.get("cmd") for s in f["steps"]]
    assert twelvedata.refusal({"code": 400, "message": "symbol XYZ not found", "status": "error"}) is None
    assert twelvedata.refusal({"code": 403, "message": "available starting with Grow", "status": "error"}) is None
    assert twelvedata.refusal({"close": "1.0"}) is None


def test_quota_twelve_data(env, monkeypatch):
    monkeypatch.setattr(twelvedata, "credits", {"day": "2026-10-28", "n": int(twelvedata.DAILY_CREDITS * 0.85)})
    assert one(run(), "quota:twelvedata")["kind"] == "info"
    source("Twelve Data", 3, first_ago_s=60, code=429, err="HTTP 429 crédits épuisés")
    found = run()
    assert one(found, "quota:twelvedata")["kind"] == "probleme" and "source:twelve-data" not in keys(found)


def test_cles_absentes(env, monkeypatch):
    monkeypatch.delenv("ALPACA_SECRET_KEY")
    monkeypatch.delenv("SEC_USER_AGENT")
    found = run()
    assert one(found, "key:missing:alpaca-api-key")["title"] == "ALPACA_API_KEY et ALPACA_SECRET_KEY absentes"
    assert one(found, "key:missing:sec-user-agent")["kind"] == "info"


# --- Discord ----------------------------------------------------------------------------------------------

def test_notifications_coupees_et_bot_desactive(env, monkeypatch):
    monkeypatch.setenv("JARVIS_NOTIFICATIONS", "off")
    monkeypatch.delenv("DISCORD_BOT_TOKEN")
    scheduler.last_runs.clear()
    scheduler.last_runs.update({name: {"at": iso(NOW), "ok": True, "every_s": every}
                                for name, every in A.expected_loops()})
    found = run()
    assert keys(found) == {"notifications:off", "discord:bot:off"}


def test_file_discord_bloquee(env):
    scheduler.outbox.items.append({"id": "x", "kind": "alerte", "key": None, "parts": ["a"], "sent": 0, "attempts": 7,
                                   "created_at": iso(NOW - timedelta(minutes=20)), "expires_at": None,
                                   "derniere_erreur": "HTTPStatusError: 503 [adresse masquée]"})
    f = one(run(), "discord:outbox")
    assert "503" in f["detail"] and "abc" not in json.dumps(f)


def test_webhook_supprime(env):
    async def gone(text):
        raise discord.DiscordError("HTTP 404 : Unknown Webhook", 0, 404)

    async def back(text):
        return 1

    scheduler.outbox.add("alerte")
    asyncio.run(scheduler.outbox.flush(gone, NOW - timedelta(minutes=1)))
    assert scheduler.outbox.items == []  # refus définitif : le message n'est pas gardé
    f = one(run(), "discord:webhook")
    assert f["kind"] == "action" and "Unknown Webhook" in f["detail"]
    scheduler.outbox.add("alerte")
    asyncio.run(scheduler.outbox.flush(back, NOW))
    assert run() == []


def test_bot_deconnecte_jeton_refuse(env):
    class LoginFailure(Exception):
        pass

    bot.note_error(LoginFailure("Improper token has been passed."))
    bot.status["depuis"] = iso(NOW - timedelta(minutes=10))
    assert keys(run()) == {"auth:discord"}
    bot.status.update(erreur_type="ConnectionClosed")
    assert keys(run()) == {"discord:bot"}
    bot.status.update(depuis=iso(NOW - timedelta(minutes=2)))  # moins de 5 min : on attend
    assert run() == []
    bot.note_connected(True)
    assert bot.status["connecte"] is True


# --- Routines et déploiement ------------------------------------------------------------------------------

def test_routines_du_soir_et_de_la_recherche(env, monkeypatch):
    scheduler.state.data["routines"] = {"soir": "2026-10-26"}  # mercredi 16 h Paris : la soirée du 27 manque
    scan = {"at": iso(NOW - timedelta(hours=2)), "jour": "2026-10-28", "picks": [{"symbol": "NVDA"}, {"symbol": "AMD"}]}
    monkeypatch.setattr(research, "last_scan", lambda path=None: scan)
    monkeypatch.setattr(research.ResearchBook, "day", lambda self, jour: [])
    found = run()
    assert keys(found) == {"routine:soir", "routine:recherche"}
    assert "2 valeurs" in one(found, "routine:recherche")["detail"]
    scheduler.state.data["routines"] = {"soir": "2026-10-27"}
    monkeypatch.setattr(research.ResearchBook, "day", lambda self, jour: [{"symbol": "NVDA"}])
    assert run() == []


def test_routine_du_soir_sans_repere(env):
    scheduler.state.data["routines"] = {}
    assert run() == []


def test_deploiement_en_attente(env, monkeypatch):
    prs = [{"numero": 80, "titre": "Rubrique À faire", "sha": "b" * 7}]
    monkeypatch.setattr(version, "snapshot", lambda: up_to_date(statut="ahead", commits_en_retard=2, pr_en_retard=prs,
                                                                code_touche=True))
    f = one(run(), "deploy:pending")
    assert f["kind"] == "action" and "#80 Rubrique À faire" in f["detail"]
    assert "git pull && docker compose up -d --build" in [s.get("cmd") for s in f["steps"]]
    monkeypatch.setattr(version, "snapshot", lambda: up_to_date(statut="ahead", commits_en_retard=1, pr_en_retard=prs,
                                                                code_touche=False))
    assert run() == []  # seulement des docs ou des tests : rien à reconstruire


def test_deploiement_pull_sans_reconstruction_et_inconnu(env, monkeypatch):
    monkeypatch.setattr(version, "snapshot", lambda: up_to_date(code="different", empreinte_disque="e2"))
    assert run() == []  # construction sans doute en cours : 10 min de grâce
    assert one(run(NOW + timedelta(minutes=11)), "deploy:rebuild")["kind"] == "action"
    monkeypatch.setattr(version, "snapshot", lambda: up_to_date(sur_le_disque=None, code=None,
                                                                erreur_disque="dépôt du serveur non monté"))
    assert "non monté" in one(run(), "deploy:unknown")["detail"]
    monkeypatch.setattr(version, "snapshot", lambda: up_to_date(erreur="HTTP 403 : limite de l'API GitHub atteinte"))
    assert one(run(), "deploy:unknown")["kind"] == "info"


def test_deploiement_pas_encore_verifie_garde_le_constat(env, monkeypatch):
    prs = [{"numero": 80, "titre": "x", "sha": "b" * 7}]
    monkeypatch.setattr(version, "snapshot", lambda: up_to_date(statut="ahead", commits_en_retard=1, pr_en_retard=prs,
                                                                code_touche=True))
    run()
    monkeypatch.setattr(version, "snapshot", lambda: up_to_date(sur_le_disque="c" * 40))  # pull : pas encore comparé
    run(NOW + timedelta(minutes=1))
    assert [r["key"] for r in A.book.open_rows()] == ["deploy:pending"]


def git_repo(root: Path, sha: str, packed: bool = False) -> None:
    g = root / ".git"
    (g / "refs" / "heads").mkdir(parents=True)
    (g / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (g / "config").write_text('[remote "origin"]\n\turl = https://jeton-secret@github.com/x/y\n', encoding="utf-8")
    if packed:
        (g / "packed-refs").write_text(f"# pack-refs with: peeled\n{sha} refs/heads/main\n", encoding="utf-8")
    else:
        (g / "refs" / "heads" / "main").write_text(sha + "\n", encoding="utf-8")
    (root / "app").mkdir()
    (root / "app" / "main.py").write_text("print('ok')\n", encoding="utf-8")


def test_version_lit_head_sans_git_ni_config(tmp_path):
    git_repo(tmp_path, "1" * 40)
    assert version.read_head(tmp_path / ".git") == {"sha": "1" * 40, "branche": "main"}
    other = tmp_path / "b"
    other.mkdir()
    git_repo(other, "2" * 40, packed=True)
    assert version.read_head(other / ".git")["sha"] == "2" * 40
    (other / ".git" / "HEAD").write_text("ref: refs/../../etc/passwd\n", encoding="utf-8")
    assert version.read_head(other / ".git")["erreur"] == "HEAD illisible"
    assert version.read_head(tmp_path / "absent")["sha"] is None


def test_version_compare_le_code_de_l_image_a_celui_du_disque(tmp_path, monkeypatch):
    git_repo(tmp_path, "1" * 40)
    monkeypatch.setenv("JARVIS_REPO_DIR", str(tmp_path))
    monkeypatch.setattr(version, "IMAGE_CODE", version.code_hash(tmp_path))
    (tmp_path / "app" / "__pycache__").mkdir()
    (tmp_path / "app" / "__pycache__" / "main.cpython-311.pyc").write_bytes(b"x")
    assert version.disk()["code"] == "identique"
    (tmp_path / "app" / "main.py").write_text("print('nouveau')\n", encoding="utf-8")  # git pull sans --build
    assert version.disk()["code"] == "different"


def test_version_compare_main_sur_github(tmp_path, monkeypatch):
    git_repo(tmp_path, "1" * 40)
    monkeypatch.setenv("JARVIS_REPO_DIR", str(tmp_path))
    monkeypatch.setattr(version, "IMAGE_CODE", version.code_hash(tmp_path))
    monkeypatch.setattr(version, "state", dict(version.state))
    monkeypatch.setenv("JARVIS_GITHUB_TOKEN", "")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "ahead", "ahead_by": 3, "files": [{"filename": "app/main.py"}],
                                         "commits": [
                                             {"sha": "c" * 40, "commit": {"message": "Ajout (#81)"}},
                                             {"sha": "d" * 40, "commit": {"message": "Merge pull request #80 from x/y\n\n"
                                                                                     "Rubrique À faire"}},
                                             {"sha": "e" * 40, "commit": {"message": "petit correctif"}}]})

    assert version.due(NOW)
    st = asyncio.run(version.check(httpx.MockTransport(handler), NOW))
    assert seen[0].url.path == f"/repos/simaoqu06-rgb/jarvis/compare/{'1' * 40}...main"
    assert "authorization" not in seen[0].headers
    assert [p["numero"] for p in st["en_retard"]] == [81, 80] and st["en_retard"][1]["titre"] == "Rubrique À faire"
    assert st["commits_en_retard"] == 3 and st["code_touche"] is True and st["main"] == "e" * 40
    assert not version.due(NOW + timedelta(minutes=10)) and version.due(NOW + timedelta(minutes=31))
    (tmp_path / ".git" / "refs" / "heads" / "main").write_text("9" * 40, encoding="utf-8")  # git pull
    assert version.due(NOW + timedelta(minutes=1))
    st = asyncio.run(version.check(httpx.MockTransport(lambda r: httpx.Response(404)), NOW))
    assert st["erreur"].startswith("HTTP 404")


def test_fichiers_qui_comptent_pour_jarvis():
    assert not version.touches_code([{"filename": "docs/CARTE-INTERFACE.md"}, {"filename": "tests/test_x.py"}])
    assert version.touches_code([{"filename": "docker-compose.yml"}])
    assert version.touches_code(None) and version.touches_code([{"filename": "README.md"}] * 300)


# --- Mes trades -------------------------------------------------------------------------------------------

def test_sortie_a_declarer_se_ferme_apres_sortie(env):
    t = trade(status="closed", events=[{"kind": "stop", "at": iso(NOW - timedelta(hours=1)), "price": 171.6}],
              targets_hit=0)
    scheduler.book.trades.append(t)
    f = one(run(), "exit:undeclared:t1")
    assert f["space"] == "trades" and "!sortie NVDA 171.6" in [s.get("cmd") for s in f["steps"]]
    T.record_exit(scheduler.book, "NVDA", 171.2)
    assert run() == []
    assert A.book.rows[0]["resolved_at"] == iso(NOW)


def test_sortie_trop_ancienne_ou_declaree(env):
    scheduler.book.trades.append(trade(status="closed", events=[
        {"kind": "tp2", "at": iso(NOW - timedelta(days=8)), "price": 189.0}]))
    scheduler.book.trades.append(trade(id="t2", status="closed", events=[
        {"kind": "close", "at": iso(NOW - timedelta(hours=1)), "price": 180.0}]))
    assert run() == []


def test_kill_encore_ouvert_apres_la_cloture(env):
    scheduler.book.trades.append(trade(killmode=True, created_at=iso(NOW - timedelta(days=1))))
    f = one(run(), "kill:open_after_close:t1")
    assert "!sortie NVDA 180.0" in [s.get("cmd") for s in f["steps"]]
    scheduler.book.trades[0]["created_at"] = iso(NOW - timedelta(hours=1))  # séance en cours
    assert "kill:open_after_close:t1" not in keys(run())


def test_cotation_d_action_hors_seance_ne_dit_rien(env):
    evening = datetime(2026, 10, 28, 22, 0, tzinfo=timezone.utc)
    beat(env, evening - timedelta(minutes=1))
    for r in scheduler.last_runs.values():
        r["at"] = iso(evening - timedelta(seconds=20))
    scheduler.book.trades.append(trade(last_check=int((evening - timedelta(hours=3)).timestamp()),
                                       targets=[{"name": "TP1", "price": 182.0, "eta_latest": None},
                                                {"name": "TP2", "price": 189.0, "eta_latest": None}]))
    assert run(evening) == []


def test_cotation_perimee_en_seance(env):
    scheduler.book.trades.append(trade(last_check=int((NOW - timedelta(minutes=30)).timestamp())))
    found = run()
    f = one(found, "quote:stale:trades")
    assert f["kind"] == "probleme" and f["evidence"]["valeurs"] == ["NVDA"]
    assert one(found, "tp:late:t1:tp1")["kind"] == "info"


def test_cotation_de_la_veille_a_l_ouverture(env, monkeypatch):
    opening = datetime(2026, 10, 28, 13, 32, tzinfo=timezone.utc)  # 9 h 32 à New York
    monkeypatch.setattr(A, "STARTED", opening - timedelta(hours=1))
    beat(env, opening)
    scheduler.book.trades.append(trade(last_check=int((opening - timedelta(hours=17)).timestamp()), targets=[]))
    for at, expected in ((opening, set()), (opening + timedelta(minutes=5), {"quote:stale:trades"})):
        for r in scheduler.last_runs.values():
            r["at"] = iso(at - timedelta(seconds=20))
        assert keys(run(at)) == expected


def test_conseil_et_signes_de_sortie(env):
    today = NOW.astimezone(A.NEW_YORK).date().isoformat()
    scheduler.book.trades.append(trade(exit_signals=["divergence"], conseils=[
        {"day": today, "decision": "SORTIR", "at": iso(NOW - timedelta(minutes=5)), "price": 180.0,
         "why": "**Résultats** ce soir", "exit_r": 0.6}], targets=[]))
    found = run()
    a = one(found, "advice:t1")
    assert a["title"] == "JARVIS conseille de sortir NVDA" and "Résultats ce soir" in a["detail"]
    assert "!sortie NVDA 180.0" in [s.get("cmd") for s in a["steps"]]
    assert "divergence RSI" in one(found, "exit:signals:t1")["detail"]


def test_arret_du_jour_et_risque_ouvert(env, monkeypatch):
    monkeypatch.setattr(API, "risk", lambda now: HEALTHY_RISK | {"r_du_jour_net": -3.2, "risque_ouvert_pct": 4.5})
    found = run()
    assert keys(found) == {"risk:day_stop", "risk:open"} and all(f["space"] == "trades" for f in found)


def test_grade_perime(env, monkeypatch):
    monkeypatch.setattr(API, "risk", lambda now: HEALTHY_RISK | {"grade_age_h": 9.0})
    assert one(run(), "grade:stale")["kind"] == "probleme"


def test_propositions_avec_boutons_de_la_liste_blanche(env):
    store = PR.proposals
    for i in range(10):
        store.items.append({"id": i + 1, "key": f"cle{i}", "value": 1, "current": 0, "why": "test",
                            "status": "pending", "created_at": iso(NOW), "decided_at": None})
    found = run()
    f = one(found, "proposal:1")
    assert [s.get("action") for s in f["steps"] if s.get("action")] == ["proposal_approve", "proposal_refuse"]
    assert f["steps"][0]["args"] == {"id": 1}
    assert len([x for x in found if x["key"].startswith("proposal:")]) == A.PROPOSALS_MAX
    assert "9, 10" in one(found, "proposals:more")["detail"]


# --- Moteur, synchronisation, stockage --------------------------------------------------------------------

def test_regle_qui_plante_garde_ses_constats(env, monkeypatch):
    (env / "lab.heartbeat").unlink()
    run()

    def boom(ctx, now):
        raise RuntimeError("bug")

    monkeypatch.setattr(A, "RULES", [(n, boom if n == "worker du labo" else r) for n, r in A.RULES])
    found = run(NOW + timedelta(minutes=1))
    assert one(found, "rule:worker-du-labo")["evidence"] == {"règle": "worker du labo", "erreur": "RuntimeError"}
    assert {r["key"] for r in A.book.open_rows()} == {"lab:down", "rule:worker-du-labo"}


def test_cycle_de_vie_recidive_rangement_et_purge(env):
    b = A.book
    f = A.item("disk", "probleme", "commun", "Disque", "plein") | {"rule": "disque"}
    b.sync([f], NOW)
    first = b.rows[0]
    assert first["first_seen"] == iso(NOW) and first["resolved_at"] is None
    assert b.ack(first["id"], True, NOW) is not None and b.ack(first["id"], True, NOW) is None  # déjà rangé
    b.sync([f], NOW + timedelta(minutes=5))
    assert A._row_view(first, NOW)["toujours_constate"] is True  # rangé, mais toujours constaté
    b.sync([], NOW + timedelta(minutes=6))
    assert first["resolved_at"] == iso(NOW + timedelta(minutes=6)) and b.ack(first["id"], False, NOW) is None
    b.sync([f], NOW + timedelta(minutes=7))  # récidive : nouvelle ligne
    assert [r["id"] for r in b.rows] == [1, 2] and b.rows[1]["ack_at"] is None
    b.sync([], NOW + timedelta(days=31))
    assert [r["id"] for r in b.rows] == [2]  # la ligne réglée il y a plus de 30 jours est partie


def test_vue_ordre_portees_historique_et_compteurs(env):
    b = A.book
    found = [A.item("a", "info", "paper", "a", "a"), A.item("b", "action", "commun", "b", "b"),
             A.item("c", "action", "trades", "c", "c"), A.item("d", "probleme", "commun", "d", "d")]
    b.sync([f | {"rule": "x"} for f in found], NOW)
    b.ack(b.rows[1]["id"], True, NOW)  # « b » rangé
    v = A.view("all", NOW + timedelta(seconds=30))
    assert [r["key"] for r in v["items"]] == ["c", "d", "a", "b"]
    assert v["counts"]["total"] == {"action": 1, "probleme": 1, "info": 1} and v["counts"]["trades"]["action"] == 1
    assert [r["key"] for r in A.view("commun", NOW)["items"]] == ["d", "b"]
    b.sync([], NOW + timedelta(hours=1))
    stored = lambda at: [r for r in A.view("all", at)["items"] if not r["live"]]  # noqa: E731
    assert len(stored(NOW + timedelta(hours=71))) == 4
    assert stored(NOW + timedelta(hours=74)) == []
    with pytest.raises(A.HTTPException):
        A.view("tout", NOW)
    s = A.summary(NOW + timedelta(seconds=30))
    assert s["top"] == [] and s["counts"]["total"]["action"] == 0


def test_moteur_arrete_constat_calcule_a_la_lecture(env):
    run()
    v = A.view("all", NOW + timedelta(minutes=10))
    assert v["items"][0]["key"] == "attention:stale" and v["items"][0]["live"] is True
    assert v["items"][0]["id"] is None
    assert A.view("all", NOW + timedelta(minutes=1))["items"] == []


def test_fichier_illisible_mis_de_cote(tmp_path, monkeypatch):
    p = tmp_path / "attention.json"
    p.write_text("{pas du json", encoding="utf-8")
    b = A.AttentionBook(str(p))
    assert b.rows == [] and b.load_error and (tmp_path / "attention.illisible").exists()
    monkeypatch.setattr(A, "book", b)
    monkeypatch.setattr(A, "STARTED", datetime.now(timezone.utc))
    assert "attention:file" in {r["key"] for r in A.view("all", datetime.now(timezone.utc))["items"]}


def test_stockage_relu_au_redemarrage(env):
    (env / "lab.heartbeat").unlink()
    run()
    again = A.AttentionBook(str(env / "attention.json"))
    assert [r["key"] for r in again.rows] == ["lab:down"] and again.next_id == 2 and again.last_check_at == iso(NOW)


def test_le_moteur_n_envoie_rien(env):
    (env / "lab.heartbeat").unlink()
    scheduler.book.trades.append(trade(last_check=int((NOW - timedelta(minutes=30)).timestamp())))
    source("Finnhub", 4, first_ago_s=600)
    assert run()
    assert scheduler.outbox.items == [] and bot.trade_texts.empty()


def test_la_boucle_du_moteur_ne_prend_pas_le_verrou(env):
    async def scenario():
        async with scheduler._lock:  # un job bloqué tient le verrou
            await asyncio.wait_for(scheduler._run(lambda: A.run(NOW), None), timeout=5)

    asyncio.run(scenario())
    assert A.book.last_check_at == iso(NOW)


# --- Routes -----------------------------------------------------------------------------------------------

@pytest.fixture
def client(env):
    from app import main

    return TestClient(main.app, base_url="https://testserver")


def csrf(client) -> str:
    r = client.get("/cockpit")
    return r.cookies.get("jarvis_csrf") or client.cookies.get("jarvis_csrf")


def test_routes_lecture_et_rangement(client, env):
    (env / "lab.heartbeat").unlink()
    run()
    r = client.get("/api/cockpit/attention?space=commun")
    assert r.status_code == 200
    row = next(i for i in r.json()["items"] if i["key"] == "lab:down")
    assert client.get("/api/cockpit/attention?space=x").status_code == 400
    url = f"/api/cockpit/attention/{row['id']}/ack"
    assert client.post(url).status_code == 403  # sans jeton CSRF
    tok = csrf(client)
    r = client.post(url, headers={"X-CSRF-Token": tok})
    assert r.status_code == 200 and r.json()["item"]["ack_at"]
    assert client.post(url, headers={"X-CSRF-Token": tok}).status_code == 404  # déjà rangé
    r = client.post(f"/api/cockpit/attention/{row['id']}/unack", headers={"X-CSRF-Token": tok})
    assert r.status_code == 200 and r.json()["item"]["ack_at"] is None
    assert client.post("/api/cockpit/attention/999/ack", headers={"X-CSRF-Token": tok}).status_code == 404
    r = client.post(url, headers={"X-CSRF-Token": tok, "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    journal = [json.loads(x) for x in open(ACT.ACTIONS_LOG, encoding="utf-8")]
    assert [(j["action"], j["status"]) for j in journal] == [
        ("attention_ack", 200), ("attention_ack", 404), ("attention_unack", 200), ("attention_ack", 404)]


def test_etat_du_cockpit_porte_les_compteurs(client, env):
    (env / "lab.heartbeat").unlink()
    run()
    body = client.get("/api/cockpit/state").json()
    assert body["attention"]["commun"]["action"] >= 1
    assert body["attention_top"][0]["key"] in ("lab:down", "attention:stale")
    health = client.get("/api/cockpit/health").json()
    assert health["bot"]["connecte"] is True and "jeton" not in json.dumps(health["bot"])
    assert health["page_a_faire"]["derniere_verification"] == iso(NOW)
    diag = client.get("/api/cockpit/diagnostics").json()
    assert "lab:down" in [r["key"] for r in diag["page_a_faire"]["ouverts"]] and "bot-jeton" not in json.dumps(diag)


# --- Instrumentations -------------------------------------------------------------------------------------

def test_sante_des_sources_succes_et_echec_dans_la_meme_seconde(monkeypatch):
    monkeypatch.setattr(sources_health, "_state", {})
    url = "https://finnhub.io/api/v1/news?token=fh-cle"
    sources_health.fail(url, SourceError("HTTP 503 token=fh-cle"), None)
    sources_health.fail(url, SourceError("HTTP 503"), None)
    s = sources_health.snapshot()["Finnhub"]
    assert s["statut"] == "erreur" and s["echecs_suite"] == 2 and s["code"] == 503 and "fh-cle" not in s["erreur"]
    first = s["premier_echec"]
    sources_health.ok(url, None)
    s = sources_health.snapshot()["Finnhub"]
    assert s["statut"] == "ok" and s["echecs_suite"] == 0 and s["premier_echec"] is None and first


def test_file_discord_garde_l_erreur_sans_l_adresse():
    class Down(Exception):
        pass

    async def send(text):
        raise Down("500 on https://discord.com/api/webhooks/1/secret-token")

    box = OB.Outbox(None)
    box.add("bonjour")
    asyncio.run(box.flush(send))
    assert box.items[0]["derniere_erreur"] == "Down: 500 on [adresse masquée]" and box.oldest() is box.items[0]


def test_scheduler_compte_les_echecs_d_affilee(monkeypatch):
    calls = []

    async def failing():
        calls.append(1)
        raise ValueError("x")

    async def scenario():
        task = asyncio.create_task(scheduler._loop("essai", failing, 0, None))
        while len(calls) < 3:
            await asyncio.sleep(0)
        task.cancel()

    monkeypatch.setattr(scheduler, "last_runs", {})
    monkeypatch.setattr(scheduler, "running", {})
    asyncio.run(scenario())
    assert scheduler.last_runs["essai"]["echecs_suite"] >= 2 and scheduler.last_runs["essai"]["ok"] is False
