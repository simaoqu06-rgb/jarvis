"""Journal des recommandations (app/trading/reco.py) : « si j'avais suivi JARVIS », espace Papier du cockpit."""
from datetime import datetime, timedelta, timezone

import pytest

from app.trading import calendar as CAL
from app.trading import killmode as K
from app.trading import reco as R

DAY = "2026-10-08"
DAY_OPEN = datetime(2026, 10, 8, 9, 30, tzinfo=CAL.NEW_YORK)
T0 = int(DAY_OPEN.timestamp())
CLOSE = T0 + int(6.5 * 3600)


def bar(i, o, h, l, c, v=1000.0):  # noqa: E741
    return {"t": T0 + i * 300, "o": o, "h": h, "l": l, "c": c, "v": v}


def at(i: int) -> datetime:
    return datetime.fromtimestamp(T0 + i * 300, timezone.utc)


@pytest.fixture(autouse=True)
def sortie_unique_et_fixe(monkeypatch):
    monkeypatch.delenv("JARVIS_IBKR_BRACKETS", raising=False)  # défaut depuis le 25/10 : sortie unique sur TP2
    monkeypatch.setattr(K, "IBKR_PLAN", "fixe")


def go_reco(qty=64, risk_eur=20.0, rate=1.16):
    go = {"status": "confirmé", "entry": 13.6, "stop": 13.45, "targets": [14.0, 14.35, 14.75], "composite": 7.6,
          "trigger": 13.81, "confirm_bar": T0 + 900,
          "plan_targets": [{"minutes_median": 30, "minutes_p75": 60}, {"minutes_median": 90, "minutes_p75": 150},
                           {"minutes_median": None}]}
    ev = {"kind": "confirmé", "t": T0 + 900, "trigger": 13.81, "vwap": 13.7}
    return R.go_from_confirm("FRSH", go, ev, DAY, at(4), qty, risk_eur, rate)


def test_go_confirme_tel_qu_envoye_sortie_unique_sur_tp2():
    r = go_reco()
    m = r["message"]
    assert (m["entry"], m["stop"], m["targets"], m["qty"], m["sortie_unique"]) == (13.81, 13.45, [14.0, 14.35, 14.75],
                                                                                   64, 1)
    assert r["activation_ts"] == T0 + 1200  # la fin de la bougie qui confirme
    bars = [bar(3, 13.64, 13.84, 13.63, 13.78),  # bougie qui confirme : pas encore d'ordre
            bar(4, 13.78, 13.85, 13.75, 13.83),  # déclenché à 13,81
            bar(5, 13.83, 14.05, 13.80, 14.0),   # TP1 : repère, rien ne sort
            bar(6, 14.0, 14.40, 13.95, 14.3)]    # TP2 : sortie unique
    assert R.step(r, bars[:2], at(5))
    assert r["status"] == "ouvert" and r["trade"]["entry"] == 13.81
    assert r["trade"]["targets"][0]["eta_median"]  # délai repart du déclenchement
    R.step(r, bars, at(7))
    assert r["status"] == "clos" and r["trade"]["targets_hit"] == 2
    res = r["result"]
    assert res["r"] == 1.5 and res["raison"] == "TP2"
    # 64 × (14,35 − 13,81) = 34,56 $ ; Fixe : 1 $ minimum à l'entrée et 1 $ à la sortie (un seul bracket)
    assert res["usd_brut"] == 34.56 and res["fees_usd"] == 2.0 and res["usd"] == 32.56
    assert res["eur"] == round(32.56 / 1.16, 2) and res["r_net"] == round(32.56 / (64 * 0.36), 3)


def test_ordre_au_dessus_de_l_ouverture_et_stop_sur_gap():
    r = go_reco()
    R.step(r, [bar(4, 13.90, 13.95, 13.85, 13.9)], at(5))
    assert r["trade"]["entry"] == 13.90  # ouverture au-dessus de l'ordre stop : le pire des deux
    R.step(r, [bar(4, 13.90, 13.95, 13.85, 13.9), bar(5, 13.40, 13.42, 13.30, 13.35)], at(6))
    assert r["status"] == "clos"
    assert r["trade"]["events"][-1] == {"kind": "stop", "price": 13.40, "at": at(5).isoformat(timespec="seconds")}
    assert r["result"]["r"] == round((13.40 - 13.90) / (13.90 - 13.45), 2)


def test_pas_declenche_avant_11h30():
    r = go_reco()
    late = 24  # bougie de 11 h 30 New York : l'ordre est déjà annulé
    R.step(r, [bar(4, 13.7, 13.79, 13.65, 13.7), bar(late, 13.78, 14.4, 13.78, 14.3)], at(late + 1))
    assert r["status"] == "pas_declenche" and r["trade"] is None


def test_sans_taille_dans_le_message_euros_inconnus():
    r = go_reco(qty=None)
    assert r["message"]["qty"] is None and r["message"]["risk_eur"] is None
    R.step(r, [bar(4, 13.78, 13.85, 13.75, 13.83), bar(5, 13.83, 14.40, 13.80, 14.3)], at(6))
    assert r["status"] == "clos" and r["result"]["eur"] is None and r["result"]["r"] == 1.5


def kill_call(**over):
    c = {"id": "k1", "groupe": "kill", "symbol": "ABC", "day": DAY, "at": T0 + 960, "envoye_a": T0 + 970,
         "entry": 10.0, "stop": 9.5, "targets": [10.5, 11.0, 11.5], "until": T0 + 5400, "close_ts": CLOSE}
    return c | over


def test_kill_stop_a_l_entree_des_tp1_et_vendu_a_la_cloture():
    r = R.kill_from_call(kill_call(), 1.15)
    assert r["message"]["qty"] == 46 and r["message"]["sortie_unique"] is None  # 20 € × 1,15 / 0,50 $
    assert r["activation_ts"] == T0 + 1080
    bars = [bar(4, 9.98, 10.05, 9.95, 10.02), bar(5, 10.02, 10.55, 10.0, 10.4), bar(6, 10.4, 10.45, 10.2, 10.3),
            bar(77, 10.3, 10.35, 10.25, 10.3)]
    R.step(r, bars[:2], at(6))
    assert r["trade"]["stop_current"] == 10.0  # gestion du KILL : l'entrée dès TP1
    R.step(r, bars, datetime.fromtimestamp(CLOSE + 120, timezone.utc))
    assert r["status"] == "clos" and r["result"]["raison"] == "vendu à la clôture (règle du KILL)"
    assert r["result"]["r"] == round((0.5 / 0.5 + 2 * 0.3 / 0.5) / 3, 2)


def test_reprise_des_kill_seulement_depuis_la_mise_en_service(monkeypatch):
    book = R.RecoBook(None)
    book.since = datetime.fromtimestamp(T0, timezone.utc).isoformat()
    calls = [kill_call(), kill_call(id="t1", groupe="temoin_catalyseur"), kill_call(id="old", at=T0 - 86400)]
    monkeypatch.setattr(K, "calls", lambda: calls)
    added = R.sync_kills(book, 1.15)
    assert [r["id"] for r in added] == ["kill-k1"]
    assert R.sync_kills(book, 1.15) == []  # une seule fois


async def test_tick_ne_lit_qu_une_fois_par_bougie_close(monkeypatch):
    book = R.RecoBook(None)
    book.add(go_reco())
    monkeypatch.setattr(K, "calls", lambda: [])
    reads = []

    async def fake_bars(symbol):
        reads.append(symbol)
        return [bar(4, 13.78, 13.85, 13.75, 13.83)]

    R._last_read.clear()
    now = at(5) + timedelta(seconds=10)
    assert await R.tick(book, now, bars_fn=fake_bars) == 1
    await R.tick(book, now + timedelta(seconds=60), bars_fn=fake_bars)
    assert reads == ["FRSH"] and book.recos[0]["status"] == "ouvert"


def test_lien_avec_mes_trades():
    r = go_reco()
    mine = {"id": "t9", "symbol": "FRSH", "created_at": at(5).isoformat(), "policy": "go_confirme"}
    other = {"id": "t8", "symbol": "FRSH", "created_at": (at(5) - timedelta(days=1)).isoformat(),
             "policy": "go_confirme"}
    assert R.linked_trade(r, [other, mine])["id"] == "t9"
    assert R.linked_trade(R.kill_from_call(kill_call(symbol="FRSH"), 1.15), [mine]) is None


async def test_tick_confirm_note_le_go_avec_la_taille_du_message(monkeypatch, tmp_path):
    from app import scheduler as SCH
    from app.trading import confirm as C

    monkeypatch.setattr(C, "STATE_PATH", str(tmp_path / "c.json"))
    monkeypatch.setattr(C, "LOG_PATH", str(tmp_path / "c.jsonl"))
    monkeypatch.setattr(SCH, "confirm_state", {})
    monkeypatch.setattr(SCH.book, "trades", [])
    monkeypatch.delenv("JARVIS_CAPITAL", raising=False)
    plan = {"entry": 13.6, "stop": 13.45, "score": {"go": True, "composite": 7},
            "targets": [{"price": 14.0, "minutes_median": 30}, {"price": 14.35}, {"price": 14.75}]}
    C.register(SCH.confirm_state, {"picks": [{"symbol": "FRSH", "plan": plan}]}, DAY)
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
    await SCH.tick_confirm((DAY_OPEN + timedelta(minutes=21)).astimezone(timezone.utc))
    (r,) = SCH.reco_book.recos
    assert f"{r['message']['qty']} actions" in sent[0]  # la taille du MP
    assert r["message"]["entry"] == 13.81 and r["message"]["stop"] == 13.45 and r["status"] == "attente"
    assert r["timing"][0]["minutes_median"] == 30


def test_bilan_si_tout_suivi():
    book = R.RecoBook(None)
    a, b = go_reco(), R.kill_from_call(kill_call(), 1.15)
    book.recos = [a, b]
    R.step(a, [bar(4, 13.78, 13.85, 13.75, 13.83), bar(5, 13.83, 14.40, 13.80, 14.3)], at(6))
    R.step(b, [bar(4, 9.9, 9.95, 9.9, 9.92)], datetime.fromtimestamp(T0 + 5400, timezone.utc))
    out = R.bilan(book, 1000.0)
    assert out["par_etat"]["clos"] == 1 and out["par_etat"]["pas_declenche"] == 1
    assert out["eur_net"] == a["result"]["eur"] and out["equite_eur"] == round(1000 + a["result"]["eur"], 2)


def test_portefeuille_fictif_a_20_euros_fixes_meme_sans_taille():
    """Grade F : le MP part sans taille. Le portefeuille fictif (choix du 29/10) prend quand même le trade à 20 € de
    risque : 20 × 1,16 / 0,36 $ = 64 actions, comme go_reco(qty=64)."""
    book = R.RecoBook(None)
    a, b = go_reco(qty=None), R.kill_from_call(kill_call(), 1.15)
    book.recos = [a, b]
    assert R.fixed_qty(a) == 64 and R.fixed_qty(b) == 46
    R.step(a, [bar(4, 13.78, 13.85, 13.75, 13.83), bar(5, 13.83, 14.40, 13.80, 14.3)], at(6))
    assert a["result"]["eur"] is None  # le journal reste tel qu'envoyé
    sized = R.result(a, 64)
    assert sized["usd"] == 32.56  # même résultat qu'un message à 64 actions (frais Fixe compris)
    out = R.portfolio(book)
    assert out["depart_eur"] == 1000.0 and out["risque_par_trade_eur"] == 20.0 and out["closes"] == 1
    assert out["capital_eur"] == round(1000 + sized["eur"], 2) and out["resultat_eur"] == sized["eur"]
    assert out["points"] == [[sized["closed_at"], sized["eur"]]] and out["gagnantes"] == 1 and out["ouvertes"] == 0


def test_portefeuille_fictif_vide_reste_au_depart():
    out = R.portfolio(R.RecoBook(None))
    assert out["capital_eur"] == 1000.0 and out["closes"] == 0 and out["latent_eur"] is None and out["points"] == []
