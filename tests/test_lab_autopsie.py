"""Autopsie des trades (autopsie.py), grade indicatif des GO confirmés et brief enrichi du soir."""
from datetime import date, datetime, timezone

import pytest

from app.lab import autopsie as AUT
from app.lab import brief as BRIEF
from app.lab import grade as GRADE
from app.lab import seal as SEAL
from app.lab.db import LabDB
from tests.lab_fixtures import synthetic_trades

NOW = datetime(2026, 10, 10, 20, 0, tzinfo=timezone.utc)


def _days(n, first=date(2025, 1, 6)):
    return [date.fromordinal(first.toordinal() + k).isoformat() for k in range(n)]


def test_elan_au_niveau_du_hasard_et_stops_jamais_partis():
    acc = AUT.Autopsy()
    for k, day in enumerate(_days(50)):
        acc.add(day, 1.0, "tp3", 1.4, -0.3, 1, 1.0)        # +1 R d'abord
        acc.add(day, -1.0, "stop", 0.05, -1.0, 0, 1.0)     # jamais parti
    m = acc.result()
    assert m["trades"] == 100 and m["elan_1r"]["part"] == 0.5 and m["elan_1r"]["repere_theorique"] == 0.5
    assert m["stops"]["part"] == 0.5 and m["stops"]["jamais_partis"] == 1.0 and m["stops"]["gain_rendu"] == 0.0
    assert m["gagnants"]["frole_stop"] == 0.0 and m["sans_tp1"]["presque_tp1"] == 0.0
    assert not any("élan" in p for p in m["pistes"])          # l'élan ne produit jamais de piste
    assert any("entrée trop tôt" in p for p in m["pistes"])


def test_pistes_sans_elan_gain_rendu_stop_dans_le_bruit_objectif_loin():
    acc = AUT.Autopsy()
    for k, day in enumerate(_days(40)):
        acc.add(day, 0.8, "cloture", 1.2, -0.9, 1, 1.0)    # gagnant qui a frôlé le stop
        for _ in range(3):
            acc.add(day, -1.0, "stop", 1.1 if k % 2 else 0.8, -1.0, 0, 1.0)  # stops, la moitié avait touché +1 R
    m = acc.result()
    assert m["elan_1r"]["part"] == pytest.approx(0.625, abs=1e-3)   # 1 + 1,5 sur 4 en moyenne
    assert m["stops"]["gain_rendu"] == 0.5 and m["gagnants"]["frole_stop"] == 1.0
    assert m["sans_tp1"]["presque_tp1"] == 1.0
    text = " ".join(m["pistes"])
    assert "gain rendu" in text and "stop dans le bruit" in text and "objectif loin" in text
    assert "élan" not in text


def test_echantillon_faible():
    acc = AUT.Autopsy()
    acc.add("2025-01-06", -1.0, "stop", 0.0, -1.0, 0, 1.0)
    assert acc.result() == {"trades": 1, "echantillon_faible": True}


def _labelled(db):
    synthetic_trades(db, n_days=60, per_day=10)
    db.conn.execute("UPDATE trades SET mfe_r = CASE WHEN r_net > 0 THEN 1.5 ELSE 0.2 END, mae_r = -0.4, "
                    "plan = json_set(plan, '$.targets[0].r_multiple', 1.0)")


def test_rapport_v1_par_setup_hors_scelle_et_strategies():
    db = LabDB(":memory:")
    _labelled(db)
    lo, _ = SEAL.bounds()
    db.conn.execute("INSERT INTO trades (origin, method_version, market, symbol, side, day, planned_at, triggered, "
                    "exit_reason, r_net, tp1_hit, mfe_r, mae_r, context) VALUES ('replay', 'v1', 'stock', 'SEAL', 'long', "
                    "?, 1, 1, 'stop', -1.0, 0, 0.0, -1.0, '{\"setup\": \"scelle\"}')", (lo,))
    db.conn.executemany(
        "INSERT INTO strat_trades (origin, market, symbol, day, strategy, slot, triggered, r_net, exit_reason, mfe_r, "
        "mae_r, tp1_hit) VALUES ('replay', 'stock', ?, ?, 'orb30', 0, 1, -1.0, 'stop', 0.3, -1.0, 0)",
        [(f"S{k}", d) for k, d in enumerate(_days(40))])
    rep = AUT.report(db.conn)
    stock = rep["v1"]["stock"]
    n = db.conn.execute("SELECT COUNT(*) FROM trades WHERE triggered = 1 AND symbol != 'SEAL'").fetchone()[0]
    assert stock["ensemble"]["trades"] == n and "scelle" not in stock
    assert set(stock) - {"ensemble"} <= {"cassure_pivot", "rebond_pivot", "divergence", "tendance_sans_declencheur"}
    orb = rep["strategies"]["stock"]["orb30"]
    assert orb["trades"] == 40 and orb["elan_1r"]["part"] == 0.0 and orb["sans_tp1"]["presque_tp1"] == 0.0
    assert not any("élan" in p for p in orb["pistes"])
    assert rep["limites"] and rep["references"]["elan_1r"].startswith("50 %")


def _go_rows(db, days=130):
    for k, day in enumerate(_days(days)):
        rows = [(f"S{j}", day, "go_confirme", int(st == "confirme"), (0.4 if k % 2 else -0.2) if st == "confirme" else None,
                 st) for j, st in enumerate(("confirme", "confirme", "annule", "expire"))]
        db.conn.executemany(
            "INSERT INTO strat_trades (origin, market, symbol, day, strategy, slot, triggered, r_sans_be, confirmation, "
            "tp1_hit) VALUES ('replay', 'stock', ?, ?, ?, 0, ?, ?, ?, 0)", rows)


def test_grade_indicatif_par_trade_et_par_go():
    db = LabDB(":memory:")
    _go_rows(db)
    gi = AUT.go_indicatif(db.conn)
    conf = gi["go_confirme"]
    assert conf["sortie"] == "r_sans_be"
    assert conf["par_trade"]["trades"] == 2 * GRADE.FENETRE_JOURS and conf["par_trade"]["jours"] == GRADE.FENETRE_JOURS
    assert conf["par_trade"]["esperance_r"] == pytest.approx(0.1) and conf["par_trade"]["grade"]
    assert conf["par_go"]["go"] == 520 and conf["par_go"]["esperance_r"] == pytest.approx(0.05, abs=1e-3)
    assert gi["go_confirme_plancher"]["par_trade"]["esperance_r"] is None   # aucune ligne : non noté
    lines = AUT.indicatif_lines(gi)
    assert any("non noté" in x for x in lines) and any("par GO de départ" in x for x in lines)


def test_step_met_en_cache_et_ne_recalcule_pas_sans_changement():
    db = LabDB(":memory:")
    _labelled(db)
    assert AUT.step(db, NOW, 0) is not None
    assert AUT.cached(db.conn)["v1"]["stock"]["ensemble"]["trades"] > 0
    assert AUT.step(db, NOW, 0) is None


def test_textes():
    assert "pas encore calculée" in AUT.text(None)
    db = LabDB(":memory:")
    _labelled(db)
    _go_rows(db)
    data = AUT.report(db.conn)
    txt = AUT.text(data)
    assert "Autopsie" in txt and "repère théorique 50 %" in txt and "Grade indicatif" in txt
    assert "crypto" not in AUT.text(data, "stock").lower().split("autopsie")[1][:40]


def test_brief_porte_detail_strategies_et_autopsie_dans_la_limite(tmp_path):
    db = LabDB(":memory:")
    _labelled(db)
    from app.lab import stats as STATS
    STATS.store(db.conn, "courant", STATS.compute(db.conn), 1, "2026-10-10T20:00:00+00:00")
    AUT.step(db, NOW, 0)
    data = BRIEF.build(db.conn, NOW, str(tmp_path / "lab.db"))
    assert data["autopsie"]["v1"]["stock"]["ensemble"]["elan_1r"]["part"] is not None
    assert "setup" in data["detail"]["stock"] and data["strategies"] is None
    assert BRIEF.size(data) <= BRIEF.MAX_BYTES


def test_drop_detail():
    d = {"stock": {"setup": {}, "note": {}, "jour_semaine": {}}, "crypto": {"note": {}}}
    assert BRIEF._drop_detail(d) and "jour_semaine" not in d["stock"]
    assert BRIEF._drop_detail(d) and d == {"stock": {"setup": {}}, "crypto": {}}
    assert not BRIEF._drop_detail(d) and not BRIEF._drop_detail(None)


def test_trades_reels_et_lignes_du_grade():
    trades = [{"market": "stock", "status": "closed", "side": "long", "entry": 10.0, "stop": 9.0, "targets_hit": 0,
               "targets": [{"price": 11.0}], "events": [{"kind": "stop", "price": 9.0}]},
              {"market": "stock", "status": "closed", "side": "long", "entry": 10.0, "stop": 9.0, "targets_hit": 1,
               "targets": [{"price": 12.0}], "events": []},
              {"market": "stock", "status": "closed", "killmode": True, "side": "long", "entry": 10.0, "stop": 9.0,
               "targets_hit": 1, "targets": [{"price": 12.0}], "events": []}]
    real = GRADE.reels(trades)
    assert {k: real["stock"][k] for k in ("termines", "esperance_r", "total_r", "gagnants")} == \
        {"termines": 2, "esperance_r": 0.5, "total_r": 1.0, "gagnants": 1}
    # Nets des frais estimés : 23 actions (20 € × 1,15 sur 1 $ de risque), un bracket, 1 $ à l'entrée et à la sortie.
    assert real["stock"]["esperance_net_r"] == pytest.approx(0.5 - 2 / 23, abs=1e-3)
    assert real["stock"]["total_net_r"] == pytest.approx(1.0 - 4 / 23, abs=0.01)
    assert real["crypto"]["termines"] == 0
    txt = GRADE.populations_text(["-# GO confirmés : E indicatif"], real)
    assert "Ce que tu trades vraiment" not in txt
    assert "GO confirmés, stop fixe, vendus à la clôture** (≠ ton suivi : stop à l'entrée après TP2, pas de sortie " \
           "au temps)" in txt
    assert "**Tes trades réels** (R nets de frais estimés" in txt and "Politique mesurée : ton suivi `!prends`" in txt
    assert "2 terminés" in txt and "+0.41 R en moyenne (+0.50 brut)" in txt and "trop peu" in txt
    assert GRADE.populations_text([], {"stock": {"termines": 0}}) == ""


def test_strategies_triees_par_trades_avec_esperance_a_20_euros():
    """Scénario 3 de GPT : une stratégie qui perd à 20 € mais touche souvent +1 R ne doit pas paraître bonne à
    côté du repère « 50 % » : chaque ligne dit son espérance (actions : r_20e de la ligne), tri par trades."""
    db = LabDB(":memory:")
    _labelled(db)  # v1 : sans elle, le marché n'est pas affiché
    rows = [(f"S{k}", d, "orb30", -0.1, -0.4, "cloture", 1.5, -0.2, 1) for k, d in enumerate(_days(40))]  # élan 100 %
    rows += [(f"T{k}", d, "orb15", 0.3, None if k < 10 else 0.2, "stop", 0.2, -1.0, 0)
             for k, d in enumerate(_days(60))]
    rows += [(f"C{k}", d, "pullback", -0.05, None, "stop", 0.3, -1.0, 0) for k, d in enumerate(_days(35))]
    db.conn.executemany(
        "INSERT INTO strat_trades (origin, market, symbol, day, strategy, slot, triggered, r_net, r_20e, exit_reason, "
        "mfe_r, mae_r, tp1_hit) VALUES ('replay', CASE WHEN ? = 'pullback' THEN 'crypto' ELSE 'stock' END, ?, ?, ?, 0, 1, "
        "?, ?, ?, ?, ?, ?)", [(r[2], *r) for r in rows])
    rep = AUT.report(db.conn)
    orb30 = rep["strategies"]["stock"]["orb30"]
    assert orb30["esperance_20e_r"] == pytest.approx(-0.4) and orb30["trades_20e"] == 40
    txt = AUT.text(rep)
    lines = [x for x in txt.splitlines() if x.startswith("-# orb")]
    assert lines[0].startswith("-# orb15 · 60 trades · +0.200 R à 20 € (sur 50 trades)")
    assert lines[1].startswith("-# orb30 · 40 trades · -0.400 R à 20 € · +1 R avant stop 100%")
    assert "**v1 actions** (R du labo, avant commission IBKR à 20 €)" in txt
    assert AUT._esp(rep["strategies"]["crypto"]["pullback"], "crypto") == "-0.050 R (frais OKX compris)"
    assert AUT._esp({"esperance_r": 0.1, "trades": 40}, "stock") == "+0.100 R du labo (avant commission IBKR à 20 €)"
    assert AUT.compact(rep)["strategies"]["stock"]["orb30"]["esperance_20e_r"] == pytest.approx(-0.4)


def test_grade_indicatif_dit_son_esperance_a_20_euros():
    db = LabDB(":memory:")
    _go_rows(db)
    db.conn.execute("UPDATE strat_trades SET r_20e = r_sans_be - 0.25 WHERE triggered = 1")
    gi = AUT.go_indicatif(db.conn)
    assert gi["go_confirme"]["par_trade_20e"] == {"trades": 2 * GRADE.FENETRE_JOURS, "esperance_r": pytest.approx(-0.15)}
    lines = AUT.indicatif_lines(gi)
    assert any("R par trade (IC95" in x and "· à 20 € : -0.150 R par trade ·" in x for x in lines)
