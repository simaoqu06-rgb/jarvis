"""Contrôle des GO après l'ouverture (app/trading/confirm.py, tick_confirm)."""
from datetime import datetime, timedelta, timezone

import pytest

from app.trading import calendar as CAL
from app.trading import confirm as C

DAY_OPEN = datetime(2026, 10, 8, 9, 30, tzinfo=CAL.NEW_YORK)
T0 = int(DAY_OPEN.timestamp())


def bar(i, o, h, l, c, v=1000.0):
    return {"t": T0 + i * 300, "o": o, "h": h, "l": l, "c": c, "v": v}


def go(**over):
    g = {"status": "attente", "entry": 13.60, "stop": 13.45, "targets": [14.0, 14.35, 14.75],
         "composite": 7.6, "last_bar": None, "events": []}
    return g | over


def scan_result():
    plan = lambda go_: {"entry": 13.6, "stop": 13.45, "targets": [{"price": 14.0}], "score": {"go": go_, "composite": 7}}  # noqa: E731
    return {"picks": [{"symbol": "FRSH", "plan": plan(True)}, {"symbol": "XYZ", "plan": plan(False)}]}


def test_register_garde_seulement_les_go():
    state = {"day": "old", "gos": {"OLD": {}}}
    assert C.register(state, scan_result(), "2026-10-08") == ["FRSH"]
    assert state["gos"]["FRSH"]["status"] == "attente" and not state["closed"]


def test_confirme_apres_repli_puis_reprise_du_vwap():
    g = go()
    bars = [bar(0, 13.8, 13.9, 13.7, 13.75), bar(1, 13.75, 13.76, 13.58, 13.62),  # repli sur 13.60
            bar(2, 13.62, 13.66, 13.6, 13.64), bar(3, 13.64, 13.80, 13.63, 13.78)]  # clôture > VWAP
    ev = C.evaluate(g, bars, DAY_OPEN)
    assert ev["kind"] == "confirmé" and ev["trigger"] == 13.81 and g["status"] == "confirmé"
    assert C.evaluate(g, bars, DAY_OPEN) is None  # déjà lu


def test_pas_de_confirmation_dans_le_premier_quart_d_heure():
    g = go()
    bars = [bar(0, 13.7, 13.72, 13.55, 13.58, v=10), bar(1, 13.58, 13.75, 13.57, 13.74, v=10)]
    assert C.evaluate(g, bars, DAY_OPEN) is None and g["status"] == "repli"


def test_annule_sous_le_stop_ou_tp1_avant():
    assert C.evaluate(go(), [bar(0, 13.6, 13.62, 13.40, 13.42)], DAY_OPEN)["kind"] == "annulé"
    ev = C.evaluate(go(), [bar(0, 13.8, 14.05, 13.75, 13.95)], DAY_OPEN)
    assert ev["kind"] == "annulé" and "TP1" in ev["why"]


def test_simulation_par_tiers():
    bars = [bar(0, 13.81, 14.1, 13.8, 14.05), bar(1, 14.05, 14.4, 14.0, 14.3), bar(2, 14.3, 14.31, 14.0, 14.1)]
    r = C.simulate(13.81, 13.45, [14.0, 14.35, 14.75], bars)
    expected = ((14.0 - 13.81) + (14.35 - 13.81) + (14.1 - 13.81)) / 0.36 / 3
    assert r == pytest.approx(round(expected, 2))
    assert C.simulate(13.81, 13.45, [14.0], [bar(0, 13.8, 14.1, 13.4, 13.5)]) == -1.0  # stop d'abord


def test_mesure_brut_et_confirme():
    g = go(status="confirmé", trigger=13.81, confirm_bar=T0 + 900)
    bars = [bar(0, 13.7, 13.75, 13.58, 13.6), bar(3, 13.64, 13.80, 13.63, 13.78),
            bar(4, 13.78, 13.9, 13.77, 13.85), bar(5, 13.85, 14.4, 13.85, 14.3)]
    out = C.outcome(g, bars)
    assert out["rempli"] == 13.81 and out["brut_r"] is not None and out["confirme_r"] > 0


def test_texte_confirme_avec_taille():
    txt = C.event_text("FRSH", go(), {"kind": "confirmé", "trigger": 13.70, "vwap": 13.65},
                       datetime(2026, 10, 8, 14, 0, tzinfo=timezone.utc), 20, 1.16)
    assert "stop d'achat 13.7" in txt and "92 actions" in txt  # 23,2 $ / 0,25


AT = datetime(2026, 10, 8, 14, 0, tzinfo=timezone.utc)
CONFIRMED = {"kind": "confirmé", "trigger": 13.70, "vwap": 13.65}
GRADE_F = {"grade": "F", "risque_propose": 0.0, "observation": True,
           "raison": "grade F : pas de capital réel tant que la méthode n'est pas au moins D"}


def test_actions_entieres_et_action_hors_budget():
    assert C.shares(10.3, 8.0, 23.0) == 10  # 23 / 2,3 : 10, pas 9 (arrondi flottant, comme Killmode)
    big = go(entry=128.0, stop=100.0, targets=[160.0, 190.0, 220.0])
    txt = C.event_text("BIG", big, {"kind": "confirmé", "trigger": 130.0, "vwap": 129.0}, AT, 20, 1.15)
    assert "stop 100 · 1 action = 26 € de risque, au-delà de 20 € : pas de taille" in txt and "actions" not in txt


def test_confirmation_grade_f_taille_hors_avis_de_jarvis():
    txt = C.event_text("FRSH", go(), CONFIRMED, AT, 20, 1.16, GRADE_F)
    assert "92 actions" in txt  # ta règle de 20 €, toujours chiffrée
    # Scénario 1 de GPT : l'étiquette est dans la ligne d'ordre même, pas seulement en note trois lignes plus bas.
    (order,) = [x for x in txt.splitlines() if x.startswith("Ordre : ")]
    assert order == ("Ordre : **stop d'achat 13.7** · stop 13.45 · **92 actions à ta règle de 20 €** (hors avis de "
                     "JARVIS ; ≈ 23 $ de risque, 1 260.4 $)")
    assert "-# Taille à ta règle de 20 € (hors avis de JARVIS : grade F, 0 % proposé)." in txt
    assert "🏅 Grade JARVIS **F** · risque proposé **0 %** : plan en observation" in txt
    a = {"grade": "A", "risque_propose": 3.0, "observation": False, "raison": "grade A"}
    txt = C.event_text("FRSH", go(), CONFIRMED, AT, 12.5, 1.16, a)
    assert "**58 actions** (12.5 € ≈ 14 $ de risque" in txt and "hors avis" not in txt


async def test_tick_confirm_taille_selon_le_grade(monkeypatch, tmp_path):
    from app import scheduler as SCH
    from app.lab import grade as GR

    monkeypatch.setattr(C, "STATE_PATH", str(tmp_path / "c.json"))
    monkeypatch.setattr(C, "LOG_PATH", str(tmp_path / "c.jsonl"))
    bars = [bar(0, 13.8, 13.9, 13.7, 13.75), bar(1, 13.75, 13.76, 13.58, 13.62),
            bar(2, 13.62, 13.66, 13.6, 13.64), bar(3, 13.64, 13.80, 13.63, 13.78)]

    async def fake_bars(symbol):
        return bars

    async def fake_rate():
        return 1.16

    now = (DAY_OPEN + timedelta(minutes=21)).astimezone(timezone.utc)
    sent = []
    monkeypatch.setattr(SCH, "_bars_5min", fake_bars)
    monkeypatch.setattr(SCH, "_usd_per_eur", fake_rate)
    monkeypatch.setattr(SCH, "_queue", lambda texts, kind="", **kw: sent.extend(texts))
    monkeypatch.setattr(SCH, "_dm_trades", lambda texts: None)
    monkeypatch.setattr(SCH, "_kick_outbox", lambda: None)
    monkeypatch.setattr(SCH.book, "trades", [])
    monkeypatch.setenv("JARVIS_CAPITAL", "500")
    stock = {"grade": "A", "rang": 16, "non_note": False, "risque_grade": 3.0, "risque_max": 3.0, "mode_survie": False}
    for market, size, off in ((stock, "48 actions", False),  # 500 € × 3 % = 15 € : 17,4 $ / 0,36
                              (stock | {"grade": "F", "risque_grade": 0.0}, "64 actions", True)):  # ta règle, 20 €
        monkeypatch.setattr(GR, "current", lambda: {"marches": {"stock": market}, "verifie_le": now.isoformat()})
        monkeypatch.setattr(SCH, "confirm_state", {})
        C.register(SCH.confirm_state, scan_result(), "2026-10-08")
        sent.clear()
        await SCH.tick_confirm(now)
        assert len(sent) == 1 and size in sent[0] and ("hors avis de JARVIS" in sent[0]) is off


def test_bilan():
    rows = [{"day": "2026-10-08", "symbol": "A", "status": "confirmé", "brut_r": -1.0, "confirme_r": 0.5},
            {"day": "2026-10-08", "symbol": "B", "status": "annulé", "brut_r": -0.4, "confirme_r": None}]
    txt = C.report_text(rows)
    assert "brut (achat à l'ouverture) : -0.70 R sur 2" in txt and "avec l'ordre confirmé +0.50 R" in txt


async def test_tick_confirm_bout_en_bout(monkeypatch, tmp_path):
    from app import scheduler as SCH

    monkeypatch.setattr(C, "STATE_PATH", str(tmp_path / "c.json"))
    monkeypatch.setattr(C, "LOG_PATH", str(tmp_path / "c.jsonl"))
    monkeypatch.setattr(SCH, "confirm_state", {})
    C.register(SCH.confirm_state, scan_result(), "2026-10-08")
    bars = [bar(0, 13.8, 13.9, 13.7, 13.75), bar(1, 13.75, 13.76, 13.58, 13.62),
            bar(2, 13.62, 13.66, 13.6, 13.64), bar(3, 13.64, 13.80, 13.63, 13.78)]

    async def fake_bars(symbol):
        return bars

    async def fake_rate():
        return 1.16

    sent = []
    monkeypatch.setattr(SCH, "_bars_5min", fake_bars)
    monkeypatch.setattr(SCH, "_usd_per_eur", fake_rate)
    monkeypatch.setattr(SCH, "_queue", lambda texts, kind="", **kw: sent.extend(texts))
    monkeypatch.setattr(SCH, "_dm_trades", lambda texts: None)
    monkeypatch.setattr(SCH, "_kick_outbox", lambda: None)
    await SCH.tick_confirm((DAY_OPEN + timedelta(minutes=10)).astimezone(timezone.utc))
    assert not sent  # avant 9 h 45
    await SCH.tick_confirm((DAY_OPEN + timedelta(minutes=21)).astimezone(timezone.utc))
    assert len(sent) == 1 and "FRSH confirmé" in sent[0]
    await SCH.tick_confirm(datetime(2026, 10, 8, 16, 3, tzinfo=CAL.NEW_YORK).astimezone(timezone.utc))
    assert SCH.confirm_state["closed"]
    rows = C.read_log()
    assert rows[0]["symbol"] == "FRSH" and rows[0]["status"] == "confirmé"


def test_ordre_non_declenche_annule_a_11h30():
    g = go(status="confirmé", trigger=13.81, confirm_bar=T0 + 900)
    late = 24  # bougie de 11 h 30 New York
    bars = [bar(0, 13.7, 13.75, 13.58, 13.6), bar(3, 13.64, 13.80, 13.63, 13.78),
            bar(10, 13.7, 13.79, 13.65, 13.7), bar(late, 13.78, 14.4, 13.78, 14.3)]
    out = C.outcome(g, bars)
    assert out["rempli"] is None and out["confirme_r"] is None and out["brut_r"] is not None


# --- Tailles des MP automatiques sous un grade à 0 % (scénario 1 de GPT) ------------------------------

def _tick_env(monkeypatch, tmp_path, sent):
    from app import scheduler as SCH

    monkeypatch.setattr(C, "STATE_PATH", str(tmp_path / "c.json"))
    monkeypatch.setattr(C, "LOG_PATH", str(tmp_path / "c.jsonl"))
    bars = [bar(0, 13.8, 13.9, 13.7, 13.75), bar(1, 13.75, 13.76, 13.58, 13.62),
            bar(2, 13.62, 13.66, 13.6, 13.64), bar(3, 13.64, 13.80, 13.63, 13.78)]

    async def fake_bars(symbol):
        return bars

    async def fake_rate():
        return 1.16

    monkeypatch.setattr(SCH, "_bars_5min", fake_bars)
    monkeypatch.setattr(SCH, "_usd_per_eur", fake_rate)
    monkeypatch.setattr(SCH, "_queue", lambda texts, kind="", **kw: sent.extend(texts))
    monkeypatch.setattr(SCH, "_dm_trades", lambda texts: None)
    monkeypatch.setattr(SCH, "_kick_outbox", lambda: None)
    monkeypatch.delenv("JARVIS_CAPITAL", raising=False)
    return SCH


async def _confirm_once(SCH, monkeypatch, now):
    monkeypatch.setattr(SCH, "confirm_state", {})
    C.register(SCH.confirm_state, scan_result(), "2026-10-08")
    await SCH.tick_confirm(now)
    return SCH.confirm_state


def _order_line(txt: str) -> str:
    (order,) = [x for x in txt.splitlines() if x.startswith("Ordre : ")]
    return order


async def test_tick_confirm_apres_l_arret_du_jour_ou_budget_plein_pas_de_taille(monkeypatch, tmp_path):
    """Choix par défaut (Simão tranchera) : l'arrêt de la journée ou le budget de risque ouvert plein ne donnent pas
    de quantité dans le MP ; le MP part quand même et l'état est sauvé (aucune exception sur la taille None)."""
    from app.lab import grade as GR
    from tests.test_lab_grade import trade

    sent = []
    SCH = _tick_env(monkeypatch, tmp_path, sent)
    now = (DAY_OPEN + timedelta(minutes=21)).astimezone(timezone.utc)
    stock = {"grade": "A", "rang": 16, "non_note": False, "risque_grade": 3.0, "risque_max": 3.0, "mode_survie": False}
    monkeypatch.setattr(GR, "current", lambda: {"marches": {"stock": stock}, "verifie_le": now.isoformat()})
    stopped = [trade(status="closed", events=[{"kind": "stop", "price": 95.0, "at": now.isoformat()}]) for _ in range(3)]
    monkeypatch.setattr(SCH.book, "trades", stopped)
    state = await _confirm_once(SCH, monkeypatch, now)
    assert len(sent) == 1 and state["gos"]["FRSH"]["status"] == "confirmé"
    assert _order_line(sent[0]).endswith(" · pas de taille : arrêt de la journée (−3,9 R)")  # −3,88 R nets de frais
    assert "actions" not in sent[0] and "Taille à ta règle" not in sent[0]
    assert "🏅 Grade JARVIS **A** · risque proposé **0 %** : plan en observation (arrêt de la journée" in sent[0]
    assert C.load()["gos"]["FRSH"]["status"] == "confirmé"  # sauvé : le MP n'est pas perdu
    # Budget de 4 % ouvert plein.
    sent.clear()
    monkeypatch.setattr(SCH.book, "trades", [trade(risk=1.0) for _ in range(4)])
    await _confirm_once(SCH, monkeypatch, now)
    assert _order_line(sent[0]).endswith(" · pas de taille : budget de risque ouvert plein")
    # Une seule variable pour revenir en arrière : ta règle de 20 €, étiquetée dans la ligne.
    sent.clear()
    monkeypatch.setattr(C, "SIZE_AFTER_STOP", True)
    await _confirm_once(SCH, monkeypatch, now)
    assert "**64 actions à ta règle de 20 €** (hors avis de JARVIS" in _order_line(sent[0])


async def test_tick_confirm_grade_illisible_taille_a_ta_regle_etiquetee(monkeypatch, tmp_path):
    """risk_now qui lève : 0 % (fail-closed), le MP chiffre ta règle de 20 €, dite hors avis dans la ligne d'ordre,
    avec la raison (grade illisible) sur la ligne du grade."""
    from app.lab import grade as GR

    sent = []
    SCH = _tick_env(monkeypatch, tmp_path, sent)
    monkeypatch.setattr(SCH.book, "trades", [])

    def boom(*a, **k):
        raise KeyError("marches")
    monkeypatch.setattr(GR, "risk_now", boom)
    now = (DAY_OPEN + timedelta(minutes=21)).astimezone(timezone.utc)
    await _confirm_once(SCH, monkeypatch, now)
    assert len(sent) == 1 and "**64 actions à ta règle de 20 €** (hors avis de JARVIS" in _order_line(sent[0])
    assert "grade illisible" in sent[0] and "-# Taille à ta règle de 20 € (hors avis de JARVIS" in sent[0]


def test_portfolio_stop_lit_les_champs_chiffres_du_grade():
    assert C.portfolio_stop({"risque_propose": 0.0, "risque_grade": 3.0, "r_du_jour": -3.3, "risque_ouvert": 0.0}) \
        == "arrêt de la journée (−3,3 R)"
    assert C.portfolio_stop({"risque_propose": 0.0, "risque_grade": 3.0, "r_du_jour": -1.0, "risque_ouvert": 4.0}) \
        == "budget de risque ouvert plein"
    assert C.portfolio_stop(GRADE_F) is None  # F : la méthode, pas le portefeuille
    assert C.portfolio_stop({"risque_propose": 0.0, "risque_grade": 3.0, "perime": True}) is None
    assert C.portfolio_stop({"risque_propose": 1.0, "risque_grade": 3.0, "r_du_jour": 0.0, "risque_ouvert": 3.0}) is None
    assert C.rule_size(GRADE_F, None, 20.0) == 20.0 and C.rule_size(None, 12.5, 20.0) == 12.5
    assert C.rule_size({"risque_propose": 0.0, "risque_grade": 3.0, "r_du_jour": -3.3}, None, 20.0) is None


def test_confirme_a_20_euros_non_compte_quand_une_action_depasse_la_mise():
    """Le MP dit « pas de taille » quand une action dépasse 20 € : la mesure à 20 € ne force plus 1 action."""
    big = go(entry=128.0, stop=100.0, targets=[160.0, 190.0, 220.0], status="confirmé", trigger=130.0,
             confirm_bar=T0 + 900)
    bars = [bar(0, 129.0, 129.5, 128.0, 129.0), bar(3, 129.0, 129.5, 128.5, 129.2),
            bar(4, 129.2, 165.0, 129.0, 164.0), bar(5, 164.0, 166.0, 150.0, 155.0)]
    out = C.outcome(big, bars, 1.15)
    assert out["rempli"] == 130.0 and out["confirme_r"] is not None
    assert out["confirme_r20"] is None and out["confirme_hors_mise"] is True
    small = C.outcome(go(status="confirmé", trigger=13.81, confirm_bar=T0 + 900),
                      [bar(0, 13.7, 13.75, 13.58, 13.6), bar(4, 13.78, 13.9, 13.77, 13.85),
                       bar(5, 13.85, 14.4, 13.85, 14.3)], 1.15)
    assert small["confirme_r20"] is not None and "confirme_hors_mise" not in small
    rows = [{"day": "2026-10-08", "symbol": "BIG", "status": "confirmé", "brut_r": 0.5} | out,
            {"day": "2026-10-08", "symbol": "FRSH", "status": "confirmé", "brut_r": 0.2} | small]
    txt = C.report_text(rows)
    assert "à 20 € " in txt and " sur 1, 1 hors mise non comptés : 1 action au-delà de 20 € de risque" in txt
