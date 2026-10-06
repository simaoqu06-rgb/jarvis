"""Calendrier NYSE calculé : fériés, rattrapages, séances courtes, minutes de cotation."""
from datetime import date, datetime, timezone

from app.trading import calendar as cal

NY = cal.NEW_YORK


def test_feries_2025():
    assert set(cal.holidays(2025)) == {
        date(2025, 1, 1), date(2025, 1, 9), date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18),
        date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4), date(2025, 9, 1), date(2025, 11, 27),
        date(2025, 12, 25),
    }
    assert cal.early_closes(2025) == {date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24)}


def test_feries_2026():
    assert set(cal.holidays(2026)) == {
        date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
        date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25),
    }
    # 4 juillet un samedi : chômé le vendredi 3, pas de séance courte le 2
    assert cal.early_closes(2026) == {date(2026, 11, 27), date(2026, 12, 24)}


def test_feries_2027():
    assert set(cal.holidays(2027)) == {
        date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26), date(2027, 5, 31),
        date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6), date(2027, 11, 25), date(2027, 12, 24),
    }
    # Noël un samedi : chômé le vendredi 24, donc pas de séance courte ce jour-là
    assert cal.early_closes(2027) == {date(2027, 11, 26)}


def test_nouvel_an_un_samedi_pas_rattrape():
    assert date(2021, 12, 31) not in cal.holidays(2021)
    assert date(2022, 1, 1) not in cal.holidays(2022) and cal.is_trading_day(date(2021, 12, 31))
    assert date(2023, 1, 2) in cal.holidays(2023)  # dimanche → lundi


def test_bornes_de_seance():
    assert cal.session_bounds(date(2026, 12, 25)) is None
    assert cal.session_bounds(date(2026, 10, 10)) is None  # samedi
    open_at, close_at = cal.session_bounds(date(2026, 11, 27))
    assert (open_at.hour, open_at.minute, close_at.hour) == (9, 30, 13)
    assert cal.session_bounds(date(2026, 10, 6))[1].hour == 16


def test_seance_ouverte():
    assert cal.session_open(datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc))  # 10 h à New York
    assert not cal.session_open(datetime(2026, 10, 6, 13, 0, tzinfo=timezone.utc))  # 9 h
    assert not cal.session_open(datetime(2026, 12, 25, 16, 0, tzinfo=timezone.utc))  # Noël
    assert not cal.session_open(datetime(2026, 11, 27, 18, 30, tzinfo=timezone.utc))  # 13 h 30, séance courte
    assert cal.session_open(datetime(2026, 11, 27, 17, 30, tzinfo=timezone.utc))


def test_minutes_de_cotation():
    start = datetime(2026, 11, 25, 15, 30, tzinfo=NY)  # veille de Thanksgiving
    eta = cal.add_market_minutes(start, 60)
    assert eta == datetime(2026, 11, 27, 10, 0, tzinfo=NY)  # 30 min + Thanksgiving fermé + 30 min
    assert cal.market_minutes_between(start, eta) == 60
    # séance courte : 210 minutes, puis le lundi
    assert cal.add_market_minutes(datetime(2026, 11, 27, 9, 30, tzinfo=NY), 211) == datetime(2026, 11, 30, 9, 31, tzinfo=NY)


def test_nieme_cloture():
    friday = datetime(2026, 11, 27, 8, 0, tzinfo=NY)
    assert cal.nth_session_close(friday, 1) == datetime(2026, 11, 27, 13, 0, tzinfo=NY)
    assert cal.nth_session_close(friday, 2) == datetime(2026, 11, 30, 16, 0, tzinfo=NY)
    assert cal.previous_session(friday)[1] == datetime(2026, 11, 25, 16, 0, tzinfo=NY)
