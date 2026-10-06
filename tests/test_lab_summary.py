"""Résumé du soir : moment de construction, contenu compact (< 8 Ko), rendu texte, stockage."""
import json
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.lab import stats as STATS
from app.lab import summary as SUM
from app.lab import validator as V
from app.lab.db import LabDB
from tests.lab_fixtures import synthetic_trades

PARIS = ZoneInfo("Europe/Paris")


@pytest.fixture
def db(tmp_path):
    return LabDB(str(tmp_path / "lab.db"))


def test_moment_du_resume():
    assert SUM.due(datetime(2026, 10, 6, 22, 0, tzinfo=PARIS), None) is None          # trop tôt
    assert SUM.due(datetime(2026, 10, 6, 22, 30, tzinfo=PARIS), None) == "2026-10-06"   # clôture + 30 min
    assert SUM.due(datetime(2026, 10, 6, 23, 50, tzinfo=PARIS), "2026-10-06") is None  # déjà fait
    assert SUM.due(datetime(2026, 10, 10, 22, 45, tzinfo=PARIS), "2026-10-09") == "2026-10-10"  # samedi : crypto
    assert SUM.due(datetime(2026, 11, 27, 22, 31, tzinfo=PARIS), None) == "2026-11-27"  # jour court
    # Semaine où New York est encore à l'heure d'été et Paris à l'heure d'hiver : clôture à 21 h à Paris.
    assert SUM.due(datetime(2026, 10, 27, 22, 30, tzinfo=PARIS), None) == "2026-10-27"


def test_jour_actions_retenu():
    assert SUM.stock_day(date(2026, 10, 10)) == date(2026, 10, 9)  # samedi → vendredi
    assert SUM.stock_day(date(2026, 10, 6)) == date(2026, 10, 6)


def _fill(db):
    synthetic_trades(db, n_days=60, per_day=20, seed=3, first=date(2026, 7, 13), effect=lambda c, r: (
        -0.9 if c["setup"] == "divergence" else 0.1))
    synthetic_trades(db, n_days=60, per_day=10, seed=4, market="crypto", first=date(2026, 8, 8))
    last = db.conn.execute("SELECT MAX(day) FROM trades WHERE market = 'stock'").fetchone()[0]
    for i in range(15):
        db.conn.execute("INSERT INTO day_moves (market, day, symbol, up_pct, gap_pct, t_high) VALUES "
                        "('stock', ?, ?, ?, 1.5, 1790000000)", (last, f"S{i:03d}", 8 + i))
    db.conn.execute("INSERT INTO catalysts (market, day, symbol, kind, news, news_status, created_at) VALUES "
                    "('stock', ?, 'S014', 'mouvement', ?, 'ok', 'x')",
                    (last, json.dumps([{"titre": "Résultat record " * 20, "publie_a": "2026-10-02T12:00+00:00",
                                        "position": "avant_ouverture", "source": "Reuters"}] * 5)))
    for i in range(40):
        db.conn.execute("INSERT INTO rules (hash, name, rule, status, reason, submitted_at) VALUES "
                        "(?, ?, ?, 'rejetee', ?, 'x')", (f"{i:064d}", "idée " * 10, json.dumps(
                            {"kind": "exclude", "cond": {"field": f"context.c{i}", "op": ">", "value": i}}),
                            json.dumps(["amélioration moyenne trop faible " * 3])))
    return last


def test_resume_compact_et_complet(db):
    last = _fill(db)
    data = SUM.build(db.conn, last)
    assert SUM.size(data) <= SUM.MAX_BYTES
    assert data["jour_chiffres"]["stock"]["jour"] == last and data["jour_chiffres"]["stock"]["declenches"] > 0
    assert set(data["cumul"]) == {"stock", "crypto"} and data["cumul"]["stock"]["ic95"]
    assert any(g["valeur"] == "divergence" for g in data["degradations"])
    movers = data["potentiel_10"]
    assert movers and movers[0]["symbole"] == "S014" and movers[0]["hausse_pct"] == 22.0
    assert movers[0]["plan"] and movers[0]["actus"] and movers[0]["type"] == "à classer"
    assert data["idees_rejetees"] and data["regles"]["rejetee"] == 40
    assert data["version"] == "v1"


def test_resume_tronque_si_trop_gros(db):
    last = _fill(db)
    data = SUM.build(db.conn, last)
    assert data.get("tronque") is True  # 40 idées de ~300 octets : on a dû couper
    small = SUM.shrink({"idees_rejetees": [], "potentiel_10": [], "degradations": [], "x": "a" * 100}, limit=50)
    assert small["x"] and "tronque" not in small  # rien à couper : on rend tel quel


def test_resume_texte(db):
    last = _fill(db)
    txt = SUM.text(SUM.build(db.conn, last))
    assert txt.startswith("🧪 Labo · résumé du") and "Actions" in txt and "Crypto" in txt
    assert "Potentiel +10 : S014 +22 %" in txt and "Règles : 0 en attente" in txt and "Se dégradent" in txt


def test_resume_reutilise_les_stats_fournies(db, monkeypatch):
    last = _fill(db)
    full = STATS.compute(db.conn, version="v1")
    calls = []
    real = STATS.compute
    monkeypatch.setattr(STATS, "compute", lambda conn, **kw: calls.append(kw) or real(conn, **kw))
    SUM.build(db.conn, last, full)
    assert all(kw.get("since") for kw in calls)  # seulement les chiffres du jour, pas le cumul


def test_stockage_et_lecture(db):
    last = _fill(db)
    data = SUM.build(db.conn, last)
    SUM.store(db, data, "texte")
    out = SUM.latest(db.conn)
    assert out["jour"] == last and out["texte"] == "texte" and out["resume"]["version"] == "v1"
    assert SUM.latest(db.conn, "1999-01-01") is None


def test_resume_base_vide(db):
    data = SUM.build(db.conn, "2026-10-06")
    assert data["version"] is None and data["potentiel_10"] == [] and data["cumul"] == {}
    assert "Règles" in SUM.text(data)


def test_compteurs_de_regles(db):
    V.submit(db, {"exclude": {"field": "composite", "op": "<", "value": 5}})
    assert V.counts(db.conn)["en_attente"] == 1
    assert SUM.build(db.conn, "2026-10-06")["regles"]["en_attente"] == 1


def test_heure_crypto_en_utc(db):
    synthetic_trades(db, n_days=3, per_day=2, seed=1, market="crypto", first=date(2026, 10, 4))
    db.conn.execute("INSERT INTO day_moves (market, day, symbol, up_pct, t_high) VALUES "
                    "('crypto', '2026-10-05', 'S000', 9, ?)", (int(datetime(2026, 10, 5, 13, 5, tzinfo=timezone.utc)
                                                                 .timestamp()),))
    movers = SUM.build(db.conn, "2026-10-06")["potentiel_10"]
    assert movers[0]["heure_haut"] == "13:05" and movers[0]["plan"]
