"""Relecture de la fusion du deuxième tour d'avis (groupe W) : archive d'audit en échec visible (W1, W3), audit apparié
sans la séance du direct recalculée (W2), révisions du coût sans fausse alerte (W4, W7), rattrapage du rejeu et du
direct séparés dans !labo (W6), commande de secours de DEPLOY.md (W5)."""
import sqlite3
from datetime import datetime, timezone

import pytest

from app.lab import audit as AUD
from app.lab.db import LabDB


def _row(symbol, r=0.5, strategy="v1", **kw):
    return {"symbol": symbol, "strategy": strategy, "slot": 0, "triggered": 1, "r_net": r, "entry": 10.0,
            "stop": 9.0} | kw


def _disque_plein(monkeypatch):
    """Disque plein réaliste (review/skep-deploiement/full_real.py) : l'archive ne peut plus grandir, SQLite renvoie
    SQLITE_FULL et annule lui-même la transaction."""
    copy = AUD._copy

    def full(conn, *a, **k):
        conn.execute(f"PRAGMA {AUD.ALIAS}.max_page_count = 4")
        return copy(conn, *a, **k)

    monkeypatch.setattr(AUD, "_copy", full)


def _anciennes(db, n=2000):
    """Lignes d'avant la n° 10 (declaration NULL) assez nombreuses pour dépasser 4 pages."""
    db.replace_strat("replay", "stock", "2025-03-10", [_row(f"S{i}") for i in range(n)])
    db.conn.execute("UPDATE strat_trades SET declaration = NULL")


# --- W3 : la vraie cause d'un échec arrive au journal ---------------------------------------------------------------

def test_transaction_annulee_par_sqlite_garde_la_vraie_cause():
    """SQLite annule seul la transaction sur certaines erreurs (disque plein) : le ROLLBACK de db.transaction levait
    « cannot rollback - no transaction is active » à la place de l'erreur d'origine."""
    db = LabDB(":memory:")
    with pytest.raises(ValueError, match="vraie cause"):
        with db.transaction():
            db.set("x", "a", 1)
            db.conn.execute("ROLLBACK")                       # ce que fait SQLite sur SQLITE_FULL
            raise ValueError("vraie cause")
    assert not db.conn.in_transaction and db.get("x", "a") is None
    with db.transaction():                                    # la connexion reste utilisable
        db.set("x", "b", 2)
    assert db.get("x", "b") == 2
    with pytest.raises(KeyError):                             # cas ordinaire : annulée par db.transaction
        with db.transaction():
            db.set("x", "c", 3)
            raise KeyError("c")
    assert db.get("x", "c") is None and not db.conn.in_transaction


def test_archive_sur_disque_plein_dit_disque_plein(tmp_path, monkeypatch):
    db = LabDB(str(tmp_path / "lab.db"))
    _anciennes(db)
    _disque_plein(monkeypatch)
    with pytest.raises(sqlite3.OperationalError, match="full"):
        AUD.step(db)
    assert [r[1] for r in db.conn.execute("PRAGMA database_list")] == ["main"]   # archive détachée
    assert not db.conn.in_transaction and not db.keys(AUD.JOB).get(f"strats:stock:d{AUD.declaration('stock')}")


def test_fichier_d_archive_vide_n_est_pas_un_audit(tmp_path, monkeypatch):
    """Archivage en échec : le fichier reste à 0 octet ; il était publié comme « Audit » (section de !strategies, route
    en 200) alors qu'il n'archive rien."""
    from fastapi.testclient import TestClient

    from app import main

    path = tmp_path / "lab.db"
    monkeypatch.setenv("JARVIS_LAB_DB", str(path))
    monkeypatch.setenv("JARVIS_LAB_API_KEY", "cle")
    db = LabDB(str(path))
    _anciennes(db)
    with monkeypatch.context() as m:
        _disque_plein(m)
        with pytest.raises(sqlite3.OperationalError):
            AUD.step(db)
    archive = AUD.archive_path(db.path, AUD.declaration("stock"))
    assert archive.exists() and archive.stat().st_size == 0
    assert AUD.report(db.conn) is None
    assert AUD.refresh(db, datetime(2026, 10, 11, 3, tzinfo=timezone.utc), 3600) == 0  # pas de cache
    assert AUD.cached(db.conn) is None and AUD.text(AUD.cached(db.conn)) is None
    client = TestClient(main.app)
    assert client.get("/api/lab/strategies/audit", headers={"X-Lab-Key": "cle"}).status_code == 404
    AUD.step(db)                                                    # place libérée : l'archivage passe
    assert AUD.report(db.conn)["archives"][archive.name]["tables"]


# --- W1 : archive d'audit en échec, dite dans !labo -------------------------------------------------------------------

def _passage_calme(monkeypatch):
    """Passage du worker sans réseau : grade, direct, Labo 2, rejeu et rattrapage neutralisés."""
    from app.lab import worker as W

    async def nothing(*a, **k):
        return {}

    monkeypatch.setattr(W.GRADE, "step", lambda db_, now: {})
    monkeypatch.setattr(W, "lab2", nothing)
    monkeypatch.setattr(W.R, "replay", nothing)
    monkeypatch.setattr(W.R, "run_live_day", nothing)
    monkeypatch.setattr(W.RAT, "step", nothing)
    monkeypatch.setattr(W.alpaca, "configured", lambda: True)
    return W


async def test_archive_en_echec_dite_dans_labo_jusqu_au_passage_qui_reussit(tmp_path, monkeypatch):
    """review/skep-cxd (S2) : archive en échec à chaque passage, le rattrapage n° 10, Killmode complet, la n° 9 et le
    recalcul du direct s'arrêtaient sans rien dans !labo ni /api/lab/status (le journal seul le disait)."""
    from datetime import timedelta

    from app.lab import status as STATUS
    from app.lab import strategies as ST

    W = _passage_calme(monkeypatch)
    path = str(tmp_path / "lab.db")
    db = LabDB(path)
    _anciennes(db)
    night = datetime(2026, 10, 11, 3, tzinfo=timezone.utc)            # dimanche : « nuit », séance US fermée
    with monkeypatch.context() as m:
        _disque_plein(m)
        for i in range(2):                                            # deux passages en échec : « depuis » le premier
            report = await W.tick(db, night + timedelta(minutes=10 * i), collect=True)
    assert report["archive_audit"] == "échec : OperationalError" and "rattrapage" not in report
    st = STATUS.status(path)
    assert st["archive_audit"] == {"etat": "echec", "raison": "OperationalError: database or disk is full",
                                   "depuis": "2026-10-11T03:00:00+00:00"}
    alert = (f"⚠️ Archive d'audit en échec depuis le 11/10/2026 à 03:00 UTC (OperationalError: database or disk is "
             f"full) : rattrapage de la n° {ST.LATEST_DECLARATION}, Killmode complet, n° 9 et recalcul du direct à "
             "l'arrêt, rien n'est réécrit (docker compose logs jarvis-lab)")
    assert alert in STATUS.text(st).splitlines()
    report = await W.tick(db, night + timedelta(minutes=30), collect=True)   # place libérée : l'archivage passe
    assert report["archive_audit"] and "rattrapage" in report
    st = STATUS.status(path)
    assert st["archive_audit"] is None and "Archive d'audit" not in STATUS.text(st)


def test_etat_d_archive_illisible_ou_sans_echec():
    from app.lab import status as STATUS

    assert STATUS.archive_line(None) is None and STATUS.archive_line({"etat": "faite"}) is None
    assert STATUS.archive_line({"etat": "echec", "raison": "OSError: x", "depuis": "pas une date"}) == (
        "⚠️ Archive d'audit en échec (OSError: x) : rattrapage, Killmode complet, n° 9 et recalcul du direct à l'arrêt, "
        "rien n'est réécrit (docker compose logs jarvis-lab)")
