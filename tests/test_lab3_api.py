"""Labo 3 : routes de la routine du soir (notes, quota par soirée, types de catalyseurs, brief),
exposition par Tailscale Funnel, page Labo du cockpit, et parcours complet avec un faux conseil."""
import asyncio
import json
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import main, scheduler
from app.lab import brief as B
from app.lab import live as LIVE
from app.lab import method as M
from app.lab import morning as MO
from app.lab import summary as SUM
from app.lab import validator as V
from app.lab.db import LabDB
from app.notify import bot
from app.trading import proposals as PR
from app.trading.settings import Settings
from tests.lab_fixtures import insert_validated, synthetic_trades

KEY = "cle-de-test-labo3"
H = {"X-Lab-Key": KEY}
PARIS = ZoneInfo("Europe/Paris")
SIGNAL = {"field": "context.signal", "op": ">", "value": 0.8}


def paris(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=PARIS).astimezone(timezone.utc)


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "lab.db"
    monkeypatch.setenv("JARVIS_LAB_DB", str(path))
    monkeypatch.setenv("JARVIS_LAB_API_KEY", KEY)
    monkeypatch.delenv("JARVIS_LAB_CHAMPION", raising=False)
    monkeypatch.setattr(LIVE, "_cache", {"key": None, "data": None})
    db = LabDB(str(path))
    props = PR.Proposals(str(tmp_path / "p.json"), Settings(None))
    monkeypatch.setattr(PR, "proposals", props)
    monkeypatch.setattr(scheduler, "proposals", props)
    monkeypatch.setattr(MO, "state", MO.State(str(tmp_path / "matin.json")))
    return db, TestClient(main.app), props


def rule_body(i: int, **extra) -> dict:
    return {"nom": f"idée {i}", "exclude": {"field": "context.rvol", "op": ">", "value": i}} | extra


# --- Règles : notes et quota ------------------------------------------------------------------

def test_notes_du_conseil_gardees_avec_la_regle(env):
    db, client, _ = env
    r = client.post("/api/lab/rules", headers=H, json=rule_body(5, pourquoi="Volume extrême = fin de mouvement.",
                                                                 council="Analyste 1 : pour ; analyste 2 : contre."))
    assert r.status_code == 200 and r.json()["etat"] == "en_file" and r.json()["regles_restantes_ce_soir"] in (0, 1, 2)
    assert V.ingest_queue(db) == 1
    rule = V.get_rule(db.conn, r.json()["empreinte"])
    assert rule["pourquoi"] == "Volume extrême = fin de mouvement." and rule["council"].startswith("Analyste 1")
    assert rule["soiree"] == B.soiree(datetime.now(timezone.utc))
    # Les notes ne changent pas l'empreinte : la même règle sans notes est « déjà connue ».
    again = client.post("/api/lab/rules", headers=H, json=rule_body(5)).json()
    assert again["deja_connue"] is True


@pytest.mark.parametrize("extra", [{"pourquoi": "x" * 1001}, {"council": "y" * 2001}, {"pourquoi": 12}])
def test_notes_trop_longues_refusees(env, extra):
    _, client, _ = env
    assert client.post("/api/lab/rules", headers=H, json=rule_body(5, **extra)).status_code == 422


def test_trois_regles_par_soiree(env, tmp_path):
    db, client, _ = env
    db_file = str(tmp_path / "lab.db")
    evening = paris(2026, 10, 6, 23, 0)
    for i in range(3):
        main._lab_rule_enqueue(rule_body(i + 1), evening)
    main._lab_rule_enqueue(rule_body(2), paris(2026, 10, 7, 0, 30))  # même règle reposée après minuit : ne compte pas
    with pytest.raises(main.HTTPException) as exc:
        main._lab_rule_enqueue(rule_body(9), paris(2026, 10, 7, 0, 45))
    assert exc.value.status_code == 429 and "au plus 3" in exc.value.detail
    assert B.quota_used(db_file, evening) and len(B.quota_used(db_file, evening)) == 3
    main._lab_rule_enqueue(rule_body(9), paris(2026, 10, 7, 23, 0))  # soirée suivante
    assert len(list(V.queue_dir(db_file).glob("*.json"))) == 4


def test_quota_via_la_route(env):
    _, client, _ = env
    codes = [client.post("/api/lab/rules", headers=H, json=rule_body(i)).status_code for i in range(1, 6)]
    assert codes == [200, 200, 200, 429, 429]


def test_regle_illisible_ne_consomme_pas_de_place(env, tmp_path):
    _, client, _ = env
    assert client.post("/api/lab/rules", headers=H, json={"exclude": {"field": "r_net", "op": ">", "value": 0}}).status_code == 422
    assert B.quota_used(str(tmp_path / "lab.db"), datetime.now(timezone.utc)) == []


def test_file_pleine_rend_la_place(env, tmp_path, monkeypatch):
    _, client, _ = env
    monkeypatch.setattr(V, "QUEUE_MAX", 0)
    assert client.post("/api/lab/rules", headers=H, json=rule_body(1)).status_code == 429
    assert B.quota_used(str(tmp_path / "lab.db"), datetime.now(timezone.utc)) == []


# --- Types de catalyseurs ---------------------------------------------------------------------

def test_types_de_catalyseurs_mis_en_file(env):
    db, client, _ = env
    db.conn.execute("INSERT INTO catalysts (market, day, symbol, kind, news, news_status, created_at) "
                    "VALUES ('stock', '2026-10-06', 'AAA', 'mouvement', '[]', 'ok', 'x')")
    r = client.post("/api/lab/catalysts/types", headers=H, json=[{"id": 1, "type": "resultats"}])
    assert r.status_code == 200 and r.json() == {"en_file": 1}
    assert db.conn.execute("SELECT catalyst_type FROM catalysts").fetchone()[0] is None  # le serveur n'écrit pas
    M.ingest_types(db)
    assert db.conn.execute("SELECT catalyst_type FROM catalysts").fetchone()[0] == "resultats"


@pytest.mark.parametrize("body", [[{"id": 1, "type": "inconnu"}], [], [{"id": "1", "type": "macro"}], {"id": 1},
                                  [{"id": True, "type": "macro"}], [{"id": i, "type": "macro"} for i in range(201)]])
def test_types_invalides(env, body):
    _, client, _ = env
    assert client.post("/api/lab/catalysts/types", headers=H, json=body).status_code == 422


def test_types_sans_cle(env):
    _, client, _ = env
    assert client.post("/api/lab/catalysts/types", json=[{"id": 1, "type": "macro"}]).status_code == 401


# --- Brief et message du matin ----------------------------------------------------------------

def test_brief_en_un_appel(env):
    db, client, _ = env
    synthetic_trades(db, n_days=10, per_day=5)
    today = datetime.now(timezone.utc).date().isoformat()
    for i in range(120):
        db.conn.execute("INSERT INTO catalysts (market, day, symbol, kind, amplitude_pct, news, news_status, created_at) "
                        "VALUES ('stock', ?, ?, 'mouvement', 12, ?, 'ok', 'x')",
                        (today, f"S{i:03d}", json.dumps([{"titre": "t" * 300, "publie_a": "x", "position": "avant"}] * 5)))
    data = SUM.build(db.conn, "2024-01-15")
    SUM.store(db, data, SUM.text(data))
    insert_validated(db, "signal tendu", SIGNAL, tested_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    r = client.get("/api/lab/brief", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert len(r.content) <= B.MAX_BYTES
    assert {"resume", "stats", "fiches_a_classer", "regles_recentes", "methode", "types_permis",
            "regles_restantes_ce_soir"} <= set(body)
    assert body["fiches_a_classer"] and len(body["fiches_a_classer"][0]["actus"]) <= 3
    assert body["regles_recentes"][0]["nom"] == "signal tendu" and body["methode"]["champion"]["version"] == "v1"
    assert body["regles_restantes_ce_soir"] == 3 and body["message_prive"] == "rodage"
    assert client.get("/api/lab/brief").status_code == 401


def test_brief_ne_propose_que_des_fiches_classables(env):
    db, client, _ = env
    today = datetime.now(timezone.utc).date().isoformat()
    rows = [(today, "NEW", "a_chercher"), ("2025-03-03", "OLD", "ok"), ("2024-11-04", "DEEP", "hors_profondeur"),
            (today, "NONE", "aucune"), (today, "FRESH", "ok")]
    for day, symbol, status in rows:
        db.conn.execute("INSERT INTO catalysts (market, day, symbol, kind, amplitude_pct, news, news_status, created_at) "
                        "VALUES ('stock', ?, ?, 'mouvement', 12, '[]', ?, 'x')", (day, symbol, status))
    body = client.get("/api/lab/brief", headers=H).json()
    # Actus d'abord (récentes puis rejeu), puis « aucune actu trouvée » ; jamais une fiche sans actus encore.
    assert [f["symbole"] for f in body["fiches_a_classer"]] == ["FRESH", "OLD", "NONE"]
    page = client.get("/api/lab/catalysts", headers=H, params={"a_classer": "true", "limit": 200}).json()
    assert [f["symbole"] for f in page] == ["FRESH", "OLD"]  # la suite pour la routine : classables seulement


def test_message_du_matin_visible_par_l_api(env, monkeypatch):
    db, client, props = env
    monkeypatch.setenv("JARVIS_LAB_DM", "off")
    synthetic_trades(db, n_days=3, per_day=2)
    insert_validated(db, "signal tendu", SIGNAL)
    MO.tick(paris(2026, 10, 7, 8, 1), props, MO.shared_state(), True)
    r = client.get("/api/lab/matin", headers=H).json()
    assert r["message_prive"] == "rodage" and r["jours"][0]["etat"] == "journalise"
    assert "signal tendu" in r["jours"][0]["texte"] and r["propositions"][0]["kind"] == "labo"
    assert client.get("/api/lab/brief", headers=H).json()["point_du_matin"]["etat"] == "journalise"


# --- Exposition par Funnel ----------------------------------------------------------------------

@pytest.mark.parametrize("headers, code", [
    ({}, 200), ({"X-Forwarded-For": "100.101.102.103"}, 200), ({"X-Forwarded-For": "127.0.0.1"}, 200),
    ({"X-Forwarded-For": "203.0.113.9"}, 401), ({"Tailscale-Funnel-Request": "?1"}, 401),
    ({"X-Forwarded-For": "203.0.113.9", "X-Lab-Key": KEY}, 200), ({"X-Forwarded-For": "pas une ip"}, 401),
    ({"Host": "jarvis.tail1234.ts.net:8443"}, 401), ({"X-Forwarded-Host": "jarvis.tail1234.ts.net:10000"}, 401),
    ({"Host": "jarvis.tail1234.ts.net"}, 200),
])
def test_etat_protege_depuis_internet(env, headers, code):
    _, client, _ = env
    assert client.get("/api/lab/status", headers=headers).status_code == code


def test_page_labo_du_cockpit(env):
    db, client, props = env
    synthetic_trades(db, n_days=3, per_day=2)
    h = insert_validated(db, "signal <tendu>", SIGNAL)
    props.propose_lab(h, "signal <tendu>", "v1", "preuve", None, "2026-10-07")
    r = client.get("/cockpit/labo")
    assert r.status_code == 200 and "Labo de performance" in r.text and "Méthode" in r.text
    assert "signal &lt;tendu&gt;" in r.text and "<tendu>" not in r.text
    assert client.get("/cockpit/labo", headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 404
    assert 'href="/cockpit/labo"' in client.get("/cockpit").text


def test_page_labo_sans_base(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_LAB_DB", str(tmp_path / "absent.db"))
    r = TestClient(main.app).get("/cockpit/labo")
    assert r.status_code == 200 and "pas encore démarré" in r.text


# --- Parcours complet avec un faux conseil ------------------------------------------------------

class FakeUser:
    def __init__(self):
        self.sent = []

    async def send(self, text, view=None):
        self.sent.append((text, view))


def hidden(ctx, rnd):
    return -0.6 if ctx["signal"] > 0.8 else 0.0


async def _parcours(env, monkeypatch, live_effect, approve=True, live_days=8, per_day=30):
    db, client, props = env
    monkeypatch.setenv("JARVIS_LAB_DM", "on")
    monkeypatch.setattr(bot, "enabled", lambda: True)
    monkeypatch.setattr(bot, "lab_texts", asyncio.Queue())
    synthetic_trades(db, n_days=200, per_day=30, seed=12, effect=hidden)
    # 23 h : le conseil propose une règle (le quota du soir est compté sur l'horloge réelle).
    r = client.post("/api/lab/rules", headers=H, json={"nom": "signal tendu", "exclude": SIGNAL,
                                                       "pourquoi": "Un signal tendu annonce l'essoufflement.",
                                                       "council": "5 analystes sur 5 pour."})
    assert r.status_code == 200
    # Nuit : le worker lit la file (frontière : ce que le conseil a pu lire), puis le rejeu continue sur des
    # jours que personne n'a vus ; la règle n'est jugée que sur eux.
    V.ingest_queue(db)
    assert V.process_pending(db, 3)[0]["etat"] == "en_attente"
    synthetic_trades(db, n_days=120, per_day=30, seed=13, effect=hidden, first=date(2025, 1, 6))
    [rule] = V.process_pending(db, 3)
    assert rule["etat"] == "validee", rule["raisons"]
    # 8 h 05 : message du matin, une seule fois.
    morning = paris(2026, 10, 7, 8, 5)
    digest = await scheduler.tick_lab(morning)
    assert digest["etat"] == "a_envoyer"
    assert "« signal tendu »" in digest["texte"] and "Pourquoi : Un signal tendu" in digest["texte"]
    user = FakeUser()

    async def fetch(oid):
        return user
    assert await MO.send_due(MO.shared_state(), props, morning, {42}, fetch, lambda pids: f"boutons {pids}") == 1
    assert await scheduler.tick_lab(paris(2026, 10, 7, 8, 30)) is None
    assert await MO.send_due(MO.shared_state(), props, paris(2026, 10, 7, 8, 31), {42}, fetch, lambda p: None) == 0
    assert len(user.sent) == 1
    pid = digest["propositions"][0]
    # Décision de Simão, puis le worker.
    answer = await bot.handle(f"!{'oui' if approve else 'non'} {pid}")
    assert ("approuvée" if approve else "refusée") in answer
    M.step(db, morning)
    if not approve:
        return db, client, props, rule
    assert M.challenger(db.conn)["version"] == "v2" and M.champion(db.conn)["version"] == "v1"
    assert json.loads(M.champion_path(db.path).read_text())["challenger"] == "v2"
    # Trades en direct des jours suivants (mêmes plans pour les deux méthodes) ; regard à 20 puis 60 jours.
    synthetic_trades(db, n_days=live_days, per_day=per_day, seed=33, origin="live", first=date(2026, 10, 8),
                     effect=live_effect)
    report = M.step(db, paris(2027, 1, 4, 23, 0))
    return db, client, props, report


async def test_parcours_complet_jusqu_a_la_bascule(env, monkeypatch):
    db, client, props, report = await _parcours(env, monkeypatch, lambda c, r: -1.0 if c["signal"] > 0.8 else 0.1,
                                                live_days=20)
    # Au 1er regard (20 jours), la promotion est proposée à Simão : rien ne bascule sans lui.
    assert report["bascule"] == "promouvoir"
    assert M.champion(db.conn)["version"] == "v1" and M.challenger(db.conn)["version"] == "v2"
    monkeypatch.setattr(bot, "outbox", asyncio.Queue())
    await scheduler.tick_lab(paris(2027, 1, 4, 23, 5))
    offered = bot.outbox.get_nowait()
    assert offered["kind"] == "promotion" and offered["value"] == "v2" and bot.outbox.empty()
    assert bot.lab_texts.empty()
    await scheduler.tick_lab(paris(2027, 1, 4, 23, 6))
    assert bot.outbox.empty()  # proposée une seule fois
    page = client.get("/cockpit/labo").text
    assert "20 jours de trades en direct (prochain regard à 40 jours)" in page
    assert "Promotion proposée à Simão" in page and "méthode → v2" in page
    answer = await bot.handle(f"!oui {offered['id']}")
    assert "approuvée" in answer and M.champion(db.conn)["version"] == "v1"
    assert M.step(db, paris(2027, 1, 4, 23, 10))["decisions"] == 1
    assert M.champion(db.conn)["version"] == "v2"
    await scheduler.tick_lab(paris(2027, 1, 4, 23, 15))
    text = bot.lab_texts.get_nowait()
    assert text.startswith("🧪 Méthode v2 en place : « signal tendu »")
    # JARVIS en direct lit le nouveau champion et écarte comme le labo.
    champ = LIVE.current()
    assert champ["version"] == "v2"
    rows = [{"market": "stock", "day": "2026-10-21", "planned_at": 1_792_000_000, "context": {"signal": s}}
            for s in (0.95, 0.2)]
    assert [v["garde"] for v in LIVE.check(rows)] == [False, True]
    assert "Méthode en place v2" in (await bot.handle("!labo"))
    # Les idées suivantes se testent sur ce que garde v2 ; le brief le dit.
    assert client.get("/api/lab/brief", headers=H).json()["methode"]["champion"]["version"] == "v2"


async def test_parcours_jusqu_au_retrait(env, monkeypatch):
    db, client, props, report = await _parcours(env, monkeypatch, lambda c, r: 1.5 if c["signal"] > 0.8 else 0.0,
                                                live_days=60, per_day=10)
    assert report["bascule"] == "retirer"
    assert M.champion(db.conn)["version"] == "v1" and json.loads(M.champion_path(db.path).read_text())["regles"] == []
    await scheduler.tick_lab(paris(2027, 1, 4, 23, 5))
    assert "Méthode v2 retirée" in bot.lab_texts.get_nowait()


async def test_parcours_refus(env, monkeypatch):
    db, client, props, rule = await _parcours(env, monkeypatch, None, approve=False)
    r = V.get_rule(db.conn, rule["empreinte"])
    assert r["etat"] == "rejetee" and r["raisons"][0] == "refusée par Simão" and M.challenger(db.conn) is None
    again = client.post("/api/lab/rules", headers=H, json={"nom": "même idée", "exclude": SIGNAL}).json()
    assert again["deja_connue"] and again["etat"] == "rejetee"
    nxt = await scheduler.tick_lab(paris(2026, 10, 8, 8, 2))
    assert nxt["etat"] == "rien"  # jamais reproposée


@pytest.mark.parametrize("headers", [{"X-Forwarded-For": "203.0.113.9"}, {"Tailscale-Funnel-Request": "?1"},
                                     {"Host": "jarvis.tail1234.ts.net:8443"}])
def test_depuis_internet_rien_hors_de_api_lab(env, headers):
    """Relecture : si Funnel publiait plus que /api/lab, une requête d'Internet ne doit pouvoir ni
    approuver une proposition du labo (route sans clé), ni voir le cockpit."""
    db, client, props = env
    h = insert_validated(db, "signal tendu", SIGNAL)
    p = props.propose_lab(h, "signal tendu", "v1", "preuve", None, "2026-10-07")
    assert client.post(f"/api/trading/proposals/{p['id']}/approve", headers=headers).status_code == 404
    assert props.get(p["id"])["status"] == "pending"
    for path in ("/cockpit", "/api/trading/proposals", "/docs", "/openapi.json"):
        assert client.get(path, headers=headers).status_code == 404
    assert client.get("/api/lab/brief", headers=headers | H).status_code == 200
    # Depuis le réseau Tailscale, inchangé.
    assert client.get("/api/trading/proposals", headers={"X-Forwarded-For": "100.101.102.103"}).status_code == 200
