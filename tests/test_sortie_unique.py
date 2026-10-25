"""Sortie unique sur TP2 (choix de Simão du 25 octobre 2026) : le suivi `!prends`, les frais estimés, le risque
ouvert et le R du grade suivent le seul bracket des plans ; un KILL et les trades d'avant gardent un tiers par
objectif."""
import json
from datetime import datetime, timezone

import pytest

from app.lab import grade as GR
from app.lab import strategies as ST
from app.trading import advice
from app.trading import fees as FEES
from app.trading import service as S

T0 = int(datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc).timestamp())
NOW = datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def sortie_unique(monkeypatch):
    monkeypatch.delenv("JARVIS_IBKR_BRACKETS", raising=False)


def bars(closes, spread=0.5, start=T0, step=60):
    return [{"t": start + i * step, "o": c, "h": c + spread, "l": c - spread, "c": c, "v": 1.0}
            for i, c in enumerate(closes)]


def plan(**over):
    return {"symbol": "ACME", "market": "stock", "side": "long", "entry": 100.0, "stop": 98.0, "stop_pct": -2.0,
         "created_at": "2026-10-06T13:59:00+00:00",
         "sizing": {"risk_amount_eur": 20.0, "risk_amount": 23.0, "eurusd": 1.15, "quantity": 11},
         "targets": [{"name": f"TP{i}", "price": p, "pct": p - 100, "minutes_median": 60, "eta_latest": None,
                      "eta_text": "~1 h", "source": "test"} for i, p in enumerate([102.0, 104.0, 106.0], start=1)]} | over


def trade(**over):
    return S.Book(None).add(plan(**over))


def test_le_trade_se_termine_en_entier_sur_tp2():
    tr = trade()
    assert tr["sortie_unique"] == 1
    msgs = S.apply_bars(tr, bars([101.0, 102.5]), NOW)
    assert "TP1 touché" in msgs[0] and "pas de break-even" in msgs[0]  # ta règle : stop d'origine jusqu'au TP2
    assert "Garde toute la position : sortie unique sur TP2." in msgs[0] and tr["status"] == "open"
    msgs = S.apply_bars(tr, bars([106.5], start=T0 + 120), NOW)  # TP2 et TP3 dans la même bougie
    assert "TP2 touché" in msgs[0] and "Sortie unique : toute la position vendue" in msgs[0]
    assert tr["status"] == "closed" and tr["targets_hit"] == 2 and len(msgs) == 1
    assert S.realized_r(tr) == 2.0  # toute la position à TP2 (104), pas un tiers par objectif
    # Un seul bracket : 2 ordres au minimum IBKR, contre 6 pour un bracket par objectif.
    thirds = tr | {"sortie_unique": None, "targets_hit": 3,
                   "events": tr["events"] + [{"kind": "tp3", "price": 106.0, "at": "x"}]}
    assert 0 < FEES.fees_r(tr) < FEES.fees_r(thirds)
    assert S.realized_r_net(tr) == pytest.approx(2.0 - FEES.fees_r(tr), abs=1e-3)


def test_stop_apres_tp1_vend_toute_la_position():
    tr = trade()
    S.apply_bars(tr, bars([101.0, 102.5]), NOW)
    msgs = S.apply_bars(tr, bars([98.2], start=T0 + 120), NOW)  # touche le stop d'origine (98) sans gap
    assert tr["status"] == "closed" and "stop suivi" in msgs[0]
    assert S.realized_r(tr) == -1.0  # aucun tiers vendu à TP1 : toute la position au stop
    assert len(FEES._exits(tr)) == 1 and FEES._exits(tr)[0]["raison"] == "stop"


def test_risque_ouvert_et_sortie_au_prix_du_moment():
    tr = trade(sizing={"risk_amount_eur": 20.0, "risk_amount": 23.0, "eurusd": 1.15, "quantity": 11,
                       "capital": 1000.0, "risk_pct": 2.0})
    S.apply_bars(tr, bars([101.0, 102.5]), NOW)
    thirds = tr | {"sortie_unique": None}
    # Après TP1, toute la position reste au risque du stop d'origine ; un tiers par objectif n'en garde que 2/3.
    assert GR.open_risk([tr]) == pytest.approx(1.5 * GR.open_risk([thirds]), abs=0.01)
    assert advice.exit_r(tr, 103.0) == 1.5 and advice.exit_r(thirds, 103.0) == pytest.approx(1.33, abs=0.01)


def test_kill_et_anciens_trades_gardent_un_tiers_par_objectif():
    assert trade(killmode=True)["sortie_unique"] is None
    assert trade(targets=trade()["targets"][:2])["sortie_unique"] is None  # mesure faite sur trois objectifs
    assert trade(sortie_unique=None)["sortie_unique"] is None  # trade rouvert : sa gestion d'origine
    old = trade()
    del old["sortie_unique"]  # trade pris avant le 25 octobre
    S.apply_bars(old, bars([101.0, 102.5]), NOW)
    assert "Garde le reste." in S.apply_bars(old, bars([104.2], start=T0 + 120), NOW)[0]
    assert old["status"] == "open"


def test_cockpit_signale_la_sortie_et_les_reperes():
    book = S.Book(None)
    book.add(plan())
    text = S.cockpit_text(book, NOW)
    assert "TP2 104 (+4.00 %) · sortie unique" in text and "TP1 102 (+2.00 %) · repère" in text


def sim_result(exits, entry=10.0, stop=9.7):
    return json.dumps({"triggered": True, "fill_price": entry, "exits": exits,
                       "order_prices": {"entry": entry, "stop": stop, "targets": [10.3, 10.6, 10.9]}})


def test_grade_mesure_le_seul_bracket_sur_tp2(monkeypatch):
    from app.trading import killmode as K

    # TP1 puis retour à l'entrée pour les deux autres tiers : la sortie unique sort à l'entrée (part du milieu).
    res = sim_result([{"part": 1 / 3, "price": 10.3, "reason": "tp1"}, {"part": 2 / 3, "price": 10.0, "reason": "stop_be"}])
    variants = ST.cost_variants("stock", json.loads(res))
    assert GR.cost_r("stock", 0.33, res) == variants["r_20e_1s"] < 0  # au Fixe, comme la mesure n° 10
    monkeypatch.setattr(K, "IBKR_PLAN", "degressif")
    deg = GR.cost_r("stock", 0.33, res)
    assert deg != variants["r_20e_1s"] and deg > variants["r_20e_deg"] - 1  # au barème du compte
    monkeypatch.setenv("JARVIS_IBKR_BRACKETS", "3")
    assert GR.cost_r("stock", 0.33, res) == variants["r_20e_deg"]  # un bracket par objectif, comme avant
