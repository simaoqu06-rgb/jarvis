"""Labo, déclaration n° 9 : scanner « potentiel +10 » (listes S+10 et Rebond, quatre essais, mesures sans essai)."""
import json
from datetime import date

import pytest

from app.lab import collector as CO
from app.lab import plus10 as P10
from app.lab import sources as SRC
from app.lab import strat_stats as SS
from app.lab import strategies as ST
from app.lab.db import LabDB
from tests.test_killmode import RANGE, RUN, daily, hist
from tests.test_lab_strategies import DAY, OPEN, bars_from

FLAT_END = (114.7, 114.8, 114.6, 114.7)
# symbole : gap d'ouverture en % (journalier ajusté) ; 7 hausses ≥ 5 % pour avoir des rangs 6+
GAPS = {"UPA": 12.0, "UPB": 10.0, "UPC": 9.0, "UPD": 8.0, "UPE": 7.0, "UPF": 6.0, "UPG": 5.0, "CALM": 0.5,
        "DOWN": -6.0, "SPLT": 9.5}


def _pop(gap, atr=6.0, seances=40, scanner=False, up=1.0, down=-1.0, prev=100.0):
    return {"gap_pct": gap, "atr_pct": atr, "prev_close": prev, "seances": seances, "open": prev * (1 + gap / 100),
            "up_pct": up, "down_pct": down, "scanner": scanner}


def test_listes_figees_par_la_declaration():
    pop = {"A": _pop(5.0), "B": _pop(9.0), "C": _pop(9.0), "D": _pop(4.99), "E": _pop(20.0, atr=4.9),
           "F": _pop(-5.0), "G": _pop(-12.0), "H": _pop(-4.0)}
    lists = P10.ranked(pop)
    assert lists == {"s10": ["B", "C", "A"], "rebond": ["G", "F"]}  # égalité B/C par symbole ; E trop calme
    assert (P10.GAP_MIN, P10.ATR_MIN, P10.TOP, P10.DRAWS) == (5.0, 5.0, 5, 20)
    assert [t["strategy"] for t in ST.PLUS10_TRIALS] == list(P10.TRIALS) and len(ST.trials()) == 152
    # La n° 9 ne relançait pas le rattrapage du labo v2 (DECLARATION restait à 8) ; la n° 10 corrige la mesure
    # sans essai de plus : 152.
    assert ST.DECLARATION == 10 and ST.LATEST_DECLARATION == 10


def test_amplitude_par_groupe_et_par_prix():
    pop = {f"U{i}": _pop(10.0 - i, up=9.0 if i < 2 else 1.0) for i in range(7)}
    pop["P"] = _pop(0.0, up=11.0, down=-9.0, scanner=True, prev=3.0)
    amp = P10.amplitude(pop, P10.ranked(pop))
    assert amp["groupes"]["base"] == [8, 3, 1, 1]
    assert amp["groupes"]["s10_top5"] == [5, 2, 0, 0] and amp["groupes"]["s10_rang6"] == [1, 0, 0, 0]  # U6 : gap 4 %
    assert amp["groupes"]["filtres_1510"] == [1, 1, 1, 1]
    assert amp["par_prix"]["base"]["1-5 $"] == [1, 1, 1, 1] and amp["par_prix"]["s10"]["≥ 20 $"][0] == 6


def test_reste_apres_la_decision_et_heure_du_plus_haut():
    session = bars_from(OPEN, [(100, 110, 99, 101), (101, 102, 100, 101), (101, 102, 100, 100),
                               (100, 109, 99, 108), (108, 108, 90, 91)])
    up, down = P10.remaining(session, OPEN, 15)
    assert up == pytest.approx(9.0) and down == pytest.approx(-10.0)  # depuis la clôture de 9 h 40 (100)
    assert P10.high_bucket(session, OPEN) == "avant 9 h 45"   # le +10 % était fait avant l'entrée
    assert P10.remaining(session[:3], OPEN, 15) == (None, None)
    assert P10.high_bucket(session, OPEN, low=True) == "9 h 45 - 10 h"
    assert P10.first_touch(session) == "+8 % d'abord" and P10.first_touch(session[1:3]) == "aucun"
    assert P10.first_touch(bars_from(OPEN, [(100, 109, 91, 100)])) == "même bougie"


def _daily_with_day(gap):
    rows = daily()
    prev = rows[-1]["c"]
    o = prev * (1 + gap / 100)
    return rows + [{"t": CO.ny_midnight(DAY), "o": o, "h": o * 1.12, "l": o * 0.97, "c": o * 1.05, "v": 2e6}]


@pytest.fixture
def lab(monkeypatch):
    db = LabDB(":memory:")
    for symbol, gap in GAPS.items():
        db.put_bars("stock", symbol, 1440, _daily_with_day(gap))
    db.replace_universe("stock", DAY.isoformat(), [{"symbol": "UPA", "rank": 1, "reason": "volume_dollars"}])
    session = bars_from(OPEN, RANGE + RUN + [FLAT_END] * (78 - len(RANGE) - len(RUN)))
    calls = []

    async def fake_bars(symbols, tf, start, end, transport=None, adjustment="split"):
        calls.append((list(symbols), adjustment))
        out = {}
        for s in symbols:
            if s == "SPLT":  # regroupement entre la veille et le jour : la veille brute est à 10 $
                out[s] = [b | {"o": b["o"] / 10, "h": b["h"] / 10, "l": b["l"] / 10, "c": b["c"] / 10}
                          for b in hist()] + session
            elif s == "UPG":
                out[s] = hist()  # pas de séance : données manquantes
            else:
                out[s] = hist() + session
        return out

    async def no_inactive(*a, **k):
        return [{"symbol": "UPF"}]

    monkeypatch.setattr(SRC, "alpaca_bars", fake_bars)
    monkeypatch.setattr(SRC, "with_retry", no_inactive)
    db.calls = calls
    return db


async def test_seance_rejouee_sur_les_prix_negocies(lab, tmp_path):
    from datetime import datetime, timezone
    assert P10.ready_days(lab) == [DAY]
    assert await P10.step(lab, datetime(2026, 10, 9, 3, tzinfo=timezone.utc), 60) == 1
    assert all(adj == "raw" for _, adj in lab.calls)
    stats = lab.get(P10.JOB, DAY.isoformat())
    assert stats["listes"]["s10"] == ["UPA", "UPB", "SPLT", "UPC", "UPD", "UPE", "UPF", "UPG"]
    assert stats["listes"]["rebond"] == ["DOWN"]
    assert stats["operations"] == ["SPLT"]                       # gap brut ≠ gap ajusté : écarté des trades
    assert stats["manquantes"]["s10"]["rang6"] == {"≥ 20 $": 1}  # UPG sans bougies de 5 min
    assert stats["inactives"]["s10"] == {"top5": 0, "rang6": 1}
    rows = {(r[0], r[1]): r[2:] for r in lab.conn.execute(
        "SELECT symbol, essai, rang, triggered, r_net, r_3c, r_20e, temoin_r FROM plus10_trades")}
    assert {e for _, e in rows} == {"plus10_orb15", "plus10_kill", "rebond_orb15", "rebond_orb30"}
    assert ("SPLT", "plus10_orb15") not in rows and rows[("UPF", "plus10_orb15")][0] == 7
    lab_row = ST.strategy_rows(ST.Day("stock", DAY, CO.regular_hours(hist())[-ST.STOCK_WARMUP_BARS:]
                                      + bars_from(OPEN, RANGE + RUN + [FLAT_END] * 65),
                                      P10.scale(daily(), hist()[-1]["c"] / daily()[-1]["c"])), "UPA", names=["orb15"])
    assert rows[("UPA", "plus10_orb15")][1:4] == (1, lab_row[0]["r_net"], lab_row[0]["r_3c"])
    assert P10.ready_days(lab) == [] and await P10.step(lab, datetime(2026, 10, 9, 3, tzinfo=timezone.utc), 60) == 0

    journal = tmp_path / "plus10_scans.jsonl"
    journal.write_text(json.dumps({"kind": "confirmee", "day": DAY.isoformat(),
                                   "listes": {"s10": ["UPA", "UPB", "ZZZ"], "rebond": ["DOWN"]}}) + "\n")
    rep = P10.report(lab.conn, 152, journal)
    assert rep["seances"] == 1 and set(rep["essais"]) == set(P10.TRIALS)
    it = rep["essais"]["plus10_orb15"]
    assert it["signaux"] == 4 and it["declenches"] == 4 and it["nom"].startswith("S+10")  # SPLT écartée
    tir = it["classement"]["tirages"]
    assert tir["n"] == 20 and it["classement"]["rang6"]["n"] == 2   # UPE et UPF ; UPG sans données
    assert rep["essais"]["plus10_orb15"]["excursions"]["mfe_r"]["n"] == 4
    assert sum(rep["premier_touche"]["s10_top5"].values()) == 5      # SPLT compte : ses bougies sont là
    assert rep["amplitude"]["s10_top5"]["n"] == 5 and rep["amplitude"]["base"]["n"] == len(GAPS)
    assert rep["concordance_direct"] == {"seances": 1, "valeurs_du_labo": 6, "retrouvees": 3, "concordance_pct": 50.0}
    assert "Potentiel +10" in P10.text(rep)
    full = SS.report(lab.conn)
    assert full["declaration"]["numero"] == 10 and full["potentiel_plus10"]["seances"] == 1


def test_tirages_reproductibles():
    pool = [f"S{i}" for i in range(12)]
    assert P10.draws("2025-01-02", "s10", pool, 3) == P10.draws("2025-01-02", "s10", pool, 3)
    assert P10.draws("2025-01-02", "s10", pool, 3) != P10.draws("2025-01-02", "s10", pool, 4)
    assert len(P10.draws("2025-01-02", "s10", pool[:2], 0)) == 2 and P10.draws("x", "s10", [], 0) == []


def test_bloc_scelle_jamais_calcule(monkeypatch):
    from app.lab import seal
    monkeypatch.setattr(seal, "SEALED_FROM", date(2026, 4, 6))
    monkeypatch.setattr(seal, "SEALED_TO", date(2026, 10, 5))
    db = LabDB(":memory:")
    db.put_bars("stock", "UPA", 1440, _daily_with_day(9.0))
    db.replace_universe("stock", "2026-05-04", [{"symbol": "UPA", "rank": 1, "reason": "volume_dollars"}])
    db.replace_universe("stock", DAY.isoformat(), [{"symbol": "UPA", "rank": 1, "reason": "volume_dollars"}])
    assert P10.ready_days(db) == [DAY]
    assert date(2026, 5, 4) not in P10.ready_days(db)
