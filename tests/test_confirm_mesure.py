"""Mesure des GO confirmés (app/trading/confirm.py) : brut sans objectif compté en perte, stop à l'entrée après
TP2, R à la taille de Simão (20 €, brackets et commissions IBKR), indicateurs gardés et politique affichée."""
import pytest

from app.trading import confirm as C
from app.trading import killmode as K
from tests.test_confirm_go import DAY_OPEN, T0, bar, go, scan_result


@pytest.fixture(autouse=True)
def frais_fixes(monkeypatch):
    monkeypatch.setattr(K, "RISK_EUR", 20.0)
    monkeypatch.setattr(K, "IBKR_PLAN", "fixe")


def test_brut_ouverture_au_dessus_des_objectifs():
    plat = lambda p: [bar(i, p, p, p, p) for i in range(3)]  # noqa: E731
    g = go(entry=10.0, stop=9.5, targets=[10.3, 10.6, 10.9], status="annulé")
    assert C.outcome(g, plat(10.5))["brut_r"] == 0.0   # avant : -0.07
    assert C.outcome(g, plat(10.7))["brut_r"] == 0.0   # au-dessus de TP2, avant : -0.14
    assert C.evaluate(go(entry=10.0, stop=9.5, targets=[10.3, 10.6, 10.9]), plat(10.5), DAY_OPEN)["kind"] == "annulé"


def path(*rows):
    return [bar(i, *r) for i, r in enumerate(rows)]


TARGETS = [11.0, 12.0, 13.0]  # 1 R, 2 R, 3 R pour un achat à 10, stop 9


def test_tp1_retour_a_l_entree_puis_stop():
    bars = path((10, 11.2, 9.9, 11), (11, 11, 10.0, 10.2), (10.2, 10.2, 8.9, 9))
    assert C.simulate(10, 9, TARGETS, bars) == pytest.approx(-0.33)
    assert C.simulate(10, 9, TARGETS, bars, be_after=2) == pytest.approx(-0.33)  # la règle de Simão : pas à TP1
    assert C.simulate(10, 9, TARGETS, bars, be_after=1) == pytest.approx(0.33)


def test_tp1_tp2_puis_retour_a_l_entree():
    bars = path((10, 11.2, 9.9, 11), (11, 12.2, 10.5, 12), (12, 12, 9.95, 10), (10, 10, 8.9, 9))
    assert C.simulate(10, 9, TARGETS, bars, be_after=2) == 1.0
    assert C.simulate(10, 9, TARGETS, bars, be_after=1) == 1.0
    assert C.simulate(10, 9, TARGETS, bars) == pytest.approx(0.67)  # stop fixe : le dernier tiers perd 1 R
    r, exits = C.simulate(10, 9, TARGETS, bars, be_after=2, detail=True)
    assert r == 1.0 and exits == [{"prix": 11.0, "raison": "tp1"}, {"prix": 12.0, "raison": "tp2"},
                                  {"prix": 10, "raison": "stop_be"}]


def test_stop_remonte_seulement_a_la_bougie_suivante():
    # La bougie du TP2 repasse sous l'entrée : son plus bas date d'avant le TP2, le stop n'y est pas encore remonté.
    bars = path((10, 11.2, 9.9, 11), (11, 12.2, 9.95, 11.5), (11.5, 11.5, 10.5, 11))
    r, exits = C.simulate(10, 9, TARGETS, bars, be_after=2, detail=True)
    assert r == pytest.approx(1.33) and exits[-1] == {"prix": 11, "raison": "cloture"}
    assert C.simulate(10, 9, TARGETS, bars) == pytest.approx(1.33)
    assert C.simulate(10, 9, TARGETS, [], detail=True) == (None, [])


def test_a_la_taille_de_simao():
    # 20 € à 1,15 = 23 $ pour 0,36 $ de risque : 63 actions en 3 brackets de 21, chaque ordre au minimum de 1 $.
    exits = [{"prix": 14.0, "raison": "tp1"}, {"prix": 14.35, "raison": "tp2"}, {"prix": 14.1, "raison": "cloture"}]
    r20 = C.at_size(13.81, 13.45, 13.81, exits)
    assert r20 == pytest.approx(round((21 * (0.19 + 0.54 + 0.29) - 6) / (63 * 0.36), 2))   # 0.68
    r = C.simulate(13.81, 13.45, [14.0, 14.35, 14.75], path((13.81, 14.1, 13.8, 14.05), (14.05, 14.4, 14.0, 14.3),
                                                           (14.3, 14.31, 14.0, 14.1)))
    assert r == 0.94 and r - r20 == pytest.approx(0.26, abs=0.01)  # 6 $ de commissions ≈ 0,26 R à 20 €
    stopped = [{"prix": 13.45, "raison": "stop"}] * 3
    assert C.at_size(13.81, 13.45, 13.81, stopped) == pytest.approx(-1.26)  # un stop coûte 1 R + 6 ordres
    # Une action dépasse le budget (risque de 30 $) : le MP a dit « pas de taille », la mesure à 20 € ne force plus
    # 1 action (avant : un seul bracket sur TP2, (60 − 2) / 30 R) ; compté à part (confirme_hors_mise).
    one = C.at_size(100.0, 70.0, 100.0, [{"prix": 130.0, "raison": "tp1"}, {"prix": 160.0, "raison": "tp2"},
                                         {"prix": 190.0, "raison": "tp3"}])
    assert one is None and C.over_stake(100.0, 70.0) and not C.over_stake(13.81, 13.45)
    assert C.at_size(13.81, 13.81, 13.81, exits) is None


def test_mesure_complete_d_un_go_confirme():
    g = go(status="confirmé", trigger=13.81, confirm_bar=T0 + 900)
    bars = [bar(0, 13.7, 13.75, 13.58, 13.6), bar(3, 13.64, 13.80, 13.63, 13.78),
            bar(4, 13.78, 13.9, 13.77, 13.85), bar(5, 13.85, 14.4, 13.85, 14.3), bar(6, 14.3, 14.3, 13.5, 13.6)]
    out = C.outcome(g, bars)
    assert set(out) == {"brut_r", "brut_r_be2", "confirme_r", "confirme_r_be2", "confirme_r20", "rempli"}
    assert out["rempli"] == 13.81
    # TP1 et TP2 touchés, puis repli à 13,50 : stop fixe = reste vendu à la clôture, BE TP2 = reste à l'entrée.
    assert out["confirme_r_be2"] == pytest.approx(round((0.19 + 0.54) / 0.36 / 3, 2))
    assert out["confirme_r"] == pytest.approx(round((0.19 + 0.54 + (13.6 - 13.81)) / 0.36 / 3, 2))
    assert out["confirme_r20"] < out["confirme_r"]
    assert out["brut_r_be2"] is not None


def test_indicateurs_et_sous_notes_dans_chaque_ligne(tmp_path):
    result = scan_result()
    result["picks"][0] |= {"features": {"rsi": 61.2, "rvol": 2.4, "gap_pct": 3.1, "atr_pct": 4.0}}
    result["picks"][0]["plan"]["score"]["parts"] = {"technique": {"score": 7, "weight": 0.4, "reasons": []},
                                                    "catalyst": {"score": None, "weight": 0, "reasons": []},
                                                    "risk": {"score": 6, "weight": 0.3, "reasons": []}}
    state = {}
    C.register(state, result, "2026-10-08")
    rows = C.close_day(state, {"FRSH": [bar(0, 13.7, 13.75, 13.58, 13.6), bar(1, 13.6, 13.7, 13.5, 13.65)]},
                       path=str(tmp_path / "c.jsonl"))
    row = C.read_log(str(tmp_path / "c.jsonl"))[0]
    assert rows[0] == row
    assert row["features"] == {"rsi": 61.2, "rvol": 2.4, "gap_pct": 3.1}
    assert row["parts"] == {"technique": 7, "catalyst": None, "risk": 6}
    # Un résultat de scan sans indicateurs ni sous-notes (ancien format) ne casse rien.
    C.register(state, scan_result(), "2026-10-09")
    assert state["gos"]["FRSH"]["features"] == {"rsi": None, "rvol": None, "gap_pct": None}


def test_bilan_dit_la_politique_mesuree():
    rows = [{"day": "2026-10-08", "symbol": "A", "status": "confirmé", "brut_r": -1.0, "confirme_r": 0.5},
            {"day": "2026-10-09", "symbol": "B", "status": "confirmé", "brut_r": -0.4, "brut_r_be2": -0.4,
             "confirme_r": 0.3, "confirme_r_be2": 0.4, "confirme_r20": 0.1},
            {"day": "2026-10-09", "symbol": "C", "status": "annulé", "brut_r": 0.2, "brut_r_be2": 0.3,
             "confirme_r": None, "confirme_r_be2": None, "confirme_r20": None}]
    txt = C.report_text(rows)
    assert "Politique mesurée" in txt and "ordre stop d'achat au déclencheur" in txt
    assert "remonté à l'entrée après TP2" in txt and "vendu à la clôture" in txt and "20 € de risque" in txt
    assert "par GO de départ (0 R sans ordre) vient surtout de là, en n'entrant pas" in txt
    assert "avec l'ordre confirmé +0.40 R (2 déclenchés ; BE TP2 +0.40 R sur 1 ; à 20 € +0.10 R sur 1)" in txt
    assert "• annulé : 1 · brut +0.20 R (BE TP2 +0.30 R)" in txt
    assert "1 ligne(s) écrite(s) avant le 10 octobre 2026" in txt
