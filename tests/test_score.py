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


# --- Finnhub muet hors scanner, labo inchangé (vérification P4, A) --------------------------------

def finnhub_down(**kw):
    """Ce que renvoie catalyst.check quand aucun appel Finnhub ne répond."""
    return {"available": False, "earnings_known": False, "name": None, "market_cap": None, "company": None,
            "next_earnings": None, "hours_to_earnings": None, "last_earnings": None, "news": [], "positive": 0,
            "negative": 0, "dilution": False, "fresh_news": False,
            "errors": ["profil : délai", "résultats : délai", "actus : délai"]} | kw


def test_finnhub_en_panne_catalyseur_neutre_et_veto(s):
    p = plan(ok=5)
    r = SC.evaluate(p, daily(), finnhub_down(), s)
    tech, rsk = r["parts"]["technique"]["score"], r["parts"]["risk"]["score"]
    assert r["parts"]["catalyst"]["score"] == 5.0 and r["parts"]["catalyst"]["weight"] == 0.3
    assert r["parts"]["catalyst"]["reasons"][0].startswith("catalyseur non vérifié")
    assert r["composite"] == round(tech * 0.4 + 5.0 * 0.3 + rsk * 0.3, 1)
    assert r["decision"] == "NO GO" and "date des résultats non vérifiée (Finnhub sans réponse)" in r["reasons"]
    # Le labo (generator, live) note ses actions avec cat=None : note renormalisée, aucun veto ajouté.
    lab = SC.evaluate(p, daily(), None, s)
    assert lab["composite"] == round((tech * 0.4 + rsk * 0.3) / 0.7, 1) and lab["go"]
    assert lab["parts"]["catalyst"]["score"] is None and lab["parts"]["catalyst"]["weight"] == 0


def test_actus_ok_et_calendrier_muet_veto(s):
    r = SC.evaluate(plan(), daily(), cat(earnings_known=False, hours_to_earnings=None), s)
    assert r["decision"] == "NO GO" and r["reasons"] == ["date des résultats non vérifiée (Finnhub sans réponse)"]
    assert SC.evaluate(plan(), daily(), cat(), s)["go"]
    assert SC.evaluate(plan(market="crypto"), daily(), {"available": False}, s)["parts"]["catalyst"]["score"] is None


# --- Note non prédictive au labo, affichée à côté du GO (P4, B) ----------------------------------

@pytest.fixture
def lab_slope(tmp_path, monkeypatch):
    from app.lab import stats as STATS
    from app.lab.db import LabDB

    path = tmp_path / "lab.db"
    monkeypatch.setenv("JARVIS_LAB_DB", str(path))
    monkeypatch.setattr(SC, "_slopes", {"key": None, "at": 0.0, "data": {}})

    def write(stock, crypto=None):
        db = LabDB(str(path))
        data = {"marches": {m: {"calibration_note": {"pente_r_par_point": v}}
                            for m, v in (("stock", stock), ("crypto", crypto)) if v is not None}}
        STATS.store(db.conn, "courant", data, 10, "2026-10-09T22:00:00+00:00")
        db.close()
        SC._slopes.update(key=None)
    return write


def scored(market="stock"):
    p = plan(market=market)
    return p | {"score": SC.evaluate(p, daily(), cat() if market == "stock" else None, Settings(None)), "catalyst": {}}


def test_note_non_predictive_affichee_partout_ou_le_go_s_affiche(lab_slope):
    from app.trading import scanner
    from app.trading import service as T
    from tests.test_lab3_live import fake_plan

    assert SC.slope_note("stock") is None  # pas de base du labo : rien à dire
    lab_slope(-0.0204, 0.012)
    assert SC.slope_note("stock") == "note non prédictive au labo (pente −0,020 R/pt, actions)"
    assert SC.slope_note("crypto") is None  # pente positive : pas de mention
    lines = "\n".join(T.score_lines(scored()))
    assert ("-# Note non prédictive au labo (pente −0,020 R/pt, actions) : note et sous-notes masquées, GO = règle "
            "de sélection figée, pas une prédiction") in lines
    assert "/10" not in lines and lines.startswith("✅ **GO**")  # audits du 25/10 : note masquée
    crypto = "\n".join(T.score_lines(scored("crypto")))
    assert "non prédictive" not in crypto and "/10" in crypto  # pente positive : la note reste
    result = {"at": "2026-10-07T13:10:00+00:00", "examined": 3, "passed": 1, "skipped": [], "rejected": {},
              "picks": [{"symbol": "AAA", "features": {"gap_pct": 3.0, "rvol": 2.0, "rsi": 60, "atr_pct": 4.0},
                         "plan": fake_plan("AAA")}]}
    assert "-# Note non prédictive au labo (pente −0,020 R/pt, actions) : le GO est une règle de sélection figée" \
        in scanner.text(result)
    assert "/10" not in scanner.text(result)
    assert "non prédictive" not in scanner.text(result | {"picks": []})


def test_pente_lue_une_fois_par_periode(lab_slope, monkeypatch):
    lab_slope(-0.03)
    assert SC.lab_slopes() == {"stock": -0.03}
    lab_slope(0.05)
    assert SC.lab_slopes() == {"stock": 0.05}
    monkeypatch.setattr(SC, "_slopes", SC._slopes | {"data": {"stock": -1.0}})
    assert SC.lab_slopes() == {"stock": -1.0}  # en cache : pas relue avant SLOPE_TTL
