"""Veille des routines Claude (audits de GPT et Gemini, 25/10/2026) : une routine qui ne dépose rien se voit le jour même."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import alerts
from app import scheduler as SCH
from app.lab import routines_watch as RW
from app.trading import research as R

PARIS = ZoneInfo("Europe/Paris")
NY = ZoneInfo("America/New_York")


def test_soirée_attendue_du_mardi_au_samedi():
    assert RW.expected_soiree(datetime(2026, 10, 27, 9, 5, tzinfo=PARIS)) == "2026-10-26"   # mardi : lundi soir
    assert RW.expected_soiree(datetime(2026, 10, 31, 9, 5, tzinfo=PARIS)) == "2026-10-30"   # samedi : vendredi
    assert RW.expected_soiree(datetime(2026, 10, 27, 8, 55, tzinfo=PARIS)) is None          # avant l'heure
    assert RW.expected_soiree(datetime(2026, 11, 1, 10, 0, tzinfo=PARIS)) is None           # dimanche
    assert RW.expected_soiree(datetime(2026, 10, 26, 10, 0, tzinfo=PARIS)) is None          # lundi


def test_alerte_du_soir_une_seule_fois():
    now = datetime(2026, 10, 28, 9, 30, tzinfo=PARIS)  # mercredi : la soirée du mardi 27 est attendue
    assert RW.soir_alert(now, "2026-10-27", None) is None
    key, text = RW.soir_alert(now, "2026-10-26", None)
    assert key == "soir:2026-10-27" and "soirée du 27/10 (dernier : 26/10)" in text and "résolu" in text
    assert RW.soir_alert(now, "2026-10-26", key) is None


def test_alerte_de_la_recherche():
    now = datetime(2026, 10, 27, 10, 45, tzinfo=NY)
    key, text = RW.recherche_alert(now, "2026-10-27", 4, 0, None)
    assert key == "recherche:2026-10-27" and "aucun verdict sur les 4 valeurs" in text
    assert RW.recherche_alert(now, "2026-10-27", 4, 0, key) is None                      # déjà envoyée
    assert RW.recherche_alert(now, "2026-10-27", 4, 2, None) is None                     # verdicts reçus
    assert RW.recherche_alert(now, "2026-10-27", 0, 0, None) is None                     # rien à chercher
    assert RW.recherche_alert(now, "2026-10-26", 4, 0, None) is None                     # pas de scan du jour
    assert RW.recherche_alert(datetime(2026, 10, 27, 10, 15, tzinfo=NY), "2026-10-27", 4, 0, None) is None


@pytest.fixture
def sched(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(SCH, "state", alerts.State(str(tmp_path / "state.json")))
    monkeypatch.setattr(SCH, "_queue", lambda texts, kind="", **kw: sent.extend(texts))
    monkeypatch.setattr(SCH, "_kick_outbox", lambda: None)
    scans = tmp_path / "scans.jsonl"
    monkeypatch.setattr(R.scanner, "SCANS_PATH", str(scans))
    monkeypatch.setattr(R, "RESEARCH_PATH", str(tmp_path / "research.json"))
    monkeypatch.setattr(R.ResearchBook.__init__, "__defaults__", (str(tmp_path / "research.json"),))
    return sent, scans


async def test_tick_routines(sched):
    sent, scans = sched
    # Mise en service un mercredi : la soirée de mardi est supposée vue, pas de fausse alerte.
    await SCH.tick_routines(datetime(2026, 10, 28, 9, 30, tzinfo=PARIS))
    assert sent == [] and SCH.state.data["routines"]["soir"] == "2026-10-27"
    # Le mercredi soir, aucun avis : alerte le jeudi matin, une seule fois.
    await SCH.tick_routines(datetime(2026, 10, 29, 9, 30, tzinfo=PARIS))
    await SCH.tick_routines(datetime(2026, 10, 29, 9, 35, tzinfo=PARIS))
    assert len(sent) == 1 and "soirée du 28/10" in sent[0]
    # L'avis du jeudi soir arrive (POST /api/lab/grade/avis) : rien le vendredi.
    SCH.routine_seen("soir", "2026-10-29")
    SCH.routine_seen("soir", "2026-10-20")  # jamais en arrière
    assert SCH.state.data["routines"]["soir"] == "2026-10-29"
    # Scan du vendredi avec deux valeurs, aucun verdict à 10 h 30 New York : alerte de la recherche.
    scans.write_text(json.dumps({"at": "2026-10-30T13:10:00+00:00", "picks": [{"symbol": "ABC"}, {"symbol": "XYZ"}]})
                     + "\n", encoding="utf-8")
    await SCH.tick_routines(datetime(2026, 10, 30, 10, 31, tzinfo=NY))
    assert len(sent) == 2 and "aucun verdict sur les 2 valeurs" in sent[1]


async def test_tick_routines_verdicts_recus(sched):
    sent, scans = sched
    SCH.state.data["routines"] = {"soir": "2026-10-29"}
    scans.write_text(json.dumps({"at": "2026-10-30T13:10:00+00:00", "picks": [{"symbol": "ABC"}]}) + "\n",
                     encoding="utf-8")
    book = R.ResearchBook()
    book.items = [{"jour": "2026-10-30", "symbol": "ABC", "verdict": "soutient"}]
    book.save()
    await SCH.tick_routines(datetime(2026, 10, 30, 10, 31, tzinfo=NY))
    assert sent == []
