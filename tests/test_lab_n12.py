"""Labo, déclaration n° 12 : passe A de Gemini et ORB 5 min des « stocks in play » rejoués sur toute la cote."""
import asyncio
import math
from datetime import date, datetime, time, timezone
from pathlib import Path

import pytest

from app.core.http import SourceError
from app.lab import collector as CO
from app.lab import ouverture as OUV
from app.lab import seal
from app.lab import sources as SRC
from app.lab import status as STATUS
from app.lab import strat_stats as SS
from app.lab import strategies as ST
from app.lab.db import LabDB
from app.trading import gemini as G
from app.trading import killmode as K

DAY = date(2026, 10, 21)      # mercredi, séance entière
NEXT = date(2026, 10, 22)
OPEN, CLOSE = CO.session_ts(DAY)
SETTLED = datetime.fromtimestamp(CO.session_ts(NEXT)[1] + OUV.SETTLED_AFTER, timezone.utc)


def ts(h, m, day=DAY):
    return OUV._ts(day, time(h, m))


def bar(t, o, h, l, c, v=1000.0):  # noqa: E741
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}


def path(day, price, moves=None, v=1000.0, minutes=None):
    """Une bougie d'1 min par minute de la séance de `day` (les `minutes` premières) ; moves : {minute depuis 9 h 30 :
    (o, h, l, c, v)} ; entre deux mouvements, bougies plates à la dernière clôture."""
    o, c = CO.session_ts(day)
    out, last = [], price
    for i in range(minutes or (c - o) // 60):
        if moves and i in moves:
            bo, bh, bl, bc, bv = moves[i]
            out.append(bar(o + 60 * i, bo, bh, bl, bc, bv))
            last = bc
        else:
            out.append(bar(o + 60 * i, last, last, last, last, v))
    return out


def daily(db, symbol, close=10.0, open_today=None, n=30, half=0.5, vol=2e6):
    """n séances journalières plates avant DAY (plus haut − plus bas = 2 × half, donc ATR = 2 × half), puis DAY qui
    ouvre à `open_today`."""
    days = [CO.sessions_back(DAY, k) for k in range(n, 0, -1)]
    rows = [bar(CO.ny_midnight(d), close, close + half, close - half, close, vol) for d in days]
    o = close if open_today is None else open_today
    rows.append(bar(CO.ny_midnight(DAY), o, o * 1.1, o * 0.95, o * 1.05, 3 * vol))
    db.put_bars("stock", symbol, 1440, rows)


def open5(db, symbol, day, o, h, l, c, v):  # noqa: E741
    db.conn.execute("INSERT OR REPLACE INTO open5 VALUES (?, ?, ?, ?, ?, ?, ?)", (day.isoformat(), symbol, o, h, l, c, v))


def warm(db, symbol, price, v=1000.0, skip=(), scale=None):
    """Bougie de 9 h 30 des WARMUP séances d'avant DAY (k = 1 : la veille) ; `skip` : séances sans échange dans les 5
    premières minutes ; `scale(k)` : facteur des prix négociés de la séance k (division d'actions)."""
    for k, d in enumerate(OUV.warmup(DAY)[1:], start=1):
        if k in skip:
            continue
        p = price * (scale(k) if scale else 1.0)
        open5(db, symbol, d, p, p, p, p, v)


def mark(db, days):
    for d in days:
        db.set(OUV.OPEN5_JOB, d.isoformat(), {"symboles": 0, "bougies": 0})


def test_declaration_et_essais():
    assert ST.LATEST_DECLARATION == OUV.NUMERO == 12 and ST.DECLARATION == ST.CRYPTO_DECLARATION == 11
    assert [t["strategy"] for t in ST.N12_TRIALS] == list(OUV.TRIALS) and len(ST.trials()) == 162
    assert {t["exit"] for t in ST.N12_TRIALS} == {"r_frais"} and {t["market"] for t in ST.N12_TRIALS} == {"stock"}
    assert set(OUV.TRIALS) < set(OUV.LABELS) and len(OUV.LABELS) == 9
    assert (OUV.SIP_TOP, OUV.SIP_RVOL_DAYS, OUV.SIP_RVOL_MIN, OUV.SIP_STOP_ATR) == (20, 14, 1.0, 0.10)
    assert OUV.SIP_FILTERS == {"prix_min": 5.0, "volume_min": 1_000_000, "atr_min": 0.50}
    assert (OUV.MA_TOP, OUV.TG_TOP, OUV.ACTIF_DES, OUV.MESURE) == (100, 50, time(9, 38), 1)
    assert (G.VERSION, G.SEUILS["gap_min"], G.SEUILS["gap_max"], G.SEUILS["rvol_min"]) == (2, 5.0, 25.0, 4.0)
    assert OUV.REGLES_GEMINI == G.VERSION    # règles du direct changées : nouvelle déclaration
    doc = (Path(__file__).resolve().parents[1] / "docs" / "labo-strategies.md").read_text()
    section = doc.split("## Déclaration n° 12")[1].split("## Bloc scellé")[0]
    assert "**162**" in section and "SSRN 4729284" in section


# --- Population : facteur des prix négociés, gap, volumes relatifs -------------------------------------------------

@pytest.fixture
def cote(monkeypatch):
    """AAA : gap de +10 % ; SPL : divisée par deux après DAY (le journalier ajusté vaut la moitié des prix négociés) ;
    MID : regroupement il y a 11 séances (fenêtres des volumes relatifs) ; OLD : il y a 23 séances (hors fenêtres) ;
    TRO : deux séances cotées sans échange dans les 5 premières minutes ; NEW : 10 séances de cote ; GONE : rien à
    l'ouverture ; STALE : pas de journalier ce jour-là."""
    monkeypatch.setattr(OUV, "MIN_COTE", 6)
    db = LabDB(":memory:")
    for symbol in ("AAA", "MID", "OLD", "TRO"):
        daily(db, symbol, 10.0, 11.0)
        open5(db, symbol, DAY, 11.0, 11.6, 10.9, 11.5, 8000.0)
    warm(db, "AAA", 10.0)
    warm(db, "MID", 10.0, scale=lambda k: 0.5 if k > 10 else 1.0)
    warm(db, "OLD", 10.0, scale=lambda k: 0.5 if k > 22 else 1.0)
    warm(db, "TRO", 10.0, skip=(2, 5))
    daily(db, "SPL", 10.0, 11.0)
    open5(db, "SPL", DAY, 22.0, 23.2, 21.8, 23.0, 4000.0)
    warm(db, "SPL", 20.0, v=500.0)
    daily(db, "NEW", 10.0, 11.0, n=10)
    open5(db, "NEW", DAY, 11.0, 11.6, 10.9, 11.5, 8000.0)
    warm(db, "NEW", 10.0, skip=range(11, OUV.WARMUP + 1))
    daily(db, "GONE", 10.0, 11.0)
    warm(db, "GONE", 10.0)
    db.put_bars("stock", "STALE", 1440, [bar(CO.ny_midnight(CO.sessions_back(DAY, k)), 10, 10.5, 9.5, 10, 2e6)
                                         for k in range(30, 0, -1)])
    open5(db, "STALE", DAY, 11.0, 11.6, 10.9, 11.5, 8000.0)
    return db


def test_population_aux_prix_negocies(cote):
    assert OUV.population(cote.conn, DAY) is None                  # bougie de 9 h 30 du jour pas encore lue
    mark(cote, [DAY])
    pop = OUV.population(cote.conn, DAY)
    assert set(pop) == {"AAA", "SPL", "MID", "OLD", "TRO", "NEW"}
    a = pop["AAA"]
    assert a["bar"] == bar(OPEN, 11.0, 11.6, 10.9, 11.5, 8000.0) and a["seances"] == 30
    assert a["facteur"] == 1.0 and a["prev_raw"] == 10.0 and a["v5"] == 8000.0
    assert a["gap_pct"] == pytest.approx(10.0) and a["chg_pct"] == pytest.approx(15.0)  # 11,5 / 11 × 1,1
    assert a["adv20"] == a["adv14"] == 2e6 and a["atr"] == pytest.approx(1.0)
    assert a["rvol_g"] == pytest.approx(8.0) and a["rvol_z"] == pytest.approx(8.0)
    s = pop["SPL"]                                                  # tout ramené aux prix et actions du jour
    assert s["facteur"] == 0.5 and s["prev_raw"] == 20.0 and s["gap_pct"] == pytest.approx(10.0)
    assert s["adv20"] == 1e6 and s["atr"] == pytest.approx(2.0) and s["rvol_g"] == pytest.approx(8.0)
    assert pop["MID"]["rvol_g"] is None and pop["MID"]["rvol_z"] is None        # division dans les fenêtres
    assert pop["OLD"]["rvol_g"] == pytest.approx(8.0) and pop["OLD"]["rvol_z"] == pytest.approx(8.0)
    assert pop["TRO"]["rvol_z"] == pytest.approx(8000 / (12 * 1000 / 14))    # séances sans échange : 0
    assert pop["TRO"]["rvol_g"] == pytest.approx(8.0)                        # passe A : séances où elle a échangé
    n = pop["NEW"]
    assert n["seances"] == 10 and n["rvol_z"] is None and n["rvol_g"] == pytest.approx(8.0)
    assert n["atr"] is None and n["adv20"] is None and n["adv14"] is None


def test_population_incomplete_reportee(cote, monkeypatch):
    mark(cote, [DAY])
    monkeypatch.setattr(OUV, "MIN_COTE", 7)                         # journalier du jour incomplet : séance reprise
    assert OUV.population(cote.conn, DAY) is None


# --- Passe A de Gemini ------------------------------------------------------------------------------------------

def p(gap=10.0, v5=10000.0, adv20=1e5, rvol_g=5.0, seances=30, chg=1.0, o=11.0, h=11.5, l=10.8, c=11.3,  # noqa: E741
      prev=10.0):
    return {"bar": bar(OPEN, o, h, l, c, v5), "seances": seances, "facteur": 1.0, "prev_raw": prev, "gap_pct": gap,
            "chg_pct": chg, "adv20": adv20, "adv14": adv20, "atr": 1.0, "rvol_g": rvol_g, "rvol_z": rvol_g, "v5": v5}


POP_A = {"A": p(v5=50000), "B": p(v5=40000, rvol_g=3.0), "C": p(v5=30000, rvol_g=None), "D": p(gap=30.0, v5=60000, o=13.0),
         "E": p(seances=20, v5=70000), "F": p(v5=20000), "G": p(v5=10000), "H": p(v5=100, chg=40.0)}


def test_passe_a_le_tri_du_direct(monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(OUV, "MA_TOP", 3)
        m.setattr(OUV, "TG_TOP", 1)
        people = OUV.gemini_population(POP_A)
        assert people == ["E", "D", "A", "H"]                       # plus forts volumes, puis plus fortes hausses
        cands, ecartees = OUV.gemini_candidates(POP_A, people)
        assert [s for s, _, _ in cands] == ["A", "H"] and ecartees == {}  # E : 20 séances ; D : gap de +30 %
    with monkeypatch.context() as m:
        m.setattr(G, "DEEP_MAX", 5)
        m.setattr(G, "FINNHUB_MAX", 2)
        cands, ecartees = OUV.gemini_candidates(POP_A, list(POP_A))
        assert [s for s, _, _ in cands] == ["A", "F"]               # H coupée au tri volume / volume moyen ; G au 8
        assert ecartees == {"B": "volume relatif 3.0", "C": "volume relatif inconnu"}
    plans, stats = OUV.gemini_plans(DAY, POP_A)
    assert stats == {"population": 8, "plans": ["A", "F", "G", "H"],
                     "ecartees": {"B": "volume relatif 3.0", "C": "volume relatif inconnu"}}
    a = plans[0]
    assert (a["kind"], a["rang"], a["etf"], a["actif_des"], a["bar_o"]) == ("A", 1, "SPY", ts(9, 38), 11.0)
    assert (a["entry"], a["stop_range"], a["rvol"]) == (11.51, 10.79, 5.0)
    assert len(a["temoins"]) == G.TEMOINS and set(a["temoins"]) <= {"B", "C", "D", "E"}  # jamais une valeur retenue
    assert OUV.gemini_plans(DAY, POP_A)[0][0]["temoins"] == a["temoins"]                 # graine fixe


def _gapr_bars(trigger_volume=20000.0):
    """5 premières minutes à 10 000 par minute (la bougie de 9 h 30 de 50 000) : seuil de la cassure 15 000."""
    rise = {0: (11.0, 11.5, 10.8, 11.3, 10000.0), 10: (11.4, 11.6, 11.4, 11.55, trigger_volume),
            30: (11.55, 12.8, 11.55, 12.8, 5000.0)}
    return path(DAY, 11.3, rise, v=10000.0) + path(NEXT, 12.8)


def test_passe_a_soldee_par_le_code_du_direct():
    pop = {"GAPR": p(v5=50000), "W1": p(gap=0.0, o=10.0), "W2": p(gap=0.0, o=10.0), "W3": p(gap=0.0, o=10.0)}
    plans, _ = OUV.gemini_plans(DAY, pop)
    plan = plans[0]
    bars = {"GAPR": _gapr_bars(), "SPY": path(DAY, 500.0) + path(NEXT, 500.0),
            **{w: path(DAY, 10.0) + path(NEXT, 10.0) for w in ("W1", "W2", "W3")}}
    rows = OUV.gemini_rows(plan, bars, NEXT, pop)
    settle = G.settle_plan(plan, bars, NEXT, OUV.EURUSD)
    x2 = G.settle_plan(plan, bars, NEXT, OUV.EURUSD, slip_mult=2.0)
    assert [r["essai"] for r in rows] == ["gemini_a_j0", "gemini_a_j1", "gemini_a_jx"]
    for r, v in zip(rows, OUV.VARIANTS):
        assert (r["r_brut"], r["r_frais"]) == (settle[v]["r_brut"], settle[v]["r_frais"])
        assert r["r_frais_x2"] == x2[v]["r_frais"] < r["r_frais"]
        assert r["declenche"] == 1 and r["t_entree"] == ts(9, 41) and r["temoin"] == settle["temoin"]
        assert r["valeur"] == round(settle[v]["actions"] * 11.55, 2) and r["gap_pct"] == 10.0 and r["cote"] == "long"
        assert r["temoin_r_frais"] == settle[v]["temoin"]["r_frais"]
    assert rows[0]["sortie"] == "tp1,temps" and rows[0]["risque"] == pytest.approx(11.55 - settle["entree"]["stop"])
    assert "nuit" in rows[2]["detail"]
    quiet = OUV.gemini_rows(plan, bars | {"GAPR": _gapr_bars(trigger_volume=14000.0)}, NEXT, pop)  # volume < 1,5 × 5 min
    assert [(r["declenche"], r["r_frais"], r["stop"]) for r in quiet] == [(0, None, 10.79)] * 3


def test_glissement_double_du_rejeu_sans_effet_sur_le_direct():
    f = {"prix": 10.0, "stop": 9.5, "tp1": 11.0, "tp2": 12.0}
    m = {"sorties": [("tp1", 11.0, "tp1", False), ("tp2", 10.2, "temps", True)], "incomplet": False}
    base = G.result(f, m, 1.15)
    assert base == G.result(f, m, 1.15, slip_mult=1.0)
    x2 = G.result(f, m, 1.15, slip_mult=2.0)
    assert x2["r_frais"] < base["r_frais"] and x2["r_brut"] == base["r_brut"]


# --- ORB des stocks in play -----------------------------------------------------------------------------------

def sp(rvol=2.0, o=20.0, c=20.4, h=20.5, l=19.8, atr=1.0, adv14=2e6):  # noqa: E741
    return {"bar": bar(OPEN, o, h, l, c, 1000.0), "rvol_z": rvol, "atr": atr, "adv14": adv14, "gap_pct": 1.0}


def test_stocks_in_play_liste_et_sens(monkeypatch):
    pop = {"UP": sp(5.0), "DN": sp(3.0, c=19.6), "DOJI": sp(9.0, c=20.0), "NORV": sp(None), "NOATR": sp(atr=None),
           "LOW": sp(0.5), "CHEAP": sp(4.0, o=4.0, c=4.2, h=4.3, l=3.9), "THIN": sp(6.0, adv14=5e5),
           "CALMATR": sp(7.0, atr=0.4), "TIE": sp(5.0)}
    assert OUV.sip_pool(pop, False) == ["UP", "DN", "LOW", "CHEAP", "THIN", "CALMATR", "TIE"]  # sans corps : retirée
    assert OUV.sip_pool(pop, True) == ["UP", "DN", "LOW", "TIE"]    # ouverture > 5 $, 1 M d'actions, ATR > 0,50 $
    assert OUV.sip_list(pop, OUV.sip_pool(pop, False)) == ["CALMATR", "THIN", "TIE", "UP", "CHEAP", "DN"]
    monkeypatch.setattr(OUV, "SIP_TOP", 3)
    assert OUV.sip_list(pop, OUV.sip_pool(pop, False)) == ["CALMATR", "THIN", "TIE"]
    assert [OUV.side(pop[s]["bar"]) for s in ("UP", "DN", "DOJI")] == ["long", "short", None]


LONG5 = bar(OPEN, 20.2, 20.5, 20.1, 20.45, 30000.0)
SHORT5 = bar(OPEN, 19.9, 20.0, 19.6, 19.65, 10000.0)
LONG_MOVES = {0: (20.2, 20.5, 20.1, 20.45, 6000.0), 6: (20.45, 20.7, 20.45, 20.65, 3000.0)}
SHORT_MOVES = {0: (19.9, 20.0, 19.6, 19.65, 2000.0), 7: (19.65, 19.66, 19.5, 19.52, 3000.0)}


def test_stocks_in_play_entree():
    bars = path(DAY, 20.45, LONG_MOVES)
    assert OUV.sip_fill(LONG5, 1.0, "long", bars, DAY) == {"t": ts(9, 36), "prix": 20.5, "stop": pytest.approx(20.4),
                                                           "niveau": 20.5}  # la bougie du range ne déclenche pas
    assert OUV.sip_fill(LONG5, 1.0, "long", bars, DAY, "range")["stop"] == 20.1
    gap = path(DAY, 20.45, {0: LONG_MOVES[0], 5: (20.6, 20.7, 20.55, 20.65, 3000.0)})
    assert OUV.sip_fill(LONG5, 1.0, "long", gap, DAY)["prix"] == 20.6     # ouverture au-delà du niveau
    short = OUV.sip_fill(SHORT5, 1.0, "short", path(DAY, 19.65, SHORT_MOVES), DAY)
    assert (short["t"], short["prix"], short["stop"]) == (ts(9, 37), 19.6, pytest.approx(19.7))
    assert OUV.sip_fill(SHORT5, 1.0, "short", path(DAY, 19.65, SHORT_MOVES), DAY, "range")["stop"] == 20.0
    assert OUV.sip_fill(LONG5, 1.0, "long", path(DAY, 20.45, {0: LONG_MOVES[0]}), DAY) is None
    late = path(DAY, 20.45, {0: LONG_MOVES[0], 389: (20.45, 20.6, 20.45, 20.6, 1000.0)})  # 15 h 59 : trop tard
    assert OUV.sip_fill(LONG5, 1.0, "long", late, DAY) is None


def test_stocks_in_play_sortie():
    f = {"t": ts(9, 36), "prix": 20.5, "stop": 20.4}
    close = {"prix": 20.65, "raison": "cloture", "au_marche": True, "incomplet": False}
    assert OUV.sip_manage(f, "long", path(DAY, 20.45, LONG_MOVES), DAY) == close
    touched = path(DAY, 20.45, LONG_MOVES | {30: (20.65, 20.66, 20.35, 20.4, 1000.0)})
    assert OUV.sip_manage(f, "long", touched, DAY)["prix"] == 20.4
    gapped = path(DAY, 20.45, LONG_MOVES | {30: (20.3, 20.32, 20.2, 20.25, 1000.0)})
    assert OUV.sip_manage(f, "long", gapped, DAY) == {"prix": 20.3, "raison": "stop", "au_marche": True,
                                                       "incomplet": False}
    same = path(DAY, 20.45, {**LONG_MOVES, 6: (20.45, 20.7, 20.35, 20.65, 3000.0)})  # stop touché dans la minute
    assert OUV.sip_manage(f, "long", same, DAY)["prix"] == 20.4
    assert OUV.sip_manage(f, "long", same, DAY, same_minute=False) == close
    cut = OUV.sip_manage(f, "long", path(DAY, 20.45, LONG_MOVES, minutes=150), DAY)
    assert cut == {"prix": 20.65, "raison": "fin des données", "au_marche": True, "incomplet": True}
    fs = {"t": ts(9, 37), "prix": 19.6, "stop": 19.7}
    assert OUV.sip_manage(fs, "short", path(DAY, 19.65, SHORT_MOVES), DAY)["prix"] == 19.52


def test_stocks_in_play_r_aux_frais():
    f, m = {"prix": 20.5, "stop": 20.4}, {"prix": 20.65, "au_marche": True}
    entry = 20.5 + G.slip(20.5)
    shares = math.floor(K.RISK_EUR * OUV.EURUSD / (entry - 20.4) + 1e-9)
    exit_px = 20.65 - G.slip(20.65)
    pnl = shares * (exit_px - entry) - G.commission(shares, entry) - G.commission(shares, exit_px)
    assert OUV.sip_result(f, m, "long") == {"r_brut": 1.5, "r_frais": round(pnl / (shares * (entry - 20.4)), 4),
                                            "hors_mise": False, "actions": shares}
    assert OUV.sip_result(f, m, "long", slip_mult=2.0)["r_frais"] < OUV.sip_result(f, m, "long")["r_frais"]
    fs, ms = {"prix": 19.6, "stop": 19.7}, {"prix": 19.52, "au_marche": True}
    entry = 19.6 - G.slip(19.6)
    shares = math.floor(K.RISK_EUR * OUV.EURUSD / (19.7 - entry) + 1e-9)
    exit_px = 19.52 + G.slip(19.52)
    pnl = shares * (entry - exit_px) - G.commission(shares, entry) - G.commission(shares, exit_px)
    assert OUV.sip_result(fs, ms, "short") == {"r_brut": 0.8, "r_frais": round(pnl / (shares * (19.7 - entry)), 4),
                                               "hors_mise": False, "actions": shares}
    assert OUV.sip_result({"prix": 30.0, "stop": 1.0}, {"prix": 31.0, "au_marche": True}, "long")["hors_mise"]
    assert OUV.sip_result(f, {"prix": None, "au_marche": True}, "long")["r_frais"] is None


def test_stocks_in_play_lignes_et_temoin():
    pop = {"UP": {**sp(5.0, o=20.2, c=20.45, h=20.5, l=20.1), "bar": LONG5}, "DN": {**sp(3.0), "bar": SHORT5}}
    bars = {"UP": path(DAY, 20.45, LONG_MOVES), "DN": path(DAY, 19.65, SHORT_MOVES),
            "W1": path(DAY, 10.0), "W2": path(DAY, 10.0, minutes=120)}
    draws = {"UP": ["W9", "W1"], "DN": ["W2"]}                      # W9 sans bougies : le suivant sert
    rows = {r["symbol"]: r for r in OUV.sip_rows(DAY, "sip", ["UP", "DN"], pop, draws, bars)}
    up, dn = rows["UP"], rows["DN"]
    assert (up["essai"], up["rang"], up["cote"], up["entry"], up["stop"], up["declenche"]) == (
        "sip_long", 1, "long", 20.5, 20.4, 1)
    assert (up["t_entree"], up["sortie"], up["r_brut"], up["r_suivante"]) == (ts(9, 36), "cloture", 1.5, up["r_frais"])
    w = OUV.sip_witness({"t": ts(9, 36), "prix": 20.5, "stop": 20.4}, bars["W1"])
    assert up["temoin"] == "W1" and w["prix"] == 10.0 and w["stop"] == pytest.approx(10.0 * 20.4 / 20.5)
    assert up["temoin_r_frais"] == OUV.sip_result(w, OUV.sip_manage(w, "long", bars["W1"], DAY), "long")["r_frais"]
    assert (dn["essai"], dn["cote"], dn["r_brut"], dn["temoin"]) == ("sip_court", "short", 0.8, "W2")
    assert dn["temoin_r_frais"] is None and dn["temoin_r_brut"] is not None  # témoin incomplet : pas de r_frais
    ranged = OUV.sip_rows(DAY, "sip_range", ["UP"], pop, draws, bars)[0]
    assert ranged["essai"] == "sip_long_range" and ranged["stop"] == 20.1 and ranged["r_brut"] == pytest.approx(0.375)
    assert OUV.sip_rows(DAY, "sip_filtres", ["UP"], pop, draws, bars)[0]["essai"] == "sip_long_filtres"


# --- Collecte, calendrier, avancement -------------------------------------------------------------------------

async def test_collecte_de_la_bougie_de_9h30(monkeypatch):
    monkeypatch.setattr(OUV, "MIN_COTE", 2)
    monkeypatch.setattr(OUV, "OPEN5_BATCH", 2)
    days = [CO.previous_trading_day(DAY), DAY]
    monkeypatch.setattr(OUV, "open5_days", lambda conn, now=None: days)
    calls, fail = [], {"on": None, "vide": False}

    async def fake_bars(symbols, tf, start, end, transport=None, adjustment="split"):
        calls.append((list(symbols), tf, adjustment))
        if fail["on"] == len(calls):
            raise SourceError("Alpaca indisponible")
        if fail["vide"]:
            return {}
        t0 = int(start.timestamp())
        return {s: [bar(t0, 10.0, 10.2, 9.9, 10.1, 0.0 if s == "CCC" else 500.0),
                    bar(t0 + 300, 10.1, 10.3, 10.0, 10.2, 900.0)] for s in symbols}

    monkeypatch.setattr(SRC, "alpaca_bars", fake_bars)

    def fresh():
        db = LabDB(":memory:")
        for s in ("AAA", "BBB", "CCC"):
            daily(db, s)
        return db

    db = fresh()
    assert await OUV.collect_open5(db, SETTLED, 0) == 0 and not calls
    assert await OUV.collect_open5(db, SETTLED, 10) == 4            # deux séances, deux lots de deux symboles
    assert {(tf, adj) for _, tf, adj in calls} == {(CO.BASE_TF, "raw")}
    assert db.conn.execute("SELECT day, symbol, o, v FROM open5 ORDER BY day, symbol").fetchall() == [
        (days[0].isoformat(), "AAA", 10.0, 500.0), (days[0].isoformat(), "BBB", 10.0, 500.0),
        (DAY.isoformat(), "AAA", 10.0, 500.0), (DAY.isoformat(), "BBB", 10.0, 500.0)]  # CCC : rien à l'ouverture
    assert db.get(OUV.OPEN5_JOB, DAY.isoformat()) == {"symboles": 3, "bougies": 2}
    assert await OUV.collect_open5(db, SETTLED, 10) == 0              # déjà lues
    db, calls[:] = fresh(), []
    assert await OUV.collect_open5(db, SETTLED, 3) == 2               # la séance suivante dépasserait le budget
    assert set(db.keys(OUV.OPEN5_JOB)) == {days[0].isoformat()}
    db, calls[:] = fresh(), []
    assert await OUV.collect_open5(db, SETTLED, 1) == 2               # une séance entière au moins
    db, calls[:], fail["on"] = fresh(), [], 2
    assert await OUV.collect_open5(db, SETTLED, 10) == 2              # lot en échec : séance reprise plus tard
    assert db.keys(OUV.OPEN5_JOB) == {} and db.conn.execute("SELECT COUNT(*) FROM open5").fetchone()[0] == 0
    db, calls[:], fail["on"], fail["vide"] = fresh(), [], None, True
    assert await OUV.collect_open5(db, SETTLED, 10) == 4              # réponse vide sans erreur : reprise plus tard
    assert db.keys(OUV.OPEN5_JOB) == {} and db.conn.execute("SELECT COUNT(*) FROM open5").fetchone()[0] == 0
    fail["vide"] = False
    monkeypatch.setattr(OUV, "MIN_COTE", 4)
    db, calls[:] = fresh(), []
    assert await OUV.collect_open5(db, SETTLED, 10) == 0 and db.keys(OUV.OPEN5_JOB) == {}  # journalier incomplet


def test_seances_calculables_et_bloc_scelle(monkeypatch):
    db = LabDB(":memory:")
    daily(db, "AAA")
    for d in (DAY, NEXT):
        db.replace_universe("stock", d.isoformat(), [{"symbol": "AAA", "rank": 1, "reason": "volume_dollars"}])
    db.put_bars("stock", "AAA", 1440, [bar(CO.ny_midnight(NEXT), 10, 10.5, 9.5, 10, 2e6)])
    assert OUV.computed_days(db.conn, SETTLED) == [DAY]              # NEXT : séance suivante pas encore close
    early = datetime.fromtimestamp(SETTLED.timestamp() - 60, timezone.utc)
    assert OUV.computed_days(db.conn, early) == []
    assert not OUV.full_session(date(2026, 11, 27)) and OUV.full_session(DAY)  # clôture anticipée : sautée
    assert OUV.open5_days(db.conn, SETTLED) == sorted(OUV.warmup(DAY)) and len(OUV.warmup(DAY)) == OUV.WARMUP + 1
    assert OUV.ready_days(db, SETTLED) == []
    mark(db, OUV.warmup(DAY)[:-1])
    assert OUV.ready_days(db, SETTLED) == []                         # une séance d'avant manque
    mark(db, OUV.warmup(DAY))
    assert OUV.ready_days(db, SETTLED) == [DAY]
    assert OUV.progress(db.conn, SETTLED) == {"seances_a_calculer": 1, "seances_nouvelles": 0, "seances_calculees": 0,
                                              "bougies_9h30": {"lues": 25, "utiles": 25}}
    n = STATUS.declaration_state(db.conn)                            # à l'heure réelle, NEXT est calculable aussi
    assert n["seances_n12"] == 2 and not n["complete"] and n["lignes_v2"] == 11
    assert "2 séances n° 12 à calculer (bougies de 9 h 30 : 25 séances lues sur 26)" in STATUS.declaration_line(n)
    db.set(OUV.JOB, DAY.isoformat(), {"mesure": OUV.MESURE + 1})
    assert OUV.ready_days(db, SETTLED) == [DAY]                      # calculée sous une autre mesure : refaite
    db.set(OUV.JOB, DAY.isoformat(), {"mesure": OUV.MESURE})
    assert OUV.ready_days(db, SETTLED) == [] and OUV.progress(db.conn, SETTLED)["seances_calculees"] == 1
    monkeypatch.setattr(OUV, "BACKLOG_UNTIL", date(2026, 10, 20))   # séance après l'historique : avancement ordinaire
    db.set(OUV.JOB, DAY.isoformat(), {"mesure": 0})
    prog = OUV.progress(db.conn, SETTLED)
    assert (prog["seances_a_calculer"], prog["seances_nouvelles"]) == (0, 1)
    monkeypatch.setattr(seal, "SEALED_FROM", date(2026, 10, 1))
    monkeypatch.setattr(seal, "SEALED_TO", date(2026, 10, 30))
    assert OUV.computed_days(db.conn, SETTLED) == [] and OUV.ready_days(db, SETTLED) == []  # jamais calculé


# --- Une séance de bout en bout -------------------------------------------------------------------------------

MINUTES = {"GAPR": _gapr_bars(),
           "PLAY": path(DAY, 20.45, LONG_MOVES) + path(NEXT, 20.65),
           "CHEAP": path(DAY, 3.08, {0: (3.03, 3.10, 3.0, 3.08, 4000.0)}) + path(NEXT, 3.08),
           "DOWN": path(DAY, 19.65, SHORT_MOVES) + path(NEXT, 19.52),
           "CALM": path(DAY, 10.01) + path(NEXT, 10.01),
           "SPY": path(DAY, 500.0) + path(NEXT, 500.0)}


@pytest.fixture
def seance(monkeypatch):
    """GAPR : gap de +10 %, volume relatif 50 (passe A et ORB) ; PLAY : achat qui finit à +1,5 R ; CHEAP : sous 5 $,
    jamais déclenchée ; DOWN : vente à découvert ; CALM : volume relatif 0,9, témoin."""
    monkeypatch.setattr(OUV, "MIN_COTE", 5)
    db = LabDB(":memory:")
    for symbol, close, opened, first, half in (
            ("GAPR", 10.0, 11.0, (11.0, 11.5, 10.8, 11.3, 50000.0), 0.5),
            ("PLAY", 20.0, 20.2, (20.2, 20.5, 20.1, 20.45, 30000.0), 0.5),
            ("CHEAP", 3.0, 3.03, (3.03, 3.10, 3.0, 3.08, 20000.0), 0.1),
            ("DOWN", 20.0, 19.9, (19.9, 20.0, 19.6, 19.65, 10000.0), 0.5),
            ("CALM", 10.0, 10.0, (10.0, 10.05, 9.98, 10.01, 900.0), 0.5)):
        daily(db, symbol, close, opened, half=half)
        open5(db, symbol, DAY, *first)
        warm(db, symbol, close)
    mark(db, OUV.warmup(DAY))
    db.replace_universe("stock", DAY.isoformat(), [{"symbol": "PLAY", "rank": 1, "reason": "volume_dollars"}])
    calls = []

    async def fake_bars(symbols, tf, start, end, transport=None, adjustment="split"):
        calls.append((sorted(symbols), tf, adjustment, int(start.timestamp()), int(end.timestamp())))
        a, b = int(start.timestamp()), int(end.timestamp())
        return {s: [x for x in MINUTES.get(s, []) if a <= x["t"] < b] for s in symbols}

    monkeypatch.setattr(SRC, "alpaca_bars", fake_bars)
    db.calls = calls
    return db


async def test_seance_calculee_de_bout_en_bout(seance):
    assert OUV.ready_days(seance, SETTLED) == [DAY]
    assert await OUV.step(seance, SETTLED, 60) == 1
    assert {(tf, adj) for _, tf, adj, _, _ in seance.calls} == {(1, "raw")}
    both = next(c for c in seance.calls if c[4] == CO.session_ts(NEXT)[1])   # plan, témoins et SPY : deux séances
    assert {"GAPR", "SPY"} <= set(both[0]) and len(both[0]) == 2 + G.TEMOINS
    stats = seance.get(OUV.JOB, DAY.isoformat())
    assert stats["plans"] == ["GAPR"] and stats["cote"] == 5 and stats["mesure"] == 1 and stats["regles_gemini"] == 2
    assert stats["sip"] == {"sip": ["GAPR", "PLAY", "CHEAP", "DOWN"], "sip_filtres": ["GAPR", "PLAY", "DOWN"]}
    rows = {(r[0], r[1]): r[2:] for r in seance.conn.execute(
        "SELECT symbol, essai, declenche, r_brut, r_frais, r_suivante, temoin, t_entree FROM ouverture_trades")}
    assert len(rows) == 14
    assert rows[("PLAY", "sip_long")][:2] == (1, 1.5) and rows[("PLAY", "sip_long")][4] == "CALM"
    assert rows[("PLAY", "sip_long_range")][1] == pytest.approx(0.375)
    assert rows[("DOWN", "sip_court")][1] == pytest.approx(0.8) and rows[("DOWN", "sip_court_range")][1] == pytest.approx(0.2)
    assert rows[("CHEAP", "sip_long")][:2] == (0, None) and ("CHEAP", "sip_long_filtres") not in rows
    gapr = rows[("GAPR", "sip_long")]                                # stop touché dans la minute de l'entrée
    assert gapr[1] == -1.0 and gapr[3] > 0
    assert rows[("GAPR", "sip_long_range")][1] == pytest.approx(1.3 / 0.7, abs=1e-4)
    assert rows[("GAPR", "gemini_a_j0")][0] == 1 and rows[("GAPR", "gemini_a_j0")][5] == ts(9, 41)
    assert OUV.ready_days(seance, SETTLED) == [] and await OUV.step(seance, SETTLED, 60) == 0
    assert OUV.progress(seance.conn, SETTLED)["seances_calculees"] == 1
    rep = OUV.report(seance.conn, 162)
    assert (rep["numero"], rep["seances"], rep["plans_a"], rep["cote"], rep["regles_gemini"]) == (12, 1, 1, 5.0, [2])
    assert set(rep["lignes"]) == set(OUV.LABELS)
    it = rep["lignes"]["sip_long"]
    assert (it["lignes"], it["declenches"], it["mesures"], it["essai"], it["tient"]) == (3, 2, 2, True, False)
    assert "essai" not in rep["lignes"]["sip_court"] and rep["lignes"]["gemini_a_j0"]["declenches"] == 1
    text = OUV.text(rep)
    assert text.startswith("🌅 **Ouverture 5 min (n° 12)** · 1 séance du 2026-10-21 au 2026-10-21 · 1 plan A")
    assert "Stocks in play, achat, sans filtre" in text and "seuil" in text
    detail = OUV.day_detail(seance.conn, DAY.isoformat())
    assert len(detail["lignes"]) == 14 and detail["comptes"]["plans"] == ["GAPR"]
    assert OUV.day_detail(seance.conn, NEXT.isoformat()) is None
    full = SS.report(seance.conn)
    assert full["ouverture_n12"]["seances"] == 1 and "Ouverture 5 min (n° 12)" in SS.text(full)


async def test_seance_reportee_si_le_reseau_tombe(seance, monkeypatch):
    async def down(*a, **k):
        raise SourceError("Alpaca indisponible")

    monkeypatch.setattr(SRC, "alpaca_bars", down)
    assert await OUV.step(seance, SETTLED, 60) == 0 and seance.keys(OUV.JOB) == {}
    assert seance.conn.execute("SELECT COUNT(*) FROM ouverture_trades").fetchone()[0] == 0


# --- Jugement --------------------------------------------------------------------------------------------------

def _rows(n_days, x2=0.3, essai="sip_long"):
    days = CO.trading_days(date(2025, 11, 17), date(2026, 3, 31))[:n_days]
    return [{"day": d.isoformat(), "symbol": "UP", "essai": essai, "declenche": 1, "incomplet": 0, "hors_mise": 0,
             "r_brut": 0.6, "r_frais": 0.4 if i % 2 else 0.6, "r_frais_x2": x2, "r_suivante": 0.5,
             "temoin_r_frais": -0.2, "valeur": 2000.0, "risque": 0.15, "rvol": 3.0, "prix": 4.0 if i % 2 else 12.0,
             "gap_pct": 12.0} for i, d in enumerate(days)]


def test_regle_candidate_du_labo():
    rows = _rows(40) + [{**_rows(1)[0], "incomplet": 1}, {**_rows(1)[0], "r_frais": None, "hors_mise": 1}]
    it = OUV.item(rows, 162, "sip_long")
    assert (it["declenches"], it["mesures"], it["incomplets"], it["hors_mise"]) == (42, 40, 1, 1)
    assert it["r_frais"]["esperance_r"] == pytest.approx(0.5) and it["ecart_temoin"]["esperance_r"] == pytest.approx(0.7)
    assert it["seuil_t"] == SS.candidate_t(40, 162) and it["tient"] and it["reussite"] == 1.0
    assert it["par_stop"]["10-20 c"]["n"] == 40 and it["par_rvol"]["2-5"]["n"] == 40 and set(it["par_annee"]) == {
        "2025", "2026"}
    assert it["stop_minute_suivante"]["esperance_r"] == pytest.approx(0.5) and it["valeur_mediane"] == 2000.0
    assert it["par_prix"]["< 5 $"]["esperance_r"] == pytest.approx(0.4) and it["par_prix"]["5-20 $"]["n"] == 20
    assert it["par_gap"]["10-20 %"]["n"] == 40 and it["par_gap"]["4-10 %"]["n"] == 0
    assert not OUV.item(_rows(40, x2=-0.1), 162, "sip_long")["tient"]   # glissement doublé négatif
    assert not OUV.item(_rows(20), 162, "sip_long")["tient"]            # moins de 30 trades
    court = OUV.item(_rows(40, essai="sip_court"), 162, "sip_court")
    assert "tient" not in court and "seuil_t" not in court               # publiée sans essai
    rep = {"seances": 40, "du": "2025-11-03", "au": "2025-12-30", "plans_a": 0, "essais_labo": 162,
           "lignes": {"sip_long": it, "sip_court": court}}
    assert "✅ tient : candidate au bloc scellé" in OUV.text(rep).splitlines()[1]
    assert "✅" not in OUV.text(rep).splitlines()[2] and "⚠️" not in OUV.text(rep | {"regles_gemini": [2]})
    assert "(v3) : la n° 12 déclare la v2" in OUV.text(rep | {"regles_gemini": [2, 3]})
    assert OUV.text(None).startswith("🌅") and "aucune séance calculée" in OUV.text({"seances": 0,
                                                                                    "bougies_9h30_lues": 3})


# --- Route et Discord -----------------------------------------------------------------------------------------

def test_route_api_et_commande_discord(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main
    from app.notify import bot

    path_db = tmp_path / "lab.db"
    monkeypatch.setenv("JARVIS_LAB_DB", str(path_db))
    monkeypatch.setenv("JARVIS_LAB_API_KEY", "cle")
    db = LabDB(str(path_db))
    client, key = TestClient(main.app), {"X-Lab-Key": "cle"}
    r = client.get("/api/lab/ouverture", headers=key)
    assert r.status_code == 200 and r.json() == {"numero": 12, "seances": 0, "avancement": {
        "seances_a_calculer": 0, "seances_nouvelles": 0, "seances_calculees": 0,
        "bougies_9h30": {"lues": 0, "utiles": 0}}}
    assert client.get("/api/lab/ouverture", params={"jour": "2026-10-21"}, headers=key).status_code == 404
    assert client.get("/api/lab/ouverture", params={"jour": "21/10/2026"}, headers=key).status_code == 422
    assert client.get("/api/lab/ouverture").status_code in (401, 403)
    assert "rien de collecté" in bot.ouverture_text()
    db.set(OUV.JOB, DAY.isoformat(), {"mesure": OUV.MESURE, "plans": [], "population": 0, "cote": 5})
    db.conn.execute("INSERT INTO ouverture_trades (day, symbol, essai, declenche) VALUES (?, 'PLAY', 'sip_long', 0)",
                    (DAY.isoformat(),))
    r = client.get("/api/lab/ouverture", params={"jour": DAY.isoformat()}, headers=key)
    assert r.status_code == 200 and r.json()["lignes"][0]["symbol"] == "PLAY"
    assert client.get("/api/lab/ouverture", headers=key).json()["seances"] == 1
    assert bot.ouverture_text().startswith("🌅 **Ouverture 5 min (n° 12)** · 1 séance")
    assert asyncio.run(bot.handle("!n12")) == asyncio.run(bot.handle("!ouverture")) == bot.ouverture_text()
    assert "!ouverture" in bot.HELP
