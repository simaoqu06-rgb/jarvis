"""Déclaration n° 11 : stop derrière le dernier creux fort, open interest sur 7 jours, profil de la veille."""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.lab import derivs as DER
from app.lab import strat_stats as SS
from app.lab import strategies as ST
from app.lab.db import STRAT_COLUMNS, LabDB
from tests.test_lab_n10 import _insert
from tests.test_lab_strategies import make_day


def test_declaration_et_essais():
    assert ST.CRYPTO_DECLARATION == 11 and ST.DECLARATION == ST.LATEST_DECLARATION == 13  # n° 12 : lignes à part
    t = ST.trials()
    assert len(t) == 165 and t[-8:-6] == list(ST.N11_TRIALS) and t[-6:] == list(ST.N12_TRIALS)
    n11 = [x for x in t if x["strategy"] in ST.N11_NAMES]
    assert len(n11) == 5 and {x["exit"] for x in n11} == {"r_net"}
    assert ST.seen_until("orb30_va", "stock") == "2026-10-16" and ST.seen_until("orb30", "stock") is None
    assert ST.seen_until("killmode", "stock") == ST.SEEN_UNTIL["stock"]
    assert {"ouverture_va", "stop_structure"} <= set(STRAT_COLUMNS)
    doc = (Path(__file__).resolve().parents[1] / "docs" / "labo-strategies.md").read_text()
    section = doc.split("## Déclaration n° 11")[1].split("## Bloc scellé")[0]
    assert "**157**" in section and "murs du carnet d'ordres est abandonnée" in section


# --- A : creux fort -------------------------------------------------------------------------

class Fake:
    def __init__(self, closes, highs, lows, atr5=None, daily_range=5.0):
        self.c, self._h, self._l = closes, highs, lows
        self.atr = atr5 or [1.0] * len(closes)
        self.daily = [{"t": k, "o": 100, "h": 100 + daily_range, "l": 100, "c": 100} for k in range(20)]

    def swing_highs(self):
        return self._h

    def swing_lows(self):
        return self._l


def test_creux_fort_suivi_d_une_cassure_de_structure():
    closes = [104] * 12
    closes[6] = 106  # clôture au-dessus du sommet de réaction (2, 105) qui précède le creux (5, 100)
    d = Fake(closes, [(2, 105.0)], [(5, 100.0)])
    assert ST.strong_low(d, 10, 108.0) == 100.0
    assert ST.strong_low(d, 7, 108.0) is None                               # creux connu à 5 + 3 = 8 seulement
    assert ST.strong_low(d, 10, 99.0) is None                               # pas sous l'entrée
    assert ST.strong_low(Fake([104] * 12, [(2, 105.0)], [(5, 100.0)]), 10, 108.0) is None  # pas de cassure
    assert ST.strong_low(Fake(closes, [], [(5, 100.0)]), 10, 108.0) is None                 # aucun sommet avant
    far = Fake([104] * 200, [(2, 105.0)], [(5, 100.0)])
    far.c[6] = 106
    assert ST.strong_low(far, 5 + ST.STRUCT_LOOKBACK + 1, 108.0) is None  # au-delà de la séance de recherche


def test_creux_fort_le_plus_recent_retenu():
    closes = [104] * 30
    closes[6], closes[16] = 106, 107
    d = Fake(closes, [(2, 105.0), (12, 106.5)], [(5, 100.0), (14, 102.0)])
    assert ST.strong_low(d, 25, 108.0) == 102.0


def test_stop_structurel_borne_par_l_atr_journalier():
    closes = [104] * 12
    closes[6] = 106
    cand = {"i": 10, "entry": 108.0, "stop": 104.0}
    d = Fake(closes, [(2, 105.0)], [(5, 100.0)], daily_range=10.0)
    stop, applied = ST.structure_stop(d, cand)
    assert applied == 1 and stop == pytest.approx(99.9)                     # creux − 0,1 ATR 5 min
    assert ST.structure_stop(Fake(closes, [(2, 105.0)], [(5, 100.0)], daily_range=5.0), cand) == (104.0, 0)
    assert ST.structure_stop(d, {**cand, "stop": 99.0}) == (99.0, 0)       # jamais plus serré que l'origine
    assert ST.structure_stop(Fake([104] * 12, [], []), cand) == (104.0, 0)  # pas de creux fort


def test_lignes_au_stop_structurel_et_ouverture():
    rows = {r["strategy"]: r for r in ST.strategy_rows(make_day(), "AAA", 3, 0.5)}
    base, struct = rows["orb30"], rows["orb30_structure"]
    assert struct["stop_structure"] in (0, 1) and base["stop_structure"] is None
    assert struct["entry"] == base["entry"] and struct["signal_at"] == base["signal_at"]
    assert struct["stop"] <= base["stop"]
    assert (struct["stop"] < base["stop"]) == bool(struct["stop_structure"])
    assert {r["ouverture_va"] for r in rows.values()} == {"dessus"}       # la veille monte, ouverture au sommet


# --- C : profil de la veille ------------------------------------------------------------------

def test_profil_de_la_veille():
    bars = [{"t": k, "o": 100, "h": 101, "l": 100, "c": 100.5, "v": 1000.0} for k in range(60)]
    bars += [{"t": 100 + k, "o": 101, "h": 110, "l": 101, "c": 109, "v": 10.0} for k in range(10)]
    poc, val, vah = ST.prior_profile(bars)
    assert 100 <= poc <= 101.2 and 100.0 <= val <= 100.4 and vah <= 101.5
    assert ST.prior_profile(bars[:ST.PROFILE_MIN_BARS - 1]) is None


# --- B : open interest sur 7 jours -----------------------------------------------------------

def test_open_interest_sur_7_jours():
    t0 = 1_700_000_000 - 1_700_000_000 % DER.OI_INTERVAL
    ts = [t0 + k * DER.OI_INTERVAL for k in range(60)]
    series = {"X": {"oi": (ts, [100.0 + k for k in range(60)])}}
    at = ts[50] + DER.OI_INTERVAL + 60  # dernier point clos : ts[50] ; 168 h plus tôt : ts[8]
    assert DER.oi7(series, "X", at) == pytest.approx((150 / 108 - 1) * 100)
    assert DER.context(series, "X", at)[1] == pytest.approx((150 / 144 - 1) * 100)
    assert DER.oi7(series, "X", ts[20]) is None and DER.oi7(series, "Y", at) is None
    assert DER.bucket(9.0, DER.OI7_BUCKETS) == "OI 7 j ≥ 9 %" and DER.bucket(-1, DER.OI7_BUCKETS) == "OI 7 j ≤ 0 %"


# --- Tableau ---------------------------------------------------------------------------------

def _days(start: str, n: int) -> list[str]:
    d0 = date.fromisoformat(start)
    return [(d0 + timedelta(days=k)).isoformat() for k in range(n)]


def test_sous_ensembles_juges_comme_des_strategies():
    db = LabDB(":memory:")
    rows = []
    for k, day in enumerate(_days("2026-10-01", 30)):
        sig = int(datetime.fromisoformat(day).replace(hour=12, tzinfo=timezone.utc).timestamp())
        for sym, r in (("HOT-USDT", -1.0), ("CALM-USDT", 0.5), ("NONE-USDT", 0.2)):
            rows.append({"market": "crypto", "symbol": sym, "day": day, "strategy": "cassure_zone_retest", "slot": 0,
                         "signal_at": sig, "triggered": 1, "r_net": r, "entry": 10.0, "stop": 9.0})
        for j, (va, r) in enumerate((("dessus", 0.4), ("dedans", -0.3), (None, 0.0))):
            rows.append({"market": "stock", "symbol": f"S{j}", "day": day, "strategy": "orb30", "slot": 0,
                         "triggered": 1, "r_net": r, "entry": 10.0, "stop": 9.0, "ouverture_va": va})
    _insert(db, rows)
    t0 = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())
    pts = []
    for k in range(60 * 6):  # 60 jours de points de 4 h
        t = t0 + k * DER.OI_INTERVAL
        pts += [("HOT-USDT", "oi", t, 100.0 * 1.003 ** k), ("CALM-USDT", "oi", t, 100.0)]
    db.conn.executemany("INSERT INTO derivs (symbol, kind, t, value) VALUES (?, ?, ?, ?)", pts)
    rep = SS.report(db.conn)
    oi7, base = rep["strategies"]["crypto"]["cassure_zone_retest_oi7"], rep["strategies"]["crypto"]["cassure_zone_retest"]
    assert base["declenches"] == 90 and oi7["declenches"] == 60                # HOT (+11 % sur 7 j) retirée
    assert oi7["esperance_r"] == pytest.approx(0.35) and oi7["jours_vus_jusqu_au"] == "2026-10-17"
    assert oi7["jours_suivants"]["declenches"] == 26 and "inconnu" in base["par_oi7"]
    assert base["par_oi7"]["OI 7 j ≥ 9 %"]["n"] == 30 and base["par_oi7"]["inconnu"]["n"] == 30
    va = rep["strategies"]["stock"]["orb30_va"]
    assert va["declenches"] == 30 and va["esperance_r"] == pytest.approx(0.4)
    assert set(rep["strategies"]["stock"]["orb30"]["par_ouverture_va"]) == {"dessus", "dedans", "inconnu"}
    text = SS.text(rep)
    assert "ouverture dessus" in text and "OI 7 j 0 à 2 % +0,50 (30), ≥ 9 % -1,00 (30)" in text and ST.LABELS["orb30_va"] in text
