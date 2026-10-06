"""Accès sécurisé au labo : clé, lecture seule, file des règles, CSRF, route d'état inchangée."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import main
from app.lab import db as lab_db
from app.lab import stats as STATS
from app.lab import summary as SUM
from app.lab import validator as V
from app.lab.db import LabDB
from tests.lab_fixtures import synthetic_trades

KEY = "cle-de-test-123"


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "lab.db"
    monkeypatch.setenv("JARVIS_LAB_DB", str(path))
    monkeypatch.setenv("JARVIS_LAB_API_KEY", KEY)
    db = LabDB(str(path))
    synthetic_trades(db, n_days=10, per_day=5, seed=1)
    db.conn.execute("INSERT INTO catalysts (market, day, symbol, kind, direction, amplitude_pct, news, news_status, "
                    "created_at) VALUES ('stock', '2024-01-02', 'AAA', 'mouvement', 'hausse', 12.5, '[]', 'ok', 'x')")
    data = SUM.build(db.conn, "2024-01-15")
    SUM.store(db, data, SUM.text(data))
    return db, TestClient(main.app)


H = {"X-Lab-Key": KEY}


def test_desactive_sans_cle(env, monkeypatch):
    _, client = env
    monkeypatch.delenv("JARVIS_LAB_API_KEY")
    r = client.get("/api/lab/summary", headers=H)
    assert r.status_code == 503 and "désactivé" in r.json()["detail"]


@pytest.mark.parametrize("headers", [{}, {"X-Lab-Key": "mauvaise"}, {"Authorization": "Bearer mauvaise"},
                                     {"X-Lab-Key": KEY[:-1]}])
def test_cle_invalide(env, headers):
    _, client = env
    assert client.get("/api/lab/stats", headers=headers).status_code == 401


def test_cle_en_bearer(env):
    _, client = env
    assert client.get("/api/lab/summary", headers={"Authorization": f"Bearer {KEY}"}).status_code == 200


def test_route_d_etat_toujours_ouverte(env, monkeypatch):
    _, client = env
    monkeypatch.delenv("JARVIS_LAB_API_KEY")
    r = client.get("/api/lab/status")
    assert r.status_code == 200 and r.json()["exists"] and "labo2" in r.json()


def test_resume(env):
    _, client = env
    r = client.get("/api/lab/summary", headers=H).json()
    assert r["jour"] == "2024-01-15" and r["texte"].startswith("🧪") and r["resume"]["version"] == "v1"
    assert client.get("/api/lab/summary?day=2020-01-01", headers=H).status_code == 404
    assert client.get("/api/lab/summary?day=hier", headers=H).status_code == 422


def test_stats_cache_puis_filtres(env):
    db, client = env
    r = client.get("/api/lab/stats", headers=H).json()
    assert r["global"]["plans"] == 50 and "calcule_le" not in r  # pas de cache : calcul direct
    STATS.store(db.conn, "courant", {"global": {"plans": -1}}, 50, "2026-10-06T00:00:00+00:00")
    assert client.get("/api/lab/stats", headers=H).json()["global"]["plans"] == -1
    f = client.get("/api/lab/stats?market=stock&since=2024-01-05", headers=H).json()
    assert f["filtres"] == {"market": "stock", "since": "2024-01-05"} and f["global"]["plans"] == 35
    assert client.get("/api/lab/stats?market=forex", headers=H).status_code == 422


def test_fiches(env):
    _, client = env
    r = client.get("/api/lab/catalysts?symbol=aaa", headers=H).json()
    assert len(r) == 1 and r[0]["symbole"] == "AAA" and r[0]["type"] == "à classer"
    assert client.get("/api/lab/catalysts?kind=contre_exemple", headers=H).json() == []
    assert client.get("/api/lab/catalysts?limit=9999", headers=H).status_code == 422


def test_soumission_mise_en_file_seulement(env):
    db, client = env
    rule = {"nom": "test", "exclude": {"field": "context.signal", "op": ">", "value": 0.9}}
    r = client.post("/api/lab/rules", headers=H, json=rule)
    assert r.status_code == 200 and r.json()["etat"] == "en_file"
    assert V.counts(db.conn)["en_attente"] == 0  # le serveur n'écrit pas dans la base
    assert len(list(V.queue_dir(db.path).glob("*.json"))) == 1
    V.ingest_queue(db)
    listing = client.get("/api/lab/rules", headers=H).json()
    assert listing["compteurs"]["en_attente"] == 1 and listing["regles"][0]["nom"] == "test"
    assert listing["seuils"]["min_affected"] == 100
    again = client.post("/api/lab/rules", headers=H, json=rule).json()
    assert again["deja_connue"] is True and again["etat"] == "en_attente"


def test_soumission_illisible(env):
    _, client = env
    r = client.post("/api/lab/rules", headers=H, json={"exclude": {"field": "r_net", "op": ">", "value": 0}})
    assert r.status_code == 422 and "avenir" in r.json()["detail"]


def test_csrf_sans_origin_accepte_et_autre_site_refuse(env):
    _, client = env
    rule = {"exclude": {"field": "composite", "op": "<", "value": 3}}
    assert client.post("/api/lab/rules", headers=H, json=rule).status_code == 200  # client sans navigateur
    r = client.post("/api/lab/rules", headers=H | {"Origin": "https://mechant.example"}, json=rule)
    assert r.status_code == 403
    r = client.post("/api/lab/rules", headers=H | {"Sec-Fetch-Site": "cross-site"}, json=rule)
    assert r.status_code == 403


def test_base_absente(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_LAB_DB", str(tmp_path / "absente.db"))
    monkeypatch.setenv("JARVIS_LAB_API_KEY", KEY)
    client = TestClient(main.app)
    assert client.get("/api/lab/rules", headers=H).status_code == 503


def test_ancienne_base_sans_tables_du_labo_2(tmp_path, monkeypatch):
    path = tmp_path / "vieille.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE trades (id INTEGER)")
    conn.close()
    monkeypatch.setenv("JARVIS_LAB_DB", str(path))
    monkeypatch.setenv("JARVIS_LAB_API_KEY", KEY)
    client = TestClient(main.app, raise_server_exceptions=False)
    assert client.get("/api/lab/catalysts", headers=H).status_code == 503


def test_connexion_du_serveur_en_lecture_seule(env):
    db, _ = env
    conn = lab_db.open_readonly(db.path)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("INSERT INTO rules (hash, rule, status, submitted_at) VALUES ('x', '{}', 'en_attente', 'x')")
    # Le worker écrit pendant que le serveur lit (WAL) : la lecture voit l'état validé.
    db.conn.execute("INSERT INTO summaries VALUES ('2030-01-01', 'x', ?, 't')", (json.dumps({"version": None}),))
    assert SUM.latest(conn)["jour"] == "2030-01-01"
    conn.close()
