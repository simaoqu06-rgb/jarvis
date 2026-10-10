"""Statisticien du labo : mesures, intervalles de confiance en grappes, découpages, régimes, capture."""
import math
from datetime import date, timedelta

import pytest

from app.lab import collector as CO
from app.lab import regime as REG
from app.lab import stats as S
from app.lab.db import LabDB
from tests.lab_fixtures import synthetic_trades


@pytest.fixture
def db(tmp_path):
    return LabDB(str(tmp_path / "lab.db"))


# --- Briques pures --------------------------------------------------------------------------

def test_erreur_type_en_grappes_nulle_si_tous_les_jours_identiques():
    days = {"d1": [2.0, 4], "d2": [2.0, 4], "d3": [2.0, 4]}
    assert S.cluster_se(days, 0.5, 12) == 0.0


def test_erreur_type_en_grappes_valeur_connue():
    days = {"d1": [1.0, 1], "d2": [-1.0, 1]}  # moyenne 0, u = ±1, G/(G-1) = 2 → √(2·2)/2 = 1
    assert S.cluster_se(days, 0.0, 2) == pytest.approx(1.0)


def test_erreur_type_inconnue_avec_un_seul_jour():
    assert S.cluster_se({"d1": [3.0, 3]}, 1.0, 3) is None
    m, se, ci = S.mean_ci({"d1": [3.0, 3]}, 3.0, 3)
    assert m == 1.0 and se is None and ci is None


def test_ic_en_t_de_student():
    # 10 jours (10 trades par jour, +0,25 ou −0,05 R) : se = 0,05, demi-largeur t(0,975 ; 9) × se = 2,262 × se.
    days = {f"d{i}": [(0.25 if i % 2 else -0.05) * 10, 10] for i in range(10)}
    m, se, ci = S.mean_ci(days, 10.0, 100)
    assert m == pytest.approx(0.1) and se == pytest.approx(0.05)
    assert (ci[1] - ci[0]) / 2 == pytest.approx(2.2622 * se, abs=1e-4)
    assert ci[0] < 0 < m - S.Z95 * se  # avec 1,96, la borne basse passait au-dessus de zéro


def test_seuil_de_degradation_en_student():
    # Bonferroni à 5 % sur ~80 tranches, en t à G − 1 degrés de liberté : ≈ 3,79 à 20 jours, 3,2 à l'infini.
    assert S.t_quantile(S.DEGRADE_P, 19) == pytest.approx(3.79, abs=0.01)
    assert S.t_quantile(S.DEGRADE_P, 100_000) == pytest.approx(S.DEGRADE_Z, abs=0.03)


def test_grappes_plus_larges_que_l_erreur_naive_quand_les_jours_se_ressemblent():
    # 10 jours, 20 trades par jour, tous les trades d'un jour identiques : 10 observations, pas 200.
    days = {f"d{i}": [(1.0 if i % 2 else -1.0) * 20, 20] for i in range(10)}
    se = S.cluster_se(days, 0.0, 200)
    naive = 1.0 / math.sqrt(200)
    assert se > 3 * naive


def test_p_unilateral():
    assert S.p_one_sided(0.0) == pytest.approx(0.5)
    assert S.p_one_sided(1.959964) == pytest.approx(0.025, abs=1e-4)


def test_accumulateur_taux_et_delais():
    acc = S.Acc()
    acc.add("d1", None, (0, 0, 0), (None, None, None))           # non déclenché
    acc.add("d1", 1.5, (1, 1, 0), (1, 0, None))
    acc.add("d2", -1.0, (0, 0, 0), (None, None, None))
    acc.add("d2", 0.5, (1, 0, 0), (1, None, None))
    r = acc.result(min_n=3, detail=True)
    assert r["plans"] == 4 and r["declenches"] == 3
    assert r["taux_declenchement"] == 0.75
    assert r["esperance_r"] == pytest.approx(1 / 3, abs=1e-4)
    assert r["reussite"] == pytest.approx(2 / 3, abs=1e-4)
    assert r["tp1_avant_stop"] == pytest.approx(2 / 3, abs=1e-4) and r["tp2"] == pytest.approx(1 / 3, abs=1e-4)
    assert r["delais_tenus"] == pytest.approx(2 / 3, abs=1e-4)
    assert r["delais_par_tp"][0] == {"tp": 1, "n": 2, "tenus": 1.0}
    assert r["echantillon_faible"] is False and r["jours"] == 2


def test_garde_fou_petit_echantillon():
    acc = S.Acc()
    for i in range(29):
        acc.add(f"d{i}", 0.1, (0, 0, 0), (None, None, None))
    assert acc.result()["echantillon_faible"] is True
    acc.add("d99", 0.1, (0, 0, 0), (None, None, None))
    assert acc.result()["echantillon_faible"] is False


def test_delai_tenu():
    assert S.delay_ok(1, 50, 60) == 1
    assert S.delay_ok(1, 61, 60) == 0
    assert S.delay_ok(0, 50, 60) is None
    assert S.delay_ok(1, 50, None) is None


def test_serie_de_pertes_et_baisse_maximale():
    c = S.Curve()
    for r in (1, -1, -1, -1, 2, -0.5, -0.5, 3):
        c.add(r)
    res = c.result()
    assert res["pire_serie_pertes"] == 3
    assert res["baisse_max_r"] == 3.0  # pic 1 → creux -2
    assert res["cumul_r"] == 2.0


def test_tranches_de_note():
    assert S.note_bucket(7.9) == "7" and S.note_bucket(8.0) == "8"
    assert S.note_bucket(None) == "n/d" and S.note_bucket(11.2) == "10" and S.note_bucket(-1) == "0"


def _bucket(mean: float, n: int, days: int = 30) -> S.Acc:
    acc = S.Acc()
    for i in range(n):
        acc.add(f"d{i % days}", mean + (0.5 if i % 2 else -0.5), (0, 0, 0), (None, None, None))
    return acc


def test_calibration_note_croissante():
    cal = S.calibration({"6": _bucket(-0.2, 100), "7": _bucket(0.0, 100), "8": _bucket(0.3, 100)})
    assert cal["ordre_respecte"] == "2/2"
    assert cal["pente_r_par_point"] == pytest.approx(0.25, abs=0.01)
    assert cal["paires"][1]["de"] == 7 and cal["paires"][1]["a"] == 8 and cal["paires"][1]["ecart_r"] > 0


def test_calibration_ignore_les_petites_tranches():
    cal = S.calibration({"7": _bucket(0.0, 100), "8": _bucket(0.3, 10)})
    assert cal["paires"] == [] and cal["ordre_respecte"] is None and cal["pente_r_par_point"] is None


def test_contraste_tranche_contre_reste():
    whole, bad = S.Acc(), S.Acc()
    for i in range(400):
        day = f"d{i % 40}"
        r = (0.2 if i % 2 else -0.0) + (-1.0 if i % 4 == 0 else 0.0)
        if i % 4 == 0:
            bad.add(day, r, (0, 0, 0), (None, None, None))
        whole.add(day, r, (0, 0, 0), (None, None, None))
    c = S.contrast(bad, whole)
    assert c["ecart_r"] < -0.9 and c["z"] < -3 and c["reste_n"] == 300


# --- Base -----------------------------------------------------------------------------------

def test_mesures_et_decoupages_sur_la_base(db):
    synthetic_trades(db, n_days=40, per_day=10, seed=2)
    synthetic_trades(db, n_days=30, per_day=5, seed=3, market="crypto")
    st = S.compute(db.conn)
    assert st["global"]["plans"] == 400 + 150
    assert set(st["marches"]) == {"stock", "crypto"}
    par = st["marches"]["stock"]["par"]
    assert set(par) == set(S.DIMENSIONS)
    assert set(par["setup"]) <= {"cassure_pivot", "rebond_pivot", "divergence", "tendance_sans_declencheur"}
    assert list(par["heure"]) == ["09h10 NY"]
    assert list(st["marches"]["crypto"]["par"]["heure"])[0].endswith("UTC")
    assert set(par["jour_semaine"]) == {"lundi", "mardi", "mercredi", "jeudi", "vendredi"}
    assert set(par["periode"]) == {"recent", "ancien"}
    assert par["periode"]["recent"]["jours"] == S.RECENT_DAYS
    assert par["regime"] == {"inconnu": par["regime"]["inconnu"]}
    assert sum(v["plans"] for v in par["setup"].values()) == 400


def test_filtres(db):
    synthetic_trades(db, n_days=20, per_day=5, seed=2)
    synthetic_trades(db, n_days=20, per_day=5, seed=4, origin="live", first=date(2024, 3, 1))
    assert S.compute(db.conn, origin="live")["global"]["plans"] == 100
    assert S.compute(db.conn, market="crypto")["global"]["plans"] == 0
    st = S.compute(db.conn, since="2024-01-08", until="2024-01-12")
    assert st["global"]["plans"] == 25 and st["filtres"] == {"since": "2024-01-08", "until": "2024-01-12"}


def test_tranche_degradee_signalee_et_petite_tranche_jamais(db):
    def effect(ctx, rnd):
        return -0.8 if ctx["setup"] == "divergence" else 0.1

    synthetic_trades(db, n_days=80, per_day=20, seed=5, effect=effect)
    st = S.compute(db.conn)
    found = [d for d in st["degradations"] if d["dimension"] == "setup"]
    assert [d["valeur"] for d in found] == ["divergence"]
    # Seuil en t de Student sur les jours de la tranche (80 au plus ici) : plus exigeant que 3,2.
    assert all(d["n"] >= S.MIN_TRADES and d["z"] <= -S.t_quantile(S.DEGRADE_P, 79) < -S.DEGRADE_Z
               for d in st["degradations"])


def test_tranche_de_trois_jours_jamais_degradee(db):
    """Direct crypto du 9 octobre : 3 jours très corrélés (849 trades) jugés comme 849 trades
    indépendants donnaient z −9,3. Il faut DEGRADE_MIN_DAYS jours avant de signaler une tranche."""
    synthetic_trades(db, n_days=80, per_day=50, seed=8, market="crypto", base=-0.2)
    synthetic_trades(db, n_days=3, per_day=300, seed=9, market="crypto", base=-0.75, noise=0.8,
                     origin="live", first=date(2024, 6, 1))
    assert not [d for d in S.compute(db.conn)["degradations"] if d["dimension"] == "origine"]


def test_tranche_assez_longue_toujours_signalee(db):
    synthetic_trades(db, n_days=80, per_day=50, seed=8, market="crypto", base=-0.2)
    synthetic_trades(db, n_days=S.DEGRADE_MIN_DAYS, per_day=50, seed=9, market="crypto", base=-0.75, noise=0.8,
                     origin="live", first=date(2024, 6, 1))
    found = [d for d in S.compute(db.conn)["degradations"] if d["dimension"] == "origine"]
    assert [d["valeur"] for d in found] == ["live"]


def test_bruit_pur_sans_degradation(db):
    synthetic_trades(db, n_days=80, per_day=20, seed=6)
    assert S.compute(db.conn)["degradations"] == []


def test_capture_du_potentiel(db):
    synthetic_trades(db, n_days=5, per_day=4, seed=7)
    day = "2024-01-02"
    rows = db.conn.execute("SELECT symbol, triggered, tp1_hit FROM trades WHERE day = ? ORDER BY symbol", (day,)).fetchall()
    for sym, *_ in rows:
        up = 12.0 if sym in ("S000", "S001") else 2.0
        db.conn.execute("INSERT INTO day_moves (market, day, symbol, open, up_pct) VALUES ('stock', ?, ?, 10, ?)",
                        (day, sym, up))
    db.conn.execute("INSERT INTO day_moves (market, day, symbol, open, up_pct) VALUES ('stock', ?, 'ZZZ', 10, 20)", (day,))
    db.conn.execute("UPDATE trades SET triggered = 1, tp1_hit = 1, r_net = 1 WHERE day = ? AND symbol = 'S000'", (day,))
    db.conn.execute("UPDATE trades SET triggered = 0, tp1_hit = 0, r_net = NULL WHERE day = ? AND symbol = 'S001'", (day,))
    cap = S.capture(db.conn)
    assert cap["mouvements"] == 3 and cap["avec_plan"] == 2 and cap["declenches"] == 1 and cap["tp1"] == 1
    assert cap["taux_capture"] == pytest.approx(1 / 3, abs=1e-3)
    assert cap["actifs_planifies"] == 4 and cap["part_des_plans"] == 0.5
    assert S.capture(db.conn, threshold=15)["mouvements"] == 1


def test_cache_aller_retour(db):
    synthetic_trades(db, n_days=5, per_day=3, seed=1)
    data = S.compute(db.conn)
    S.store(db.conn, "courant", data, 15, "2026-10-06T00:00:00+00:00")
    back = S.cached(db.conn)
    assert back["global"] == data["global"] and back["trades_base"] == 15
    assert S.cached(db.conn, "autre") is None


# --- Régimes --------------------------------------------------------------------------------

def _index_bars(market: str, first: date, n: int, trend: float = 0.001) -> list[dict]:
    out, p = [], 100.0
    for i in range(n):
        d = first + timedelta(days=i)
        p *= 1 + trend + (0.01 if i % 3 == 0 else -0.005)
        t = CO.ny_midnight(d) if market == "stock" else CO.utc_midnight(d)
        out.append({"t": t, "o": p, "h": p, "l": p, "c": p, "v": 1.0})
    return out


def test_regime_sans_regard_vers_l_avenir():
    bars = _index_bars("crypto", date(2024, 1, 1), 400)
    day = date(2024, 12, 1)
    before = REG.series(bars, "crypto", [day])[day.isoformat()]
    future = [b if b["t"] < CO.utc_midnight(day) else {**b, "c": b["c"] * 0.2} for b in bars]
    assert REG.series(future, "crypto", [day])[day.isoformat()] == before
    assert before["metrics"]["as_of"] == CO.utc_midnight(day) - 86400  # clôture de la veille


def test_regime_tendance_et_tiers():
    up = REG.series(_index_bars("crypto", date(2024, 1, 1), 300, 0.003), "crypto", [date(2024, 10, 1)])
    down = REG.series(_index_bars("crypto", date(2024, 1, 1), 300, -0.006), "crypto", [date(2024, 10, 1)])
    assert up["2024-10-01"]["trend"] == "haussier" and down["2024-10-01"]["trend"] == "baissier"
    assert up["2024-10-01"]["vol_tercile"] in (0, 1, 2)
    assert up["2024-10-01"]["label"].startswith("haussier_")


def test_regime_inconnu_sans_historique():
    res = REG.series(_index_bars("crypto", date(2024, 1, 1), 30), "crypto", [date(2024, 1, 20)])
    assert res["2024-01-20"]["label"] == "inconnu"
    short = REG.series(_index_bars("crypto", date(2024, 1, 1), 70), "crypto", [date(2024, 3, 5)])
    assert short["2024-03-05"]["label"].endswith("_inconnu")  # tendance connue, pas assez de volatilités


def test_regime_enregistre_et_utilise_par_les_stats(db):
    synthetic_trades(db, n_days=10, per_day=5, seed=1, market="crypto", first=date(2024, 12, 1))
    db.put_bars("crypto", "BTC-USDT", 1440, _index_bars("crypto", date(2024, 1, 1), 360))
    assert REG.refresh(db, "crypto") == 10
    assert REG.refresh(db, "crypto") == 0  # déjà connus
    par = S.compute(db.conn)["marches"]["crypto"]["par"]["regime"]
    assert "inconnu" not in par and sum(v["plans"] for v in par.values()) == 50
