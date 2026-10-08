"""Lot B (déclaration n° 4) : potentiel +10 par gap et par type de catalyseur, dérivés crypto (Bybit)."""
import json
from datetime import datetime, timezone

import httpx
import pytest

from app.lab import derivs as DER
from app.lab import potentiel as PO
from app.lab import sources as SRC
from app.lab import strat_stats as SS
from app.lab.db import LabDB


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_LAB_DB", str(tmp_path / "lab.db"))
    monkeypatch.delenv("JARVIS_LAB_POTENTIEL", raising=False)
    PO._cache.update(key=None, data=None)
    return LabDB(str(tmp_path / "lab.db"))


def add_moves(db, rows):
    """rows : (jour, symbole, gap %, hausse depuis l'ouverture %)."""
    db.conn.executemany("INSERT INTO day_moves (market, day, symbol, gap_pct, up_pct) VALUES ('stock', ?, ?, ?, ?)",
                        rows)


# --- Potentiel +10 ----------------------------------------------------------------------------

def test_tranches_de_gap():
    assert PO.gap_bucket(None) is None
    assert PO.gap_bucket(-6) == "gap ≤ −5 %" and PO.gap_bucket(0.0) == "gap ±0,5 %"
    assert PO.gap_bucket(2.0) == "gap +2 à +5 %" and PO.gap_bucket(4.99) == "gap +2 à +5 %"
    assert PO.gap_bucket(12) == "gap ≥ +10 %"


def test_part_des_seances_qui_touchent_plus_8_selon_le_gap(db):
    # 40 séances à gap +3 % : 10 touchent +8 %, dont 4 +10 % ; 20 séances à gap nul, aucune.
    rows = [(f"2025-01-{1 + i % 28:02d}", f"S{i}", 3.0, 12.0 if i < 4 else 9.0 if i < 10 else 2.0) for i in range(40)]
    rows += [(f"2025-02-{1 + i:02d}", f"Z{i}", 0.1, 1.0) for i in range(20)]
    add_moves(db, rows)
    rep = PO.report(db.conn)
    cell = rep["par_gap"]["gap +2 à +5 %"]
    assert cell == {"n": 40, "zone_pct": 25.0, "plus10_pct": 10.0, "hausse_mediane_pct": 2.0, "echantillon_faible": False}
    assert rep["par_gap"]["gap ±0,5 %"]["echantillon_faible"] is True
    assert rep["ensemble"]["n"] == 60 and rep["du"] == "2025-01-01" and rep["au"] == "2025-02-20"
    line = PO.hint(3.5, rep)
    assert line == ("🎯 Potentiel +10 : 25 % des séances à gap +2 à +5 % ont touché +8 % depuis l'ouverture "
                    "(10 % +10 %), sur 40 séances du labo")
    assert PO.hint(0.0, rep) is None and PO.hint(None, rep) is None   # moins de 30 séances, ou gap inconnu
    assert "25 % ≥ +8 %" in PO.text(rep) and "Aucun type encore" in PO.text(rep)


def test_types_de_catalyseurs_et_contre_exemples(db):
    ins = ("INSERT INTO catalysts (market, day, symbol, kind, direction, amplitude_pct, news_status, catalyst_type, "
           "created_at) VALUES ('stock', ?, ?, ?, ?, ?, ?, ?, 'x')")
    db.conn.executemany(ins, [
        ("2025-01-02", "A", "mouvement", "hausse", 12.0, "ok", "fda"),
        ("2025-01-03", "B", "mouvement", "baisse", -9.0, "ok", "fda"),
        ("2025-01-03", "C", "mouvement", "hausse", 20.0, "ok", "fda"),
        ("2025-01-04", "D", "contre_exemple", None, None, "ok", "fda"),
        ("2025-01-04", "E", "mouvement", "hausse", 8.5, "hors_profondeur", None),
    ])
    rep = PO.report(db.conn)
    assert rep["par_type"]["fda"] == {"hausse": 2, "baisse": 1, "contre_exemples": 1, "amplitude_mediane_pct": 12.0}
    assert rep["fiches"] == {"mouvements": 4, "classees": 3, "actus": {"ok": 3, "hors_profondeur": 1}}
    assert "fda : 2 hausses, 1 baisses · amplitude médiane 12 % · 1 contre-exemples" in PO.text(rep)


def test_le_worker_ecrit_le_fichier_lu_par_le_scanner(db):
    add_moves(db, [(f"2025-03-{1 + i % 28:02d}", f"S{i}", 6.0, 10.5) for i in range(30)])
    assert PO.step(db) == 30
    assert PO.step(db) is None            # rien de changé : pas de recalcul
    data = json.loads(PO.path().read_text(encoding="utf-8"))
    assert data["par_gap"]["gap +5 à +10 %"]["zone_pct"] == 100.0
    assert PO.cached(db.conn)["ensemble"]["n"] == 30
    assert PO.hint(7.0).startswith("🎯 Potentiel +10 : 100 % des séances à gap +5 à +10 %")


def test_scanner_sans_fichier_potentiel(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_LAB_POTENTIEL", str(tmp_path / "absent.json"))
    PO._cache.update(key=None, data=None)
    assert PO.current() is None and PO.hint(3.0) is None


# --- Dérivés crypto ---------------------------------------------------------------------------

def test_perpetuels_candidats_et_tranches():
    assert DER.candidates("PEPE-USDT") == ["PEPEUSDT", "1000PEPEUSDT"] and DER.candidates("BTC-USDC") == []
    assert DER.bucket(None, DER.FUNDING_BUCKETS) == "inconnu"
    assert DER.bucket(-0.002, DER.FUNDING_BUCKETS) == "funding < 0"
    assert DER.bucket(0.01, DER.FUNDING_BUCKETS) == "funding 0 à 0,01 %"     # taux neutre de Bybit
    assert DER.bucket(0.05, DER.FUNDING_BUCKETS) == "funding ≥ 0,03 %"
    assert DER.bucket(12.0, DER.OI_BUCKETS) == "OI 24 h ≥ +10 %"


def test_contexte_connu_au_signal_seulement():
    h = 3600
    series = {"BTC-USDT": {"funding": ([0, 8 * h, 16 * h], [0.0001, 0.0003, -0.0002]),
                           "oi": ([k * 4 * h for k in range(12)], [100.0 + 10 * k for k in range(12)])}}
    # Signal à 17 h : funding de 16 h (-0,02 %) ; dernier point d'OI clos = celui de 12 h (130), 24 h avant :
    # aucun point à -12 h, donc pas de variation.
    assert DER.context(series, "BTC-USDT", 17 * h) == (pytest.approx(-0.02), None)
    # Signal à 36 h : funding de 16 h ; OI de 32 h (180) contre 8 h (120) : +50 %.
    f, oi = DER.context(series, "BTC-USDT", 36 * h)
    assert f == pytest.approx(-0.02) and oi == pytest.approx(50.0)
    # Le point de 4 h qui commence à 32 h n'est pas encore clos à 35 h : on lit celui de 28 h (170 / 110).
    assert DER.context(series, "BTC-USDT", 35 * h)[1] == pytest.approx((170 / 110 - 1) * 100)
    assert DER.context(series, "ETH-USDT", 36 * h) == (None, None)


def bybit_handler(listed=("BTCUSDT",), calls=None):
    def handler(request):
        p = request.url.params
        if calls is not None:
            calls.append((request.url.path, p["symbol"]))
        if p["symbol"] not in listed:
            return httpx.Response(200, json={"retCode": 10001, "retMsg": "params error: symbol invalid"})
        start, end = int(p["startTime"]), int(p["endTime"])
        step = (8 if "funding" in request.url.path else 4) * 3600 * 1000
        key = "fundingRateTimestamp" if "funding" in request.url.path else "timestamp"
        ts = list(range(start - start % step + step, end + 1, step))[-SRC.BYBIT_PAGE:]
        rows = [{key: str(t), "fundingRate": "0.0001", "openInterest": str(1000 + t // step)} for t in ts]
        return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": {"list": rows[::-1]}})
    return httpx.MockTransport(handler)


async def test_collecte_des_derives_par_fenetres(db):
    db.replace_universe("crypto", "2026-09-20", [{"symbol": "BTC-USDT", "rank": 1, "reason": "x"},
                                                 {"symbol": "ZZZ-USDT", "rank": 2, "reason": "x"}])
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    calls = []
    out = await DER.collect(db, now, 50, transport=bybit_handler(calls=calls))
    assert db.get(DER.JOB, "map:BTC-USDT") == "BTCUSDT" and db.get(DER.JOB, "map:ZZZ-USDT") == ""
    n_f = db.conn.execute("SELECT COUNT(*) FROM derivs WHERE kind = 'funding'").fetchone()[0]
    n_oi = db.conn.execute("SELECT COUNT(*) FROM derivs WHERE kind = 'oi'").fetchone()[0]
    assert out["points"] == n_f + n_oi and n_f >= 57 and n_oi >= 115   # ~20 jours : 3 funding et 6 OI par jour
    end = int(now.timestamp())
    assert db.get(DER.JOB, "oi:BTC-USDT") == end and db.get(DER.JOB, "funding:BTC-USDT") == end
    again = await DER.collect(db, now, 50, transport=bybit_handler(calls=calls))
    assert again["appels"] == 0                                       # à jour : aucun appel
    assert ("/v5/market/funding/history", "1000ZZZUSDT") in calls      # petites unités essayées aussi


async def test_collecte_respecte_le_budget(db):
    db.replace_universe("crypto", "2025-01-01", [{"symbol": "BTC-USDT", "rank": 1, "reason": "x"}])
    out = await DER.collect(db, datetime(2026, 10, 8, tzinfo=timezone.utc), 5, transport=bybit_handler())
    assert out["appels"] == 5 and db.get(DER.JOB, "funding:BTC-USDT") < int(datetime(2026, 10, 8, tzinfo=timezone.utc).timestamp())


def test_tableau_des_strategies_par_tranche_de_derives(db):
    t0 = int(datetime(2025, 1, 10, tzinfo=timezone.utc).timestamp())
    db.conn.executemany("INSERT INTO derivs VALUES ('BTC-USDT', ?, ?, ?)",
                        [("funding", t0 - 3600, 0.0005)] + [("oi", t0 - k * 4 * 3600, 100.0 - k) for k in range(1, 10)])
    rows = []
    for i in range(40):
        rows.append(("replay", "crypto", "BTC-USDT" if i % 2 == 0 else "ETH-USDT", f"2025-01-{10 + i % 5:02d}",
                     "cassure_zone_retest_15m", i, t0, 100.0, 99.0, 1.0, 1, t0 + 300, 0.1))
    db.conn.executemany("INSERT INTO strat_trades (origin, market, symbol, day, strategy, slot, signal_at, entry, "
                        "stop, risk_pct, triggered, filled_at, r_net) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        rows)
    item = SS.report(db.conn)["strategies"]["crypto"]["cassure_zone_retest_15m"]
    assert item["par_funding"]["funding ≥ 0,03 %"]["n"] == 20 and item["par_funding"]["inconnu"]["n"] == 20
    # OI du dernier point clos (t0 - 4 h : 99) contre 24 h plus tôt (t0 - 28 h : 93) : +6,5 %.
    assert set(item["par_oi"]) == {"OI 24 h +3 à +10 %", "inconnu"} and item["par_oi"]["inconnu"]["n"] == 20
    assert item["nom"] == "Cassure d'une zone S/R puis retest, bougies de 15 min"
