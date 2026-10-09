"""Tests hors ligne du moteur de plans : niveaux, délais rejoués, heures de cotation, TP/SL touchés."""
from datetime import datetime, timezone

import pytest

from app.trading import plan as P
from app.trading import service as S


def bars(closes, spread=1.0, start=0, step=3600):
    return [{"t": start + i * step, "o": c, "h": c + spread, "l": c - spread, "c": c, "v": 1.0}
            for i, c in enumerate(closes)]


def test_niveaux_par_defaut_long():
    candles = bars([100.0] * 30, step=60)  # une seule séance : pas de pivots
    lv = P.default_levels(candles, "long")
    prices = [t.price for t in lv.targets]
    assert lv.stop < lv.entry < prices[0] < prices[1] < prices[2]
    # Marché plat : pas de résistance, on complète par 1 R, 2 R, 3 R.
    assert prices[1] - lv.entry == pytest.approx(2 * lv.risk)


def test_niveaux_incoherents_refuses():
    with pytest.raises(ValueError):
        P.default_levels(bars([100.0] * 30), "long", entry=100, stop=101)
    with pytest.raises(ValueError):
        P.default_levels(bars([100.0] * 30), "short", entry=100, stop=102, targets=[105])


def test_delais_sur_tendance_reguliere():
    # +1 par bougie, amplitude ±0,5 : le TP à +2 % est atteint, le stop à -3 % jamais.
    candles = bars([100.0 + i for i in range(100)], spread=0.5)
    lv = P.Levels("long", 100.0, 97.0, [P.Target(102.0, "test")])
    timing = P.historical_timing(candles, lv, horizon_bars=10)
    assert timing["stop_first_rate"] == 0
    assert timing["targets"][0]["hit_rate"] == 1
    assert 1 <= timing["targets"][0]["bars_median"] <= 3


def test_bougie_qui_touche_tp_et_stop_compte_comme_stop():
    candles = bars([100.0] * 60, spread=5.0)
    lv = P.Levels("long", 100.0, 98.0, [P.Target(102.0, "test")])
    timing = P.historical_timing(candles, lv, horizon_bars=5)
    assert timing["targets"][0]["hit_rate"] == 0
    assert timing["stop_first_rate"] == 1


def test_minutes_de_cotation_sautent_la_nuit_et_le_week_end():
    friday_1530_ny = datetime(2026, 10, 2, 19, 30, tzinfo=timezone.utc)
    eta = S.add_market_minutes(friday_1530_ny, 60, closed=set(), early=set())
    # 30 min vendredi, puis 30 min lundi après l'ouverture de 9 h 30
    assert eta.astimezone(S.NEW_YORK).isoformat().startswith("2026-10-05T10:00")


def test_jour_ferme_saute():
    eta = S.add_market_minutes(datetime(2026, 11, 25, 20, 59, tzinfo=timezone.utc), 30,
                               closed={"2026-11-26"}, early={"2026-11-27"})
    assert eta.astimezone(S.NEW_YORK).isoformat().startswith("2026-11-27T09:59")


def trade(**over):
    t = {"symbol": "BTC", "side": "long", "entry": 100.0, "stop": 98.0, "stop_pct": -2.0,
         "created_at": "2026-10-06T00:00:00+00:00",
         "targets": [{"name": f"TP{i}", "price": p, "pct": p - 100, "minutes_median": 60, "eta_latest": None,
                      "eta_text": "~1 h", "source": "test"} for i, p in enumerate([102.0, 104.0, 106.0], start=1)]}
    book = S.Book(None)
    return book.add(t | over)


NOW = datetime(2026, 10, 6, 3, 0, tzinfo=timezone.utc)
T0 = int(datetime(2026, 10, 6, tzinfo=timezone.utc).timestamp())


def test_stop_inchange_apres_tp1_puis_a_l_entree_apres_tp2():
    tr = trade()
    msgs = S.apply_bars(tr, bars([101.0, 102.5], spread=0.5, start=T0, step=60), NOW)
    assert len(msgs) == 1 and "TP1 touché" in msgs[0] and "pas de break-even" in msgs[0]
    assert tr["stop_current"] == 98.0 and tr["status"] == "open"
    # La même bougie relue au passage suivant ne renvoie pas d'alerte en double.
    assert S.apply_bars(tr, bars([102.5], spread=0.5, start=T0 + 60, step=60), NOW) == []
    msgs = S.apply_bars(tr, bars([104.2], spread=0.5, start=T0 + 120, step=60), NOW)
    assert "TP2 touché" in msgs[0] and tr["stop_current"] == 100.0
    msgs = S.apply_bars(tr, bars([99.0], spread=0.5, start=T0 + 180, step=60), NOW)
    assert "stop suivi" in msgs[0] and tr["status"] == "closed"


def test_un_kill_pris_remonte_le_stop_des_tp1():
    """`!prends … kill` : la gestion du KILL, celle que mesure son journal (stop à l'entrée dès TP1)."""
    tr = trade(killmode=True)
    msgs = S.apply_bars(tr, bars([101.0, 102.5], spread=0.5, start=T0, step=60), NOW)
    assert "TP1 touché" in msgs[0]
    assert "Vérifie que le stop ajustable des brackets restants est passé à l'entrée" in msgs[0]
    assert tr["stop_current"] == 100.0
    msgs = S.apply_bars(tr, bars([99.0], spread=0.5, start=T0 + 120, step=60), NOW)
    assert "stop suivi" in msgs[0] and tr["status"] == "closed"


def test_un_kill_pris_vend_avant_une_cloture_courte():
    """Séance courte (vendredi 27 novembre 2026, clôture à 13 h New York) : la vente du reste se fait vers 12 h 59."""
    start = int(datetime(2026, 11, 27, 16, 0, tzinfo=timezone.utc).timestamp())  # 11 h New York
    tr = trade(killmode=True, symbol="ACME", market="stock", created_at="2026-11-27T15:00:00+00:00")
    msgs = S.apply_bars(tr, bars([101.0, 102.5], spread=0.5, start=start, step=60),
                        datetime(2026, 11, 27, 16, 5, tzinfo=timezone.utc))
    assert "Le reste se vend au marché vers 12 h 59 New York." in msgs[0]


def test_stop_direct():
    tr = trade()
    msgs = S.apply_bars(tr, bars([97.0], spread=0.5, start=T0, step=60), NOW)
    assert "stop touché" in msgs[0] and tr["status"] == "closed" and tr["targets_hit"] == 0


def test_tous_les_tp_en_une_bougie():
    tr = trade()
    msgs = S.apply_bars(tr, bars([105.0], spread=1.5, start=T0, step=60), NOW)
    assert len(msgs) == 3 and tr["status"] == "closed"


def test_alerte_de_retard_une_seule_fois():
    tr = trade()
    tr["targets"][0]["eta_latest"] = "2026-10-06T02:00:00+00:00"
    msgs = S.apply_bars(tr, bars([100.5], spread=0.2, start=T0, step=60), NOW)
    assert len(msgs) == 1 and "pas encore touché" in msgs[0]
    assert S.apply_bars(tr, bars([100.5], spread=0.2, start=T0 + 60, step=60), NOW) == []


def test_esperance_et_rr_minimal():
    e = P.expectancy(2.0, hit_rate=0.4, stop_rate=0.5)
    assert e["expectancy_r"] == pytest.approx(0.3)
    assert e["breakeven_r"] == pytest.approx(1.25)


def test_objectifs_sur_les_resistances():
    # Deux sommets nets au-dessus du prix : les TP s'y placent, juste avant le niveau.
    closes = [100.0] * 20 + [110.0] + [100.0] * 20 + [120.0] + [100.0] * 20
    candles = bars(closes, spread=0.5, step=60)  # une seule séance : pas de pivots
    lv = P.default_levels(candles, "long", entry=100.0, stop=98.0)
    assert [t.source for t in lv.targets[:2]] == ["sommet de swing", "sommet de swing"]
    assert 109 < lv.targets[0].price < 110.5 and 119 < lv.targets[1].price < 120.5


def test_sizing_emperorbtc():
    s = S.sizing(entry=100.0, stop=95.0, capital=10_000, risk_pct=3)
    assert s["risk_amount"] == 300 and s["quantity"] == 60 and s["leverage"] == pytest.approx(1.0)


def test_signal_de_sortie_une_seule_fois(monkeypatch):
    tr = trade()
    tr["last_price"] = 101.0
    monkeypatch.setattr(S.S, "exit_signals", lambda bars, side: [("weis", "anomalie")])
    assert len(S.apply_exit_signals(tr, [])) == 1
    assert S.apply_exit_signals(tr, []) == []


def test_pas_de_signal_de_sortie_en_perte(monkeypatch):
    tr = trade()
    tr["last_price"] = 99.0
    monkeypatch.setattr(S.S, "exit_signals", lambda bars, side: [("weis", "anomalie")])
    assert S.apply_exit_signals(tr, []) == []


def test_cockpit_texte():
    book = S.Book(None)
    assert "Aucun trade" in S.cockpit_text(book, NOW)
    t = book.add(trade())
    t["targets_hit"] = 1
    t["events"].append({"kind": "tp1", "price": 102.0, "at": "2026-10-06T01:00:00+00:00"})
    text = S.cockpit_text(book, NOW)
    assert "BTC" in text and "✅ touché" in text and "TP2" in text


async def test_cockpit_live_sans_appel_pour_les_actions(monkeypatch):
    async def fail(*args, **kwargs):
        raise AssertionError("aucun appel Twelve Data depuis le cockpit")

    monkeypatch.setattr(S.twelvedata, "candles", fail)
    book = S.Book(None)
    t = book.add(trade(symbol="NVDA", market="stock"))
    S._candle_cache.pop(("NVDA", "stock", 1), None)
    assert (await S.live(book))[0] == {"id": t["id"], "price": 100.0, "chart": [], "as_of": None}
    S._candle_cache[("NVDA", "stock", 1, "")] = (0.0, bars([100.0, 101.5], start=T0, step=60))
    live = (await S.live(book))[0]
    assert live["price"] == 101.5 and live["chart"] == [[T0, 100.0], [T0 + 60, 101.5]]


def scored(go: bool, **over):
    reasons = [] if go else ["R/R 1.2 sous le minimum de 1.5"]
    return trade(score={"go": go, "decision": "GO" if go else "NO GO", "reasons": reasons}, **over)


def test_desaccord_dit_franchement_et_marque_le_trade():
    tr = scored(False)
    assert S.against_jarvis(tr) and S.summary(tr)["against_jarvis"]
    assert "pas d'accord" in S.disagreement_text(tr) and "R/R 1.2" in S.disagreement_text(tr)
    assert not S.against_jarvis(scored(True)) and not S.against_jarvis(trade())


def test_gain_en_r_un_tiers_par_objectif():
    tr = trade()
    S.apply_bars(tr, bars([97.0], spread=0.5, start=T0, step=60), NOW)
    assert S.realized_r(tr) == -1.0  # stop direct
    tr = trade()
    S.apply_bars(tr, bars([102.5, 104.2, 99.0], spread=0.5, start=T0, step=60), NOW)
    assert S.realized_r(tr) == 1.0  # TP1 (1 R) + TP2 (2 R) + dernier tiers au stop remonté à l'entrée (0 R)
    tr = trade()
    S.apply_bars(tr, bars([105.0], spread=1.5, start=T0, step=60), NOW)
    assert S.realized_r(tr) == 2.0
    assert S.realized_r(trade()) is None  # en cours


def test_tableau_des_desaccords():
    book = S.Book(None)
    for go, close in [(True, 105.0)] * 10 + [(False, 97.0)] * 10:
        tr = book.add(scored(go))
        S.apply_bars(tr, bars([close], spread=1.5 if close > 100 else 0.5, start=T0, step=60), NOW)
    book.add(scored(False))  # un trade contre l'avis encore en cours
    st = S.disagreements(book)
    assert st["avec"]["closed"] == 10 and st["avec"]["avg_r"] == 2.0
    assert st["contre"]["closed"] == 10 and st["contre"]["open"] == 1 and st["contre"]["avg_r"] == -1.0
    assert "JARVIS a raison" in S.disagreements_text(st)
    st["contre"]["closed"] = 3
    assert "Pas encore de verdict" in S.disagreements_text(st)
