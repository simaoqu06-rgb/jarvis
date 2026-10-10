"""Labo v2, déclaration n° 10 : mesures corrigées sans essai de plus (témoin à la même heure, colonnes descriptives,
prix négociés au rattrapage, frontière du rattrapage, séances de la n° 9 recalculées, règles de lecture du tableau)."""
import json
import random
from bisect import bisect_left
from datetime import date, datetime, time, timedelta

import pytest

from app.lab import brief as BRIEF
from app.lab import collector as CO
from app.lab import plus10 as P10
from app.lab import rattrapage as RAT
from app.lab import simulator as SIM
from app.lab import stats as STATS
from app.lab import strat_stats as SS
from app.lab import strategies as ST
from app.lab.db import STRAT_N10, LabDB
from app.trading import killmode as K
from tests.test_lab_n7_confirmation import CONFIRMED, PLAN, by_name, make
from tests.test_lab_strategies import CLOSE, DAY, OPEN, ZERO, _rows, bars_from, make_day, stock_day_bars

FLAT = (100.0, 100.0, 100.0, 100.0)
TARGETS = [1.0, 2.0, 3.0]


def at(h, m):
    return int(datetime.combine(DAY, time(h, m), CO.NEW_YORK).timestamp())


def day_of(rows):
    hist, _, daily = stock_day_bars()
    return ST.Day("stock", DAY, CO.regular_hours(hist)[-ST.STOCK_WARMUP_BARS:] + bars_from(OPEN, rows), daily)


def stock_cost(p):
    return SIM.costs("stock", p)


# --- Témoin à la même heure (L1) ------------------------------------------------------------------

def test_temoin_entre_5_a_30_min_apres_l_execution():
    d = day_of([FLAT] * 78)

    def window(h, m):
        w = ST.witness_window(d, {"triggered": 1, "filled_at": at(h, m)})
        return w and (d.t[w[0]], d.t[w[1]])

    assert window(9, 50) == (at(9, 55), at(10, 25))      # orb15 exécuté à 9 h 50 : entrées de 9 h 55 à 10 h 20
    assert window(9, 30) == (at(9, 35), at(10, 5))       # go_brut exécuté à l'ouverture
    assert window(15, 40) == (at(15, 45), at(15, 55))    # jamais la dernière bougie de la séance
    assert window(15, 55) is None and window(15, 50) is None
    assert ST.witness_window(d, {"triggered": 0, "filled_at": None}) is None
    key, m = ("stock", "AAA", DAY, "orb15"), {"triggered": 1, "filled_at": at(15, 55)}
    w = ST.witnesses(d, key, 1.0, TARGETS, lambda p: ZERO, m, (d.first, d.last))
    assert w["temoin_r"] is None and w["temoin_triggered"] == 0 and w["temoin_journee_triggered"] == 1
    assert ST.witnesses(d, key, 1.0, TARGETS, lambda p: ZERO, {"triggered": 0}, (d.first, d.last)) == ST.NO_WITNESS
    crypto = ST.Day("crypto", DAY, d.bars, d.daily)   # crypto : témoin inchangé, sur sa fenêtre
    assert "temoin_journee_r" not in ST.witnesses(crypto, key, 1.0, TARGETS, lambda p: ZERO, m, (0, 10))


def test_le_temoin_n_achete_plus_avant_l_execution():
    # Séance plate à 100, cassure à 10 h 30 jusqu'à 101, puis plate : un ordre stop est exécuté à 10 h 30.
    k = (at(10, 30) - OPEN) // 300
    d = day_of([FLAT] * k + [(100.0, 101.0, 100.0, 101.0)] + [(101.0, 101.0, 101.0, 101.0)] * (77 - k))
    f = bisect_left(d.t, at(10, 30))
    key = ("stock", "AAA", DAY, "orb15")
    centred = ST.witness(d, key, 1.0, TARGETS, stock_cost, (f - 6, f + 7))
    after = ST.witnesses(d, key, 1.0, TARGETS, stock_cost, {"triggered": 1, "filled_at": at(10, 30)},
                         (d.first, d.last))
    alone = ST.witness(d, key, 1.0, TARGETS, stock_cost, (f + 1, f + 2))  # une entrée à 101, séance plate ensuite
    assert centred["temoin_r"] > 0.2               # les tirages d'avant achètent à 100 en « sachant » la cassure
    assert after["temoin_r"] == pytest.approx(alone["temoin_r"]) and -0.2 < after["temoin_r"] < 0   # − frais


def test_tirages_du_temoin_reproductibles_et_publies():
    d = make_day()
    key = ("stock", "AAA", DAY, "pullback")
    a = ST.witness(d, key, 1.0, TARGETS, stock_cost, (d.first, d.last))
    assert a == ST.witness(d, key, 1.0, TARGETS, stock_cost, (d.first, d.last))
    draws = json.loads(a["temoin_tirages"])
    kept = [x for x in draws if x is not None]
    assert len(draws) == ST.WITNESS_DRAWS == 10 and a["temoin_r"] == pytest.approx(sum(kept) / len(kept), abs=1e-4)


def test_lignes_d_une_action_avec_les_deux_temoins(monkeypatch):
    windows: dict = {}
    real = ST.witness

    def spy(d, key, risk_pct, multiples, cost_of, window, exit_mode="v1"):
        windows.setdefault(key[3], []).append(window)
        return real(d, key, risk_pct, multiples, cost_of, window, exit_mode)

    monkeypatch.setattr(ST, "witness", spy)
    hist, session, daily = stock_day_bars()
    entry = session[0]["o"]
    v1 = {"plan": {"entry": entry, "stop": entry - 1.0, "targets": [{"price": entry + k} for k in (1, 2, 3)]},
          "decided_at": OPEN - 20 * 60, "entry_until": CLOSE, "last_price": entry, "cost": SIM.costs("stock", entry)}
    rows = {r["strategy"]: r for r in ST.stock_day(DAY, "AAA", hist, session, daily, 1, v1, traded=True)}
    d = ST.Day("stock", DAY, CO.regular_hours(hist)[-ST.STOCK_WARMUP_BARS:] + session, daily)
    for name, row in rows.items():
        if row["triggered"]:
            same = ST.witness_window(d, row)
            assert windows[name][0] == same and same[0] == bisect_left(d.t, row["filled_at"]) + 1, name
            assert row["temoin_journee_triggered"] == 1 and row["temoin_tirages"]
    assert windows["v1"][1] == (d.first, d.last)            # le témoin « journée » d'avant la n° 10


# --- Colonnes descriptives (X1, L4, L3) -------------------------------------------------------------

def _v1(entry, stop, targets):
    return {"plan": {"entry": entry, "stop": stop, "targets": [{"price": t} for t in targets]},
            "decided_at": OPEN - 20 * 60, "entry_until": CLOSE, "last_price": entry, "cost": SIM.costs("stock", entry)}


def _v1_row(hist, session, daily, v1):
    (row,) = [r for r in ST.stock_day(DAY, "AAA", hist, session, daily, 1, v1, traded=True) if r["strategy"] == "v1"]
    return row


def test_v1_achetee_a_l_ouverture_meme_non_declenchee():
    hist, _, daily = stock_day_bars()
    prev = hist[-1]["c"]
    rows, q = [], prev + 0.5                       # gap qui tient : l'ordre limité au dernier cours n'est pas exécuté
    for _ in range(78):
        rows.append((q, q + 0.3, q - 0.05, q + 0.2))
        q += 0.2
    row = _v1_row(hist, bars_from(OPEN, rows), daily, _v1(prev, prev - 1, (prev + 1, prev + 2, prev + 3)))
    assert row["triggered"] == 0 and row.get("r_be_tp2") is None
    assert 1.4 < row["r_ouverture"] < 1.5     # tiers à +1, +2, +3 achetés 0,5 plus haut, moins les frais
    low = [(prev - 1.5, prev - 1.4, prev - 1.6, prev - 1.5)] * 78    # ouverture sous le stop : pas d'achat
    assert _v1_row(hist, bars_from(OPEN, low), daily, _v1(prev, prev - 1, (prev + 1, prev + 2, prev + 3)))[
        "r_ouverture"] is None
    crypto = ST.v1_row(ST.Day("crypto", DAY, bars_from(OPEN, rows), daily), "BTC", ST._plan(prev, prev - 1, [prev + 1]),
                       OPEN - 1200, OPEN + 3600, prev, ZERO)
    assert "r_ouverture" not in crypto


def test_stop_a_l_entree_apres_le_tp2_sur_v1_et_les_go():
    hist, session, daily = stock_day_bars()
    entry = session[0]["o"]
    v1 = _v1(entry, entry - 1.0, (entry + 1, entry + 2, entry + 3))
    row = _v1_row(hist, session, daily, v1)
    d = ST.Day("stock", DAY, CO.regular_hours(hist)[-ST.STOCK_WARMUP_BARS:] + session, daily)
    p = ST._plan(entry, entry - 1.0, [entry + 1, entry + 2, entry + 3])
    be2 = ST._sim(d, p, v1["decided_at"], CLOSE, d.exit_at(v1["decided_at"]), v1["cost"], "limit", entry,
                  exit_mode="be_tp2")
    assert row["triggered"] == 1 and row["r_be_tp2"] == be2["r_net"] is not None
    assert row["r_ouverture"] is not None                        # exécutée à l'ouverture : les deux existent
    go = by_name(ST.go_rows(make(CONFIRMED), "AAA", {"origine": PLAN}, 3))
    assert all((r.get("r_be_tp2") is not None) == bool(r["triggered"]) for r in go.values())
    assert go["go_brut"]["triggered"] and "r_be_tp2" not in ST.strategy_rows(make_day(), "AAA", 3, 1.0)[0]


def _stopped(targets):
    """Achat au marché à 20 $ (stop 19,80 : 115 actions pour 20 €), stop touché à la bougie suivante."""
    plan = {"side": "long", "entry": 20.0, "stop": 19.8, "targets": [{"price": t} for t in targets]}
    bars = bars_from(OPEN, [(20.0, 20.05, 19.95, 20.0), (20.0, 20.0, 19.5, 19.6)])
    return SIM.simulate(plan, bars, market="stock", decided_at=OPEN, entry_until=OPEN + 300, exit_at=CLOSE,
                        last_price=20.0, cost=SIM.costs("stock", 20.0), entry_order="market", sim_rev=2)


def test_mise_de_20_euros_bareme_et_nombre_de_brackets():
    v = ST.cost_variants("stock", _stopped([20.2, 20.4, 20.6]))
    # Un ordre d'entrée et un de sortie par bracket, 1 $ minimum chacun (plan Fixe) : 6 $, 4 $ ou 2 $ sur 23 $ de risque.
    assert v["r_20e_2s"] - v["r_20e"] == pytest.approx(2 / 23, abs=2e-4)
    assert v["r_20e_1s"] - v["r_20e"] == pytest.approx(4 / 23, abs=2e-4)
    assert v["r_20e_deg"] > v["r_20e"]                            # Dégressif : 0,35 $ minimum par ordre
    two = ST.cost_variants("stock", _stopped([20.2, 20.4]))
    assert two["r_20e"] is not None and two["r_20e_1s"] is None and two["r_20e_2s"] is None
    plan = {"side": "long", "entry": 20.0, "stop": 19.8, "targets": [{"price": t} for t in (20.2, 20.4, 20.6)]}
    up = bars_from(OPEN, [(20.0, 20.05, 19.95, 20.0), (20.0, 20.7, 19.99, 20.65)])
    won = SIM.simulate(plan, up, market="stock", decided_at=OPEN, entry_until=OPEN + 300, exit_at=CLOSE,
                       last_price=20.0, cost=SIM.costs("stock", 20.0), entry_order="market", sim_rev=2)
    v = ST.cost_variants("stock", won)
    assert 1.8 < v["r_20e_1s"] < 2.0                               # tout au TP2 (2 R), deux ordres


def test_colonnes_de_la_n10_dans_les_tables():
    db = LabDB(":memory:")
    for table in ("strat_trades", "strat_trades_sealed", "plus10_trades"):
        cols = {r[1] for r in db.conn.execute(f"PRAGMA table_info({table})")}
        assert {c for c, _ in STRAT_N10} <= cols, table
    row = {"symbol": "AAA", "strategy": "v1", "slot": 1, "triggered": 1, "r_net": 0.5, "r_be_tp2": 0.7,
           "r_ouverture": 0.2, "temoin_journee_r": -0.1, "temoin_tirages": "[0.1, null]"}
    db.replace_strat("replay", "stock", "2025-01-06", [row])
    got = db.conn.execute("SELECT r_be_tp2, r_ouverture, temoin_journee_r, temoin_tirages FROM strat_trades").fetchone()
    assert tuple(got) == (0.7, 0.2, -0.1, "[0.1, null]")


# --- Rattrapage aux prix négociés -----------------------------------------------------------------

def _consistent():
    """Séance de test et journalier dont la dernière clôture est celle des bougies de 5 min (sans division)."""
    hist, session, daily = stock_day_bars()
    daily = P10.scale(daily, hist[-1]["c"] / daily[-1]["c"])
    today = {"t": CO.ny_midnight(DAY), "o": session[0]["o"], "h": max(b["h"] for b in session),
             "l": min(b["l"] for b in session), "c": session[-1]["c"], "v": 2e6}
    return hist, session, daily, today


def _half(rows):
    return [{**r, "o": r["o"] / 2, "h": r["h"] / 2, "l": r["l"] / 2, "c": r["c"] / 2, "v": r["v"] * 2} for r in rows]


def _plan_row(db, symbol, entry, k=1.0):
    plan = {"entry": entry * k, "stop": (entry - 1.0) * k, "targets": [{"price": (entry + j) * k} for j in (1, 2, 3)],
            "score": {"go": True}}
    db.conn.execute("INSERT INTO trades (origin, method_version, market, symbol, side, day, planned_at, plan) "
                    "VALUES ('replay', 'v1', 'stock', ?, 'long', ?, ?, ?)",
                    (symbol, DAY.isoformat(), OPEN - 20 * 60, json.dumps(plan)))
    return plan


async def test_rattrapage_aux_prix_negocies_et_operation_sur_titre(tmp_path, monkeypatch):
    hist, session, daily, today = _consistent()
    db = LabDB(str(tmp_path / "lab.db"))
    db.replace_universe("stock", DAY.isoformat(), [{"symbol": "AAA", "rank": 1, "reason": "test"},
                                                   {"symbol": "SPLT", "rank": 2, "reason": "test"}])
    # AAA : divisée par 2 après la séance ; la base (journalier, plan v1) est ajustée, les bougies relues sont brutes.
    db.put_bars("stock", "AAA", 1440, _half(daily + [today]))
    _plan_row(db, "AAA", hist[-1]["c"], 0.5)
    # SPLT : divisée par 2 le jour même ; gap brut −50 % contre gap ajusté normal : écartée.
    db.put_bars("stock", "SPLT", 1440, _half(daily + [today]))
    raw = {"AAA": hist + session, "SPLT": hist + _half(session)}
    adjustments = []

    async def fake_bars(symbols, tf, start, end, transport=None, adjustment="split"):
        adjustments.append(adjustment)
        return {s: raw[s] for s in symbols if s in raw}

    monkeypatch.setattr(RAT.SRC, "alpaca_bars", fake_bars)
    rows = await RAT.stock_rows(db, "replay", DAY)
    assert set(adjustments) == {"raw"} and {r["symbol"] for r in rows} == {"AAA"}
    trade = {"planned_at": OPEN - 20 * 60, "plan": _plan_row(LabDB(":memory:"), "AAA", hist[-1]["c"])}
    expected = ST.stock_day(DAY, "AAA", hist, session, daily, 1, RAT._v1("stock", trade, CLOSE, 1), None, traded=True)
    assert rows == expected and any(r["strategy"] == "v1" for r in rows)
    assert {r["strategy"] for r in rows} >= {"go_brut", "go_confirme"}       # le GO du plan, remis à l'échelle


def test_facteur_des_prix_negocies():
    assert RAT._factor(100.0, 100.0) == RAT._factor(100.0, 97.0) == 1.0     # clôture officielle / dernière bougie
    assert RAT._factor(100.0, 50.0) == 2.0 and RAT._factor(10.0, 100.0) == pytest.approx(0.1)
    assert RAT._factor(None, 50.0) == RAT._factor(100.0, 0.0) == 1.0
    trade = {"planned_at": 1, "plan": {"entry": 50.0, "stop": 49.0, "targets": [{"price": 51.0}],
                                       "plancher": {"entry": 49.5, "stop": 48.0, "targets": [52.0], "go": True}}}
    v = RAT._v1("stock", trade, 2, 3, 2.0)
    assert v["plan"] == {"entry": 100.0, "stop": 98.0, "targets": [{"price": 102.0}]} and v["last_price"] == 100.0
    assert v["plancher"]["plan"] == {"entry": 99.0, "stop": 96.0, "targets": [{"price": 104.0}]}
    assert v["cost"] == SIM.costs("stock", 100.0, 3)
    hist, session, daily, today = _consistent()
    assert not RAT.operation(hist, session, daily, [today]) and not RAT.operation(hist, session, daily, [])
    assert RAT.operation(hist, _half(session), daily, [today])


def test_frontiere_du_rattrapage(tmp_path):
    db = LabDB(str(tmp_path / "lab.db"))
    db.set("replay:crypto", "start", "2026-09-14")
    assert RAT.due_days(db, "crypto", "replay") == []                    # rien de traité : pas de frontière
    db.set("replay:crypto", "last_day", "2026-09-16")
    # Rejeu déjà fini quand la déclaration change : aucun jour calculé en ligne, on va jusqu'au dernier jour traité.
    assert RAT.due_days(db, "crypto", "replay") == [date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16)]
    db.set(ST.progress_job("crypto"), "inline_from:replay", "2026-09-16")
    assert RAT.due_days(db, "crypto", "replay") == [date(2026, 9, 14), date(2026, 9, 15)]
    db.set(ST.progress_job("crypto"), "rattrapage:replay", "2026-09-14")
    assert RAT.due_days(db, "crypto", "replay") == [date(2026, 9, 15)]
    # Actions, direct : le générateur n'écrit plus leurs lignes, la frontière du jour en ligne ne compte pas.
    db.set("lab", "live_start", "2026-09-19")
    db.set(ST.progress_job("stock"), "inline_from:live", "2026-09-21")
    assert RAT.due_days(db, "stock", "live") == []
    db.set("live:stock", "last_day", "2026-09-22")
    assert RAT.due_days(db, "stock", "live") == [date(2026, 9, 21), date(2026, 9, 22)]
    assert RAT.remaining(db.conn, "stock") == {"replay": 0, "live": 2}


# --- n° 9 : séances recalculées --------------------------------------------------------------------

def test_seances_de_la_n9_recalculees_pour_la_n10():
    from tests.test_lab_n9 import _daily_with_day

    db = LabDB(":memory:")
    db.put_bars("stock", "UPA", 1440, _daily_with_day(9.0))
    db.replace_universe("stock", DAY.isoformat(), [{"symbol": "UPA", "rank": 1, "reason": "volume_dollars"}])
    db.set(P10.JOB, DAY.isoformat(), {"population": 1, "listes": {"s10": [], "rebond": []}})   # calculée avant
    assert P10.ready_days(db) == [DAY] and P10.report(db.conn, 152) is None
    db.set(P10.JOB, DAY.isoformat(), {"absente": True})
    assert P10.ready_days(db) == []
    db.set(P10.JOB, DAY.isoformat(), {"population": 1, "mesure": P10.MESURE})
    assert P10.ready_days(db) == [] and P10.MESURE == ST.DECLARATION == 10
    assert set(P10.TRADE_KEYS) >= {"temoin_journee_r", "temoin_journee_triggered", "temoin_tirages", "r_be_tp2"}


# --- Règles de lecture (strat_stats) -----------------------------------------------------------------

def _insert(db, rows):
    cols = sorted({c for r in rows for c in r})
    db.conn.executemany(f"INSERT INTO strat_trades (origin, {', '.join(cols)}) VALUES ('replay', "
                        f"{', '.join('?' * len(cols))})", [[r.get(c) for c in cols] for r in rows])


def _day(k):
    return (date(2025, 1, 6) + timedelta(days=k)).isoformat()   # lundi 6 janvier 2025


def test_grappes_par_semaine_et_se_naif():
    rng = random.Random(7)
    weekly, daily = SS.Mean(SS._week), SS.Mean()
    for w in range(30):                      # un choc commun à la semaine : les jours ne sont pas indépendants
        shock = rng.gauss(0, 1)
        for k in range(7):
            x = shock + rng.gauss(0, 0.2)
            weekly.add(_day(7 * w + k), x)
            daily.add(_day(7 * w + k), x)
    res, naive = weekly.result(), daily.result()
    assert res["jours"] == naive["jours"] == naive["grappes"] == 210 and res["grappes"] == 30
    assert res["se_jour"] == naive["se"] and res["inflation_se"] == pytest.approx(res["se"] / naive["se"], abs=0.01)
    assert res["inflation_se"] > 1.5 and "se_jour" not in naive
    assert (res["ic95"][1] - res["ic95"][0]) / 2 == pytest.approx(STATS.t_quantile(0.975, 29) * res["se"], abs=1e-3)
    assert daily.by(SS._week).result() == res and SS._week("2025-01-05") == "2025-W01" != SS._week(_day(0))


def test_crypto_jugee_en_semaines_actions_en_sensibilite():
    db = LabDB(":memory:")
    rng = random.Random(3)
    rows = []
    for market in ("crypto", "stock"):
        for k in range(42):
            for j in range(3):
                r = 0.3 + rng.gauss(0, 0.5)
                rows.append({"market": market, "symbol": f"C{j}", "day": _day(k), "strategy": "pullback", "slot": j,
                             "triggered": 1, "r_net": r, "r_glisse_x2": r, "r_glisse_x3": r, "r_retard": r,
                             "r_manque": r, "r_3c": r, "r_20e": r, "temoin_r": rng.gauss(-0.2, 0.5),
                             "temoin_triggered": 1, "entry": 10.0, "stop": 9.0})
    _insert(db, rows)
    rep = SS.report(db.conn)
    crypto, stock = rep["strategies"]["crypto"]["pullback"], rep["strategies"]["stock"]["pullback"]
    assert crypto["jours"] == 42 and crypto["grappes_semaine"]["semaines"] == 6
    assert crypto["seuil_t"] == SS.candidate_t(6) and crypto["grappes_semaine"]["inflation_se"] is not None
    assert crypto["candidate_compte"] == crypto["candidate"] and "semaines" not in crypto
    assert stock["seuil_t"] == SS.candidate_t(42) and stock["semaines"]["grappes"] == 6       # sans effet
    assert stock["semaines"]["seuil_t"] == SS.candidate_t(6) and "grappes_semaine" not in stock
    text = SS.text(rep)
    assert "6 semaines, se ×" in text and "en semaines t " in text


def test_effet_de_l_heure_et_dispersion_des_tirages():
    db = LabDB(":memory:")
    _insert(db, [{"market": "stock", "symbol": f"S{j}", "day": _day(k), "strategy": "orb15", "slot": 0,
                  "triggered": 1, "r_net": 0.3, "temoin_r": 0.1, "temoin_triggered": 1, "temoin_journee_r": -0.1,
                  "temoin_journee_triggered": 1, "temoin_tirages": json.dumps([0.0, 0.2, None])}
                 for k in range(30) for j in range(2)])
    wit = SS.report(db.conn)["strategies"]["stock"]["orb15"]["temoin"]
    assert wit["ecart_r"] == pytest.approx(0.2) and wit["journee"]["esperance_r"] == pytest.approx(0.4)
    assert wit["effet_heure"]["esperance_r"] == pytest.approx(0.2) and wit["effet_heure"]["n"] == 60
    assert wit["dispersion_tirages"] == {"tirages": 2, "ecart_type_r": pytest.approx(0.1414, abs=1e-4)}
    assert "effet de l'heure +0,20 R" in SS.text(SS.report(db.conn)) and "tirages ±0,14 R" in SS.text(SS.report(db.conn))


def test_se_du_labo_pour_l_ecart_labo_direct():
    db = LabDB(":memory:")
    _rows(db, "killmode", 0.3, -0.2, days=30)
    it = SS.report(db.conn)["strategies"]["stock"]["killmode"]
    ci, g = it["ic95_ibkr"], it["jours"]
    assert it["se_ibkr"] == pytest.approx((ci[1] - ci[0]) / 2 / STATS.t_quantile(0.975, g - 1), abs=1e-4)
    q = STATS.t_quantile(0.975, 19)
    lab = {"esperance_ibkr_r": 0.0, "ic95_ibkr": [-q * 0.1, q * 0.1], "jours": 20}
    st = {"esperance_r": 0.197, "se": 1e-9}
    assert K._lab_gap(st, lab) is not None                  # 1,97 se du labo (IC en t sur 20 jours) : au-delà du bruit
    assert K._lab_gap(st, lab | {"jours": None}) is None   # l'ancien calcul (IC lu en loi normale) l'étouffait
    assert K._lab_gap(st, lab | {"se_ibkr": 0.2}) is None   # le se du tableau prime


def _trade(db, symbol, day, entry, tp1):
    db.conn.execute("INSERT INTO trades (origin, method_version, market, symbol, side, day, planned_at, plan) "
                    "VALUES ('replay', 'v1', 'stock', ?, 'long', ?, 1, ?)",
                    (symbol, day, json.dumps({"entry": entry, "stop": entry - 1, "targets": [{"price": tp1}]})))


def test_entree_de_v1_limite_ou_ouverture():
    db = LabDB(":memory:")
    v1 = {"market": "stock", "strategy": "v1", "slot": 1, "entry": 10.0, "stop": 9.0}
    go = {"market": "stock", "strategy": "go_brut", "slot": 0, "stop": 9.0, "triggered": 1}
    _insert(db, [
        # gap qui tient : v1 non exécutée, go_brut +0,5 R sur un risque double (ouverture 11) → +1,0 R contre 0
        v1 | {"symbol": "AAA", "day": _day(0), "triggered": 0, "r_ouverture": 1.0, "gap_open_pct": 3.0},
        go | {"symbol": "AAA", "day": _day(0), "entry": 11.0, "r_sans_be": 0.5},
        # ouverture sous l'entrée : les deux exécutées
        v1 | {"symbol": "BBB", "day": _day(1), "triggered": 1, "r_sans_be": -1.0, "r_ouverture": -0.9,
              "gap_open_pct": -1.0},
        go | {"symbol": "BBB", "day": _day(1), "entry": 9.9, "r_sans_be": -1.0},
        # ouverture au-dessus du TP1 du plan gardé : à part
        v1 | {"symbol": "CCC", "day": _day(2), "triggered": 0, "gap_open_pct": 25.0},
        go | {"symbol": "CCC", "day": _day(2), "entry": 12.5, "r_sans_be": 0.0},
        v1 | {"symbol": "DDD", "day": _day(3), "triggered": 1, "r_sans_be": 0.5, "r_ouverture": 0.5,
              "gap_open_pct": 0.0}])
    for symbol, day in (("AAA", 0), ("BBB", 1), ("CCC", 2)):
        _trade(db, symbol, _day(day), 10.0, 12.0)
    e = SS.report(db.conn)["entree_v1"]
    g = e["go"]
    assert (g["paires"], g["limite_executees"], g["ouverture_executees"]) == (2, 1, 2)
    assert g["ecart"]["esperance_r"] == pytest.approx((1.0 + 0.1) / 2)
    assert g["par_ouverture"]["ouverture > +3 %"]["ecart"]["esperance_r"] == pytest.approx(1.0)
    assert g["par_ouverture"]["ouverture ≤ entrée"]["ecart"]["esperance_r"] == pytest.approx(0.1)
    assert g["au_dessus_tp1"]["paires"] == 1
    p = e["plans"]
    assert (p["paires"], p["limite_executees"], p["ouverture_executees"]) == (4, 2, 3)
    assert p["par_gap"]["gap ≥ +2 %"]["ecart"]["esperance_r"] == pytest.approx(0.5)   # +1,0 (AAA) et 0 (CCC)
    assert p["par_gap"]["gap ±0,5 %"]["ecart"]["esperance_r"] == 0
    text = SS.text(SS.report(db.conn))
    assert "Entrée de v1 (n° 10, mesure)" in text and "1 ouverts au-dessus du TP1 à part" in text
    assert "Politique mesurée : entrée au signal" in text and "stop à l'entrée après TP2" in text
    assert SS.entree_v1_appariee(LabDB(":memory:").conn) is None


def test_declaration_ecrite_avant_les_chiffres():
    from pathlib import Path

    doc = (Path(__file__).resolve().parents[1] / "docs" / "labo-strategies.md").read_text()
    section = doc.split("## Déclaration n° 10 (10 octobre 2026)")[1].split("## Bloc scellé")[0]
    assert ST.REGISTERED_ON == "2026-10-10" and len(ST.trials()) == 152 and "**Essais : 152" in section
    assert "Hypothèses pour la n° 11" in section and f"{ST.WITNESS_DRAWS} au lieu de 5" in section


def test_brief_et_lignes_plus10_et_killmode(monkeypatch):
    item = {"declenches": 40, "jours": 30, "esperance_ref_r": 0.2, "candidate": True, "candidate_compte": False,
            "rentable_20e": False, "mise_20e": {"esperance_r": -0.05}, "temoin": {"ecart_r": 0.3}}
    monkeypatch.setattr(SS, "cached", lambda conn: {"strategies": {"stock": {"pullback": item}}})
    got = BRIEF._strategies(None)["marches"]["stock"]["pullback"]
    assert (got["candidate"], got["candidate_compte"], got["rentable_20e"], got["mise_20e_r"]) == (True, False, False,
                                                                                                    -0.05)
    line = K.lab_line(item | {"esperance_ibkr_r": 0.2})
    assert "☑️ avantage au labo, perd à 20 €" in line and "✅" not in line
    assert "✅ candidate au labo" in K.lab_line(item | {"esperance_ibkr_r": 0.2, "candidate_compte": True})
