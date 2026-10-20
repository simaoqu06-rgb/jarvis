"""Délais rejoués honnêtes : départs qui se chevauchent (fenêtres indépendantes, IC de Wilson), « 3 cas sur 4 »
ramené à tous les cas, et retard qui ne décide SORTIR que si rien de mesurable ne dit de garder (depuis la PR #30,
la mesure « garder jusqu'au prochain objectif » est retirée des conseils)."""
import random
import statistics
from datetime import datetime, timezone

from app.trading import advice as A
from app.trading import plan as P
from app.trading import service as S
from tests.test_advice import NOW as NOW_ADVICE
from tests.test_advice import trade as trade_advice
from tests.test_trading import NOW, T0, bars, trade

LEVELS = P.Levels("long", 100.0, 98.0, [P.Target(102.0, "a"), P.Target(103.0, "b"), P.Target(104.0, "c")])


def walk(n, seed, vol=0.004):
    """Marche aléatoire de bougies de 15 min (graine fixe ; random() est reproductible d'une version à l'autre)."""
    rnd = random.Random(seed)
    c, out = 100.0, []
    for i in range(n):
        o = c
        c = o * (1 + (rnd.random() - 0.5) * 3.4 * vol)
        out.append({"t": i * 900, "o": o, "h": max(o, c) * (1 + vol / 3), "l": min(o, c) * (1 - vol / 3), "c": c,
                    "v": 1.0})
    return out


def test_fenetres_independantes_et_ic_de_wilson():
    timing = P.historical_timing(walk(1553, 1), LEVELS, 52)
    assert timing["starts"] == 1500 and timing["effective_starts"] == 1500 // 52
    # Non-régression : les clés d'avant gardent leurs valeurs (calculées avec le code d'avant ce changement).
    assert timing["stop_first_rate"] == 0.395
    assert [(t["hit_rate"], t["bars_p75"]) for t in timing["targets"]] == [(0.549, 28.5), (0.357, 37.5), (0.194, 42.5)]
    lo, hi = timing["stop_first_ci95"]
    assert lo <= timing["stop_first_rate"] <= hi
    for t in timing["targets"]:
        lo, hi = t["hit_ci95"]
        assert lo <= t["hit_rate"] <= hi and hi - lo > 0.2  # 28 fenêtres : IC large, pas 1 500 tirages
    assert P.wilson(0.5, 0) == [0.0, 1.0]
    assert P.wilson(0.0, 10)[0] == 0.0 and P.wilson(1.0, 10)[1] == 1.0


def test_l_ic_a_n_effectif_couvre_la_dispersion_reelle():
    """30 marches aléatoires : le hit_rate varie bien plus que ne le dirait l'erreur naïve sur tous les départs,
    et l'IC de Wilson à starts // horizon couvre la moyenne des graines dans au moins 85 % des cas."""
    runs = [P.historical_timing(walk(800, 100 + seed), LEVELS, 26) for seed in range(30)]
    rates = [r["targets"][0]["hit_rate"] for r in runs]
    mean = statistics.mean(rates)
    naive = (mean * (1 - mean) / runs[0]["starts"]) ** 0.5
    assert statistics.pstdev(rates) >= 2 * naive
    covered = sum(r["targets"][0]["hit_ci95"][0] <= mean <= r["targets"][0]["hit_ci95"][1] for r in runs)
    assert covered >= 0.85 * len(runs)


def test_p75_ramene_a_tous_les_cas():
    timing = P.historical_timing(walk(1553, 1), LEVELS, 52)
    tp1, tp2, tp3 = timing["targets"]
    assert 0.4 <= tp1["hit_rate"] <= 0.6
    for t in (tp1, tp2, tp3):
        assert abs(t["reached_by_p75"] - 0.75 * t["hit_rate"]) <= 0.05
        assert t["reached_by_p75"] <= t["hit_rate"] and 0 <= t["still_after_p75"] <= 1
    assert tp1["reached_by_p75_given_prev"] is None
    # TP2 une fois TP1 pris : la part se lit sur les départs qui ont touché TP1, pas sur tous.
    assert abs(tp2["reached_by_p75_given_prev"] - tp2["reached_by_p75"] * timing["starts"] / tp1["samples"]) <= 0.01
    assert tp2["reached_by_p75_given_prev"] > tp2["reached_by_p75"]


def test_jamais_touche_pas_de_part():
    t = P.historical_timing(bars([100.0] * 60, spread=0.1), P.Levels("long", 100.0, 98.0, [P.Target(105.0, "t")]),
                            5)["targets"][0]
    assert t["hit_rate"] == 0 and t["reached_by_p75"] is None and t["still_after_p75"] is None


def test_alerte_de_retard_dit_la_part_de_tous_les_cas():
    tr = trade()
    tr["targets"][0] |= {"eta_latest": "2026-10-06T02:00:00+00:00", "reached_by_p75": 0.3, "still_after_p75": 0.25,
                         "reached_by_p75_given_prev": None}
    msg = S.apply_bars(tr, bars([100.5], spread=0.2, start=T0, step=60), NOW)[0]
    assert "pas encore touché" in msg and "3 cas passés sur 4" not in msg
    assert "30 % des cas passés l'avaient atteint" in msg and "25 % l'ont touché ensuite" in msg


def test_alerte_de_retard_du_tp2_sur_les_cas_qui_ont_touche_tp1():
    tr = trade()
    tr["targets"][1] |= {"eta_latest": "2026-10-06T02:00:00+00:00", "reached_by_p75": 0.2, "still_after_p75": 0.1,
                         "reached_by_p75_given_prev": 0.43}
    msgs = S.apply_bars(tr, bars([101.0, 102.5], spread=0.5, start=T0, step=60), NOW)
    assert "TP1 touché" in msgs[0]
    assert "43 % des cas passés qui avaient touché TP1 l'avaient atteint" in msgs[-1]


def test_alerte_de_retard_d_un_trade_suivi_avant_les_mesures():
    tr = trade()
    tr["targets"][0]["eta_latest"] = "2026-10-06T02:00:00+00:00"
    msg = S.apply_bars(tr, bars([100.5], spread=0.2, start=T0, step=60), NOW)[0]
    assert "3 sur 4 des cas passés qui l'ont touché" in msg and "3 cas passés sur 4" not in msg


def test_textes_du_plan():
    closes = [100 + 0.2 * i + (1.5 if i % 7 == 0 else 0) for i in range(400)]
    now = datetime(2026, 10, 7, 19, 45, tzinfo=timezone.utc)
    hourly = [{"t": int(now.timestamp()) - (401 - i) * 3600, "o": c, "h": c + 0.8, "l": c - 0.8, "c": c, "v": 1.0}
              for i, c in enumerate(closes)]
    p = S.plan_from_bars("BTC", "crypto", "long", hourly, None, None, now)
    assert "départs qui se chevauchent (≈" in p["method"] and "fenêtres indépendantes au moins" in p["method"]
    assert "depuis chacune" not in p["method"]
    reached = next(t for t in p["targets"] if t["eta_latest"])
    assert "pour 3 sur 4 des cas qui l'ont touché (" in reached["eta_text"]
    assert "% de tous les cas)" in reached["eta_text"]
    assert reached["hit_ci95"] and reached["reached_by_p75"] is not None
    assert "touché avant le stop" in S.plan_text(p) and "(IC 95 % : " in S.plan_text(p)
    assert len(p["stop_first_ci95"]) == 2


def bars_advice(closes, step):
    """Bougies d'heure finies juste avant NOW_ADVICE."""
    return bars(closes, spread=0.3, start=int(NOW_ADVICE.timestamp()) - (len(closes) + 1) * step, step=step)


def late_trade(price, entry):
    t = trade_advice(entry, stop=entry - 2.0, last_price=price,
                     targets=[{"name": f"TP{i}", "price": entry + 2.0 * i, "eta_latest": None, "eta_text": "~2 h"}
                              for i in (1, 2, 3)])
    t["targets"][0]["eta_latest"] = "2026-10-07T12:00:00+00:00"  # dépassé à NOW
    return t


def test_retard_decide_sortir_si_rien_n_est_mesurable():
    closes = [100 + 0.2 * i for i in range(40)]  # aucune mesure « garder » depuis la PR #30
    price = closes[-1]
    a = A.advise(late_trade(price, entry=price + 0.5), price, bars_advice(closes, 3600), None, None, NOW_ADVICE)
    assert a["late"] and a["r_now"] <= 0 and "hold" not in a
    assert a["decision"] == "SORTIR" and "en retard" in a["why"]
