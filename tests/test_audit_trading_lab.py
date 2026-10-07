"""Audit complet (2026-10-07) : régressions du trading et du labo."""
from datetime import date, datetime, timezone

import pytest

from app.core.http import SourceError
from app.lab import collector as CO
from app.lab import summary as SUM
from app.lab import validator as V
from app.lab import worker as W
from app.lab.db import LabDB
from app.trading import catalyst as C
from app.trading import indicators as I
from app.trading import paper as PA
from app.trading import scanner as SC
from app.trading import score as SCORE
from app.trading import service as S
from app.trading.settings import Settings, validate

from tests.lab_fixtures import fill_stock

NOW = datetime(2026, 10, 7, 13, 10, tzinfo=timezone.utc)  # 09:10 à New York


# --- Veto résultats : calendrier Finnhub en échec ------------------------------------------------

async def test_resultats_non_verifies_plafonnent_et_ecartent_du_scanner(monkeypatch):
    async def ok(*a, **k):
        return []

    async def quota(*a, **k):
        raise SourceError("429")

    monkeypatch.setattr(C.finnhub, "profile", ok)
    monkeypatch.setattr(C.finnhub, "company_news", ok)
    monkeypatch.setattr(C.finnhub, "earnings", quota)
    cat = await C.check("ABC", NOW)
    assert cat["available"] and cat["earnings_known"] is False
    score, reasons = SCORE.catalyst(cat | {"fresh_news": True, "positive": 3}, "long", Settings(None))
    assert score <= 5.0 and any("non vérifiée" in r for r in reasons)

    f = {"price": 50.0, "gap_pct": 3.0, "rvol": None, "atr_pct": 4.0, "rsi": 60.0, "ma20": 40.0, "ma50": 35.0}

    async def fake(value):
        return value

    monkeypatch.setattr(SC.alpaca, "configured", lambda: True)
    monkeypatch.setattr(SC, "universe", lambda s: fake(["ABC"]))
    monkeypatch.setattr(SC.alpaca, "snapshots", lambda symbols: fake({"ABC": {"price": 50.0}}))
    monkeypatch.setattr(SC.alpaca, "bars", lambda *a, **k: fake({"ABC": [{"c": 50.0}]}))
    monkeypatch.setattr(SC, "features", lambda *a: f)
    monkeypatch.setattr(SC.C, "safe_check", lambda sym, now: fake(cat))
    monkeypatch.setattr(SC.T, "build_plan", lambda *a, **k: fake({"score": {"composite": 7.0}}))
    result = await SC.scan(NOW, s=Settings(None), save=False)
    assert not result["picks"] and "non vérifiée" in result["skipped"][0]


# --- Plan hors séance : la dernière bougie close n'est plus jetée ------------------------------------

def test_bougie_close_de_la_veille_gardee_hors_seance():
    t_last = int(datetime(2026, 10, 6, 19, 45, tzinfo=timezone.utc).timestamp())  # 15:45 NY, close
    bars = [{"t": t_last - 900 * i, "o": 100, "h": 101, "l": 99, "c": 100, "v": 10.0} for i in range(5, 0, -1)]
    bars.append({"t": t_last, "o": 100, "h": 110, "l": 99, "c": 109, "v": 50.0})
    out = S.with_open_bar(bars, 15, NOW)
    assert out[-2] is bars[-1] and out[-1]["pending"] and out[-1]["c"] == 109 and out[-1]["v"] == 0
    assert S.with_open_bar(out, 15, NOW) is out  # pas de seconde bougie ajoutée
    live = datetime.fromtimestamp(t_last + 60, timezone.utc)  # bougie encore en cours
    assert S.with_open_bar(bars, 15, live) is bars


async def test_cache_des_bougies_separe_par_source(monkeypatch):
    calls = []

    async def alp(symbol, interval, days=None):
        calls.append("alpaca")
        return [{"t": 0, "o": 1, "h": 1, "l": 1, "c": 111, "v": 1}]

    async def td(symbol, interval, outputsize=None):
        calls.append("twelvedata")
        return [{"t": 0, "o": 1, "h": 1, "l": 1, "c": 222, "v": 1}]

    monkeypatch.setattr(S.alpaca, "candles", alp)
    monkeypatch.setattr(S.twelvedata, "candles", td)
    S._candle_cache.clear()
    assert (await S.candles("ZZZ", "stock", 15, "alpaca"))[0]["c"] == 111
    assert (await S.candles("ZZZ", "stock", 15))[0]["c"] == 222
    assert calls == ["alpaca", "twelvedata"]
    S._candle_cache.clear()


# --- Suivi : la bougie du TP2 relue ne touche pas le stop remonté --------------------------------

def test_bougie_du_tp2_relue_ne_ferme_pas_au_stop_suivi():
    book = S.Book(None)
    tr = book.add({"symbol": "BTC", "side": "long", "entry": 100.0, "stop": 98.0, "stop_pct": -2.0,
                   "created_at": "2026-10-06T00:00:00+00:00",
                   "targets": [{"name": f"TP{i}", "price": p, "pct": p - 100, "minutes_median": 60,
                                "eta_latest": None, "eta_text": "~1 h", "source": "test"}
                               for i, p in enumerate([101.0, 102.0, 106.0], start=1)]})
    t0 = int(datetime(2026, 10, 6, 1, tzinfo=timezone.utc).timestamp())
    bar = {"t": t0, "o": 100.0, "h": 102.2, "l": 99.8, "c": 102.0, "v": 1.0}
    S.apply_bars(tr, [bar], NOW)
    assert tr["targets_hit"] == 2 and tr["stop_current"] == 100.0
    assert S.apply_bars(tr, [bar], NOW) == [] and tr["status"] == "open"  # même bougie relue
    S.apply_bars(tr, [{"t": t0 + 60, "o": 101, "h": 101, "l": 99.9, "c": 100, "v": 1.0}], NOW)
    assert tr["status"] == "closed"  # une bougie suivante, elle, touche le stop


# --- Divergence RSI : le sens demandé n'est plus masqué -----------------------------------------

def test_divergence_dans_le_sens_demande():
    import random
    rnd = random.Random(7158)
    p, candles = 100.0, []
    for i in range(80):
        p *= 1 + rnd.gauss(0, 0.02)
        candles.append({"t": i, "o": p, "h": p, "l": p, "c": p, "v": 1.0})
    up, down = I.rsi_divergence(candles, want="haussière"), I.rsi_divergence(candles, want="baissière")
    assert up in (None, "haussière") and down in (None, "baissière")
    assert I.rsi_divergence(candles) == (up or down)  # sans sens demandé : comme avant


# --- Réglages : poids impossibles et fichier retouché à la main ---------------------------------

def test_poids_technique_et_risque_nuls_refuses(tmp_path):
    with pytest.raises(ValueError):
        validate("weights", {"technique": 0, "risk": 0, "catalyst": 1})
    path = tmp_path / "s.json"
    path.write_text('{"go_min": 6.5, "scan_time_ny": "25:99", "weights": {"technique": 1}, "inconnu": 1}')
    s = Settings(str(path))
    assert s["go_min"] == 6.5 and s["scan_time_ny"] == "09:10" and s["weights"]["catalyst"] == 0.3


# --- Forward-test : sortie au cours inconnu écartée, pas comptée 0 R ------------------------------

def test_sortie_au_cours_inconnu_non_comptee():
    entry = {"side": "long", "entry": 100.0, "stop": 98.0, "legs": [
        {"name": "TP1", "qty": 1, "fill_price": 100.0, "exit": "expiré", "exit_price": None},
        {"name": "TP2", "qty": 1, "fill_price": 100.0, "exit": "stop", "exit_price": 98.0}]}
    PA.finish(entry)
    assert entry["result_r"] == -1.0
    entry["legs"].pop()
    PA.finish(entry)
    assert entry["result_r"] is None and entry["outcome"] == "expiré"


# --- Validateur : règle par marché avec un champion filtrant ------------------------------------

def test_part_manquante_avec_champion_et_marches(tmp_path, monkeypatch):
    db = LabDB(str(tmp_path / "lab.db"))
    monkeypatch.setattr(V, "champion_filter", lambda conn: ("(t.composite > ? AND t.composite < ?)", [0, 99]))
    canon = {"cond": {"field": "context.rvol", "op": ">", "value": 2}, "markets": ["stock"]}
    assert V.missing_share(db.conn, canon, "v1") == {"context.rvol": 1.0}


# --- Labo : résumé du soir et ménage des bougies ------------------------------------------------

def test_resume_rattrape_le_lendemain_matin():
    evening = datetime(2026, 10, 7, 20, 40, tzinfo=timezone.utc)  # 22 h 40 à Paris
    assert SUM.due(evening, "2026-10-06") == "2026-10-07"
    morning = datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc)  # 8 h à Paris
    assert SUM.due(morning, "2026-10-06") == "2026-10-07"
    assert SUM.due(morning, "2026-10-07") is None
    assert SUM.due(datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc), "2026-10-06") is None  # 14 h


def test_labo2_avant_le_rejeu_quand_le_resume_est_attendu(tmp_path):
    evening = datetime(2026, 10, 7, 20, 40, tzinfo=timezone.utc)
    assert W.summary_first(evening, "2026-10-06") and not W.summary_first(evening, "2026-10-07")
    db = LabDB(str(tmp_path / "lab.db"))
    assert W.market_order(db, ["stock", "crypto"]) == ["stock", "crypto"]
    assert W.market_order(db, ["stock", "crypto"]) == ["crypto", "stock"]
    assert W.market_order(db, ["stock", "crypto"]) == ["stock", "crypto"]


def test_menage_efface_les_vieilles_bougies_et_rogne_la_couverture(tmp_path):
    db = LabDB(str(tmp_path / "lab.db"))
    fill_stock(db, ["AAA", "BBB"], date(2026, 6, 1), date(2026, 6, 30))
    first, last = CO.session_ts(date(2026, 6, 1))[0], CO.session_ts(date(2026, 6, 30))[1]
    db.set("cov5:stock", "AAA", [first, last])
    cut = CO.session_ts(date(2026, 6, 15))[0]
    db.set("cov5:stock", "BBB", [first, cut])
    removed = CO.prune_intraday(db, "stock", cut)
    assert removed > 0
    left = db.conn.execute("SELECT MIN(ts) FROM bars WHERE market = 'stock' AND tf = 5 AND symbol = 'AAA'").fetchone()[0]
    assert left >= cut and db.get("cov5:stock", "AAA") == [cut, last]
    assert db.get("cov5:stock", "BBB") is None  # entièrement avant la coupure
    assert db.conn.execute("SELECT COUNT(*) FROM bars WHERE tf = 1440").fetchone()[0] > 0  # journalier gardé


def test_coupure_respecte_rejeu_direct_et_mouvements(tmp_path):
    db = LabDB(str(tmp_path / "lab.db"))
    now = datetime(2026, 10, 7, 2, 0, tzinfo=timezone.utc)
    db.set("replay:stock", "start", "2024-10-01")
    db.set("replay:stock", "end", "2026-10-05")
    db.set("replay:stock", "last_day", "2024-10-18")
    cut = W.prune_cutoff(db, "stock", now)
    assert cut <= CO.session_ts(CO.sessions_back(date(2024, 10, 18), CO.STOCK_HISTORY_SESSIONS))[0]
    db.set("replay:stock", "last_day", "2026-10-05")  # rejeu fini : seul le direct compte
    cut = W.prune_cutoff(db, "stock", now)
    assert cut <= CO.session_ts(CO.sessions_back(date(2026, 10, 6), CO.STOCK_HISTORY_SESSIONS))[0]
    assert cut > CO.session_ts(date(2026, 1, 2))[0]
    db.replace_universe("stock", "2025-03-03", [{"symbol": "AAA", "rank": 1, "reason": "x"}])
    assert W.prune_cutoff(db, "stock", now) < CO.session_ts(date(2025, 3, 3))[0]  # mouvements à calculer


async def test_retour_dans_l_univers_ne_comble_pas_le_trou(tmp_path, monkeypatch):
    db = LabDB(str(tmp_path / "lab.db"))
    old = CO.session_ts(date(2025, 1, 31))
    db.set("cov5:stock", "AAA", [old[0], old[1]])
    db.put_bars("stock", "AAA", 5, [{"t": old[0], "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}])
    asked = []

    async def bars(symbols, tf, a, b, transport=None):
        asked.append(a)
        return {}

    monkeypatch.setattr(CO.SRC, "alpaca_bars", bars)
    start, end = CO.session_ts(date(2025, 12, 1))[0], CO.session_ts(date(2025, 12, 2))[1]
    await CO.ensure_intraday(db, "stock", ["AAA"], start, end, now=datetime(2026, 1, 5, tzinfo=timezone.utc))
    assert min(asked).timestamp() >= start and len(asked) == 2
    assert db.get("cov5:stock", "AAA") == [start, end]
    assert db.conn.execute("SELECT COUNT(*) FROM bars WHERE tf = 5").fetchone()[0] == 0
