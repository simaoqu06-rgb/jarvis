"""Note composite, veto souple sur le R/R, catalyseur et réglages (hors ligne)."""
from datetime import datetime, timezone

import pytest

from app.trading import catalyst as C
from app.trading import score as SC
from app.trading.settings import Settings


def plan(rr2=2.0, ok=6, tp1_r=1.0, expectancy=0.3, hit=0.5, market="stock", side="long", stop_atr=1.5):
    keys = ["trend_daily", "ema200_4h", "stoch_rsi", "vwap", "pivot", "volume"]
    items = [{"key": k, "label": k, "ok": i < ok, "detail": ""} for i, k in enumerate(keys)]
    items += [{"key": "divergence", "label": "Divergence RSI (bonus)", "ok": None, "detail": ""},
              {"key": "candle", "label": "Bougie (bonus)", "ok": None, "detail": ""}]
    target = {"r_multiple": tp1_r, "expectancy_r": expectancy, "hit_rate": hit}
    return {"side": side, "market": market, "entry": 100.0, "stop": 100 - stop_atr, "atr": 1.0,
            "stop_first_rate": 0.3, "checklist": {"items": items},
            "targets": [target, {**target, "r_multiple": rr2}, {**target, "r_multiple": rr2 + 1}]}


def daily(n=60, start=50.0, step=0.5, vol=1e6):
    return [{"t": i * 86400, "o": start + i * step, "h": start + i * step + 1, "l": start + i * step - 1,
             "c": start + i * step + (0.3 if i % 3 else -0.6), "v": vol} for i in range(n)]


@pytest.fixture
def s():
    return Settings(None)


def cat(**kw):
    base = {"available": True, "fresh_news": True, "positive": 2, "negative": 0, "news": [{}], "dilution": False,
            "last_earnings": None, "hours_to_earnings": 300.0, "errors": []}
    return base | kw


def test_plan_complet_go(s):
    r = SC.evaluate(plan(), daily(), cat(), s)
    assert r["go"] and r["composite"] >= 7


def test_rr_juste_sous_le_minimum_tolere_si_le_reste_suit(s):
    r = SC.evaluate(plan(rr2=1.9), daily(), cat(), s)
    assert r["go"]
    assert "toléré" in r["reasons"][0]


def test_rr_trop_bas_bloque(s):
    r = SC.evaluate(plan(rr2=1.5), daily(), cat(), s)
    assert not r["go"] and any("R/R" in x for x in r["reasons"])


def test_rr_juste_sous_le_minimum_bloque_si_le_reste_est_moyen(s):
    r = SC.evaluate(plan(rr2=1.9, ok=3), daily(), cat(positive=0, fresh_news=False), s)
    assert not r["go"]


def test_resultats_imminents_veto(s):
    r = SC.evaluate(plan(), daily(), cat(hours_to_earnings=20.0), s)
    assert not r["go"] and any("résultats" in x for x in r["reasons"])


def test_crypto_sans_catalyseur_renormalise(s):
    r = SC.evaluate(plan(market="crypto"), daily(), None, s)
    assert r["parts"]["catalyst"]["score"] is None and r["parts"]["catalyst"]["weight"] == 0
    assert r["composite"] == pytest.approx((r["parts"]["technique"]["score"] * 0.4 + r["parts"]["risk"]["score"] * 0.3) / 0.7, abs=0.06)


def test_dilution_et_actus_negatives_font_baisser(s):
    good = SC.catalyst(cat(), "long", s)[0]
    bad = SC.catalyst(cat(positive=0, negative=2, dilution=True), "long", s)[0]
    assert bad < 3 < good


def test_peu_liquide_penalise(s):
    liquid = SC.risk(plan(), daily(vol=1e6), s)[0]
    thin = SC.risk(plan(), daily(vol=1e3), s)[0]
    assert thin <= liquid - 3


def test_tonalite_des_titres():
    assert C.tone("Acme beats estimates and raises guidance") == "positive"
    assert C.tone("Acme downgraded at Morgan Stanley") == "negative"
    assert C.tone("Acme prices $200 million public offering") == "dilution"
    assert C.tone("What you need to know about Acme") == "neutre"


def test_actus_qui_parlent_vraiment_de_la_societe():
    assert C._about({"title": "Nvidia hits record"}, "NVDA", "NVIDIA Corp")
    assert C._about({"title": "Why NVDA is up"}, "NVDA", None)
    assert not C._about({"title": "Disney could be the next Nike"}, "NVDA", "NVIDIA Corp")


def test_heure_des_resultats():
    assert C._earnings_moment({"date": "2026-10-20", "hour": "amc"}) == datetime(2026, 10, 20, 21, tzinfo=timezone.utc)


def test_reglages_modifiables_et_retour_au_defaut(tmp_path):
    st = Settings(str(tmp_path / "s.json"))
    st.update({"rsi_max": "70", "scan_above_ma50": "non", "scan_price_max": "800"})
    again = Settings(str(tmp_path / "s.json"))
    assert again["rsi_max"] == 70.0 and again["scan_above_ma50"] is False and again["scan_price_max"] == 800.0
    again.update({"rsi_max": None})
    assert again["rsi_max"] == 75.0
    with pytest.raises(KeyError):
        again.update({"inconnu": 1})
