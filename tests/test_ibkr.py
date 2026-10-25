"""Brackets IBKR : taille du plan en euros, actions entières, commissions Fixe et Dégressif (barème de Killmode)."""
from app.trading import ibkr
from app.trading import killmode as K
from app.trading import service as S


def _plan(entry=5.0, stop=4.5, tps=(5.5, 6.0, 6.5), market="stock", side="long", risk_eur=20, rate=1.10):
    return {"symbol": "TEST", "market": market, "side": side, "entry": entry, "stop": stop,
            "targets": [{"name": f"TP{i}", "price": x} for i, x in enumerate(tps, start=1)],
            "sizing": S.euro_sizing(entry, stop, market, risk_eur, rate)}


def test_quantite_decoule_du_stop_et_du_risque_en_euros():
    p = _plan()
    ibkr.attach(p)
    b = p["ibkr"]
    assert b["risk_usd"] == 22.0  # 20 € × 1,10
    assert b["quantity"] == 44 == p["sizing"]["quantity"]  # 22 $ / 0,50 $ par action, arrondi en dessous
    assert b["real_risk_usd"] <= b["risk_usd"]
    assert [r["quantity"] for r in b["brackets"]] == [15, 15, 14]
    assert [r["take_profit"] for r in b["brackets"]] == [5.5, 6.0, 6.5]


def test_la_taille_du_plan_fait_foi():
    # Règle unique de main : le grade propose moins de 20 € (capital connu), les brackets suivent cette taille.
    p = _plan(risk_eur=12)
    ibkr.attach(p)
    assert p["ibkr"]["risk_eur"] == 12 and p["ibkr"]["quantity"] == p["sizing"]["quantity"] == 26


def test_commissions_minimums_par_ordre():
    b = ibkr.brackets(_plan())
    # Fixe : 6 ordres de moins de 100 $, chacun plafonné à 1 % de sa valeur (sous le minimum de 1 $),
    # pire cas = tous les TP touchés ; un seul bracket de 44 actions : 2 × 1 $.
    assert b["fees"]["fixe"]["usd"] == 4.83
    assert b["fees"]["fixe"]["un_bracket_usd"] == 2.0
    assert b["fees"]["degressif"]["usd"] < b["fees"]["fixe"]["usd"]
    assert b["cheapest"] == "degressif"


def test_plafond_un_pour_cent_sur_les_actions_a_quelques_centimes():
    # Le barème des brackets est celui de Killmode (une seule copie).
    assert K.order_commission(100, 0.5, K.IBKR_PLANS["fixe"]) == 0.5  # 1 % de 50 $ < minimum de 1 $
    assert ibkr.trade_commission([100], 0.5, [0.5], K.IBKR_PLANS["fixe"]) == 1.0


def test_stop_trop_loin_pour_une_action():
    b = ibkr.brackets(_plan(entry=500.0, stop=470.0, tps=(530.0,)))
    assert b["quantity"] == 0 and "stop trop loin" in b["raison"]


def test_moins_d_actions_que_de_tp():
    # Objectif du milieu de la part de chaque bracket, comme killmode.bracket_orders et le labo (r_20e).
    tps = (110.0, 120.0, 130.0)
    two = ibkr.brackets(_plan(entry=100.0, stop=90.0, tps=tps))     # 22 $ / 10 $ par action : 2 actions
    assert [(r["quantity"], r["name"]) for r in two["brackets"]] == [(1, "TP1"), (1, "TP3")]
    one = ibkr.brackets(_plan(entry=100.0, stop=85.0, tps=tps))     # 22 $ / 15 $ : 1 action
    assert [(r["quantity"], r["name"], r["take_profit"]) for r in one["brackets"]] == [(1, "TP2", 120.0)]
    assert [(n, x) for n, x in K.bracket_orders(100.0, 85.0, list(tps), 1.10)] == [(1, 120.0)]


def test_crypto_sans_brackets():
    c = _plan(market="crypto")
    ibkr.attach(c)
    assert "ibkr" not in c


def test_grade_a_zero_ta_regle_hors_avis():
    # Le grade propose 0 % : pas de taille de JARVIS, les brackets suivent ta règle de 20 €, dite comme telle.
    p = _plan() | {"grade": {"risque_propose": 0}}
    p["sizing_regle"] = p.pop("sizing") | {"hors_grade": True}
    ibkr.attach(p)
    assert p["ibkr"]["hors_avis"] and p["ibkr"]["quantity"] == 44
    assert "ta règle, hors avis de JARVIS" in ibkr.lines(p)[0]
    rien = _plan() | {"grade": {"risque_propose": 0}, "sizing": None}
    ibkr.attach(rien)
    assert "ibkr" not in rien
    assert not ibkr.brackets(_plan())["hors_avis"]


def test_lignes_discord():
    p = _plan()
    ibkr.attach(p)
    text = "\n".join(ibkr.lines(p))
    assert "44 actions" in text and "Bracket 3 : 14 @ entrée 5.00" in text
    assert "en Dégressif" in text  # le moins cher est signalé quand le plan du compte est le Fixe
    assert S.IBKR is ibkr


def test_sortie_unique_sur_tp2_par_defaut(monkeypatch):
    monkeypatch.delenv("JARVIS_IBKR_BRACKETS")
    b = ibkr.brackets(_plan())
    assert b["brackets"] == [{"name": "TP2", "quantity": 44, "take_profit": 6.0}]
    assert b["sortie_unique"] and b["reperes"] == ["TP1", "TP3"]
    # Un seul bracket : 2 ordres de 1 $ au Fixe ; un bracket par objectif reste affiché pour comparer.
    assert b["fees"]["fixe"]["usd"] == 2.0 == b["fees"]["fixe"]["un_bracket_usd"]
    assert b["fees"]["fixe"]["par_objectif_usd"] == 4.83
    p = _plan()
    ibkr.attach(p)
    text = "\n".join(ibkr.lines(p))
    assert "Bracket 1 : 44 @ entrée 5.00" in text and "TP2 6.00" in text and "Bracket 2" not in text
    assert "Sortie unique sur TP2 (TP1 et TP3 : repères, sans ordre)" in text
    assert "un bracket par objectif : 4.83 $" in text


def test_sortie_unique_seulement_sur_trois_objectifs_hors_kill(monkeypatch):
    """La mesure porte sur les plans à trois objectifs ; un KILL garde la gestion que mesure son journal."""
    monkeypatch.delenv("JARVIS_IBKR_BRACKETS")
    b = ibkr.brackets(_plan(tps=(5.5,)))
    assert b["brackets"] == [{"name": "TP1", "quantity": 44, "take_profit": 5.5}] and not b["sortie_unique"]
    assert len(ibkr.brackets(_plan(tps=(5.5, 6.0)))["brackets"]) == 2
    kill = _plan() | {"killmode": True}
    assert len(ibkr.brackets(kill)["brackets"]) == 3 and ibkr.single_target(kill) is None
    assert ibkr.single_target(_plan()) == 1 and ibkr.single_target(_plan(market="crypto")) is None
    monkeypatch.setenv("JARVIS_IBKR_BRACKETS", "3")
    assert ibkr.single_target(_plan()) is None
