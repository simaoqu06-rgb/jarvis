"""Labo v2, déclaration n° 6 : mesure de Killmode sans essai (colonnes, témoin 9 h 45-11 h, jours suivants,
robustesse dans « candidate », tranches, comparaisons avec orb15, couverture de l'univers)."""
from bisect import bisect_left
from datetime import date, datetime, timedelta

import pytest

from app.lab import collector as CO
from app.lab import strat_stats as SS
from app.lab import strategies as ST
from app.lab import universe as U
from app.lab.db import LabDB
from tests.test_killmode import RANGE, RUN, daily, day
from tests.test_lab_strategies import DAY, OPEN, ZERO


def rows_by_name(d):
    return {r["strategy"]: r for r in ST.strategy_rows(d, "AAA", 3, 5.0)}


def test_colonnes_atr_volume_et_stop_avant_entree():
    k = rows_by_name(day())["killmode"]
    assert k["atr_pct"] == pytest.approx(6.0, abs=0.01)
    assert k["vol15_adv"] == pytest.approx(0.003)            # 3 × 1 000 sur 1 000 000 par séance
    assert k["stop_avant_entree"] == 0
    dip = [(106.3, 106.4, 104.0, 104.3), (104.3, 105.5, 104.2, 105.4), (105.4, 106.6, 105.3, 106.55)]
    k = rows_by_name(day(RANGE + dip + RUN))["killmode"]     # stop balayé à 9 h 45, cassure à 9 h 55
    assert k["triggered"] == 1 and k["stop_avant_entree"] == 1
    inside = [(106.3, 106.6, 104.0, 106.5)]                    # stop touché dans la bougie d'exécution : inconnu
    assert rows_by_name(day(RANGE + inside + RUN))["killmode"]["stop_avant_entree"] == 0
    d = day()
    plan = ST._plan(106.0, 104.0, [108.0, 110.0, 112.0])
    v1 = ST.v1_row(d, "AAA", plan, OPEN - 1200, OPEN + 3600, 106.0, ZERO)
    assert v1["atr_pct"] is not None and v1["vol15_adv"] is None and v1["stop_avant_entree"] is None
    crypto = ST._row(ST.Day("crypto", DAY, d.bars, daily()), "BTC", "pullback", 0, plan, OPEN, {}, {}, None, None)
    assert crypto["atr_pct"] is None and crypto["vol15_adv"] is None


def test_vol15_inconnu_sans_historique():
    assert ST.kill_context(day(d=daily(n=19)))["vol15_adv"] is None
    assert ST.kill_context(day(RANGE[:2]))["vol15_adv"] is None   # range de 15 min pas encore complet


def test_temoin_de_killmode_pendant_la_vie_de_son_ordre(monkeypatch):
    windows = {}
    real = ST.witness

    def spy(d, key, risk_pct, multiples, cost_of, window):
        windows[key[3]] = window
        return real(d, key, risk_pct, multiples, cost_of, window)

    monkeypatch.setattr(ST, "witness", spy)
    d = day(RANGE + RUN + [(114.7, 114.9, 114.5, 114.7)] * 40)   # séance jusqu'après 11 h
    rows_by_name(d)
    until = int(datetime.combine(DAY, ST.KILL_UNTIL, CO.NEW_YORK).timestamp())
    assert windows["killmode"] == (d.first, bisect_left(d.t, until))
    assert windows["killmode"][1] - windows["killmode"][0] == 15   # 9 h 45 à 10 h 55
    assert windows["orb15"] == (d.first, d.last)


def _days(start: str, n: int) -> list[str]:
    d0 = date.fromisoformat(start)
    return [(d0 + timedelta(days=k)).isoformat() for k in range(n)]


def _insert(db, rows):
    """rows : (stratégie, symbole, jour, déclenchée, r_net, témoin, extra) ; les tests de robustesse valent r_net
    sauf indication dans extra."""
    cols = ("market", "symbol", "day", "strategy", "slot", "triggered", "r_net", "r_gross", "temoin_r",
            "temoin_triggered", "tp1_hit", "r_glisse_x2", "r_glisse_x3", "r_retard", "r_manque", "gap_open_pct",
            "atr_pct", "vol15_adv", "stop_avant_entree", "risk_pct")
    data = []
    for strategy, symbol, day_, trig, r, wit, extra in rows:
        row = {"market": "stock", "symbol": symbol, "day": day_, "strategy": strategy, "slot": 0, "triggered": trig,
               "r_net": r, "r_gross": r, "temoin_r": wit, "temoin_triggered": int(wit is not None), "tp1_hit": 0,
               "r_glisse_x2": r, "r_glisse_x3": r, "r_retard": r, "r_manque": r, "gap_open_pct": None,
               "atr_pct": None, "vol15_adv": None, "stop_avant_entree": None, "risk_pct": None, **extra}
        data.append([row[c] for c in cols])
    db.conn.executemany(f"INSERT INTO strat_trades (origin, {', '.join(cols)}) VALUES ('replay', "
                        f"{', '.join('?' * len(cols))})", data)


def _good(strategy, days, extra=None, shift=0.0):
    return [(strategy, f"S{j}", d, 1, 0.5 + shift + 0.01 * (k % 3) + 0.01 * j, 0.0, extra or {})
            for k, d in enumerate(days) for j in range(2)]


def test_jours_suivants_et_robustesse_dans_candidate():
    db = LabDB(":memory:")
    seen = _days("2024-12-01", 40)                    # avant le 27 janvier 2025
    after = _days("2025-02-03", 40)
    _insert(db, _good("orb15", seen) + _good("killmode", seen) + _good("pullback", seen, {"r_glisse_x3": -0.1})
            + _good("orb30", seen[:20], shift=0.0) + _good("orb30", seen[20:], shift=-1.0)
            + _good("cassure_retest", seen, {"r_retard": None}))
    items = SS.report(db.conn)["strategies"]["stock"]
    assert items["orb15"]["candidate"] and items["orb15"]["robuste"]["tient"]
    k = items["killmode"]
    assert k["jours_vus_jusqu_au"] == "2025-01-27" and k["jours_suivants"] is None and not k["candidate"]
    assert not items["pullback"]["candidate"] and items["pullback"]["robuste"]["r_glisse_x3"] < 0
    halves = items["orb30"]["robuste"]
    assert halves["moitie_1"] > 0 > halves["moitie_2"] and not items["orb30"]["candidate"]
    assert items["cassure_retest"]["robuste"]["r_retard"] is None and not items["cassure_retest"]["candidate"]
    _insert(db, _good("killmode", after))
    k = SS.report(db.conn)["strategies"]["stock"]["killmode"]
    sub = k["jours_suivants"]
    assert sub["declenches"] == 80 and sub["jours"] == 40 and sub["candidate"] and k["candidate"]
    assert k["declenches"] == 160 and set(sub) >= {"esperance_ibkr_r", "ic95_ibkr", "temoin", "echantillon_faible"}
    text = SS.text(SS.report(db.conn))
    assert "jugée après le 27/01/2025 : +0,5" in text and "robuste (glissement ×2 et ×3" in text


def test_tranches_et_comparaisons_avec_orb15():
    db = LabDB(":memory:")
    days = _days("2025-03-03", 30)
    rows = []
    for k, d in enumerate(days):
        strong = {"gap_open_pct": 6.0, "atr_pct": 5.0, "vol15_adv": 0.6, "risk_pct": 2.0, "stop_avant_entree": k % 2}
        weak = {"gap_open_pct": 1.0, "atr_pct": 3.0, "vol15_adv": 0.1, "risk_pct": 2.0, "stop_avant_entree": 0}
        rows += [("orb15", "FORT", d, 1, 0.4, 0.1, strong), ("killmode", "FORT", d, 1, 0.4, 0.2, strong),
                 ("orb15", "TARD", d, 1, -1.0 + 0.01 * (k % 3), 0.0, strong),
                 ("killmode", "TARD", d, 0, None, None, strong),
                 ("orb15", "SANS", d, 1, 0.1 + 0.01 * (k % 2), 0.0, strong),
                 ("orb15", "CALME", d, 1, -0.2 + 0.01 * (k % 3), 0.0, weak)]
    _insert(db, rows)
    rep = SS.report(db.conn)
    item = rep["strategies"]["stock"]["orb15"]
    assert {b: v["n"] for b, v in item["tranches"]["gap"].items()} == {"gap +0,5 à +2 %": 30, "gap +5 à +10 %": 90}
    assert set(item["tranches"]["place_2r"]) == {"2 R entre 0,5 et 1 ATR", "2 R ≥ 1 ATR"}   # 2×2/5 = 0,8 ; 2×2/3
    assert item["tranches"]["stop_avant_entree"]["stop touché avant l'entrée"]["n"] == 45
    assert rep["strategies"]["stock"]["killmode"]["tranches"]["vol15"]["vol15 0,5-1"]["n"] == 30
    kill = rep["killmode"]
    assert kill["tranches"]["atr"]["ATR 4-6 %"]["n"] == 120       # orb15 (90) et killmode (30), v2 ensemble
    sel = kill["selection"]
    assert sel["gap_atr_forts"]["n"] == 90 and sel["autres"]["n"] == 30 and sel["ecart_r"] is not None
    cond = kill["conditions"]
    assert cond["avec_killmode"]["n"] == 60 and cond["sans"]["n"] == 30
    lim = kill["limite_11h"]                                   # TARD : orb15 exécutée après 11 h, Killmode non
    assert lim["n"] == 60 and lim["seances_differentes"] == 30
    assert lim["esperance_r"] == pytest.approx(sum(1.0 - 0.01 * (k % 3) for k in range(30)) / 60, abs=1e-4)
    assert "Killmode décomposé (n° 6, mesure)" in SS.text(rep)
    assert rep["strategies"]["stock"]["orb15"]["par_rang"]["rang inconnu"]["n"] == 120


def _daily_bars(day_: date, closes: list[float], gap_open: float | None, half_range: float) -> list[dict]:
    """Journalier des séances avant `day_` (une par jour ouvré) puis, si gap_open, la séance du jour."""
    sessions = [CO.sessions_back(day_, k) for k in range(len(closes), 0, -1)]
    bars = [{"t": CO.ny_midnight(s), "o": c, "h": c + half_range, "l": c - half_range, "c": c, "v": 1e6}
            for s, c in zip(sessions, closes)]
    if gap_open is not None:
        bars.append({"t": CO.ny_midnight(day_), "o": gap_open, "h": gap_open, "l": gap_open, "c": gap_open, "v": 1e6})
    return bars


def test_couverture_de_l_univers(tmp_path):
    db = LabDB(str(tmp_path / "lab.db"))
    d = date(2025, 3, 4)
    db.put_bars("stock", "DEDANS", 1440, _daily_bars(d, [10.0] * 20, 10.5, 0.3))     # gap +5 %, ATR 6 %
    db.put_bars("stock", "DEHORS", 1440, _daily_bars(d, [0.5] * 20, 0.53, 0.02))     # gap +6 %, ATR 8 %, < 1 $
    db.put_bars("stock", "CALME", 1440, _daily_bars(d, [10.0] * 20, 10.1, 0.3))      # gap +1 %
    db.put_bars("stock", "PLAT", 1440, _daily_bars(d, [10.0] * 20, 10.5, 0.05))      # ATR 1 %
    db.put_bars("stock", "COURT", 1440, _daily_bars(d, [10.0] * 10, 10.5, 0.3))      # moins de 15 séances
    db.replace_universe("stock", d.isoformat(), [{"symbol": "DEDANS", "rank": 1, "reason": "test"}])
    got = U.killmode_coverage(db.conn, [d])
    assert got[d.isoformat()]["5-20 $"] == [1, 1] and got[d.isoformat()]["< 1 $"] == [1, 0]
    later, sealed = date(2025, 3, 5), date(2026, 5, 5)                                # pas encore de journalier
    for day_ in (later, sealed):
        db.replace_universe("stock", day_.isoformat(), [{"symbol": "DEDANS", "rank": 1, "reason": "test"}])
    assert U.coverage_step(db, 10) == 1 and set(db.keys(U.COVERAGE_JOB)) == {d.isoformat()}
    hole = date(2025, 1, 15)                                  # journalier absent alors que la suite est là
    db.replace_universe("stock", hole.isoformat(), [{"symbol": "DEDANS", "rank": 1, "reason": "test"}])
    assert U.coverage_step(db, 10) == 0 and db.get(U.COVERAGE_JOB, hole.isoformat()) == {}
    cov = SS.coverage(db.conn)
    assert cov["gappers"] == 2 and cov["dans_univers"] == 1 and cov["part"] == 0.5 and cov["seances"] == 1
    assert cov["par_prix"]["< 1 $"]["part"] == 0.0
    assert SS.signature(db.conn)[-1] == 2                     # le tableau se recalcule quand la couverture avance
