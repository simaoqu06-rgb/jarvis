"""Calendrier de la Bourse de New York (NYSE), calculé sans réseau.

Jours fériés (règle 7.2 du NYSE) : Nouvel An, Martin Luther King (3e lundi de janvier), Presidents' Day
(3e lundi de février), Vendredi saint, Memorial Day (dernier lundi de mai), Juneteenth (19 juin, depuis
2022), fête de l'Indépendance (4 juillet), Labor Day (1er lundi de septembre), Thanksgiving (4e jeudi de
novembre) et Noël. Un férié tombant un samedi est chômé le vendredi d'avant, un dimanche le lundi
d'après ; exception : le Nouvel An un samedi n'est pas rattrapé (le 31 décembre clôt l'exercice).

Séances courtes (fermeture à 13 h) : veille de l'Indépendance (3 juillet, si c'est un jour de semaine
ouvré), lendemain de Thanksgiving et veille de Noël (24 décembre, si c'est un jour de semaine ouvré).

Les fermetures exceptionnelles (deuil national...) ne se calculent pas : connues dans SPECIAL_CLOSURES,
à compléter au besoin avec JARVIS_NYSE_EXTRA_CLOSED=AAAA-MM-JJ,AAAA-MM-JJ.
"""
import os
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
OPEN, CLOSE, EARLY_CLOSE = time(9, 30), time(16, 0), time(13, 0)
SPECIAL_CLOSURES = {
    date(2025, 1, 9): "Deuil national (Jimmy Carter)",
}


def _extra_closed() -> dict[date, str]:
    out = {}
    for raw in os.getenv("JARVIS_NYSE_EXTRA_CLOSED", "").split(","):
        try:
            out[date.fromisoformat(raw.strip())] = "Fermeture exceptionnelle"
        except ValueError:
            continue
    return out


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-ième `weekday` (0 = lundi) du mois ; n = -1 pour le dernier."""
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = (date(year, month + 1, 1) if month < 12 else date(year + 1, 1, 1)) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def easter(year: int) -> date:
    """Dimanche de Pâques (calendrier grégorien, algorithme de Meeus/Jones/Butcher)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


@lru_cache(maxsize=64)
def _holidays(year: int) -> dict[date, str]:
    out: dict[date, str] = {}
    new_year = date(year, 1, 1)
    if new_year.weekday() != 5:  # samedi : pas de fermeture le 31 décembre précédent
        out[_observed(new_year)] = "Nouvel An"
    out[_nth_weekday(year, 1, 0, 3)] = "Martin Luther King Jr. Day"
    out[_nth_weekday(year, 2, 0, 3)] = "Presidents' Day"
    out[easter(year) - timedelta(days=2)] = "Vendredi saint"
    out[_nth_weekday(year, 5, 0, -1)] = "Memorial Day"
    if year >= 2022:
        out[_observed(date(year, 6, 19))] = "Juneteenth"
    out[_observed(date(year, 7, 4))] = "Fête de l'Indépendance"
    out[_nth_weekday(year, 9, 0, 1)] = "Labor Day"
    out[_nth_weekday(year, 11, 3, 4)] = "Thanksgiving"
    out[_observed(date(year, 12, 25))] = "Noël"
    return out


def holidays(year: int) -> dict[date, str]:
    """Jours de fermeture complète de l'année (fériés et fermetures exceptionnelles connues)."""
    out = dict(_holidays(year))
    out |= {d: n for d, n in (SPECIAL_CLOSURES | _extra_closed()).items() if d.year == year}
    return out


def early_closes(year: int) -> set[date]:
    """Séances qui ferment à 13 h, heure de New York."""
    closed = holidays(year)
    candidates = [date(year, 7, 3), _nth_weekday(year, 11, 3, 4) + timedelta(days=1), date(year, 12, 24)]
    return {d for d in candidates if d.weekday() < 5 and d not in closed}


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in holidays(day.year)


def session_bounds(day: date) -> tuple[datetime, datetime] | None:
    """Ouverture et clôture de la séance régulière du jour (heure de New York), None si fermé."""
    if not is_trading_day(day):
        return None
    close = EARLY_CLOSE if day in early_closes(day.year) else CLOSE
    return datetime.combine(day, OPEN, NEW_YORK), datetime.combine(day, close, NEW_YORK)


def session_open(now: datetime) -> bool:
    """Séance régulière en cours à cet instant (jours fériés et séances courtes compris)."""
    local = now.astimezone(NEW_YORK)
    bounds = session_bounds(local.date())
    return bool(bounds) and bounds[0] <= local < bounds[1]


def current_or_next_session(moment: datetime) -> tuple[datetime, datetime]:
    """Séance en cours à `moment`, sinon la prochaine."""
    local = moment.astimezone(NEW_YORK)
    day = local.date()
    for _ in range(30):
        bounds = session_bounds(day)
        if bounds and local < bounds[1]:
            return bounds
        day += timedelta(days=1)
    raise ValueError("aucune séance NYSE dans les 30 jours")


def previous_session(moment: datetime) -> tuple[datetime, datetime] | None:
    """Dernière séance terminée avant `moment`."""
    local = moment.astimezone(NEW_YORK)
    day = local.date()
    for _ in range(30):
        bounds = session_bounds(day)
        if bounds and bounds[1] <= local:
            return bounds
        day -= timedelta(days=1)
    return None


def nth_session_close(moment: datetime, n: int) -> datetime:
    """Clôture de la n-ième séance à partir de `moment` (1 = la séance en cours ou la prochaine)."""
    _, close = current_or_next_session(moment)
    for _ in range(max(n, 1) - 1):
        _, close = current_or_next_session(close)
    return close


def add_market_minutes(start: datetime, minutes: float) -> datetime:
    """Avance de `minutes` minutes de cotation (séances courtes et fériés compris)."""
    current = start.astimezone(NEW_YORK)
    remaining = timedelta(minutes=minutes)
    for _ in range(2000):
        open_at, close_at = current_or_next_session(current)
        current = max(current, open_at)
        available = close_at - current
        if remaining <= available:
            return current + remaining
        remaining -= available
        current = close_at
    raise ValueError("calendrier de cotation introuvable")


def market_minutes_between(start: datetime, end: datetime) -> float:
    """Minutes de cotation entre deux instants."""
    if end <= start:
        return 0.0
    total, current = timedelta(0), start.astimezone(NEW_YORK)
    end = end.astimezone(NEW_YORK)
    for _ in range(2000):
        open_at, close_at = current_or_next_session(current)
        if open_at >= end:
            break
        total += min(close_at, end) - max(current, open_at)
        if close_at >= end:
            break
        current = close_at
    return total.total_seconds() / 60
