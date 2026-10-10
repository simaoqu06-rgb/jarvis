"""Relecture de la fusion du deuxième tour d'avis (groupe W) : archive d'audit en échec visible (W1, W3), audit apparié
sans la séance du direct recalculée (W2), révisions du coût sans fausse alerte (W4, W7), rattrapage du rejeu et du
direct séparés dans !labo (W6), commande de secours de DEPLOY.md (W5)."""
import sqlite3

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
