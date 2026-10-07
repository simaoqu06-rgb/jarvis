"""Labo v2 : stratégies à déclencheur, sorties, témoin, bloc scellé, rattrapage et tableau."""
import asyncio
import json
from datetime import date, datetime, timezone

import pytest

from app.lab import collector as CO
from app.lab import rattrapage as RAT
from app.lab import runner as R
from app.lab import seal
from app.lab import simulator as SIM
from app.lab import status as ST_STATUS
from app.lab import strat_stats as SS
from app.lab import strategies as ST
from app.lab.db import LabDB
from tests.lab_fixtures import fill_crypto, fill_stock

DAY = date(2026, 10, 6)  # mardi, séance normale
OPEN, CLOSE = CO.session_ts(DAY)
ZERO = {"slippage": 0.0, "maker": 0.0, "taker": 0.0}


def bars_from(start, rows):
    return [{"t": start + 300 * i, "o": o, "h": h, "l": lo, "c": c, "v": 1000.0} for i, (o, h, lo, c) in enumerate(rows)]


def drift(n, p, step):
    out = []
    for _ in range(n):
        c = p + step
        out.append((p, max(p, c) + 0.05, min(p, c) - 0.05, c))
        p = c
    return out, p


def stock_day_bars():
    """Deux séances qui montent doucement, puis une séance : impulsion, repli de 3 bougies vers la
    zone, reprise au-dessus du VWAP, hausse régulière."""
    r1, p = drift(78, 95.0, 0.03)
    r2, p = drift(78, p, 0.03)
    hist = bars_from(CO.session_ts(CO.sessions_back(DAY, 2))[0], r1) + bars_from(CO.session_ts(CO.sessions_back(DAY, 1))[0], r2)
    rows, q = [], p
    for _ in range(6):
        rows.append((q, q + 0.6, q - 0.05, q + 0.5))
        q += 0.5
    for _ in range(3):
        rows.append((q, q + 0.05, q - 0.9, q - 0.8))
        q -= 0.8
    rows.append((q, q + 1.0, q - 0.1, q + 0.9))
    q += 0.9
    for _ in range(60):
        rows.append((q, q + 0.3, q - 0.1, q + 0.2))
        q += 0.2
    session = bars_from(OPEN, rows)
    daily = [{"t": CO.ny_midnight(DAY) - (30 - i) * 86400, "o": 80 + i, "h": 81 + i, "l": 79 + i, "c": 80.5 + i,
              "v": 1e6} for i in range(30)]
    return hist, session, daily


def make_day(hist=None, session=None, daily=None):
    h, s, d = stock_day_bars()
    hist, session, daily = hist or h, session or s, daily or d
    bars = CO.regular_hours(hist)[-ST.STOCK_WARMUP_BARS:] + session
    return ST.Day("stock", DAY, bars, daily)


# --- Simulateur : nouveaux ordres et sorties ------------------------------------------------

def plan(entry=100.0, stop=98.0, tps=(102.0, 104.0, 106.0)):
    return {"side": "long", "entry": entry, "stop": stop, "targets": [{"price": p} for p in tps]}


def sim(bars, p=None, **kw):
    return SIM.simulate(p or plan(), bars, market="stock", decided_at=OPEN, entry_until=CLOSE, exit_at=CLOSE,
                        last_price=99.0, cost=ZERO, **kw)


def test_ordre_stop_execute_quand_le_prix_atteint_l_entree():
    r = sim(bars_from(OPEN, [(99, 99.5, 98.8, 99.2), (99.2, 100.2, 99.1, 100.1)] + [(100.1, 100.3, 99.9, 100)] * 3),
            entry_order="stop")
    assert r["triggered"] and r["fill_price"] == 100.0 and r["entry_mode"] == "stop" and r["entry_liquidity"] == "taker"


def test_ordre_stop_au_dessus_en_gap_execute_a_l_ouverture():
    r = sim(bars_from(OPEN, [(101, 101.5, 100.5, 101)] * 3), entry_order="stop")
    assert r["fill_price"] == 101 and r["entry_mode"] == "ouverture"


def test_ordre_stop_jamais_atteint():
    assert not sim(bars_from(OPEN, [(99, 99.5, 98.8, 99.2)] * 5), entry_order="stop")["triggered"]


def test_ordre_au_marche_a_l_ouverture_de_la_premiere_bougie():
    r = sim(bars_from(OPEN, [(99.4, 99.5, 99.0, 99.2)] * 3), entry_order="market")
    assert r["fill_price"] == 99.4 and r["entry_mode"] == "ouverture"


def test_sans_break_even_le_retour_a_l_entree_ne_sort_pas():
    rows = [(100, 100.2, 99.9, 100.1), (100.1, 102.2, 100.0, 102.1), (102, 102.1, 99.5, 99.8), (99.8, 104.5, 99.7, 104.4),
            (104.4, 106.5, 104.0, 106.4)]
    v1 = sim(bars_from(OPEN, rows))
    no_be = sim(bars_from(OPEN, rows), exit_mode="sans_be")
    assert v1["exit_reason"] == "stop_be" and no_be["exit_reason"] == "tp3"
    assert no_be["r_net"] > v1["r_net"]


def test_stop_suiveur_suit_le_plus_haut():
    rows = [(100, 100.2, 99.9, 100.1), (100.1, 105.0, 100.0, 104.8), (104.8, 105.1, 102.5, 102.7)]
    r = sim(bars_from(OPEN, rows), exit_mode="suiveur", trail_r=1.0)
    # risque 2 : après le plus haut 105, le stop passe à 103 ; la bougie suivante descend à 102,5
    assert r["exit_reason"] == "stop_suiveur" and r["exits"][0]["price"] == pytest.approx(103.0)


def test_net_r_glissement_x1_egal_au_simulateur():
    cost = {"slippage": 0.001, "maker": 0.0008, "taker": 0.001}
    r = SIM.simulate(plan(), bars_from(OPEN, [(99.5, 102.5, 99.4, 102.4), (102.4, 106.5, 102, 106.2)]), market="stock",
                     decided_at=OPEN, entry_until=CLOSE, exit_at=CLOSE, last_price=99.0, cost=cost)
    assert SIM.net_r(r, cost, slip_factor=1.0) == pytest.approx(r["r_net"], abs=1e-4)
    assert SIM.net_r(r, cost, slip_factor=3.0) < SIM.net_r(r, cost, slip_factor=2.0) < r["r_net"]


def test_valeurs_par_defaut_inchangees_pour_v1():
    with pytest.raises(ValueError):
        sim(bars_from(OPEN, [(99, 99.5, 98.8, 99.2)]), entry_order="iceberg")


# --- Signaux ------------------------------------------------------------------------------

def test_pullback_attend_le_repli_et_la_reprise():
    d = make_day()
    cands = ST.pullback(d)
    first = cands[0]
    k = first["i"] - d.d0
    assert k == 10  # pas pendant l'impulsion (le VWAP de l'ouverture colle au prix), après le repli
    i = first["i"]
    assert d.c[i] > d.o[i] and d.c[i] > d.vwap[i] and d.c[i] > d.ema[i] and d.c[i] > d.h[i - 1]
    assert first["entry"] == pytest.approx(d.h[i] + 0.01) and first["stop"] < min(d.l[i - 2:i + 1])
    assert first["decided_at"] == d.t[i] + 300 and first["entry_until"] == d.t[i] + 4 * 300


def test_pullback_ne_regarde_pas_l_avenir():
    hist, session, daily = stock_day_bars()
    d = make_day(hist, session, daily)
    first = ST.pullback(d)[0]
    k = first["i"] - d.d0
    crash = session[:k + 1] + [{**b, "o": 50.0, "h": 50.5, "l": 49.5, "c": 50.0} for b in session[k + 1:]]
    d2 = make_day(hist, crash, daily)
    assert ST.pullback(d2)[0] == first


def test_pullback_hors_tendance_journaliere():
    hist, session, daily = stock_day_bars()
    falling = [{**b, "c": 200 - i} for i, b in enumerate(daily)]
    assert ST.pullback(make_day(hist, session, falling)) == []


def test_orb_niveaux_et_validite():
    d = make_day()
    (c15,) = ST.orb(d, 15)
    assert c15["entry"] == pytest.approx(max(d.h[d.d0:d.d0 + 3]) + 0.01)
    assert c15["stop"] == pytest.approx(min(d.l[d.d0:d.d0 + 3]) - 0.01)
    assert c15["decided_at"] == OPEN + 15 * 60
    assert c15["entry_until"] == int(datetime(2026, 10, 6, 16, 0, tzinfo=timezone.utc).timestamp())  # 12 h NY
    (c30,) = ST.orb(d, 30)
    assert c30["decided_at"] == OPEN + 30 * 60


def test_cassure_retest():
    hist, session, daily = stock_day_bars()
    level = daily[-1]["h"]  # 110
    rows = [(108, 108.5, 107.8, 108.3)] * 3 + [(108.3, 110.6, 108.2, 110.5), (110.5, 111.2, 110.4, 111.0),
                                                (111.0, 111.1, 110.1, 110.6), (110.6, 112, 110.5, 111.8)]
    rows += [(111.8, 112.2, 111.6, 112.0)] * 30
    d = make_day(hist, bars_from(OPEN, rows), daily)
    cands = ST.cassure_retest(d)
    assert cands and cands[0]["i"] - d.d0 == 5
    assert cands[0]["entry"] == pytest.approx(111.11) and cands[0]["stop"] < level


# --- Déclaration n° 2 : zones S/R EmperorBTC ---------------------------------------------

def zigzag_hist():
    """Deux séances qui oscillent entre 108 et 110 par pas de 0,25 : creux de réaction à 107,98 et
    sommets à 110,02, sur les mêmes prix que le plus bas (108) et le plus haut (110) de la veille."""
    rows, p, step = [], 110.0, -0.25
    for _ in range(156):
        c = p + step
        rows.append((p, max(p, c) + 0.02, min(p, c) - 0.02, c))
        p = c
        if p <= 108.0 or p >= 110.0:
            step = -step
    return (bars_from(CO.session_ts(CO.sessions_back(DAY, 2))[0], rows[:78])
            + bars_from(CO.session_ts(CO.sessions_back(DAY, 1))[0], rows[78:]))


def zone_day(rows):
    _, _, daily = stock_day_bars()  # veille : plus haut 110, plus bas 108, clôture 109,5
    return make_day(zigzag_hist(), bars_from(OPEN, rows + [(r[3], r[3] + 0.1, r[3] - 0.05, r[3] + 0.05)
                                                        for r in rows[-1:]] * 40), daily)


REBOND = [(109.0, 109.1, 108.9, 109.0)] * 3 + [
    (109.0, 109.02, 108.78, 108.8), (108.8, 108.82, 108.58, 108.6), (108.6, 108.62, 108.38, 108.4),
    (108.4, 108.42, 108.18, 108.2),
    (108.2, 108.25, 107.95, 108.1),    # contact de la zone 108, clôture au-dessus du bas de zone
    (108.1, 108.5, 108.05, 108.45), (108.45, 108.85, 108.4, 108.8), (108.8, 109.2, 108.75, 109.15)]


def test_zones_s_r_emperor():
    d = zone_day(REBOND)
    zs = d.zones()
    lows = [z for z in zs if z[0] < 108.1]
    highs = [z for z in zs if 109.9 < z[0] < 110.1]
    assert len(lows) == 1 and lows[0][0] == pytest.approx(107.98) and lows[0][2] >= 3   # creux + veille
    assert len(highs) == 1 and highs[0][0] == pytest.approx(110.0) and highs[0][2] >= 3
    assert all(z[2] >= ST.ZONE_MIN_CONTACTS for z in zs)
    # Un pivot seul (P à 109,17) n'est pas une zone : une ligne valide touche au moins deux points.
    assert not any(z[0] <= 109.17 <= z[1] for z in zs)
    # Rien de ce qui se passe dans la fenêtre ne change les zones.
    crash = REBOND[:3] + [(100.0, 100.5, 99.5, 100.0)] * 10
    assert zone_day(crash).zones() == zs


def test_rebond_confirme_attend_la_cassure_de_la_resistance():
    d = zone_day(REBOND)
    first = ST.rebond_confirme(d)[0]
    i = first["i"]
    assert i - d.d0 == 7
    assert first["entry"] == pytest.approx(max(d.h[i - 5:i + 1]) + 0.01)   # pas d'entrée au contact
    assert first["stop"] == pytest.approx(107.95 - ST.STOP_BUFFER_ATR * d.atr[i])
    assert first["entry_until"] == d.t[i] + 300 + ST.BOUNCE_VALID_BARS * 300
    rows = {r["strategy"]: r for r in ST.strategy_rows(d, "AAA", 3, 0.0)}
    assert rows["rebond_confirme"]["triggered"] == 1
    assert rows["rebond_confirme"]["filled_at"] == d.t[i + 3]


def test_rebond_confirme_annule_si_le_stop_casse_avant():
    rows = REBOND[:8] + [(108.1, 108.2, 107.5, 107.6), (107.6, 109.5, 107.5, 109.4)]
    d = zone_day(rows)
    first = ST.rebond_confirme(d)[0]
    assert first["i"] - d.d0 == 7 and first["entry_until"] == d.t[first["i"] + 1]
    plan = ST._plan(first["entry"], first["stop"], ST._r_targets(first["entry"], first["stop"]))
    res = ST._sim(d, plan, first["decided_at"], first["entry_until"], d.exit_at(first["decided_at"]),
                  SIM.costs("stock", first["entry"], 3), "stop", d.c[first["i"]])
    assert not res["triggered"]   # le prix repasse l'entrée à la bougie suivante, mais l'ordre est annulé


def test_cassure_zone_retest():
    rows = [(109.6, 109.7, 109.5, 109.6)] * 3 + [
        (109.6, 109.9, 109.55, 109.85), (109.85, 110.4, 109.8, 110.3),   # cassure de la zone 110
        (110.3, 110.6, 110.25, 110.5), (110.5, 110.55, 110.0, 110.2),    # retour sur la zone, clôture au-dessus
        (110.2, 110.9, 110.15, 110.8)]
    d = zone_day(rows)
    first = ST.cassure_zone_retest(d)[0]
    assert first["i"] - d.d0 == 6
    assert first["entry"] == pytest.approx(110.56) and first["stop"] == pytest.approx(110.0 - 0.1 * d.atr[first["i"]])


def test_sortie_emperor_objectifs_et_signal_de_retournement():
    d = zone_day(REBOND)
    c = ST.rebond_confirme(d)[0]
    targets = ST.emperor_targets(d, c["entry"], c["stop"])
    risk = c["entry"] - c["stop"]
    assert targets[0] == pytest.approx(110.0 - ST.TARGET_BUFFER_ATR * d.zone_atr)   # prochaine résistance
    assert targets == sorted(targets) and len(targets) == 3
    assert targets[1] - targets[0] >= ST.MIN_TARGET_R * risk - 1e-9
    plan = ST._plan(c["entry"], c["stop"], targets)
    cost = SIM.costs("stock", c["entry"], 3)
    args = (d, plan, c["decided_at"], c["entry_until"], d.exit_at(c["decided_at"]), cost, "stop", d.c[c["i"]])
    d._exit_signal = {k: False for k in range(len(d.t))}
    plain = ST._sim(*args)
    assert ST._emperor(*args) == plain["r_net"]
    d._exit_signal = {k: True for k in range(len(d.t))}
    k0 = d.t.index(plain["filled_at"])
    k = next(k for k in range(k0, len(d.t)) if d.c[k] > plain["fill_price"])
    early = ST._sim(*args[:4], d.t[k + 1] + 300, *args[5:], exit_reason_end="signal_sortie")
    assert early["exit_reason"] == "signal_sortie" and ST._emperor(*args) == early["r_net"]


def test_nouvelle_declaration_relance_le_rattrapage(tmp_path, monkeypatch):
    db = LabDB(str(tmp_path / "lab.db"))
    db.set("replay:stock", "start", "2026-09-14")
    db.set(ST.progress_job("stock"), "inline_from:replay", "2026-09-16")
    db.set(ST.progress_job("stock"), "rattrapage:replay", "2026-09-15")
    assert RAT.due_days(db, "stock", "replay") == []
    monkeypatch.setattr(ST, "DECLARATION", ST.DECLARATION + 1)
    assert RAT.due_days(db, "stock", "replay") == []          # pas encore de frontière pour la nouvelle
    db.set(ST.progress_job("stock"), "inline_from:replay", "2026-09-17")
    assert RAT.due_days(db, "stock", "replay") == [date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16)]


def test_seuil_candidate_corrige_du_nombre_d_essais():
    assert SS.candidate_z(32) == pytest.approx(2.96, abs=0.01) and SS.candidate_z(60) == pytest.approx(3.14, abs=0.01)
    db = LabDB(":memory:")
    _rows(db, "pullback", 0.3, 0.2, days=40)   # bat son témoin (z ≈ 2,5), mais trop peu pour 60 essais
    db.conn.execute("UPDATE strat_trades SET temoin_r = r_net - (0.1 + CASE WHEN CAST(substr(day, 9, 2) AS INTEGER) "
                    "% 2 = 1 THEN 0.25 ELSE -0.25 END), r_gross = r_net + 0.15")
    rep = SS.report(db.conn)
    item = rep["strategies"]["stock"]["pullback"]
    assert 2 <= item["temoin"]["z"] < SS.candidate_z() and not item["candidate"]
    assert item["frais_r"] == pytest.approx(0.15) and "rang inconnu" in item["par_rang"]


# --- Lignes, témoin, v1 -------------------------------------------------------------------

def test_une_ligne_par_strategie_et_temoin_reproductible():
    d = make_day()
    rows = ST.strategy_rows(d, "AAA", 3, 0.5)
    assert sorted(r["strategy"] for r in rows) == ["cassure_retest", "orb15", "orb30", "pullback"]
    again = ST.strategy_rows(make_day(), "AAA", 3, 0.5)
    assert [r["temoin_r"] for r in rows] == [r["temoin_r"] for r in again]
    for r in rows:
        assert r["triggered"] == 1 and r["filled_at"] >= r["signal_at"]
        for k in ("r_sans_be", "r_2r", "r_suiveur", "r_glisse_x2", "r_glisse_x3"):
            assert r[k] is not None
        assert r["r_glisse_x3"] <= r["r_glisse_x2"] <= r["r_net"]


def test_v1_rejouee_avec_ses_objectifs():
    hist, session, daily = stock_day_bars()
    entry = session[0]["o"]
    p = {"entry": entry, "stop": entry - 1.0, "targets": [{"price": entry + 1}, {"price": entry + 2}, {"price": entry + 3}]}
    v1 = {"plan": p, "decided_at": OPEN - 20 * 60, "entry_until": CLOSE, "last_price": entry,
          "cost": SIM.costs("stock", entry)}
    rows = ST.stock_day(DAY, "AAA", hist, session, daily, 1, v1)
    (row,) = [r for r in rows if r["strategy"] == "v1"]
    assert row["slot"] == OPEN - 20 * 60 and row["triggered"] == 1 and row["gap_open_pct"] is not None


def test_essais_declares():
    t = ST.trials()
    # Déclaration n° 1 : 32 essais ; n° 2 : + 2 stratégies sur deux marchés et la sortie EmperorBTC partout.
    assert len(t) == 60 and {x["exit"] for x in t} == set(ST.EXITS) and len(ST.EXITS) == 5


# --- Bloc scellé --------------------------------------------------------------------------

@pytest.fixture
def vrai_scelle(monkeypatch):
    monkeypatch.setattr(seal, "SEALED_FROM", date(2026, 4, 6))
    monkeypatch.setattr(seal, "SEALED_TO", date(2026, 10, 5))


def test_scelle_range_les_trades_a_part(vrai_scelle):
    db = LabDB(":memory:")
    row = {"symbol": "AAA", "side": "long", "day": "2026-05-04", "planned_at": 1, "plan": {}, "context": {},
           "result": {}, "flat": {"triggered": 1, "r_net": 2.0}}
    db.replace_trades("replay", "v1", "stock", [1], [row])
    db.replace_trades("live", "v1", "stock", [2], [{**row, "planned_at": 2}])
    db.replace_trades("replay", "v1", "stock", [3], [{**row, "day": "2026-03-02", "planned_at": 3}])
    assert db.conn.execute("SELECT COUNT(*) FROM trades_sealed").fetchone()[0] == 1
    assert {r[0] for r in db.conn.execute("SELECT origin FROM trades")} == {"live", "replay"}
    db.replace_strat("replay", "stock", "2026-05-04", [{"symbol": "AAA", "strategy": "pullback", "slot": 0}])
    db.replace_strat("replay", "stock", "2026-03-02", [{"symbol": "AAA", "strategy": "pullback", "slot": 0}])
    assert db.conn.execute("SELECT day FROM strat_trades").fetchall() == [("2026-03-02",)]
    assert db.conn.execute("SELECT day FROM strat_trades_sealed").fetchall() == [("2026-05-04",)]
    db.replace_strat("replay", "stock", "2026-05-04", [])  # rejouer le jour : remplacé, pas doublé
    assert db.conn.execute("SELECT COUNT(*) FROM strat_trades_sealed").fetchone()[0] == 0


def test_etat_montre_le_scelle_sans_ses_resultats(tmp_path, vrai_scelle):
    path = tmp_path / "lab.db"
    db = LabDB(str(path))
    row = {"symbol": "AAA", "side": "long", "day": "2026-05-04", "planned_at": 1, "plan": {}, "context": {},
           "result": {}, "flat": {"triggered": 1, "r_net": 2.0}}
    db.replace_trades("replay", "v1", "stock", [1], [row])
    st = ST_STATUS.status(str(path))
    assert st["scelle"]["trades"] == {"stock": 1} and st["trades"] == {}
    assert "Bloc scellé" in ST_STATUS.text(st)


# --- Rejeu, direct et rattrapage ---------------------------------------------------------

START, END = date(2026, 9, 14), date(2026, 9, 16)
NOW = datetime(2026, 9, 20, tzinfo=timezone.utc)


async def test_rejeu_ecrit_les_strategies_et_la_frontiere_du_rattrapage(tmp_path):
    db = LabDB(str(tmp_path / "lab.db"))
    fill_stock(db, ["AAA", "BBB", "CCC"], CO.sessions_back(START, 125), END)
    await R.replay(START, END, "stock", db, collect=False, now=NOW)
    names = {r[0] for r in db.conn.execute("SELECT DISTINCT strategy FROM strat_trades")}
    assert "v1" in names and names <= {"v1", *ST.STRATEGIES}
    assert db.get(ST.progress_job("stock"), "inline_from:replay") == START.isoformat()
    n = db.conn.execute("SELECT COUNT(*) FROM strat_trades").fetchone()[0]
    await R.replay(START, END, "stock", db, collect=False, now=NOW)  # rien à refaire, rien de doublé
    assert db.conn.execute("SELECT COUNT(*) FROM strat_trades").fetchone()[0] == n


def test_crypto_attend_que_toutes_les_sorties_soient_connues(tmp_path):
    db = LabDB(str(tmp_path / "lab.db"))
    day = date(2026, 9, 14)
    fill_crypto(db, ["BTC-USDT", "ETH-USDT"], date(2026, 7, 20), date(2026, 9, 17))
    start = CO.utc_midnight(day)
    early = datetime.fromtimestamp(start + 30 * 3600, timezone.utc)
    R.run_day(db, "crypto", day, "live", now=early)
    assert db.conn.execute("SELECT COUNT(*) FROM strat_trades").fetchone()[0] == 0
    late = datetime.fromtimestamp(start + 44 * 3600, timezone.utc)
    R.run_day(db, "crypto", day, "live", now=late)
    rows = db.conn.execute("SELECT strategy, COUNT(*) FROM strat_trades GROUP BY strategy").fetchall()
    assert dict(rows).get("v1", 0) >= 6  # un par créneau de 4 h et par paire


async def test_rattrapage_relit_les_bougies_sans_les_stocker(tmp_path, monkeypatch):
    db = LabDB(str(tmp_path / "lab.db"))
    fill_stock(db, ["AAA", "BBB"], CO.sessions_back(START, 125), END)
    await R.replay(START, END, "stock", db, collect=False, now=NOW)
    # Le labo v2 arrive après coup : on efface ses lignes et on fait comme si le 16 était son premier jour.
    stored = {s: db.get_bars("stock", s, 5, 0, 2**40) for s in ("AAA", "BBB")}
    db.conn.execute("DELETE FROM strat_trades WHERE day < '2026-09-16'")
    db.set(ST.progress_job("stock"), "inline_from:replay", "2026-09-16")
    db.conn.execute("DELETE FROM bars WHERE tf = 5")  # le rejeu a fait le ménage

    async def fake_bars(symbols, tf, start, end, transport=None):
        a, b = start.timestamp(), end.timestamp()
        return {s: [x for x in stored.get(s, []) if a <= x["t"] < b] for s in symbols}

    monkeypatch.setattr(RAT.SRC, "alpaca_bars", fake_bars)
    assert RAT.due_days(db, "stock", "replay") == [date(2026, 9, 14), date(2026, 9, 15)]
    assert await RAT.step(db, "stock", 60) == 2
    days = {r[0] for r in db.conn.execute("SELECT DISTINCT day FROM strat_trades")}
    assert {"2026-09-14", "2026-09-15"} <= days
    assert db.conn.execute("SELECT COUNT(*) FROM bars WHERE tf = 5").fetchone()[0] == 0
    assert RAT.due_days(db, "stock", "replay") == []
    v1 = db.conn.execute("SELECT COUNT(*) FROM strat_trades WHERE strategy = 'v1' AND day = '2026-09-14'").fetchone()[0]
    assert v1 == 2


# --- Tableau ------------------------------------------------------------------------------

def _rows(db, strategy, r, wit, days=40, per_day=3):
    out = []
    for k in range(days):
        day = date.fromordinal(date(2025, 1, 6).toordinal() + k).isoformat()
        rows = [{"symbol": f"S{j}", "strategy": strategy, "slot": 0, "triggered": 1, "r_net": r + 0.1 * ((j + k) % 3 - 1),
                 "r_sans_be": r, "r_2r": r, "r_suiveur": r, "r_glisse_x2": r - 0.05, "r_glisse_x3": r - 0.1,
                 "r_retard": r, "r_manque": r, "temoin_r": wit + 0.1 * ((j * 2 + k) % 3 - 1), "temoin_triggered": 1,
                 "tp1_hit": 1} for j in range(per_day)]
        out.append((day, rows))
    for day, rows in out:
        db.conn.executemany(
            "INSERT INTO strat_trades (origin, market, symbol, day, strategy, slot, triggered, r_net, r_sans_be, r_2r, "
            "r_suiveur, r_glisse_x2, r_glisse_x3, r_retard, r_manque, temoin_r, temoin_triggered, tp1_hit) "
            "VALUES ('replay', 'stock', ?, ?, ?, 0, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1)",
            [(x["symbol"], day, strategy, x["r_net"], x["r_sans_be"], x["r_2r"], x["r_suiveur"], x["r_glisse_x2"],
              x["r_glisse_x3"], x["r_retard"], x["r_manque"], x["temoin_r"]) for x in rows])


def test_tableau_classe_par_esperance_et_exige_de_battre_le_temoin():
    db = LabDB(":memory:")
    _rows(db, "pullback", 0.3, -0.2)   # gagne et bat son témoin
    _rows(db, "orb15", 0.3, 0.3)       # gagne, mais pas mieux que le hasard
    _rows(db, "v1", -0.2, -0.2)
    rep = SS.report(db.conn)
    items = rep["strategies"]["stock"]
    assert list(items)[:2] == ["pullback", "orb15"] or list(items)[:2] == ["orb15", "pullback"]
    assert items["pullback"]["candidate"] and not items["orb15"]["candidate"] and not items["v1"]["candidate"]
    assert items["pullback"]["temoin"]["ecart_r"] == pytest.approx(0.5, abs=0.01)
    assert items["pullback"]["jours"] == 40 and items["pullback"]["reussite"] is not None
    text = SS.text(rep)
    assert "✅ Pullback" in text and "témoin" in text and "scellé" in text


def test_diagnostic_v1_par_gap_et_brut_net():
    db = LabDB(":memory:")
    rows = []
    for k, gap in enumerate((-3.0, -1.0, 0.0, 1.0, 3.0) * 8):
        rows.append({"symbol": f"S{k}", "side": "long", "day": f"2025-01-{1 + k % 20:02d}", "planned_at": k,
                     "plan": {}, "context": {"gap_open_pct": gap}, "result": {"entry_mode": "ouverture" if gap < 0 else "limite"},
                     "flat": {"triggered": 1, "r_net": gap / 10 - 0.1, "r_gross": gap / 10, "exit_reason": "stop"}})
    db.replace_trades("replay", "v1", "stock", list(range(len(rows))), rows)
    diag = SS.diagnostic_v1(db.conn)["stock"]
    assert diag["frais_r"] == pytest.approx(0.1, abs=1e-6)
    assert diag["par_gap"]["gap ≤ −2 %"]["esperance_r"] < diag["par_gap"]["gap ≥ +2 %"]["esperance_r"]
    assert set(diag["par_entree"]) == {"ouverture", "limite"}


def test_route_api_des_strategies(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main

    path = tmp_path / "lab.db"
    monkeypatch.setenv("JARVIS_LAB_DB", str(path))
    monkeypatch.setenv("JARVIS_LAB_API_KEY", "cle")
    db = LabDB(str(path))
    _rows(db, "pullback", 0.3, -0.2)
    r = TestClient(main.app).get("/api/lab/strategies", headers={"X-Lab-Key": "cle"})
    assert r.status_code == 200 and "pullback" in r.json()["strategies"]["stock"]
    assert r.json()["declaration"]["essais"] == 60 and r.json()["declaration"]["z_candidate"] == 3.14
