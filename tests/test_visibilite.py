"""Audit anti-fuite : délai de visibilité des actus Finnhub mesuré en direct (vue à 9 h 46 New York, relue à 11 h)."""
import json
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.lab import archive as ARC
from app.lab import status as STATUS
from app.trading import killmode as K
from app.trading import plus10 as PLUS10
from app.trading import visibilite as VIS

NY = ZoneInfo("America/New_York")
DAY = date(2026, 9, 15)  # mardi
CUT = VIS.cutoff_ts(DAY)


def at(h, m, day=DAY):
    return datetime(day.year, day.month, day.day, h, m, tzinfo=NY).astimezone(timezone.utc)


def item(ts, title):
    return {"published_ts": ts, "title": title, "url": f"https://x/{title}"}


@pytest.fixture
def live(monkeypatch, tmp_path):
    monkeypatch.setattr(VIS, "JOURNAL_PATH", str(tmp_path / "finnhub_visibilite.jsonl"))
    monkeypatch.setattr(VIS, "STATE_PATH", str(tmp_path / "finnhub_visibilite_state.json"))
    monkeypatch.setattr(VIS.alpaca, "configured", lambda: True)
    monkeypatch.setenv("FINNHUB_API_KEY", "cle-de-test")
    pop = {"UPA": {"gap_pct": 12.0, "atr_pct": 6.0}, "UPB": {"gap_pct": 5.0, "atr_pct": 4.5},
           "CALM": {"gap_pct": 4.5, "atr_pct": 2.0}, "DOWN": {"gap_pct": -9.0, "atr_pct": 8.0},
           "ERR": {"gap_pct": 7.0, "atr_pct": 9.0}}
    feed = {"UPA": [item(CUT - 3600, "contrat"), item(CUT + 600, "apres")], "UPB": [], "ERR": []}

    async def lists(now, at_open, extra=None):
        assert at_open
        return {}, pop, {}

    async def news(symbol, start, end):
        if symbol == "ERR":
            raise RuntimeError("quota")
        return list(feed[symbol])

    monkeypatch.setattr(PLUS10, "lists", lists)
    monkeypatch.setattr(VIS.finnhub, "company_news_between", news)
    return feed


def test_reglages_alignes_sur_l_archive_et_killmode():
    assert VIS.DECISION == ARC.DECISION and VIS.CATALYST_HOURS == K.CATALYST_HOURS
    assert VIS.cutoff_ts(DAY) == ARC.cutoff_ts(DAY)


def test_gappers_de_la_regle_killmode():
    pop = {f"S{i}": {"gap_pct": 4 + i, "atr_pct": 5.0} for i in range(30)} | {"LOW": {"gap_pct": 9.0, "atr_pct": None}}
    out = VIS.gappers(pop)
    assert len(out) == VIS.MAX_SYMBOLS and out[0] == "S29" and "LOW" not in out


def test_actu_en_retard_et_catalyseur_gagne():
    seen = {"A": [[CUT - 7200, "k1"]], "B": [], "C": [[CUT - 100, "k3"]]}
    later = {"A": [[CUT - 7200, "k1"], [CUT - 60, "k2|Contrat signé"]], "B": [[CUT - 300, "k4|Rachat"]],
             "C": [[CUT - 100, "k3"], [CUT + 60, "k5|Après la coupure"]]}
    out = VIS.compare(seen, later, DAY)
    assert out["valeurs"] == 3 and out["actus_avant_945"] == 4
    assert [(x["symbol"], x["titre"]) for x in out["en_retard"]] == [("A", "Contrat signé"), ("B", "Rachat")]
    assert out["catalyseur_gagne"] == ["B"]  # A avait déjà une actu dans les 48 h : son catalyseur ne change pas


def test_passages_du_jour(live):
    assert VIS.due(at(9, 45)) is None
    assert VIS.due(at(11, 0)) is None  # la relecture attend la vue de 9 h 46
    assert VIS.due(at(9, 46)) == "vue"


async def test_vue_puis_relue_journal_et_etat_du_labo(live, tmp_path, monkeypatch):
    await VIS.run("vue", at(9, 46))
    state = json.loads((tmp_path / "finnhub_visibilite_state.json").read_text())
    assert state["gappers"] == ["UPA", "ERR", "UPB"] and set(state["actus"]) == {"UPA", "UPB"}  # ERR en erreur
    assert VIS.due(at(9, 50)) is None and VIS.due(at(11, 1)) == "relue"
    live["UPB"].append(item(CUT - 120, "mise en ligne tard"))
    entry = await VIS.run("relue", at(11, 1))
    assert entry["valeurs"] == 2 and entry["actus_avant_945"] == 2 and entry["gappers"] == 3
    assert [x["symbol"] for x in entry["en_retard"]] == ["UPB"] and entry["catalyseur_gagne"] == ["UPB"]
    assert VIS.due(at(11, 5)) is None
    v = VIS.summary()
    assert v == {"seances": 1, "valeurs": 2, "actus_avant_945": 2, "en_retard": 1, "part_en_retard": 0.5,
                 "catalyseur_gagne": 1, "part_catalyseur_gagne": 0.5}
    assert "1 arrivées après 9 h 46 (50,0 %)" in VIS.line(v)
    monkeypatch.delenv("JARVIS_FINNHUB_VISIBILITE_PATH", raising=False)
    assert VIS.journal_path(str(tmp_path / "lab.db")) == tmp_path / "finnhub_visibilite.jsonl"


async def test_relue_sans_vue_ne_journalise_rien(live, tmp_path):
    VIS.mark_done("vue", at(9, 46))  # vue en échec
    assert await VIS.run("relue", at(11, 0)) is None and VIS.summary() is None


async def test_tick_du_scheduler_en_echec_ne_reessaie_pas(live, monkeypatch):
    from app import scheduler as SCH

    calls = []

    async def boom(kind, now):
        calls.append(kind)
        raise RuntimeError("Alpaca en panne")

    monkeypatch.setattr(VIS, "run", boom)
    await SCH.tick_visibilite(at(9, 46))
    await SCH.tick_visibilite(at(9, 47))
    assert calls == ["vue"]


def test_limites_du_labo_nomment_les_trois_points():
    text = " ".join(STATUS.LIMITATIONS)
    assert "Disponibilité historique des actus non vérifiée" in text
    assert "limite non vérifiable" in text and "minimum de 5 $" in text
