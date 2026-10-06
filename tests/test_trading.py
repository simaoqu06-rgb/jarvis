"""Tests hors ligne du moteur de plans : niveaux, délais rejoués, heures de cotation, TP/SL touchés."""
from datetime import datetime, timezone

import pytest

from app.trading import plan as P
from app.trading import service as S


def bars(closes, spread=1.0, start=0, step=3600):
    return [{"t": start + i * step, "o": c, "h": c + spread, "l": c - spread, "c": c, "v": 1.0}
            for i, c in enumerate(closes)]


def test_niveaux_par_defaut_long():
    candles = bars([100.0] * 30)
    lv = P.default_levels(candles, "long")
    assert lv.stop < lv.entry < lv.targets[0] < lv.targets[1] < lv.targets[2]
    assert lv.targets[1] - lv.entry == pytest.approx(2 * lv.risk)


def test_niveaux_incoherents_refuses():
    with pytest.raises(ValueError):
        P.default_levels(bars([100.0] * 30), "long", entry=100, stop=101)
    with pytest.raises(ValueError):
        P.default_levels(bars([100.0] * 30), "short", entry=100, stop=102, targets=[105])


def test_delais_sur_tendance_reguliere():
    # +1 par bougie, amplitude ±0,5 : le TP à +2 % est atteint, le stop à -3 % jamais.
    candles = bars([100.0 + i for i in range(100)], spread=0.5)
    lv = P.Levels("long", 100.0, 97.0, [102.0])
    timing = P.historical_timing(candles, lv, horizon_bars=10)
    assert timing["stop_first_rate"] == 0
    assert timing["targets"][0]["hit_rate"] == 1
    assert 1 <= timing["targets"][0]["bars_median"] <= 3


def test_bougie_qui_touche_tp_et_stop_compte_comme_stop():
    candles = bars([100.0] * 60, spread=5.0)
    lv = P.Levels("long", 100.0, 98.0, [102.0])
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
                      "eta_text": "~1 h"} for i, p in enumerate([102.0, 104.0, 106.0], start=1)]}
    book = S.Book(None)
    return book.add(t | over)


NOW = datetime(2026, 10, 6, 3, 0, tzinfo=timezone.utc)
T0 = int(datetime(2026, 10, 6, tzinfo=timezone.utc).timestamp())


def test_tp1_puis_stop_remonte_a_l_entree():
    tr = trade()
    msgs = S.apply_bars(tr, bars([101.0, 102.5], spread=0.5, start=T0, step=60), NOW)
    assert len(msgs) == 1 and "TP1 touché" in msgs[0]
    assert tr["stop_current"] == 100.0 and tr["status"] == "open"
    # La même bougie relue au passage suivant ne renvoie pas d'alerte en double.
    assert S.apply_bars(tr, bars([102.5], spread=0.5, start=T0 + 60, step=60), NOW) == []
    msgs = S.apply_bars(tr, bars([99.0], spread=0.5, start=T0 + 120, step=60), NOW)
    assert "stop suivi" in msgs[0] and tr["status"] == "closed"


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
