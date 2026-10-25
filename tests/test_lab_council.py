"""Autonomie totale du council (choix de Simão du 25 octobre 2026) : règles approuvées et challenger promu sans lui,
veto `!veto`, et déclarations du council jugées sur les séances après leur dépôt."""
import json
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import main
from app.lab import council as CNC
from app.lab import method as M
from app.lab import strat_stats as SS
from app.lab import strategies as ST
from app.lab import validator as V
from app.lab.db import LabDB
from app.notify import bot
from tests.lab_fixtures import insert_validated, synthetic_trades

PARIS = ZoneInfo("Europe/Paris")
SIGNAL = {"field": "context.signal", "op": ">", "value": 0.8}
KEY = "cle-de-test-council"


def paris(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=PARIS).astimezone(timezone.utc)


@pytest.fixture
def autonome(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_LAB_AUTONOMIE", "totale")
    monkeypatch.delenv("JARVIS_LAB_CHAMPION", raising=False)
    monkeypatch.setattr(ST, "COUNCIL", {})
    return LabDB(str(tmp_path / "lab.db"))


def _events(db):
    return [t for (t,) in db.conn.execute("SELECT text FROM lab_events ORDER BY id")]


def test_le_council_approuve_promeut_et_simao_garde_un_veto(autonome):
    db = autonome
    synthetic_trades(db, n_days=200, per_day=30, seed=12, effect=lambda c, r: -0.6 if c["signal"] > 0.8 else 0.0)
    h = insert_validated(db, "signal tendu", SIGNAL)
    rep = M.step(db, paris(2026, 10, 7, 8, 5))
    # Pas de proposition à Simão : la règle part en essai tout de suite.
    assert rep["council"] == 1 and V.get_rule(db.conn, h)["decision"] == "en_essai"
    assert M.challenger(db.conn)["version"] == "v2"
    assert any("approuvée par le council" in t for t in _events(db))
    synthetic_trades(db, n_days=20, per_day=30, seed=33, origin="live", first=date(2026, 10, 8),
                     effect=lambda c, r: -1.0 if c["signal"] > 0.8 else 0.1)
    rep = M.step(db, paris(2027, 1, 4, 23, 0))
    assert rep["bascule"] == "promouvoir"
    assert M.champion(db.conn)["version"] == "v2" and M.challenger(db.conn) is None
    assert M.version_settings(db.conn, "v2")["promu_par"] == M.COUNCIL
    assert any("promue par le council" in t and "`!veto`" in t for t in _events(db))
    # Veto de Simão : v1 revient, la règle rejoint la mémoire des échecs.
    M.enqueue_veto(db.path)
    M.step(db, paris(2027, 1, 5, 9, 0))
    assert M.champion(db.conn)["version"] == "v1"
    rule = V.get_rule(db.conn, h)
    assert rule["etat"] == "rejetee" and M.VETO_REASON in rule["raisons"]
    assert "Veto de Simão : v1 remise en place" in _events(db)[-1]


def test_veto_retire_le_challenger_en_essai(autonome):
    db = autonome
    synthetic_trades(db, n_days=30, per_day=10, seed=3)
    h = insert_validated(db, "signal tendu", SIGNAL)
    M.step(db, paris(2026, 10, 7, 8, 5))
    assert M.challenger(db.conn)["version"] == "v2"
    text = M.apply_veto(db, paris(2026, 10, 7, 9, 0))
    assert "retirée" in text and M.challenger(db.conn) is None and M.champion(db.conn)["version"] == "v1"
    assert V.get_rule(db.conn, h)["etat"] == "rejetee"
    assert "rien à annuler" in M.apply_veto(db)


def test_sans_autonomie_rien_ne_part_sans_simao(autonome, monkeypatch):
    monkeypatch.setenv("JARVIS_LAB_AUTONOMIE", "off")
    db = autonome
    synthetic_trades(db, n_days=30, per_day=10, seed=3)
    h = insert_validated(db, "signal tendu", SIGNAL)
    assert M.step(db, paris(2026, 10, 7, 8, 5))["council"] == 0
    assert V.get_rule(db.conn, h)["decision"] is None and M.challenger(db.conn) is None


# --- Déclarations du council ---------------------------------------------------------------

def test_declaration_mal_formee_ou_contre_les_regles_de_simao():
    ok = {"nom": "ORB 30 sur gros gap", "market": "stock", "base": "orb30",
          "quand": {"field": "gap_open_pct", "op": ">=", "value": 4}}
    canon = CNC.parse(ok)
    assert canon["sortie"] == "r_net"
    assert CNC.decl_hash(canon) == CNC.decl_hash(CNC.parse(ok | {"nom": "même chose reformulée"}))
    for bad, why in ((ok | {"quand": {"field": "rank", "op": "<", "value": 50}}, "non permis"),
                     (ok | {"base": "go_confirme_v2"}, "base"), (ok | {"market": "crypto"}, "mesurée"),
                     (ok | {"sortie": "r_inconnue"}, "sortie"), ({k: v for k, v in ok.items() if k != "quand"}, "quand")):
        with pytest.raises(CNC.DeclarationError, match=why):
            CNC.parse(bad)


def _rows(db, days=40):
    rows = []
    for k in range(days):
        day = date.fromordinal(date(2025, 1, 6).toordinal() + k).isoformat()
        for j in range(4):
            big = j < 2  # deux signaux par jour sur un gros gap, qui gagnent ; les autres perdent
            r = 0.5 + 0.1 * ((j + k) % 3 - 1) if big else -0.4
            rows.append(("stock", f"S{j}", day, "orb30", r, r, -0.2 + 0.1 * ((j * 2 + k) % 3 - 1), 6.0 if big else 1.0))
    db.conn.executemany(
        "INSERT INTO strat_trades (origin, market, symbol, day, strategy, slot, triggered, r_net, r_sans_be, r_2r, "
        "r_suiveur, r_glisse_x2, r_glisse_x3, r_retard, r_manque, temoin_r, temoin_triggered, tp1_hit, r_3c, r_20e, "
        "gap_open_pct) VALUES ('replay', ?, ?, ?, ?, 0, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?, ?)",
        [(m, s, d, n, r, r2, r, r, r - 0.05, r - 0.1, r, r, w, r - 0.05, r - 0.1, g)
         for m, s, d, n, r, r2, w, g in rows])


def test_declaration_jugee_seulement_apres_son_depot(autonome):
    db = autonome
    _rows(db)
    before = len(ST.trials())
    out = CNC.enqueue(db.path, {"nom": "ORB 30 sur gros gap", "market": "stock", "base": "orb30",
                                "quand": {"field": "gap_open_pct", "op": ">=", "value": 4},
                                "pourquoi": "Les gros gaps tiennent mieux leur cassure.", "council": "3 voix sur 3."},
                      "2025-01-24")
    assert out["etat"] == "en_file"
    assert CNC.ingest(db, datetime(2025, 1, 25, 3, 0, tzinfo=timezone.utc)) == 1  # 24 janvier à New York
    [d] = CNC.load(db.conn)
    assert d["vu_jusqu_au"] == "2025-01-24" and d["strategie"] == "council_1"
    assert any("Déclaration du council n° 1" in t for t in _events(db))
    rep = SS.report(db.conn)
    assert rep["declaration"]["essais"] == before + 1 and rep["declaration"]["council"] == 1
    item = rep["strategies"]["stock"]["council_1"]
    assert item["nom"] == "Council n° 1 : ORB 30 sur gros gap"
    assert item["declenches"] == 80 and item["jours_vus_jusqu_au"] == "2025-01-24"
    assert item["jours_suivants"]["jours"] == len([k for k in range(40) if
                                                    date.fromordinal(date(2025, 1, 6).toordinal() + k)
                                                    > date(2025, 1, 24)])
    assert item["esperance_r"] > rep["strategies"]["stock"]["orb30"]["esperance_r"] + 0.3  # seuls les gros gaps
    # La même déclaration n'est jamais comptée deux fois, même reformulée ; la base l'a déjà.
    again = CNC.enqueue(db.path, {"nom": "autre nom", "market": "stock", "base": "orb30",
                                  "quand": {"field": "gap_open_pct", "op": ">=", "value": 4}}, "2025-01-25", db.conn)
    assert again["etat"] == "deja_declaree"


def test_garde_fou_du_soir(autonome):
    db = autonome
    for v in range(CNC.PER_EVENING):
        CNC.enqueue(db.path, {"nom": f"essai {v}", "market": "stock", "base": "orb15",
                              "quand": {"field": "atr_pct", "op": ">", "value": v}}, "2025-01-24", db.conn)
    with pytest.raises(CNC.DeclarationError, match="garde-fou"):
        CNC.enqueue(db.path, {"nom": "un de trop", "market": "stock", "base": "orb15",
                              "quand": {"field": "atr_pct", "op": ">", "value": 9}}, "2025-01-24", db.conn)


def test_routes_et_commande_veto(tmp_path, monkeypatch):
    path = tmp_path / "lab.db"
    monkeypatch.setenv("JARVIS_LAB_DB", str(path))
    monkeypatch.setenv("JARVIS_LAB_API_KEY", KEY)
    LabDB(str(path))
    client = TestClient(main.app)
    h = {"X-Lab-Key": KEY}
    info = client.get("/api/lab/declarations", headers=h).json()
    assert "gap_open_pct" in info["champs"] and "rank" not in info["champs"] and "orb30" in info["bases"]
    r = client.post("/api/lab/declarations", headers=h,
                    json={"nom": "ORB 30 sur gros gap", "market": "stock", "base": "orb30",
                          "quand": {"field": "gap_open_pct", "op": ">=", "value": 4}})
    assert r.status_code == 200 and r.json()["etat"] == "en_file"
    bad = client.post("/api/lab/declarations", headers=h, json={"nom": "liquidité", "market": "stock", "base": "orb30",
                                                                  "quand": {"field": "dollar_volume", "op": ">", "value": 1}})
    assert bad.status_code == 422
    assert client.post("/api/lab/declarations", json={}).status_code in (401, 403)
    import asyncio

    answer = asyncio.run(bot.handle("!veto"))
    assert "Veto déposé" in answer
    [f] = list((M.queue_root(str(path)) / "decisions").glob("veto-*.json"))
    assert json.loads(f.read_text())["veto"] is True
