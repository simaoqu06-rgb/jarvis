"""Confirmation d'entrée et plancher du stop en direct (demande de Simão, 2026-10-08, FRSH)."""
from datetime import datetime, timezone

from app.trading import plan as P
from app.trading import service as S


def bars(closes, spread=1.0, start=0, step=3600):
    return [{"t": start + i * step, "o": c, "h": c + spread, "l": c - spread, "c": c, "v": 1000.0}
            for i, c in enumerate(closes)]


def plan(side="long", market="stock", at="2026-10-08T13:19:00+00:00"):
    return {"side": side, "market": market, "entry": 13.59, "stop": 13.5036, "created_at": at,
            "targets": [{"price": 13.718}, {"price": 13.8747}]}


def test_confirmation_long_avant_l_ouverture():
    lines = S.confirmation_lines(plan())
    assert lines[0].startswith("⏱️ Pas d'entrée avant aujourd'hui 15:45")  # 9 h 45 New York = 15 h 45 Paris
    assert "repli vers 13.59" in lines[1] and "sous 13.5036" in lines[1] and "VWAP" in lines[1]
    assert "TP1 13.718 touché avant l'entrée" in lines[2]


def test_confirmation_pendant_la_seance_et_short():
    lines = S.confirmation_lines(plan(side="short", at="2026-10-08T15:00:00+00:00"))
    assert not lines[0].startswith("⏱️")
    assert "sous le VWAP" in lines[0] and "au-dessus de 13.5036" in lines[1]


def test_confirmation_crypto_sans_horaire():
    lines = S.confirmation_lines(plan(market="crypto"))
    assert len(lines) == 2 and "15 min" in lines[0]


def test_plancher_du_stop_a_un_tiers_de_l_atr_journalier():
    intraday = bars([100 + (i % 3) * 0.1 for i in range(200)], spread=0.05, step=900)
    daily = bars([100] * 30, spread=3.0, step=86400)  # ATR journalier 6
    now = datetime(2026, 10, 8, 13, 0, tzinfo=timezone.utc)
    levels = S.plan_levels(intraday, "stock", "long", 100.0, None, None, now)
    assert levels[0].risk < 2
    floored = S._floor_stop(intraday, daily, "stock", "long", 100.0, None, now, levels)
    assert floored[0].stop == P.round_price(98.0)
    assert "1/3 de l'ATR journalier" in floored[0].stop_source
    assert all(t.price > 100 for t in floored[0].targets)


def test_plancher_sans_effet_si_stop_deja_large_ou_sans_journalier():
    intraday = bars([100 + (i % 3) * 0.1 for i in range(200)], spread=0.05, step=900)
    now = datetime(2026, 10, 8, 13, 0, tzinfo=timezone.utc)
    levels = S.plan_levels(intraday, "stock", "long", 100.0, None, None, now)
    assert S._floor_stop(intraday, None, "stock", "long", 100.0, None, now, levels) is levels
    small = bars([100] * 30, spread=0.01, step=86400)
    assert S._floor_stop(intraday, small, "stock", "long", 100.0, None, now, levels) is levels
