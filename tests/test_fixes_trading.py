"""Tests de non-régression des corrections du moteur de trading (plans, indicateurs, note, réglages,
scanner, catalyseur)."""
from datetime import datetime, timezone

import pytest

from app.core.http import SourceError
from app.trading import catalyst as C
from app.trading import indicators as I
from app.trading import plan as P
from app.trading import scanner as SC
from app.trading import score as SCORE
from app.trading import signals as SIG
from app.trading.settings import DEFAULTS, Settings


def bars(closes, spread=1.0, start=0, step=3600):
    return [{"t": start + i * step, "o": c, "h": c + spread, "l": c - spread, "c": c, "v": 1.0}
            for i, c in enumerate(closes)]


# --- 1. TP du mauvais côté de l'entrée ----------------------------------------------------------

def test_tp_jamais_du_mauvais_cote_de_l_entree(monkeypatch):
    # Résistance juste au-dessus de l'entrée : avec la marge de 0,1 ATR, le TP passerait sous l'entrée.
    monkeypatch.setattr(P, "_candidate_levels", lambda *a, **k: [P.Target(100.01, "sommet de swing")])
    lv = P.default_levels(bars([100.0] * 30), "long", entry=100.0, stop=99.9)
    assert all(t.price > lv.entry for t in lv.targets)


# --- 2. Espacement minimal des TP complétés en multiples de R -----------------------------------

def test_tp_completes_espaces_d_au_moins_un_demi_r(monkeypatch):
    # ATR 2, risque 2 : niveau à 106,2 → TP1 à 106,0 = 3 R… placé ici à 2,9 R (105,8).
    monkeypatch.setattr(P, "_candidate_levels", lambda *a, **k: [P.Target(106.0, "sommet de swing")])
    lv = P.default_levels(bars([100.0] * 30), "long", entry=100.0, stop=98.0)
    prices = [t.price for t in lv.targets]
    assert prices[0] == pytest.approx(105.8)
    for a, b in zip([lv.entry] + prices, prices):
        assert b - a >= P.MIN_TARGET_R * lv.risk - 1e-9


# --- 3. Arrondi des micro-prix et risque nul ----------------------------------------------------

def test_micro_prix_niveaux_distincts():
    lv = P.default_levels(bars([1.1e-05] * 30, spread=1e-07), "long")
    prices = [t.price for t in lv.targets]
    assert lv.stop < lv.entry < prices[0] < prices[1] < prices[2]
    assert P.round_price(1.23456789e-05) == pytest.approx(1.2345679e-05)


def test_risque_nul_refuse_proprement():
    with pytest.raises(ValueError, match="ATR"):
        P.default_levels(bars([100.0] * 30, spread=0.0), "long")
    with pytest.raises(ValueError, match="risque nul"):
        P.historical_timing(bars([100.0] * 60), P.Levels("long", 100.0, 100.0, [P.Target(101.0, "t")]), 5)


def plan_dict(atr=1.0, rr2=2.0):
    items = [{"key": k, "label": k, "ok": True} for k in SCORE.CORE + SCORE.BONUS]
    tps = [{"r_multiple": r, "expectancy_r": 0.3, "hit_rate": 0.5} for r in (1.0, rr2, 3.0)]
    return {"side": "long", "market": "crypto", "entry": 1.1e-05, "stop": 1.06e-05, "atr": atr,
            "targets": tps, "stop_first_rate": 0.2, "checklist": {"items": items}}


def test_note_de_risque_sans_atr_mesurable():
    _, reasons = SCORE.risk(plan_dict(atr=0.0), None, DEFAULTS)
    assert "stop en ATR non mesurable (ATR indisponible)" in reasons


# --- 4. Séance du jour avant l'ouverture US -----------------------------------------------------

def session(day, closes):
    open_ = int(datetime(2026, 10, day, 13, 30, tzinfo=timezone.utc).timestamp())  # 9 h 30 à New York
    return bars(closes, spread=1.0, start=open_, step=900)


def test_avant_l_ouverture_la_veille_est_la_derniere_seance_complete():
    candles = session(5, [100.0] * 26) + session(6, [110.0] * 26)
    pre_open = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)  # 8 h à New York
    assert I.vwap(candles, "stock", pre_open) is None
    assert I.previous_session(candles, "stock", pre_open)["day"] == "2026-10-06"
    assert I.previous_session(candles, "stock", pre_open)["h"] == 111.0
    # En séance : inchangé.
    intraday = datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc)
    assert I.vwap(candles, "stock", intraday) == pytest.approx(110.0)
    assert I.previous_session(candles, "stock", intraday)["day"] == "2026-10-05"
    # Crypto : la dernière bougie fait foi, quelle que soit l'heure.
    assert I.previous_session(candles, "crypto", pre_open)["day"] == "2026-10-05"
    # Checklist et niveaux suivent la même séance.
    check = SIG.checklist(candles, None, None, "stock", "long", 1.0, now=pre_open)
    vwap_item = next(i for i in check["items"] if i["key"] == "vwap")
    assert vwap_item["ok"] is None and "pas encore ouverte" in vwap_item["detail"]
    levels = P._candidate_levels(candles, "stock", "long", 105.0, pre_open)
    assert any(t.source == "plus haut de la veille" and t.price == 111.0 for t in levels)


# --- 5. Scanner : pas d'exclusion « ETF ? » sur un profil Finnhub en échec ----------------------

async def test_profil_finnhub_en_echec_n_exclut_pas(monkeypatch):
    async def ok(*a, **k):
        return []

    async def timeout(*a, **k):
        raise SourceError("délai dépassé")

    async def unknown(*a, **k):
        raise SourceError("ABC : profil inconnu")

    monkeypatch.setattr(C.finnhub, "earnings", ok)
    monkeypatch.setattr(C.finnhub, "company_news", ok)
    monkeypatch.setattr(C.finnhub, "profile", timeout)
    now = datetime(2026, 10, 6, 13, 0, tzinfo=timezone.utc)
    cat = await C.check("ABC", now)
    assert cat["available"] and cat["market_cap"] is None and cat["company"] is None
    monkeypatch.setattr(C.finnhub, "profile", unknown)
    assert (await C.check("ABC", now))["company"] is False

    f = {"price": 50.0, "gap_pct": 3.0, "rvol": None, "atr_pct": 4.0, "rsi": 60.0, "ma20": 40.0, "ma50": 35.0}

    async def fake(value):
        return value

    monkeypatch.setattr(SC.alpaca, "configured", lambda: True)
    monkeypatch.setattr(SC, "universe", lambda s: fake(["ABC"]))
    monkeypatch.setattr(SC.alpaca, "snapshots", lambda symbols: fake({"ABC": {"price": 50.0}}))
    monkeypatch.setattr(SC.alpaca, "bars", lambda *a, **k: fake({"ABC": [{"c": 50.0}]}))
    monkeypatch.setattr(SC, "features", lambda *a: f)
    monkeypatch.setattr(SC.C, "safe_check", lambda sym, now: fake(cat))
    monkeypatch.setattr(SC.T, "build_plan", lambda *a, **k: fake({"score": {"composite": 7.0}}))
    result = await SC.scan(now, s=Settings(None), save=False)
    assert [p["symbol"] for p in result["picks"]] == ["ABC"] and not result["skipped"]


# --- 6. Actus : la société par son nom, en mots entiers ------------------------------------------

def test_actu_reconnue_par_un_nom_distinctif():
    title = lambda t: {"title": t}  # noqa: E731
    assert not C._about(title("The market rallies as traders cheer"), "TTD", "The Trade Desk Inc")
    assert C._about(title("Trade Desk shares jump on earnings"), "TTD", "The Trade Desk Inc")
    assert not C._about(title("Advanced driver-assistance stocks rally"), "AMD", "Advanced Micro Devices Inc")
    assert C._about(title("Advanced Micro Devices unveils new chip"), "AMD", "Advanced Micro Devices Inc")
    assert not C._about(title("Metaverse hype fades"), "META", "Meta Platforms Inc")
    assert C._about(title("Meta beats estimates"), "META", "Meta Platforms Inc")
    assert C._about(title("Nvidia hits record"), "NVDA", "NVIDIA Corp")


# --- 7. Dilution : ni « non-dilutive » ni rachat de convertibles --------------------------------

def test_dilution_sans_negation_ni_rachat():
    assert C.tone("XYZ secures $50M non-dilutive financing") != "dilution"
    assert C.tone("XYZ adopts anti-dilution provisions") != "dilution"
    assert C.tone("XYZ announces convertible notes repurchase") != "dilution"
    assert C.tone("XYZ completes redemption of convertible notes") != "dilution"
    assert C.tone("XYZ prices $200M public offering") == "dilution"
    assert C.tone("XYZ issues convertible notes due 2030") == "dilution"
    assert C.tone("Shareholders fear dilutive deal") == "dilution"


# --- 8. Réglages : types vérifiés clé par clé ---------------------------------------------------

def test_reglages_valides_par_type():
    st = Settings(None)
    st.update({"scan_max_results": "4", "scan_universe": "nvda, amd", "rr_min": "2,5"})
    assert st["scan_max_results"] == 4 and isinstance(st["scan_max_results"], int)
    assert st["scan_universe"] == ["NVDA", "AMD"] and st["rr_min"] == 2.5
    for bad in ({"rr_min": 0}, {"weights": {"technique": 1}}, {"weights": {"technique": "x", "catalyst": 1, "risk": 1}},
                {"scan_universe": 5}, {"scan_max_results": "4.5"}, {"scan_enabled": "peut-être"},
                {"scan_time_ny": "25:00"}, {"go_min": "abc"}):
        with pytest.raises(ValueError):
            st.update(bad)
    with pytest.raises(ValueError):  # rien n'est appliqué si une valeur est mauvaise
        st.update({"go_min": 6.0, "rr_min": -1})
    assert st["go_min"] == 7.0


# --- 9. Scanner : volume de la veille inconnu ---------------------------------------------------

def test_texte_du_scanner_sans_rvol():
    plan = {"entry": 50.0, "stop": 48.0, "stop_pct": -4.0, "targets": [], "catalyst": None,
            "score": {"go": True, "decision": "GO", "composite": 7.5, "reasons": [],
                      "parts": {"technique": {"score": 8}, "catalyst": {"score": None}, "risk": {"score": 7}}}}
    result = {"at": "2026-10-06T13:10:00+00:00", "examined": 1, "passed": 1, "skipped": [], "rejected": {},
              "picks": [{"symbol": "ABC", "plan": plan,
                         "features": {"gap_pct": 3.0, "rvol": None, "rsi": 60.0, "atr_pct": 4.0}}]}
    assert "volume veille n/d" in SC.text(result)


# --- 10. R/R minimal nul ------------------------------------------------------------------------

def test_technique_sans_division_par_zero():
    score, _ = SCORE.technique(plan_dict(), None, DEFAULTS | {"rr_min": 0})
    assert 0 <= score <= 10


# --- 11. ATR et stop sur les bougies closes ----------------------------------------------------

def test_atr_et_stop_ignorent_la_bougie_en_cours():
    candles = bars([100.0] * 30)
    candles[-1] = {**candles[-1], "h": 150.0, "l": 50.0}  # bougie en cours, énorme amplitude
    lv = P.default_levels(candles, "long")
    assert lv.stop == pytest.approx(98.0)  # 1 ATR sous l'entrée (dernier creux à 99 + marge, ramené à 1 ATR)
    assert lv.atr == pytest.approx(2.0)


# --- 12. Délai au milieu de la bougie qui touche le TP ------------------------------------------

def test_delai_compte_au_milieu_de_la_bougie():
    candles = bars([100.0 + i for i in range(100)], spread=0.5)
    lv = P.Levels("long", 100.0, 97.0, [P.Target(100.5, "test")])
    timing = P.historical_timing(candles, lv, horizon_bars=10)
    assert timing["targets"][0]["bars_median"] == 0.5
