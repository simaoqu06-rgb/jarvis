"""Labo, déclaration n° 12 : passe A de Gemini et ORB 5 min des « stocks in play » rejoués sur toute la cote."""
import asyncio
import json
import math
from datetime import date, datetime, time, timezone
from pathlib import Path

import pytest

from app.core.http import SourceError
from app.lab import collector as CO
from app.lab import grade as GR
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
S = 10**9                     # nanosecondes par seconde (horodatage des transactions et cotations)


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


def tick_minute(m, trades, quotes, q0):
    """Une minute relue comme OUV.minute_ticks la rend : transactions [(secondes, prix, taille, conditions)], cotations
    NBBO [(secondes, bid, ask, taille du bid, taille de l'ask)] et la cotation en vigueur à son début."""
    return {"t": [[m * S + int(x * S), p, size, cond, "C", ""] for x, p, size, cond in trades],
            "q": [[m * S + int(x * S), b, a, bs, az] for x, b, a, bs, az in quotes], "q0": q0}


def test_declaration_et_essais():
    assert ST.LATEST_DECLARATION == OUV.NUMERO == 12 and ST.DECLARATION == ST.CRYPTO_DECLARATION == 11
    assert [t["strategy"] for t in ST.N12_TRIALS] == list(OUV.TRIALS) and len(ST.trials()) == 163
    assert {t["exit"] for t in ST.N12_TRIALS} == {"r_frais"} and {t["market"] for t in ST.N12_TRIALS} == {"stock"}
    assert set(OUV.TRIALS) < set(OUV.LABELS) and len(OUV.LABELS) == 11 and set(OUV.ORB_TRIALS) < set(OUV.TRIALS)
    assert (OUV.SIP_TOP, OUV.SIP_RVOL_DAYS, OUV.SIP_RVOL_MIN, OUV.SIP_STOP_ATR) == (20, 14, 1.0, 0.10)
    assert OUV.SIP_FILTERS == {"prix_min": 5.0, "volume_min": 1_000_000, "atr_min": 0.50}
    assert (OUV.MA_TOP, OUV.TG_TOP, OUV.ACTIF_DES, OUV.ARTICLE_DES, OUV.MESURE) == (100, 50, time(9, 38), time(9, 35), 1)
    assert (OUV.TOP5, OUV.N12_MIN_TRADES, OUV.N12_MIN_SEANCES) == (5, 150, 100)    # protocole v2, C5 et D3
    assert OUV.POSITIONS_MAX == GR.RISQUE_OUVERT_MAX / GR.RISQUE_MAX["A"] == 3       # risque ouvert du grade A
    assert (OUV.TICK_ROUNDS, OUV.TICK_MINUTES_DAY, OUV.LOTS_UNTIL) == (6, 400, date(2025, 11, 3))
    assert (G.VERSION, G.SEUILS["gap_min"], G.SEUILS["gap_max"], G.SEUILS["rvol_min"]) == (2, 5.0, 25.0, 4.0)
    assert OUV.REGLES_GEMINI == G.VERSION    # règles du direct changées : nouvelle déclaration
    doc = (Path(__file__).resolve().parents[1] / "docs" / "labo-strategies.md").read_text()
    section = doc.split("## Déclaration n° 12")[1].split("## Bloc scellé")[0]
    assert "**163**" in section and "SSRN 4729284" in section and "protocole-n12-v2-fige-2026-10-27.md" in section
    assert "150 trades sur au moins 100 séances" in section and "`sip_long_top5`" in section


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
LONG_MOVES = {0: (20.2, 20.5, 20.1, 20.45, 6000.0), 9: (20.45, 20.7, 20.45, 20.65, 3000.0)}    # cassure à 9 h 39
SHORT_MOVES = {0: (19.9, 20.0, 19.6, 19.65, 2000.0), 10: (19.65, 19.66, 19.5, 19.52, 3000.0)}  # à 9 h 40


def test_stocks_in_play_entree():
    bars = path(DAY, 20.45, LONG_MOVES)
    assert OUV.sip_fill(LONG5, 1.0, "long", bars, DAY) == {
        "t": ts(9, 39), "ns": None, "seq": None, "brut": 20.5, "prix": 20.5, "mesure": False, "taille_ask": None,
        "stop": pytest.approx(20.4), "niveau": 20.5}                 # sur bougies : prix non mesuré
    assert OUV.sip_fill(LONG5, 1.0, "long", bars, DAY, "range")["stop"] == 20.1
    gap = path(DAY, 20.45, {0: LONG_MOVES[0], 9: (20.6, 20.7, 20.55, 20.65, 3000.0)})
    assert OUV.sip_fill(LONG5, 1.0, "long", gap, DAY)["brut"] == 20.6     # ouverture au-delà du niveau
    short = OUV.sip_fill(SHORT5, 1.0, "short", path(DAY, 19.65, SHORT_MOVES), DAY)
    assert (short["t"], short["prix"], short["stop"]) == (ts(9, 40), 19.6, pytest.approx(19.7))
    assert OUV.sip_fill(SHORT5, 1.0, "short", path(DAY, 19.65, SHORT_MOVES), DAY, "range")["stop"] == 20.0
    assert OUV.sip_fill(LONG5, 1.0, "long", path(DAY, 20.45, {0: LONG_MOVES[0]}), DAY) is None
    late = path(DAY, 20.45, {0: LONG_MOVES[0], 389: (20.45, 20.6, 20.45, 20.6, 1000.0)})  # 15 h 59 : trop tard
    assert OUV.sip_fill(LONG5, 1.0, "long", late, DAY) is None


def test_ordres_actifs_a_9h38():
    early = path(DAY, 20.45, {0: LONG_MOVES[0], 6: (20.45, 20.7, 20.45, 20.65, 3000.0)})  # cassure à 9 h 36
    assert OUV.franchi(LONG5, "long", early, DAY) and not OUV.franchi(LONG5, "long", path(DAY, 20.45, LONG_MOVES), DAY)
    f = OUV.sip_fill(LONG5, 1.0, "long", early, DAY)                 # encore au-delà à 9 h 38 : au marché
    assert (f["t"], f["brut"], f["stop"]) == (ts(9, 38), 20.65, pytest.approx(20.55))  # stop sur la transaction
    assert OUV.sip_fill(LONG5, 1.0, "long", early, DAY, start=OUV.ARTICLE_DES)["t"] == ts(9, 36)  # l'article
    back = path(DAY, 20.45, {0: LONG_MOVES[0], 6: (20.45, 20.7, 20.45, 20.48, 3000.0),
                             20: (20.48, 20.6, 20.48, 20.55, 1000.0)})
    f = OUV.sip_fill(LONG5, 1.0, "long", back, DAY)                  # revenu sous le niveau : la cassure suivante
    assert OUV.franchi(LONG5, "long", back, DAY) and (f["t"], f["brut"]) == (ts(9, 50), 20.5)
    down = path(DAY, 19.65, {0: SHORT_MOVES[0], 7: (19.65, 19.66, 19.5, 19.52, 3000.0)})
    assert OUV.franchi(SHORT5, "short", down, DAY) and OUV.sip_fill(SHORT5, 1.0, "short", down, DAY)["t"] == ts(9, 38)


def test_stocks_in_play_sortie():
    f = {"t": ts(9, 39), "ns": None, "brut": 20.5, "prix": 20.5, "stop": 20.4}
    close = {"t": ts(15, 59), "ns": None, "brut": 20.65, "prix": 20.65, "mesure": False, "raison": "cloture",
             "au_marche": True, "incomplet": False}
    assert OUV.sip_manage(f, "long", path(DAY, 20.45, LONG_MOVES), DAY) == close
    touched = path(DAY, 20.45, LONG_MOVES | {30: (20.65, 20.66, 20.35, 20.4, 1000.0)})
    assert (OUV.sip_manage(f, "long", touched, DAY)["t"], OUV.sip_manage(f, "long", touched, DAY)["prix"]) == (
        ts(10, 0), 20.4)
    gapped = path(DAY, 20.45, LONG_MOVES | {30: (20.3, 20.32, 20.2, 20.25, 1000.0)})
    assert OUV.sip_manage(f, "long", gapped, DAY) == {"t": ts(10, 0), "ns": None, "brut": 20.3, "prix": 20.3,
                                                       "mesure": False, "raison": "stop", "au_marche": True,
                                                       "incomplet": False}
    same = path(DAY, 20.45, {**LONG_MOVES, 9: (20.45, 20.7, 20.35, 20.65, 3000.0)})  # stop touché dans la minute
    assert OUV.sip_manage(f, "long", same, DAY)["prix"] == 20.4
    assert OUV.sip_manage(f, "long", same, DAY, same_minute=False) == close
    cut = OUV.sip_manage(f, "long", path(DAY, 20.45, LONG_MOVES, minutes=150), DAY)
    assert cut == {"t": ts(11, 59), "ns": None, "brut": 20.65, "prix": 20.65, "mesure": False,
                   "raison": "fin des données", "au_marche": True, "incomplet": True}
    fs = {"t": ts(9, 40), "ns": None, "brut": 19.6, "prix": 19.6, "stop": 19.7}
    assert OUV.sip_manage(fs, "short", path(DAY, 19.65, SHORT_MOVES), DAY)["prix"] == 19.52


def test_stocks_in_play_r_aux_frais():
    f, m = {"prix": 20.5, "stop": 20.4}, {"prix": 20.65, "au_marche": True}
    entry = 20.5 + G.slip(20.5)
    shares = math.floor(K.RISK_EUR * OUV.EURUSD / (entry - 20.4) + 1e-9)
    exit_px = 20.65 - G.slip(20.65)
    pnl = shares * (exit_px - entry) - G.commission(shares, entry) - G.commission(shares, exit_px)
    assert OUV.sip_result(f, m, "long") == {"r_brut": 1.5, "r_frais": round(pnl / (shares * (entry - 20.4)), 4),
                                            "hors_mise": False, "actions": shares, "px_entree": round(entry, 4),
                                            "px_sortie": round(exit_px, 4)}
    assert OUV.sip_result(f, m, "long", slip_mult=2.0)["r_frais"] < OUV.sip_result(f, m, "long")["r_frais"]
    fs, ms = {"prix": 19.6, "stop": 19.7}, {"prix": 19.52, "au_marche": True}
    entry = 19.6 - G.slip(19.6)
    shares = math.floor(K.RISK_EUR * OUV.EURUSD / (19.7 - entry) + 1e-9)
    exit_px = 19.52 + G.slip(19.52)
    pnl = shares * (entry - exit_px) - G.commission(shares, entry) - G.commission(shares, exit_px)
    got = OUV.sip_result(fs, ms, "short")
    assert (got["r_brut"], got["r_frais"], got["actions"]) == (0.8, round(pnl / (shares * (19.7 - entry)), 4), shares)
    out = OUV.sip_result({"prix": 30.0, "stop": 1.0}, {"prix": 31.0, "au_marche": True}, "long")
    assert out["hors_mise"] and out["actions"] is None and out["r_frais"] is None
    assert OUV.sip_result(f, {"prix": None, "au_marche": True}, "long")["r_frais"] is None
    # prix relus (protocole v2, A3) : tels quels ; le stop vient de la transaction, la taille du prix payé
    fm = {"brut": 20.52, "prix": 20.54, "mesure": True, "stop": 20.42}
    mm = {"brut": 20.38, "prix": 20.37, "mesure": True, "au_marche": True}
    shares = math.floor(K.RISK_EUR * OUV.EURUSD / (20.54 - 20.42) + 1e-9)
    pnl = shares * (20.37 - 20.54) - G.commission(shares, 20.54) - G.commission(shares, 20.37)
    got = OUV.sip_result(fm, mm, "long")
    assert got["r_brut"] == pytest.approx(-1.4) and got["r_frais"] == round(pnl / (shares * (20.54 - 20.42)), 4)
    assert (got["actions"], got["px_entree"], got["px_sortie"]) == (shares, 20.54, 20.37)
    x2 = OUV.sip_result(fm, mm, "long", slip_mult=2.0)               # robustesse : un glissement du labo de plus
    assert (x2["px_entree"], x2["px_sortie"]) == (round(20.54 + G.slip(20.54), 4), round(20.37 - G.slip(20.37), 4))
    assert OUV.article_r(fm, mm, "long") == pytest.approx((20.38 - 20.52 - 2 * OUV.ARTICLE_COMMISSION) / 0.1)
    assert OUV.article_r({"brut": 19.6, "stop": 19.7}, {"brut": 19.52}, "short") == 0.73   # frais de l'article


def test_relecture_en_transactions():
    """Protocole v2, A : ne déclenche qu'un dernier cours dans le NBBO ; achat au pire ask, stop au pire bid ; dans la
    minute de l'achat, seules les transactions qui suivent comptent ; un témoin entre au début de sa minute."""
    m = ts(9, 39)
    bars = path(DAY, 20.45, {0: LONG_MOVES[0], 9: (20.45, 20.7, 20.40, 20.65, 3000.0)})
    q0 = [m * S - S, 20.44, 20.46, 3.0, 2.0]
    data = tick_minute(m, [(2, 20.40, 100.0, "@"), (5, 20.45, 100.0, "@"), (10, 20.55, 50.0, "@I"),
                           (12, 20.56, 100.0, "@Z"), (20, 20.52, 200.0, "@"), (40, 20.70, 100.0, "@"),
                           (55, 20.65, 100.0, "@")],
                       [(15, 20.50, 20.53, 2.0, 4.0), (20.5, 20.51, 20.54, 1.0, 3.0), (39, 20.68, 20.71, 1.0, 1.0),
                        (54, 20.64, 20.66, 1.0, 1.0)], q0)
    read = K.refine(bars, {str(m): data})
    f = OUV.sip_fill(LONG5, 1.0, "long", read, DAY)                   # lot incomplet (I), hors séquence (Z) : rien
    assert (f["t"], f["ns"], f["brut"], f["prix"], f["mesure"], f["taille_ask"]) == (
        m, m * S + 20 * S, 20.52, 20.54, True, 3.0)                    # pire ask de la cotation et de la suivante
    assert f["stop"] == pytest.approx(20.42)
    assert OUV.sip_manage(f, "long", read, DAY)["raison"] == "cloture"  # 20,40 avant l'achat : ne compte pas
    fb = OUV.sip_fill(LONG5, 1.0, "long", bars, DAY)                   # la même minute sur sa bougie : le pire
    assert fb["brut"] == 20.5 and OUV.sip_manage(fb, "long", bars, DAY)["raison"] == "stop"
    assert OUV.sip_manage(fb, "long", bars, DAY, same_minute=False)["raison"] == "cloture"
    w = {"t": m, "ns": None, "brut": 20.45, "prix": 20.46, "mesure": True, "stop": 20.41}  # témoin : début de minute
    hit = OUV.sip_manage(w, "long", read, DAY, debut=True)
    assert (hit["ns"], hit["prix"], hit["mesure"], hit["raison"]) == (m * S + 2 * S, 20.40, True, "stop")
    after = tick_minute(m, [(5, 20.45, 100.0, "@"), (20, 20.52, 200.0, "@"), (45, 20.40, 100.0, "@")],
                        [(15, 20.50, 20.53, 2.0, 4.0), (20.5, 20.51, 20.54, 1.0, 3.0), (44, 20.39, 20.41, 5.0, 5.0)], q0)
    read = K.refine(bars, {str(m): after})
    out = OUV.sip_manage(OUV.sip_fill(LONG5, 1.0, "long", read, DAY), "long", read, DAY)
    assert (out["t"], out["ns"], out["brut"], out["prix"], out["mesure"], out["raison"]) == (
        m, m * S + 45 * S, 20.40, 20.39, True, "stop")                 # vendu au pire bid, après la transaction
    wide = tick_minute(m, [(20, 20.52, 200.0, "@")], [(19, 20.30, 20.35, 1.0, 1.0)], None)  # hors du NBBO à 0,5 %
    late = OUV.sip_fill(LONG5, 1.0, "long", K.refine(bars, {str(m): wide}), DAY)
    assert (late["t"], late["mesure"]) == (ts(9, 40), False)          # la minute ne déclenche rien : la suivante


def test_temoins_a_b_et_spy():
    f = {"t": ts(9, 39), "ns": None, "brut": 20.5, "prix": 20.5, "stop": 20.4}
    w1 = path(DAY, 10.0)
    w = OUV.sip_witness(f, w1, "long", [ts(9, 39) * S - S, 9.99, 10.01, 5.0, 5.0])
    assert (w["t"], w["brut"], w["prix"], w["mesure"]) == (ts(9, 39), 10.0, 10.01, True)  # ask en vigueur, relu
    assert w["stop"] == pytest.approx(10.0 * 20.4 / 20.5)               # même stop en %
    assert (OUV.sip_witness(f, w1)["prix"], OUV.sip_witness(f, w1)["mesure"]) == (10.0, False)
    assert OUV.sip_witness(f, w1, "long", [0, 10.02, 10.01, 1.0, 1.0])["mesure"] is False  # cotation croisée
    quiet = [b for b in w1 if not ts(9, 39) <= b["t"] < ts(9, 41)]     # pas d'échange à 9 h 39 ni à 9 h 40
    assert OUV.sip_witness(f, quiet)["t"] == ts(9, 41)
    assert OUV.sip_witness(f, [b for b in w1 if not ts(9, 39) <= b["t"] < ts(9, 44)]) is None
    bars = path(DAY, 20.45, LONG_MOVES)                                # B : la même valeur au marché à 9 h 38
    b = OUV.sip_immediate(LONG5, 1.0, "long", bars, DAY, "atr", [ts(9, 38) * S - S, 20.44, 20.46, 1.0, 1.0])
    assert (b["t"], b["brut"], b["prix"], b["stop"]) == (ts(9, 38), 20.45, 20.46, pytest.approx(20.35))
    assert OUV.sip_immediate(LONG5, 1.0, "long", bars, DAY, "range")["stop"] == 20.1
    under = path(DAY, 20.05, {0: LONG_MOVES[0], 1: (20.45, 20.45, 20.0, 20.05, 1000.0)})
    assert OUV.sip_immediate(LONG5, 1.0, "long", under, DAY, "range") is None  # sous le bas du range : pas de trade



def test_stocks_in_play_lignes_et_temoins():
    pop = {"UP": {**sp(5.0, o=20.2, c=20.45, h=20.5, l=20.1), "bar": LONG5}, "DN": {**sp(3.0), "bar": SHORT5}}
    bars = {"UP": path(DAY, 20.45, LONG_MOVES), "DN": path(DAY, 19.65, SHORT_MOVES),
            "W1": path(DAY, 10.0), "W2": path(DAY, 10.0, minutes=120), "SPY": path(DAY, 500.0)}
    draws = {"UP": ["W9", "W1"], "DN": ["W2"]}                      # W9 sans bougies : le suivant sert
    rows = {r["symbol"]: r for r in OUV.sip_rows(DAY, "sip", ["UP", "DN"], pop, draws, bars, seen={"UP"})}
    up, dn = rows["UP"], rows["DN"]
    assert (up["essai"], up["rang"], up["cote"], up["entry"], up["stop"], up["declenche"]) == (
        "sip_long", 1, "long", 20.5, 20.4, 1)
    assert (up["t_entree"], up["t_sortie"], up["sortie"], up["r_brut"]) == (ts(9, 39), ts(15, 59), "cloture", 1.5)
    assert up["r_suivante"] == up["r_bougies"] == up["r_frais"] and up["releve"] is None  # sans relecture : bougies
    assert (up["univers"], up["franchi"], dn["univers"], up["px_entree"]) == (1, 0, 0, round(20.5 + G.slip(20.5), 4))
    fu = {"t": ts(9, 39), "ns": None, "brut": 20.5, "prix": 20.5, "stop": 20.4}
    w = OUV.sip_witness(fu, bars["W1"])
    assert up["temoin"] == "W1" and w["prix"] == 10.0 and w["stop"] == pytest.approx(10.0 * 20.4 / 20.5)
    assert up["temoin_r_frais"] == OUV.sip_result(w, OUV.sip_manage(w, "long", bars["W1"], DAY, debut=True),
                                                  "long")["r_frais"]
    b = OUV.sip_immediate(LONG5, 1.0, "long", bars["UP"], DAY, "atr")
    assert up["temoin_b_r_frais"] == OUV.sip_result(b, OUV.sip_manage(b, "long", bars["UP"], DAY, debut=True),
                                                    "long")["r_frais"]
    s = OUV.sip_witness(fu, bars["SPY"])
    assert up["spy_r_frais"] == OUV.sip_result(s, OUV.sip_manage(s, "long", bars["SPY"], DAY, debut=True),
                                               "long")["r_frais"] < 0
    assert json.loads(up["detail"])["entree"]["brut"] == 20.5
    assert (dn["essai"], dn["cote"], dn["r_brut"], dn["temoin"]) == ("sip_court", "short", 0.8, "W2")
    assert dn["temoin_r_frais"] is None and dn["temoin_r_brut"] is not None  # témoin incomplet : pas de r_frais
    ranged = OUV.sip_rows(DAY, "sip_range", ["UP"], pop, draws, bars)[0]
    assert ranged["essai"] == "sip_long_range" and ranged["stop"] == 20.1 and ranged["r_brut"] == pytest.approx(0.375)
    assert OUV.sip_rows(DAY, "sip_filtres", ["UP"], pop, draws, bars)[0]["essai"] == "sip_long_filtres"


def test_taille_affichee_en_lots_avant_novembre_2025():
    day = date(2025, 10, 15)
    o = CO.session_ts(day)[0]
    m = o + 9 * 60                                                    # 9 h 39
    pop = {"UP": {**sp(5.0), "bar": bar(o, 20.2, 20.5, 20.1, 20.45, 30000.0)}}
    moves = {0: LONG_MOVES[0], 9: LONG_MOVES[9]}
    q0 = [m * S - S, 20.44, 20.46, 3.0, 2.0]

    def row(d, t, size):
        data = {"UP": {str(t): tick_minute(t, [(20, 20.52, 200.0, "@")], [(15, 20.50, 20.53, 2.0, size)], q0)}}
        need = {"full": set(), "q0": set()}
        return OUV.sip_rows(d, "sip", ["UP"], pop, {}, {"UP": path(d, 20.45, moves)}, data, {}, need)[0]

    lots = row(day, m, 4.0)                                           # 4 lots de 100 avant le 3 novembre 2025
    assert lots["releve"] == 1 and lots["actions"] <= 400 and lots["carnet"] == 1
    shares = row(DAY, ts(9, 39), 4.0)                                 # 4 actions depuis
    assert shares["carnet"] == 0 and shares["actions"] > 4


def test_essai_6_trois_positions():
    def r(symbol, essai, t, rang, fired=1):
        return dict.fromkeys(OUV.COLUMNS) | {"symbol": symbol, "essai": essai, "rang": rang, "declenche": fired,
                                              "r_frais": 0.5 if fired else None, "hors_mise": 0, "incomplet": 0,
                                              "temoin": "W", "temoin_b_r_frais": 0.1} | (
            {"_ordre": (t, t * S, rang)} if fired else {})

    rows = [r("A", "sip_long_range", ts(9, 50), 1), r("B", "sip_court_range", ts(9, 39), 2),
            r("C", "sip_long_range", ts(9, 41), 3), r("D", "sip_long_range", None, 4, fired=0),
            r("E", "sip_long_range", ts(9, 39), 5), r("F", "sip_long_range", ts(9, 45), 6),
            r("G", "sip_long_range", ts(9, 38), 7), r("A", "sip_long", ts(9, 38), 1)]
    top = {x["symbol"]: x for x in OUV.top5_rows(rows)}
    assert list(top) == ["A", "C", "D", "E", "F"]                    # les 5 premières vertes ; G, 6e, hors liste
    assert [x["rang"] for x in top.values()] == [1, 2, 3, 4, 5] and {x["essai"] for x in top.values()} == {
        "sip_long_top5"}
    assert [s for s, x in top.items() if x["declenche"]] == ["C", "E", "F"]  # 9 h 39, 9 h 41, 9 h 45 : trois achats
    assert (top["A"]["sortie"], top["A"]["r_frais"], top["A"]["temoin"]) == ("sautée : 3 positions", None, None)
    assert top["A"]["temoin_b_r_frais"] == 0.1 and top["E"]["temoin_b_r_frais"] is None  # B : les 3 premiers rangs


def test_replique_de_l_article():
    early = {0: LONG_MOVES[0], 6: (20.45, 20.7, 20.45, 20.65, 3000.0)}  # cassure à 9 h 36
    pop = {"UP": {**sp(5.0), "bar": LONG5}, "DN": {**sp(3.0), "bar": SHORT5}}
    bars = {"UP": path(DAY, 20.45, early), "DN": path(DAY, 19.65, SHORT_MOVES)}
    rows = {r["symbol"]: r for r in OUV.article_rows(DAY, ["UP", "DN"], pop, bars)}
    up, dn = rows["UP"], rows["DN"]
    assert (up["essai"], up["t_entree"], up["r_brut"], up["cote"]) == ("article", ts(9, 36), 1.5, "long")  # 9 h 35
    assert up["r_article"] == pytest.approx((20.65 - 20.5 - 2 * OUV.ARTICLE_COMMISSION) / 0.1)
    assert up["r_bougies"] == up["r_frais"] and up["temoin"] is None
    assert (dn["cote"], dn["r_article"]) == ("short", 0.73)           # achats et ventes, sans essai
    assert OUV.sip_rows(DAY, "sip", ["UP"], pop, {}, bars)[0]["t_entree"] == ts(9, 38)  # l'essai : 9 h 38


def test_portefeuille_contraint():
    def r(symbol, t, rang, actions, px_in, px_out, cote="long", **kw):
        return {"day": DAY.isoformat(), "symbol": symbol, "essai": "sip_long", "declenche": 1, "incomplet": 0,
                "r_frais": 0.1, "t_entree": t, "rang": rang, "cote": cote, "actions": actions, "px_entree": px_in,
                "px_sortie": px_out} | kw

    rows = [r("T1", ts(9, 39), 2, 100, 20.0, 20.5), r("T2", ts(9, 39), 1, 50, 10.0, 9.8),
            r("T3", ts(9, 45), 3, 10, 1000.0, 990.0, cote="short"), r("T4", ts(9, 50), 4, 100, 5.0, 6.0),
            r("T5", ts(9, 38), 5, 100, 5.0, 6.0, incomplet=1), r("T6", ts(9, 38), 6, 100, 5.0, 6.0, declenche=0)]
    out = OUV.portfolio(rows, "sip_long", 2000.0)
    one_r = K.RISK_EUR * OUV.EURUSD
    cash = 2000.0 * OUV.EURUSD / 3

    def pnl(n, a, b, sign=1):
        return n * sign * (b - a) - G.commission(n, a) - G.commission(n, b)

    t1, t2 = pnl(math.floor(cash / 20.0), 20.0, 20.5), pnl(50, 10.0, 9.8)  # T1 plafonnée ; T4 : 4e achat, hors jeu
    c = out["comptant"]
    assert (out["capital_eur"], c["plafond_usd"], c["trop_cheres"]) == (2000.0, round(cash, 2), 1)  # T3 : 1 000 $
    assert c["par_trade"]["n"] == 2 and c["par_trade"]["esperance_r"] == pytest.approx((t1 + t2) / 2 / one_r, abs=1e-4)
    assert c["euros"] == round((t1 + t2) / one_r * K.RISK_EUR, 2) and c["par_seance"]["n"] == 1
    t3 = pnl(math.floor(4 * cash / 1000.0), 1000.0, 990.0, -1)       # marge 4× : la vente passe
    m = out["marge_4x"]
    assert m["trop_cheres"] == 0 and m["par_trade"]["n"] == 3
    assert m["euros"] == round((pnl(100, 20.0, 20.5) + t2 + t3) / one_r * K.RISK_EUR, 2)


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


def test_seance_dont_la_suivante_est_scellee(monkeypatch):
    monkeypatch.setattr(seal, "SEALED_FROM", date(2026, 4, 6))         # le vrai bloc (conftest le déplace)
    monkeypatch.setattr(seal, "SEALED_TO", date(2026, 10, 5))
    db = LabDB(":memory:")
    for d in (date(2026, 4, 1), date(2026, 4, 2)):
        db.replace_universe("stock", d.isoformat(), [{"symbol": "AAA", "rank": 1, "reason": "volume_dollars"}])
    db.put_bars("stock", "AAA", 1440, [bar(CO.ny_midnight(DAY), 10, 10.5, 9.5, 10, 2e6)])
    assert OUV.next_trading_day(date(2026, 4, 2)) == date(2026, 4, 6) and seal.sealed("replay", date(2026, 4, 6))
    assert OUV.computed_days(db.conn, SETTLED) == [date(2026, 4, 1)]  # la passe A du 2 avril se solderait dans le bloc


# --- Une séance de bout en bout -------------------------------------------------------------------------------

MINUTES = {"GAPR": _gapr_bars(),
           "PLAY": path(DAY, 20.45, LONG_MOVES) + path(NEXT, 20.65),
           "CHEAP": path(DAY, 3.08, {0: (3.03, 3.10, 3.0, 3.08, 4000.0)}) + path(NEXT, 3.08),
           "DOWN": path(DAY, 19.65, SHORT_MOVES) + path(NEXT, 19.52),
           "CALM": path(DAY, 10.01) + path(NEXT, 10.01),
           "SPY": path(DAY, 500.0) + path(NEXT, 500.0)}
# Seule minute qu'Alpaca rend en transactions : l'achat de PLAY à 9 h 39 (les autres sont refusées pour de bon)
PLAY_939 = tick_minute(ts(9, 39), [(5, 20.45, 100.0, "@"), (20, 20.52, 200.0, "@"), (40, 20.70, 100.0, "@"),
                                   (55, 20.65, 100.0, "@")],
                       [(15, 20.50, 20.53, 2.0, 4.0), (20.5, 20.51, 20.54, 1.0, 3.0), (39, 20.68, 20.71, 1.0, 1.0),
                        (54, 20.64, 20.66, 1.0, 1.0)], [ts(9, 39) * S - S, 20.44, 20.46, 3.0, 2.0])


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

    async def fake_ticks(symbol, m, transport=None):
        db.reads.append((symbol, m))
        if (symbol, m) == ("PLAY", ts(9, 39)):
            return PLAY_939
        raise SourceError("HTTP 403")                                   # refus définitif : jouée sur sa bougie

    async def fake_quote(symbol, t, transport=None):
        db.quotes.append((symbol, t))
        px = next(b["o"] for b in MINUTES[symbol] if b["t"] == t)
        return [t * S - S, round(px - 0.01, 2), round(px + 0.01, 2), 5.0, 5.0]

    monkeypatch.setattr(SRC, "alpaca_bars", fake_bars)
    monkeypatch.setattr(OUV, "minute_ticks", fake_ticks)
    monkeypatch.setattr(OUV, "quote_at", fake_quote)
    monkeypatch.delenv("JARVIS_CAPITAL", raising=False)
    db.calls, db.reads, db.quotes = calls, [], []
    return db


async def test_seance_calculee_de_bout_en_bout(seance):
    assert OUV.ready_days(seance, SETTLED) == [DAY]
    assert await OUV.step(seance, SETTLED, 60) == 1
    assert {(tf, adj) for _, tf, adj, _, _ in seance.calls} == {(1, "raw")}
    both = next(c for c in seance.calls if c[4] == CO.session_ts(NEXT)[1])   # plan, témoins et SPY : deux séances
    assert {"GAPR", "SPY"} <= set(both[0]) and len(both[0]) == 2 + G.TEMOINS
    # minutes qui décident des achats de l'ORB (jamais des ventes) ; cotations des témoins A et B à leur entrée
    assert seance.reads == [("GAPR", ts(9, 40)), ("PLAY", ts(9, 39))]
    assert sorted(seance.quotes) == sorted([("CALM", ts(9, 39)), ("CALM", ts(9, 40)), ("CHEAP", ts(9, 38)),
                                            ("GAPR", ts(9, 38)), ("PLAY", ts(9, 38))])
    stats = seance.get(OUV.JOB, DAY.isoformat())
    assert stats["plans"] == ["GAPR"] and stats["cote"] == 5 and stats["mesure"] == 1 and stats["regles_gemini"] == 2
    assert stats["sip"] == {"sip": ["GAPR", "PLAY", "CHEAP", "DOWN"], "sip_filtres": ["GAPR", "PLAY", "DOWN"]}
    assert (stats["minutes_relues"], stats["cotations_relues"], stats["minutes_refusees"], stats["a_relire"],
            stats["plafond_releve"]) == (1, 5, 1, 0, False)
    rows = {(r[0], r[1]): r[2:] for r in seance.conn.execute(
        "SELECT symbol, essai, declenche, r_brut, r_frais, r_suivante, temoin, t_entree, releve, px_entree, carnet, "
        "r_bougies, r_article, univers FROM ouverture_trades")}
    assert len(rows) == 20                                            # 4 + 3 + 4 ORB, 3 top 5, 3 article, 3 passe A
    play = rows[("PLAY", "sip_long")]                                 # relue : transaction de 20,52, ask de 20,54
    assert (play[0], play[1], play[4], play[5], play[6], play[7], play[8], play[11]) == (
        1, 1.3, "CALM", ts(9, 39), 1, 20.54, 0, 1)                    # 3 actions à l'ask : moins que les nôtres
    assert play[9] > play[2]                                          # sur bougies : niveau de 20,50 + glissement
    assert rows[("PLAY", "sip_long_range")][1] == pytest.approx(0.13 / 0.42, abs=1e-4)
    assert rows[("DOWN", "sip_court")][1] == pytest.approx(0.8) and rows[("DOWN", "sip_court_range")][1] == pytest.approx(0.2)
    assert rows[("DOWN", "sip_court")][6] is None                     # ventes sur bougies, jamais relues
    assert rows[("CHEAP", "sip_long")][:2] == (0, None) and ("CHEAP", "sip_long_filtres") not in rows
    gapr = rows[("GAPR", "sip_long")]                                 # minute refusée : stop dans la minute de l'achat
    assert (gapr[1], gapr[6]) == (-1.0, 0) and gapr[3] > 0
    assert rows[("GAPR", "sip_long_range")][1] == pytest.approx(1.3 / 0.7, abs=1e-4)
    assert [k[0] for k in rows if k[1] == "sip_long_top5" and rows[k][0]] == ["GAPR", "PLAY"]
    assert ("CHEAP", "sip_long_top5") in rows and rows[("PLAY", "article")][10] == pytest.approx(1.43)
    assert rows[("GAPR", "article")][5] == ts(9, 40) and rows[("DOWN", "article")][10] == pytest.approx(0.73)
    assert rows[("GAPR", "gemini_a_j0")][0] == 1 and rows[("GAPR", "gemini_a_j0")][5] == ts(9, 41)
    assert OUV.ready_days(seance, SETTLED) == [] and await OUV.step(seance, SETTLED, 60) == 0
    assert OUV.progress(seance.conn, SETTLED)["seances_calculees"] == 1
    rep = OUV.report(seance.conn, 163)
    assert (rep["numero"], rep["seances"], rep["plans_a"], rep["cote"], rep["regles_gemini"]) == (12, 1, 1, 5.0, [2])
    assert rep["relecture"] == {"minutes": 1, "cotations": 5, "refusees": 1, "seances_au_plafond": 0}
    assert rep["capital"] == {"euros": 2000.0, "suppose": True} and rep["cote_par_annee"] == {"2026": 5.0}
    assert set(rep["lignes"]) == set(OUV.LABELS) and set(rep["portefeuille"]) == set(OUV.ORB_TRIALS)
    it = rep["lignes"]["sip_long"]
    assert (it["lignes"], it["declenches"], it["mesures"], it["essai"], it["tient"]) == (3, 2, 2, True, False)
    assert (it["relus"], it["au_dela_de_l_ask"], it["ecart_b"]["n"]) == (0.5, 1.0, 1)
    assert "essai" not in rep["lignes"]["sip_court"] and rep["lignes"]["gemini_a_j0"]["declenches"] == 1
    assert rep["lignes"]["article"]["r_article"]["n"] == 3 and rep["lignes"]["sip_long_top5"]["declenches"] == 2
    assert rep["portefeuille"]["sip_long_top5"]["comptant"]["par_trade"]["n"] == 2
    text = OUV.text(rep)
    assert text.startswith("🌅 **Ouverture 5 min (n° 12)** · 1 séance du 2026-10-21 au 2026-10-21 · 1 plan A")
    assert "Stocks in play, achat, sans filtre" in text and "seuil" in text and "contre l'achat à 9 h 38" in text
    assert "Top 5 avec 2 000 € (capital supposé) au comptant" in text and "sur 1 séance (" in text
    detail = OUV.day_detail(seance.conn, DAY.isoformat())
    assert len(detail["lignes"]) == 20 and detail["comptes"]["plans"] == ["GAPR"]
    assert OUV.day_detail(seance.conn, NEXT.isoformat()) is None
    full = SS.report(seance.conn)
    assert full["ouverture_n12"]["seances"] == 1 and "Ouverture 5 min (n° 12)" in SS.text(full)


async def test_seance_reportee_si_le_reseau_tombe(seance, monkeypatch):
    async def down(*a, **k):
        raise SourceError("Alpaca indisponible")

    monkeypatch.setattr(SRC, "alpaca_bars", down)
    assert await OUV.step(seance, SETTLED, 60) == 0 and seance.keys(OUV.JOB) == {}
    assert seance.conn.execute("SELECT COUNT(*) FROM ouverture_trades").fetchone()[0] == 0


async def test_seance_reportee_si_les_transactions_tombent(seance, monkeypatch):
    async def busy(symbol, m, transport=None):
        raise SourceError("HTTP 503")                                  # panne passagère : rien n'est écrit

    monkeypatch.setattr(OUV, "minute_ticks", busy)
    assert await OUV.step(seance, SETTLED, 60) == 0 and seance.keys(OUV.JOB) == {}
    assert seance.conn.execute("SELECT COUNT(*) FROM ouverture_trades").fetchone()[0] == 0


async def test_plafond_de_relecture(seance, monkeypatch):
    monkeypatch.setattr(OUV, "TICK_MINUTES_DAY", 0)
    assert await OUV.step(seance, SETTLED, 60) == 1 and seance.reads == []
    stats = seance.get(OUV.JOB, DAY.isoformat())
    assert (stats["minutes_relues"], stats["a_relire"], stats["plafond_releve"]) == (0, 2, True)
    assert seance.conn.execute("SELECT r_brut, releve FROM ouverture_trades WHERE symbol = 'PLAY' AND "
                               "essai = 'sip_long'").fetchone() == (1.5, 0)  # non relue : jouée sur sa bougie
    assert OUV.report(seance.conn, 163)["relecture"]["seances_au_plafond"] == 1


# --- Jugement --------------------------------------------------------------------------------------------------

def _rows(n_days, x2=0.3, essai="sip_long", per_day=1):
    days = CO.trading_days(date(2025, 6, 2), date(2026, 3, 31))[:n_days]
    return [{"day": d.isoformat(), "symbol": f"UP{k}", "essai": essai, "declenche": 1, "incomplet": 0, "hors_mise": 0,
             "r_brut": 0.6, "r_frais": 0.4 if i % 2 else 0.6, "r_frais_x2": x2, "r_suivante": 0.5,
             "temoin_r_frais": -0.2, "valeur": 2000.0, "risque": 0.15, "rvol": 3.0, "prix": 4.0 if i % 2 else 12.0,
             "gap_pct": 12.0, "univers": i % 2, "franchi": 0, "releve": 1, "carnet": 1, "temoin_b_r_frais": 0.1,
             "spy_r_frais": 0.0, "r_bougies": 0.2} for i, d in enumerate(days) for k in range(per_day)]


def test_regle_candidate_du_labo():
    rows = _rows(160) + [{**_rows(1)[0], "incomplet": 1}, {**_rows(1)[0], "r_frais": None, "hors_mise": 1}]
    it = OUV.item(rows, 163, "sip_long", 2300.0)
    assert (it["declenches"], it["mesures"], it["incomplets"], it["hors_mise"]) == (162, 160, 1, 1)
    assert it["r_frais"]["esperance_r"] == pytest.approx(0.5) and it["ecart_temoin"]["esperance_r"] == pytest.approx(0.7)
    assert it["seuil_t"] == SS.candidate_t(160, 163) and it["tient"] and it["reussite"] == 1.0
    assert it["effet_detectable"] == round((it["seuil_t"] + 0.8416) * it["ecart_temoin"]["se"], 4)  # 80 %, protocole D5
    assert it["par_stop"]["10-20 c"]["n"] == 160 and it["par_rvol"]["2-5"]["n"] == 160 and set(it["par_annee"]) == {
        "2025", "2026"}
    assert it["stop_minute_suivante"]["esperance_r"] == pytest.approx(0.5) and it["valeur_mediane"] == 2000.0
    assert it["par_prix"]["< 5 $"]["esperance_r"] == pytest.approx(0.4) and it["par_prix"]["5-20 $"]["n"] == 80
    assert it["par_gap"]["10-20 %"]["n"] == 160 and it["par_gap"]["4-10 %"]["n"] == 0
    # mesures publiées sans essai (protocole v2, F)
    assert it["ecart_b"]["esperance_r"] == pytest.approx(0.4) and it["ecart_spy"]["esperance_r"] == pytest.approx(0.5)
    assert it["bougies"]["esperance_r"] == pytest.approx(0.2) and (it["relus"], it["au_dela_de_l_ask"]) == (1.0, 0.0)
    assert it["par_franchi"]["pas franchi"]["n"] == 160 and it["par_franchi"]["franchi avant 9 h 38"]["n"] == 0
    assert it["par_univers"]["vue à 9 h 10"]["n"] == it["par_univers"]["manquée à 9 h 10"]["n"] == 80
    assert it["au_dela_du_capital"] == {"1 ×": 0.0, "2 ×": 0.0, "4 ×": 0.0} and it["valeur_p90"] == 2000.0
    assert it["sans_2_meilleurs"]["n"] == 158 and [h["n"] for h in it["moities_t"]] == [80, 80]
    assert it["moities_ecart"] == [pytest.approx(0.7), pytest.approx(0.7)]
    assert not OUV.item(_rows(160, x2=-0.1), 163, "sip_long")["tient"]   # glissement doublé négatif
    assert not OUV.item(_rows(140), 163, "sip_long")["tient"]            # moins de 150 trades
    assert not OUV.item(_rows(75, per_day=2), 163, "sip_long")["tient"]  # 150 trades sur moins de 100 séances
    assert OUV.item(_rows(100, per_day=2), 163, "sip_long")["tient"]
    court = OUV.item(_rows(160, essai="sip_court"), 163, "sip_court")
    assert "tient" not in court and "seuil_t" not in court and "effet_detectable" not in court  # sans essai
    rep = {"seances": 160, "du": "2025-06-02", "au": "2026-01-15", "plans_a": 0, "essais_labo": 163,
           "lignes": {"sip_long": it, "sip_court": court}}
    first = OUV.text(rep).splitlines()[1]
    assert "✅ tient : candidate au bloc scellé" in first and "contre l'achat à 9 h 38 +0,400" in first
    assert "effet détectable +0," in first
    assert "✅" not in OUV.text(rep).splitlines()[2] and "⚠️" not in OUV.text(rep | {"regles_gemini": [2]})
    assert "(v3) : la n° 12 déclare la v2" in OUV.text(rep | {"regles_gemini": [2, 3]})
    top5 = {"comptant": {"par_seance": {"n": 3, "esperance_r": 0.25}, "euros": 15.0}}
    line = OUV.text(rep | {"capital": {"euros": 5000.0, "suppose": False}, "portefeuille": {"sip_long_top5": top5}})
    assert "- Top 5 avec 5 000 € au comptant : +15 € sur 3 séances (+0,250 R par séance)" in line
    assert OUV.text(None).startswith("🌅") and "aucune séance calculée" in OUV.text({"seances": 0,
                                                                                    "bougies_9h30_lues": 3})


def test_capital_de_simao(monkeypatch):
    monkeypatch.delenv("JARVIS_CAPITAL", raising=False)
    assert OUV.capital() == (2000.0, True)                           # supposé, et dit
    monkeypatch.setenv("JARVIS_CAPITAL", "3500")
    assert OUV.capital() == (3500.0, False)


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
