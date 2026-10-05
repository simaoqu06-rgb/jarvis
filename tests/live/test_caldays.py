from datetime import date

from app.sources import caldays
from tests.live.conftest import HOLIDAY, check


async def test_caldays_france_annee_en_cours():
    check(await caldays.holidays("FR", date.today().year), HOLIDAY, required=("date", "name"))
