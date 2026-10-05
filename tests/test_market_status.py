"""Tests hors ligne de l'ouverture de la Bourse de New York."""
from datetime import datetime, timezone

import pytest

from app import needs
from app.core.result import Result

HOLIDAYS = [
    {"date": "2026-04-03", "name": "Good Friday (closed)", "closed": True, "early_close": False},
    {"date": "2026-11-27", "name": "Black Friday — Early Close 1pm ET", "closed": False, "early_close": True},
]


@pytest.fixture(autouse=True)
def fake_calendar(monkeypatch):
    async def calendar(year=None):
        return Result("us_market_holidays", HOLIDAYS, "test", None, "2026-01-01T00:00:00+00:00")

    monkeypatch.setattr(needs, "us_market_holidays", calendar)


@pytest.mark.parametrize(
    "utc, is_open",
    [
        ("2026-10-05T14:00:00", True),   # lundi 10 h à New York
        ("2026-10-05T13:00:00", False),  # lundi 9 h, avant l'ouverture
        ("2026-10-05T20:00:00", False),  # lundi 16 h, clôture
        ("2026-10-03T15:00:00", False),  # samedi
        ("2026-04-03T15:00:00", False),  # Vendredi saint
        ("2026-11-27T17:30:00", True),   # Black Friday 12 h 30
        ("2026-11-27T18:30:00", False),  # Black Friday 13 h 30, fermeture anticipée
    ],
)
async def test_ouverture(utc, is_open):
    now = datetime.fromisoformat(utc).replace(tzinfo=timezone.utc)
    assert (await needs.us_market_status(now))["is_open"] is is_open
