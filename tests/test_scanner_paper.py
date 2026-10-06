"""Scanner (mesures et filtres), forward-test (répartition, issue, bilan) et commandes Discord, hors ligne."""
from datetime import datetime, timezone

import pytest

from app.notify import bot
from app.sources import alpaca
from app.trading import paper, scanner
from app.trading import proposals as PR
from app.trading.settings import Settings
from tests.test_score import daily


@pytest.fixture
def s():
    return Settings(None)


def test_mesures_du_scanner_ignorent_la_seance_du_jour():
    rows = daily(80)
    rows[-1]["v"] = 9e9  # bougie « d'aujourd'hui » : ne doit pas compter dans le volume de la veille
    today = datetime.fromtimestamp(rows[-1]["t"], alpaca.NEW_YORK).date().isoformat()
    f = scanner.features(rows, price=rows[-2]["c"] * 1.03, prev_close=None, today=today)
    assert f["gap_pct"] == pytest.approx(3.0, abs=0.01)
    assert f["rvol"] == pytest.approx(1.0)


def test_filtres_du_scanner(s):
    f = {"price": 50.0, "gap_pct": 3.0, "rvol": 1.0, "dollar_volume": 5e7, "atr_pct": 4.0, "rsi": 60.0,
         "ma20": 45.0, "ma50": 40.0}
    assert scanner.filter_reasons(f, s) == []
    assert "pas en jeu" in scanner.filter_reasons(f | {"gap_pct": 0.5}, s)
    assert set(scanner.filter_reasons(f | {"price": 3.0, "rsi": 80.0, "ma50": 60.0}, s)) >= {"prix", "RSI", "sous la MM50"}


def test_repartition_des_parts_sur_les_tp():
    tps = [{"name": "TP1"}, {"name": "TP2"}, {"name": "TP3"}]
    assert [q for q, _ in paper.legs_for(10, tps)] == [4, 3, 3]
    assert [t["name"] for _, t in paper.legs_for(2, tps)] == ["TP1", "TP2"]
    assert paper.legs_for(0, tps) == []


def entry(decision="GO", composite=7.5, rsi=60, exits=("tp", "stop", "tp")):
    e = {"id": "x", "created_at": "2026-10-06T13:10:00+00:00", "symbol": "ACME", "side": "long",
         "entry": 100.0, "stop": 98.0, "features": {"rsi": rsi, "rvol": 2.0, "gap_pct": 3.0},
         "decision": decision, "composite": composite, "parts": {"technique": 8, "catalyst": 6, "risk": 7},
         "status": "open", "legs": [], "result_r": None, "outcome": None}
    for name, ex, price in zip(("TP1", "TP2", "TP3"), exits, (102.0, 104.0, 106.0)):
        e["legs"].append({"name": name, "qty": 1, "target": price, "predicted_minutes": 60, "order_id": name,
                          "filled_at": "2026-10-06T13:40:00Z", "fill_price": 100.0, "exit": ex,
                          "exit_price": price if ex == "tp" else 98.0, "exit_at": "2026-10-06T14:40:00Z"})
    return e


def test_ordre_alpaca_lu_et_issue_en_r():
    leg = {"filled_at": None, "fill_price": None, "exit": None, "exit_price": None, "exit_at": None}
    paper.apply_order(leg, {"filled_at": "2026-10-06T13:40:00Z", "filled_avg_price": "100", "legs": [
        {"id": "a", "type": "limit", "status": "filled", "filled_avg_price": "102", "filled_at": "2026-10-06T14:00:00Z"},
        {"id": "b", "type": "stop", "status": "canceled"}]})
    assert leg["exit"] == "tp" and leg["exit_price"] == 102.0 and leg["child_ids"] == ["a", "b"]
    e = entry()
    paper.finish(e)
    assert e["result_r"] == pytest.approx((1 - 1 + 3) / 3, abs=0.01) and e["outcome"] == "TP3 touché"


def test_bilan_et_proposition_de_reglage(s):
    book = paper.PaperBook(None)
    for _ in range(paper.MIN_SAMPLES):
        bad, good = entry(rsi=70, exits=("stop", "stop", "stop")), entry(rsi=60)
        paper.finish(bad)
        paper.finish(good)
        book.entries += [bad, good]
    st = paper.stats(book, s)
    assert st["by"]["rsi"]["groups"]["≥ 65"]["avg_r"] == -1.0
    assert any(sg["key"] == "rsi_max" for sg in st["suggestions"])
    assert st["timing"]["median_ratio"] == 1.0
    assert "propositions de réglages suivent" in paper.report_text(st)


def test_seance_reguliere_seulement():
    open_ = int(datetime(2026, 10, 6, 13, 30, tzinfo=timezone.utc).timestamp())  # 9 h 30 à New York
    rows = [{"t": open_ - 600}, {"t": open_}, {"t": open_ + 390 * 60}]
    assert alpaca.regular_hours(rows) == [{"t": open_}]


def test_commande_discord_lit_le_ticker():
    assert bot.parse_trade(["nvda"]) == {"symbol": "NVDA", "market": "stock", "side": "long"}
    assert bot.parse_trade(["BTCUSD", "short"]) == {"symbol": "BTC", "market": "crypto", "side": "short"}
    assert bot.parse_trade(["coin", "crypto"])["market"] == "crypto"
    with pytest.raises(ValueError):
        bot.parse_trade([])


def test_propositions_approuvees_ou_refusees(tmp_path):
    st = Settings(str(tmp_path / "s.json"))
    pr = PR.Proposals(str(tmp_path / "p.json"), st)
    assert pr.propose("rsi_max", 75.0, "rien ne change") is None
    p = pr.propose("rsi_max", 65, "RSI ≥ 65 : -0.40 R")
    assert p["current"] == 75.0 and st["rsi_max"] == 75.0  # rien n'est appliqué avant l'accord
    assert pr.propose("rsi_max", 65, "doublon") is None
    pr.decide(p["id"], approve=True)
    assert st["rsi_max"] == 65
    with pytest.raises(ValueError):
        pr.decide(p["id"], approve=False)
    q = pr.propose("go_min", 6.5, "notes 6-7 positives")
    pr.decide(q["id"], approve=False)
    assert st["go_min"] == 7.0
    assert pr.propose("go_min", 6.5, "encore") is None  # refusée : pas reproposée


async def test_commandes_de_proposition(monkeypatch, tmp_path):
    st = Settings(str(tmp_path / "s.json"))
    pr = PR.Proposals(str(tmp_path / "p.json"), st)
    monkeypatch.setattr(PR, "proposals", pr)
    monkeypatch.setattr(bot, "settings", st)
    assert await bot.handle("!propositions") == "Aucune proposition en attente."
    p = pr.propose("rsi_max", 65, "test")
    assert "Proposition 1" in await bot.handle("!propositions")
    assert "approuvée" in await bot.handle(f"!oui {p['id']}")
    assert st["rsi_max"] == 65
    assert "déjà" in await bot.handle(f"!non {p['id']}")
    assert "numéro" in await bot.handle("!oui")
    assert "rsi_max" in await bot.handle("!regle")
    assert await bot.handle("bonjour") is None
    assert "!oui" in await bot.handle("!aide")


def test_bot_reserve_au_proprietaire(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.delenv("JARVIS_DISCORD_OWNER_ID", raising=False)
    assert not bot.enabled()
    monkeypatch.setenv("JARVIS_DISCORD_OWNER_ID", "561632751028142111")
    assert bot.enabled() and bot.owners() == {561632751028142111}


def test_scanner_a_l_heure_sans_rattrapage(monkeypatch):
    from app import scheduler
    monkeypatch.setattr(scheduler.alpaca, "configured", lambda: True)
    monkeypatch.setattr(scheduler, "state", type("S", (), {"data": {}})())
    at = lambda h, m: datetime(2026, 10, 6, h, m, tzinfo=alpaca.NEW_YORK)  # noqa: E731
    assert not scheduler.scan_due(at(9, 5))
    assert scheduler.scan_due(at(9, 10)) and scheduler.scan_due(at(9, 39))
    assert not scheduler.scan_due(at(11, 0))
    scheduler.state.data["scan_date"] = "2026-10-06"
    assert not scheduler.scan_due(at(9, 12))
