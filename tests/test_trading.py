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


def bar(o, h, l, c, t=T0):  # noqa: E741
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": 1.0}


def test_stop_direct():
    tr = trade()
    msgs = S.apply_bars(tr, [bar(99.0, 99.2, 97.5, 97.8)], NOW)  # stop touché dans la bougie, sans gap
    assert "stop touché à 98" in msgs[0] and tr["status"] == "closed" and tr["targets_hit"] == 0
    assert tr["events"][-1]["price"] == 98.0


def test_stop_franchi_en_gap_execute_a_l_ouverture():
    """Ouverture sous le stop : l'ordre stop part au marché, au pire du stop et de l'ouverture (règle du labo)."""
    tr = trade()
    msgs = S.apply_bars(tr, [bar(94.0, 94.5, 93.0, 94.0)], NOW)
    assert tr["events"][-1]["price"] == 94.0 and S.realized_r(tr) == -3.0
    assert "stop exécuté vers 94 (gap sous le stop 98" in msgs[0] and "-6.00 %" in msgs[0]
    short = trade(side="short", stop=102.0, targets=[{"name": f"TP{i}", "price": p, "pct": 100 - p, "minutes_median": 60,
                                                      "eta_latest": None, "eta_text": "~1 h", "source": "test"}
                                                     for i, p in enumerate([98.0, 96.0, 94.0], start=1)])
    msgs = S.apply_bars(short, [bar(106.0, 107.0, 105.5, 106.5)], NOW)
    assert short["events"][-1]["price"] == 106.0 and S.realized_r(short) == -3.0
    assert "gap au-dessus du stop 102" in msgs[0]
    # Après TP2, le stop à l'entrée franchi en gap : le dernier tiers sort à l'ouverture, pas à l'entrée.
    tr = trade()
    S.apply_bars(tr, [bar(101.0, 104.5, 100.5, 104.2), bar(99.0, 99.5, 98.5, 99.0, T0 + 60)], NOW)
    assert tr["events"][-1]["price"] == 99.0 and S.realized_r(tr) == pytest.approx(0.83)


def test_objectif_compte_franchi_d_un_pas():
    """Comme au labo : un objectif seulement touché ne compte pas (ordre limite peut-être pas rempli)."""
    tr = trade()
    assert S.apply_bars(tr, [bar(101.0, 102.0, 100.5, 101.5)], NOW) == [] and tr["targets_hit"] == 0
    msgs = S.apply_bars(tr, [bar(101.5, 102.01, 101.0, 101.8, T0 + 60)], NOW)
    assert tr["targets_hit"] == 1 and "TP1 touché" in msgs[0]
    crypto = trade(market="crypto")  # crypto : un pas = 1/10 000 du prix (0,0102 à 102)
    S.apply_bars(crypto, [bar(101.0, 102.01, 100.5, 101.5)], NOW)
    assert crypto["targets_hit"] == 0
    S.apply_bars(crypto, [bar(101.0, 102.02, 100.5, 101.5, T0 + 60)], NOW)
    assert crypto["targets_hit"] == 1


def test_r_net_de_frais_estimes():
    """100/98 stoppé sans taille : 11 actions (20 € × 1,15 / 2 $) en 3 brackets IBKR, 1 $ minimum à chaque entrée
    et à chaque sortie, soit 6 $ pour 22 $ de risque."""
    tr = trade()
    S.apply_bars(tr, [bar(99.0, 99.2, 97.5, 97.8)], NOW)
    assert S.realized_r(tr) == -1.0
    assert S.realized_r_net(tr) == pytest.approx(-1.273, abs=1e-3) and tr["realized_r_net"] == S.realized_r_net(tr)
    assert S.realized_r_net(trade()) is None  # en cours
    big = trade(sizing={"risk_amount": 100.0})  # 57 actions en 3 brackets de 19 : les mêmes 6 $ pèsent moins
    S.apply_bars(big, [bar(99.0, 99.2, 97.5, 97.8)], NOW)
    assert S.realized_r_net(big) == pytest.approx(-1.0 - 6 / 114, abs=1e-3)
    coin = trade(market="crypto")  # crypto : 0,10 % à l'entrée et à la sortie, sur 2 % de risque
    S.apply_bars(coin, [bar(99.0, 99.2, 97.5, 97.8)], NOW)
    assert S.realized_r_net(coin) == pytest.approx(-1.1, abs=1e-3)


def test_frais_a_la_taille_en_euros():
    """Taille en euros (risque en € × change noté avec elle, pas risk_amount déjà en $) ; sans taille proposée, ta
    règle (`sizing_regle`) ; un short paie les mêmes commissions."""
    from app.trading import fees as FEES

    eur = trade(sizing={"risk_amount_eur": 50.0, "risk_amount": 55.0, "eurusd": 1.1, "quantity": 27})
    S.apply_bars(eur, [bar(99.0, 99.2, 97.5, 97.8)], NOW)
    assert FEES.shares(eur, 2.0) == 27  # 50 € × 1,1 / 2 $, pas 55 $ × 1,15
    assert S.realized_r_net(eur) == pytest.approx(-1.0 - 6 / 54, abs=1e-3)
    rule = trade(sizing=None, sizing_regle={"risk_amount_eur": 20.0, "risk_amount": 24.0, "eurusd": 1.2})
    S.apply_bars(rule, [bar(99.0, 99.2, 97.5, 97.8)], NOW)
    assert FEES.shares(rule, 2.0) == 12 and S.realized_r_net(rule) == pytest.approx(-1.25, abs=1e-3)
    # Une action trop chère pour le risque : une action quand même, un seul bracket (sur TP2).
    one = trade(entry=100.0, stop=70.0, sizing={"risk_amount_eur": 20.0, "eurusd": 1.15, "quantity": 0})
    S.apply_bars(one, [bar(69.0, 69.5, 68.0, 69.0)], NOW)
    assert FEES.shares(one, 30.0) == 1 and S.realized_r_net(one) == pytest.approx(-1.03 - (1 + 0.69) / 30, abs=1e-3)
    short = trade(side="short", stop=102.0, targets=[{"name": f"TP{i}", "price": p, "pct": 100 - p,
                                                      "minutes_median": 60, "eta_latest": None, "eta_text": "~1 h",
                                                      "source": "test"} for i, p in enumerate([98.0, 96.0, 94.0], 1)])
    S.apply_bars(short, [bar(101.0, 102.5, 100.8, 102.2)], NOW)
    assert S.realized_r(short) == -1.0 and S.realized_r_net(short) == pytest.approx(-1.273, abs=1e-3)


def test_r_net_brut_si_frais_illisibles(monkeypatch):
    from app.trading import fees as FEES

    def broken(t):
        raise KeyError("price")

    tr = trade()
    S.apply_bars(tr, [bar(99.0, 99.2, 97.5, 97.8)], NOW)
    monkeypatch.setattr(FEES, "fees_r", broken)
    assert S.realized_r_net(tr) == -1.0  # jamais d'exception : l'arrêt de la journée et `!desaccords` tiennent


def test_anciens_trades_sans_nouveaux_champs(tmp_path):
    """Trades écrits avant ce suivi (ni marché, ni realized_r_net, ancienne taille, stop noté au prix du stop) :
    tout se lit, rien ne plante."""
    import json

    from app.lab import grade as GRADE

    base = {"side": "long", "entry": 100.0, "stop": 98.0, "stop_pct": -2.0, "stop_current": 98.0,
            "created_at": "2026-10-01T14:00:00+00:00", "last_price": 100.0, "last_check": 0, "late_warned": [],
            "exit_signals": [], "note": None,
            "targets": [{"name": f"TP{i}", "price": p, "pct": p - 100, "minutes_median": 60, "eta_latest": None,
                         "eta_text": "~1 h", "source": "test"} for i, p in enumerate([102.0, 104.0, 106.0], 1)],
            "score": {"go": True, "decision": "GO", "composite": 7.2, "reasons": []}}
    old = [base | {"id": "a1", "symbol": "OLD", "status": "closed", "targets_hit": 0,
                   "sizing": {"capital": 5000.0, "risk_pct": 1.0, "risk_amount": 50.0, "quantity": 25.0,
                              "notional": 2500.0, "leverage": 1.0},
                   "events": [{"kind": "stop", "price": 98.0, "at": "2026-10-01T15:00:00+00:00"}]},
           base | {"id": "a2", "symbol": "TOP", "status": "closed", "targets_hit": 3, "sizing": None,
                   "score": {"go": False, "decision": "NO GO", "composite": 5.1, "reasons": []},
                   "events": [{"kind": f"tp{i}", "price": p, "at": "2026-10-02T15:00:00+00:00"}
                              for i, p in enumerate([102.0, 104.0, 106.0], 1)]},
           base | {"id": "a3", "symbol": "RUN", "status": "open", "targets_hit": 1,
                   "events": [{"kind": "tp1", "price": 102.0, "at": "2026-10-03T15:00:00+00:00"}]}]
    path = tmp_path / "trades.json"
    path.write_text(json.dumps(old), encoding="utf-8")
    book = S.Book(str(path))
    assert [S.summary(t)["realized_r_net"] for t in book.trades] == [pytest.approx(-1 - 6 / 56, abs=1e-3),
                                                                      pytest.approx(2 - 6 / 22, abs=1e-3), None]
    st = S.disagreements(book, {})
    assert st["avec"]["closed"] == 1 and st["contre"]["closed"] == 1 and st["contre"]["open"] == 0
    assert "Pas de verdict" in S.disagreements_text(st)
    real = GRADE.reels(book.trades)
    assert real["stock"]["termines"] == 2 and "Tes trades réels" in GRADE.populations_text([], real)
    assert GRADE.day_r(book.trades, datetime(2026, 10, 2, 18, 0, tzinfo=timezone.utc)) == pytest.approx(1.73, abs=0.01)
    res = S.record_exit(book, "TOP", 105.0)  # dernier objectif noté, rempli plus bas : corrigé
    assert res["corrige"] and S.realized_r(book.get("a2")) == pytest.approx((1 + 2 + 2.5) / 3, abs=0.01)
    assert "sortie corrigée à 105 au lieu de 106" in S.exit_text(res, NOW)
    assert json.loads(path.read_text(encoding="utf-8"))[1]["events"][-1]["prix_suivi"] == 106.0


def test_sortie_au_prix_reel():
    """`!sortie` : ferme le trade ouvert à ton prix, ou corrige la dernière sortie notée par le suivi."""
    book = S.Book(None)
    tr = book.add(trade(symbol="NVDA", entry=132.5, stop=130.0))
    res = S.record_exit(book, "nvda", 131.2)
    assert not res["corrige"] and tr["status"] == "closed" and tr["events"][-1]["price"] == 131.2
    assert "clôturé à 131.2" in S.exit_text(res, NOW) and tr["realized_r_net"] is not None
    acme = book.add(trade(symbol="ACME"))
    S.apply_bars(acme, [bar(99.0, 99.2, 97.5, 97.8)], NOW)
    res = S.record_exit(book, "ACME", 96.5)
    assert res["corrige"] and S.realized_r(acme) == -1.75
    assert acme["events"][-1]["corrige"] and acme["events"][-1]["prix_suivi"] == 98.0
    assert acme["realized_r_net"] == pytest.approx(-1.75 - 6 / 22, abs=1e-3)
    assert "sortie corrigée à 96.5 au lieu de 98" in S.exit_text(res, NOW)
    # Dernier objectif noté atteint mais pas rempli : la sortie corrigée change le dernier tiers.
    full = book.add(trade(symbol="FULL"))
    S.apply_bars(full, [bar(105.0, 106.5, 104.0, 105.0)], NOW)
    S.record_exit(book, "FULL", 103.0)
    assert S.realized_r(full) == pytest.approx((1 + 2 + 1.5) / 3, abs=0.01)
    assert S.record_exit(book, "MSFT", 400.0) is None


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
    S.apply_bars(tr, [bar(99.0, 99.2, 97.5, 97.8)], NOW)  # sans gap : le gap est testé à part
    assert S.realized_r(tr) == -1.0  # stop direct
    tr = trade()
    S.apply_bars(tr, bars([102.5, 104.2], spread=0.5, start=T0, step=60) + [bar(100.5, 100.6, 99.5, 99.8, T0 + 120)],
                 NOW)
    assert S.realized_r(tr) == 1.0  # TP1 (1 R) + TP2 (2 R) + dernier tiers au stop remonté à l'entrée (0 R)
    # Ta règle (stop à l'entrée après TP2 seulement) : TP1, retour à l'entrée puis stop d'origine = ⅓ × 1 R − ⅔ R.
    tr = trade()
    S.apply_bars(tr, [bar(101.0, 102.5, 100.5, 102.0), bar(100.0, 100.2, 99.0, 99.5, T0 + 60),
                      bar(99.0, 99.2, 97.5, 97.8, T0 + 120)], NOW)
    assert tr["events"][-1]["kind"] == "stop" and S.realized_r(tr) == pytest.approx(1 / 3 - 2 / 3, abs=0.01)
    tr = trade()
    S.apply_bars(tr, bars([105.0], spread=1.5, start=T0, step=60), NOW)
    assert S.realized_r(tr) == 2.0
    assert S.realized_r(trade()) is None  # en cours


DAY = 86400
# Séquences de bougies d'1 min, sans gap : « tp » les trois objectifs (+2 R), « be » TP1, TP2 puis le stop remonté à
# l'entrée (+1 R), « stop » le stop d'origine (−1 R). Frais : 6 $ pour 22 $ de risque (0,273 R) sur chacune.
OUTCOMES = {"tp": [(105.0, 106.5, 104.0, 105.0)],
            "be": [(101.0, 102.5, 100.5, 102.0), (103.0, 104.5, 102.5, 104.0), (100.5, 100.6, 99.5, 99.8)],
            "stop": [(99.0, 99.2, 97.5, 97.8)]}


def closed(book, go: bool, outcome: str, day: int):
    tr = book.add(scored(go))
    S.apply_bars(tr, [bar(*ohlc, t=T0 + day * DAY + 60 * i) for i, ohlc in enumerate(OUTCOMES[outcome])], NOW)
    return tr


def desaccords(go_outcomes, nogo_outcomes, days, slopes=None):
    book = S.Book(None)
    for i, (a, c) in enumerate(zip(go_outcomes, nogo_outcomes)):
        closed(book, True, a, i % days)
        closed(book, False, c, i % days)
    return S.disagreements(book, slopes)


def test_tableau_des_desaccords():
    """R nets de frais estimés (bruts à côté), jours de sortie comptés en grappes, pas de verdict à 10 contre 10."""
    book = S.Book(None)
    for i in range(10):
        closed(book, True, "tp", i)
        closed(book, False, "stop", i)
    book.add(scored(False))  # un trade contre l'avis encore en cours
    st = S.disagreements(book, {"stock": 0.05})
    assert st["avec"]["closed"] == 10 and st["avec"]["avg_r_brut"] == 2.0 and st["avec"]["days"] == 10
    assert st["avec"]["avg_r"] == pytest.approx(2.0 - 6 / 22, abs=1e-3)
    assert st["contre"]["closed"] == 10 and st["contre"]["open"] == 1 and st["contre"]["avg_r_brut"] == -1.0
    text = S.disagreements_text(st)
    assert "R nets de frais estimés, bruts entre parenthèses" in text and "Politique mesurée" in text
    assert "stop d'origine jusqu'au TP2 puis à l'entrée, aucune sortie au temps" in text
    assert "Pas de verdict : écart -3.00 R, IC 95 % [" in text and "(10/10 trades, 10/10 jours)" in text
    assert "JARVIS a raison" not in text and "tu as raison" not in text
    assert "**2. NO GO trop sévère ?** Pas de réponse" in text


def test_desaccords_pas_de_verdict_sur_trois_jours():
    st = desaccords(["tp"] * 30, ["stop"] * 30, days=3, slopes={"stock": 0.05})
    text = S.disagreements_text(st)
    assert "Pas de verdict" in text and "(30/30 trades, 3/3 jours)" in text and "JARVIS a raison" not in text


def test_desaccords_verdict_avec_ic_et_pente():
    go = ["tp", "be"] * 15  # +1,5 R brut en moyenne, avec de la dispersion
    st = desaccords(go, ["stop"] * 30, days=30, slopes={"stock": 0.05})
    text = S.disagreements_text(st)
    assert "**JARVIS a raison** : les GO font mieux que les NO GO pris (écart -2.50 R, IC 95 % [" in text
    assert "Non, JARVIS a raison de dire NO GO" in text and "non prédictive" not in text
    # Note non prédictive au labo (pente ≤ 0) : jamais « JARVIS a raison », la mention est affichée.
    st = desaccords(go, ["stop"] * 30, days=30, slopes={"stock": -0.02})
    text = S.disagreements_text(st)
    assert "JARVIS a raison" not in text and "Note non prédictive au labo (pente −0,020 R/pt, actions)" in text
    assert "Pas de verdict pour JARVIS" in text
    st = desaccords(go, ["stop"] * 30, days=30)  # pente inconnue (labo absent)
    assert "JARVIS a raison" not in S.disagreements_text(st)
    # Les NO GO pris font nettement mieux : la note classe à l'envers.
    text = S.disagreements_text(desaccords(["stop"] * 30, go, days=30, slopes={"stock": 0.05}))
    assert "**tu as raison contre JARVIS**" in text and "**Oui pour l'instant**" in text


def test_pente_de_la_note_lue_dans_le_cache_du_labo(tmp_path, monkeypatch):
    from app.lab import stats as LS
    from app.lab.db import LabDB

    path = str(tmp_path / "lab.db")
    monkeypatch.setenv("JARVIS_LAB_DB", path)
    S.SC._slopes.update(key=None, at=0.0, data={})  # cache de score.lab_slopes vidé
    assert S.note_slopes() == {}  # pas de labo
    S.SC._slopes.update(key=None, at=0.0, data={})
    db = LabDB(path)
    LS.store(db.conn, "courant", {"marches": {"stock": {"calibration_note": {"pente_r_par_point": -0.02}},
                                              "crypto": {"calibration_note": {"pente_r_par_point": None}}}},
             10, "2026-10-10T00:00:00+00:00")
    db.close()
    assert S.note_slopes() == {"stock": -0.02}  # pente non mesurée : absente


async def test_commande_desaccords_lit_la_pente(monkeypatch):
    from app import scheduler
    from app.notify import bot

    book = S.Book(None)
    for i in range(3):
        closed(book, True, "tp", i)
    monkeypatch.setattr(scheduler, "book", book)
    monkeypatch.setattr(bot.trading, "note_slopes", lambda: {"stock": -0.02})
    text = await bot.handle("!desaccords")
    assert "Note non prédictive au labo (pente −0,020 R/pt, actions)" in text and "3 terminé(s) sur 3 jour(s)" in text


def test_desaccords_egalite_ne_va_pas_a_jarvis():
    st = desaccords(["tp", "stop"] * 15, ["stop", "tp"] * 15, days=30, slopes={"stock": 0.05})
    text = S.disagreements_text(st)
    assert st["avec"]["avg_r"] == pytest.approx(st["contre"]["avg_r"])
    assert "Pas de verdict : écart" in text and "JARVIS a raison" not in text and "tu as raison" not in text
