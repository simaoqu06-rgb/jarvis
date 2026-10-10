"""Labo : ce que coûte chaque filtre du scanner de 15 h 10 (filtres.py), sur une base SQLite factice."""
import json
from datetime import date, datetime, timezone

import pytest

from app.lab import collector as CO
from app.lab import filtres as FIL
from app.lab import seal
from app.lab.db import LabDB
from app.trading.settings import Settings

SESSIONS = [d for d in CO.trading_days(date(2026, 1, 2), date(2026, 6, 30))]
TRADE_DAYS = SESSIONS[-10:]


def series(base: float, monotonic: bool) -> list[dict]:
    """Journalier en hausse : en dents de scie (RSI ~ 55-60) ou sans un jour de baisse (RSI 100) ; gap de +3 %
    à chaque ouverture, volume constant (volume veille 1×) : en jeu par le gap seul."""
    rows, prev = [], base
    for k, d in enumerate(SESSIONS):
        c = base * (1.013 ** k if monotonic else 1.003 ** k * (1.012 if k % 2 else 0.988))
        o = prev * 1.03
        rows.append({"t": CO.ny_midnight(d), "o": o, "h": max(o, c) * 1.01, "l": min(o, c) * 0.99, "c": c, "v": 1e6})
        prev = c
    return rows


# symbole : (prix de départ, hausse sans repli, R nets des 10 trades, verdict)
BOOK = {"OK": (50.0, False, [0.5, 0.3, 0.4, 0.6, 0.2, 0.5, 0.3, 0.4, 0.6, 0.2], "GO"),
        "HOT": (50.0, True, [-1.0, -0.5, -0.8, -0.6, -1.0, -0.4, -0.9, -0.7, -0.5, -0.6], "GO"),
        "CHEAP": (2.0, False, [1.0, 0.8, 1.2, 0.9, 1.1, 1.0, 0.7, 1.3, 1.0, 1.0], "NO GO"),
        "BOTH": (0.5, True, [0.1] * 10, "NO GO")}


@pytest.fixture
def db():
    return fill(LabDB(":memory:"))


def fill(db: LabDB) -> LabDB:
    db.conn.execute("INSERT INTO method_versions (version, created_at, settings) VALUES "
                    "('v1', '2026-01-01T00:00:00+00:00', '{}')")
    rows = []
    for symbol, (base, mono, rs, decision) in BOOK.items():
        bars = series(base, mono)
        db.put_bars("stock", symbol, 1440, bars)
        for d, r in zip(TRADE_DAYS, rs):
            entry = bars[SESSIONS.index(d) - 1]["c"]  # plan v1 : dernier cours connu, la clôture de la veille
            rows.append(("replay", "v1", "stock", symbol, "long", d.isoformat(), CO.ny_midnight(d) + 9 * 3600 + 600,
                         json.dumps({"entry": round(entry, 2)}), "{}", "{}", 1, r, decision, 7.5 if decision == "GO" else 6.0))
    db.conn.executemany("INSERT INTO trades (origin, method_version, market, symbol, side, day, planned_at, plan, "
                        "context, result, triggered, r_net, decision, composite) VALUES "
                        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    # Bloc scellé : un trade énorme qui ne doit jamais compter, ni même être lu.
    db.conn.execute("INSERT INTO trades_sealed (origin, method_version, market, symbol, side, day, planned_at, plan, "
                    "triggered, r_net, decision) VALUES ('replay', 'v1', 'stock', 'OK', 'long', ?, 0, '{\"entry\": 50}', "
                    "1, 99.0, 'GO')", (TRADE_DAYS[0].isoformat(),))
    return db


def test_groupes_par_filtre_rate(db):
    s = Settings(None)
    seen: list[str] = []
    db.conn.set_trace_callback(seen.append)
    rep = FIL.report(db.conn, s)
    db.conn.set_trace_callback(None)
    assert not any("trades_sealed" in q for q in seen)  # le bloc scellé n'est jamais lu
    g = rep["groupes"]
    assert rep["trades"] == 40 and rep["version"] == "v1"
    assert g["passe tout"]["tous"]["n"] == 10 and g["passe tout"]["tous"]["esperance_r"] == pytest.approx(0.4)
    assert g["rate seulement RSI"]["tous"]["esperance_r"] == pytest.approx(-0.7)
    assert g["rate seulement RSI"]["go"]["n"] == 10 and g["rate seulement RSI"]["tous"]["gagnants"] == 0
    assert g["rate seulement prix"]["tous"]["esperance_r"] == pytest.approx(1.0) and g["rate seulement prix"]["go"]["n"] == 0
    assert g["rate 2 ou plus"]["tous"]["n"] == 10
    assert g["passe tout"]["tous"]["jours"] == 10 and g["passe tout"]["tous"]["ic95"][0] > 0
    assert g["historique court"]["tous"]["n"] == 0
    assert rep["par_prix"]["1-5 $"]["tous"]["n"] == 20 and rep["par_prix"]["≥ 20 $"]["tous"]["n"] == 20
    assert rep["reglages"]["scan_price_min"] == 5.0 and rep["limites"]
    # Un réglage qui retire le gap comme porte d'entrée : tout le monde rate « pas en jeu » en plus.
    s.update({"scan_gap_rvol_min": 1.5})
    g2 = FIL.report(db.conn, s)["groupes"]
    assert g2["rate seulement pas en jeu"]["tous"]["n"] == 10 and g2["passe tout"]["tous"]["n"] == 0


def test_jours_scelles_exclus(db, monkeypatch):
    monkeypatch.setattr(seal, "SEALED_FROM", TRADE_DAYS[0])
    monkeypatch.setattr(seal, "SEALED_TO", TRADE_DAYS[4])
    assert FIL.report(db.conn, Settings(None))["trades"] == 20


def test_historique_court(db):
    db.conn.execute("DELETE FROM bars WHERE symbol = 'OK' AND ts < ?", (CO.ny_midnight(SESSIONS[-40]),))
    g = FIL.report(db.conn, Settings(None))["groupes"]
    assert g["historique court"]["tous"]["n"] == 10 and g["passe tout"]["tous"]["n"] == 0


def test_cache_du_worker(db, monkeypatch):
    monkeypatch.setattr(FIL, "Settings", lambda: Settings(None))
    now = datetime(2026, 7, 1, 3, 0, tzinfo=timezone.utc)
    assert FIL.step(db, now, 3600) == 40
    assert FIL.cached(db.conn)["groupes"]["passe tout"]["tous"]["n"] == 10
    assert FIL.step(db, now, 3600) is None  # rien n'a changé
    later = datetime(2026, 7, 1, 4, 1, tzinfo=timezone.utc)
    assert FIL.step(db, later, 3600) is None  # vieux, mais mêmes trades
    db.conn.execute("UPDATE trades SET r_net = r_net - 1 WHERE symbol = 'OK'")  # R net recalculé sur place (cout.py)
    assert FIL.step(db, later, 3600) == 40
    assert FIL.cached(db.conn)["groupes"]["passe tout"]["tous"]["esperance_r"] == pytest.approx(-0.6)
    compact = FIL.compact(FIL.cached(db.conn))
    assert compact["groupes"]["rate seulement RSI"]["go_esperance_r"] == pytest.approx(-0.7)
    assert "historique court" not in compact["groupes"] and len(json.dumps(compact, ensure_ascii=False)) < 4000


def test_route_et_brief_du_soir(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main
    from app.lab import brief as BRIEF

    path = tmp_path / "lab.db"
    monkeypatch.setenv("JARVIS_LAB_DB", str(path))
    monkeypatch.setenv("JARVIS_LAB_API_KEY", "cle")
    monkeypatch.setattr(FIL, "Settings", lambda: Settings(None))
    db = fill(LabDB(str(path)))
    client = TestClient(main.app)
    assert client.get("/api/lab/filtres").status_code == 401
    r = client.get("/api/lab/filtres", headers={"X-Lab-Key": "cle"}).json()
    assert r["trades"] == 40 and "calcule_le" not in r  # pas encore de cache : calcul direct
    now = datetime(2026, 7, 1, 3, 0, tzinfo=timezone.utc)
    assert BRIEF.build(db.conn, now, str(path))["filtres_scanner"] is None
    FIL.step(db, now, 3600)
    r = client.get("/api/lab/filtres", headers={"X-Lab-Key": "cle"}).json()
    assert r["calcule_le"] and r["groupes"]["rate seulement RSI"]["tous"]["n"] == 10
    data = BRIEF.build(db.conn, now, str(path))
    assert data["filtres_scanner"]["groupes"]["passe tout"]["n"] == 10 and data["filtres_scanner"]["limites"]
    assert BRIEF.size(data) <= BRIEF.MAX_BYTES
