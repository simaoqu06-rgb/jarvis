"""Mesure Polymarket Fed (polyfed.py) et source Polymarket (polymarket.py) : issues, cote à 9 h 45, groupes par séance,
collecte, texte."""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.core.http import SourceError
from app.lab import polyfed as P
from app.lab import worker as W
from app.lab.db import LabDB
from app.notify.discord import split_message
from app.sources import polymarket as pm

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 25, 2, 0, tzinfo=timezone.utc)


def ny(y, m, d, hh, mm) -> int:
    return int(datetime(y, m, d, hh, mm, tzinfo=NY).timestamp())


# --- Source Polymarket ----------------------------------------------------------------------

def test_issues_en_points_de_base():
    assert pm.bps("25 bps decrease") == -25
    assert pm.bps("50+ bps decrease") == -50
    assert pm.bps("25+ bps increase") == 25
    assert pm.bps("No change") == 0
    assert pm.bps("Fed decreases interest rates by 25 bps after March 2025 meeting?") is None
    assert pm.bps("autre chose") is None


def test_titre_de_l_evenement():
    assert pm.meeting_month("Fed decision in December?") == (12, None)
    assert pm.meeting_month("Fed Decision in March 2025?") == (3, 2025)
    assert pm.meeting_month("Fed rate cut in 2025?") is None
    assert P.match_meeting("Fed decision in December?", "2024-12-18T00:00:00Z") == date(2024, 12, 18)
    assert P.match_meeting("Fed decision in October 2026?", None) == date(2026, 10, 28)
    assert P.match_meeting("Fed decision in August?", "2025-08-01") is None  # pas de réunion en août


def test_issues_d_un_evenement():
    event = {"markets": [
        {"groupItemTitle": "No change", "clobTokenIds": '["y0", "n0"]', "outcomes": '["Yes", "No"]'},
        {"groupItemTitle": "25 bps decrease", "clobTokenIds": ["y1", "n1"], "outcomes": ["Yes", "No"]},
        {"groupItemTitle": "Autre", "clobTokenIds": '["y2", "n2"]'},
        {"groupItemTitle": "50+ bps decrease", "clobTokenIds": '["n3", "y3"]', "outcomes": '["No", "Yes"]'},
    ]}
    assert [(o["token"], o["bps"]) for o in pm.outcomes(event)] == [("y0", 0), ("y1", -25)]


def test_historique():
    data = {"history": [{"t": 20, "p": 0.5}, {"t": 10, "p": "0.4"}, {"t": 30, "p": 1.7}, {"x": 1}]}
    assert pm.parse_history(data) == [(10, 0.4), (20, 0.5)]
    with pytest.raises(SourceError):
        pm.parse_history([])


# --- Fonctions pures ------------------------------------------------------------------------

def test_prochaine_reunion():
    assert P.next_meeting(date(2026, 10, 28)) == date(2026, 10, 28)  # décision à 14 h : encore à venir à 9 h 45
    assert P.next_meeting(date(2026, 10, 29)) == date(2026, 12, 9)
    assert P.previous_meeting(date(2026, 10, 28)) == date(2026, 9, 16)
    assert P.previous_meeting(P.MEETINGS[0]) is None


def test_cote_jamais_apres_et_pas_trop_vieille():
    s = [(100, 0.2), (200, 0.3), (300, 0.4)]
    assert P.price_at(s, 250) == 0.3
    assert P.price_at(s, 99) is None
    assert P.price_at(s, 300 + P.STALE_S + 1) is None


def test_decision_attendue():
    outs = [(0, [(100, 0.6)]), (-25, [(100, 0.4)])]
    assert P.expected_bps(outs, 150) == pytest.approx(-10.0)
    assert P.expected_bps([(0, [(100, 0.5)])], 150) is None  # issues manquantes : ne somme pas à 1


def test_groupes():
    assert P.group(None) == "sans_cote"
    assert P.group(-2.0) == "colombe" and P.group(2.0) == "faucon" and P.group(1.9) == "neutre"


def test_fenetre_de_la_seance():
    lo, hi = P.day_window(date(2026, 10, 19))
    assert lo == ny(2026, 10, 16, 16, 0) and hi == ny(2026, 10, 19, 9, 45)
    assert P.day_window(date(2026, 10, 18)) is None


def test_welch_et_jours_requis():
    w = P.welch([1.0, 1.2, 0.8], [0.0, 0.2, -0.2])
    assert w["ecart"] == pytest.approx(1.0) and w["z"] > 5 and w["jours"] == 3
    assert P.welch([1.0], [0.0])["z"] is None
    assert P.required_days(1.0, 1.96) == 1568
    assert P.required_days(None, 1.96) is None


# --- Mesure sur une base ----------------------------------------------------------------------

def _market(db, token, meeting, value):
    db.conn.execute("INSERT INTO pm_markets (token, meeting, bps, status) VALUES (?, ?, ?, 'ok')",
                    (token, meeting, value))


def _price(db, token, ts, p):
    db.conn.execute("INSERT INTO pm_prices (token, ts, p) VALUES (?, ?, ?)", (token, ts, p))


def _trade(db, symbol, day, gross, strategy="orb15", exit_reason="tp1", temoin=0.0):
    db.conn.execute("INSERT INTO strat_trades (origin, market, symbol, day, strategy, slot, triggered, r_gross, r_20e, "
                    "temoin_r, exit_reason) VALUES ('live', 'stock', ?, ?, ?, 0, 1, ?, ?, ?, ?)",
                    (symbol, day, strategy, gross, gross - 0.2, temoin, exit_reason))


@pytest.fixture
def db():
    db = LabDB(":memory:")
    meeting = "2026-10-28"
    _market(db, "nc", meeting, 0)
    _market(db, "c25", meeting, -25)
    # Mardi 20 : la baisse passe de 40 % à 60 % pendant la nuit (−5 pb, colombe).
    _price(db, "nc", ny(2026, 10, 19, 15, 0), 0.6)
    _price(db, "c25", ny(2026, 10, 19, 15, 0), 0.4)
    _price(db, "nc", ny(2026, 10, 20, 9, 0), 0.4)
    _price(db, "c25", ny(2026, 10, 20, 9, 0), 0.6)
    # Mercredi 21 : rien ne bouge (neutre) ; une cote APRÈS 9 h 45 ne doit pas compter.
    _price(db, "nc", ny(2026, 10, 21, 9, 0), 0.4)
    _price(db, "c25", ny(2026, 10, 21, 9, 0), 0.6)
    _price(db, "nc", ny(2026, 10, 21, 10, 0), 0.9)
    _price(db, "c25", ny(2026, 10, 21, 10, 0), 0.1)
    _trade(db, "AAA", "2026-10-20", 1.0, temoin=0.5)
    _trade(db, "BBB", "2026-10-20", -1.0, exit_reason="stop", temoin=-0.5)
    _trade(db, "AAA", "2026-10-21", 0.5)
    _trade(db, "CCC", "2026-10-26", 2.0)   # aucune cote ce jour-là (trop vieille) : sans_cote
    return db


def test_variable_et_groupes(db):
    f = P.features(db.conn, ["2026-10-20", "2026-10-21", "2026-10-26"])
    assert f["2026-10-20"]["repricing_pb"] == pytest.approx(-5.0) and f["2026-10-20"]["groupe"] == "colombe"
    assert f["2026-10-21"]["repricing_pb"] == pytest.approx(0.0) and f["2026-10-21"]["groupe"] == "neutre"
    assert f["2026-10-26"]["groupe"] == "sans_cote"
    assert f["2026-10-20"]["reunion"] == "2026-10-28"


def test_rapport(db):
    data = P.report(db.conn)
    t = data["tradable"]["orb15"]
    assert t["par_groupe"]["colombe"] == {"jours": 1, "trades": 2, "r_brut": 0.0, "r_20e": -0.2, "temoin_r": 0.0,
                                          "part_stop": 0.5, "r_brut_par_jour": 0.0, "r_moins_temoin_par_jour": 0.0}
    assert t["par_groupe"]["neutre"]["jours"] == 1 and t["par_groupe"]["faucon"]["jours"] == 0
    assert t["sans_cote"] == {"jours": 1, "trades": 1}
    assert t["ecarts_par_jour"]["colombe_moins_neutre"]["r_brut"]["z"] is None  # une séance par groupe
    cov = data["couverture"]
    assert cov["reunions"]["2026-10-28"]["issues"] == 2 and cov["reunions_sans_marche"] == []
    assert cov["seances"] == {"colombe": 1, "neutre": 1, "sans_cote": 1}


def test_step_met_en_cache_puis_attend(db):
    assert P.step(db, NOW, 3600) == 8
    assert P.step(db, NOW, 3600) is None
    assert P.cached(db.conn)["seuil_pb"] == P.SEUIL_BPS


def test_texte_discord(db):
    assert "pas encore calculée" in P.text(None)
    P.step(db, NOW, 3600)
    txt = P.text(P.cached(db.conn))
    assert "Mesure Polymarket Fed" in txt and "orb15" in txt and "colombe" in txt and "sans essai" in txt
    assert all(len(part) <= 2000 for part in split_message(txt))
    empty = LabDB(":memory:")
    _trade(empty, "AAA", "2026-10-20", 1.0)
    assert "Pas encore de cote" in P.text(P.report(empty.conn))


# --- Collecte ---------------------------------------------------------------------------------

@pytest.fixture
def fake_pm(monkeypatch):
    calls = []

    async def events(params):
        calls.append(("events", tuple(sorted(params.items()))))
        if params.get("tag_slug") == "fed-rates" and params.get("closed") == "false":
            return [{"title": "Fed decision in October?", "slug": "fed-decision-in-october", "endDate": "2026-10-28",
                     "startDate": "2026-09-17T00:00:00Z",
                     "markets": [{"groupItemTitle": "No change", "clobTokenIds": '["nc", "x"]'},
                                 {"groupItemTitle": "25 bps decrease", "clobTokenIds": '["c25", "y"]'}]},
                    {"title": "Fed decision in July?", "slug": "fed-decision-in-july", "endDate": "2020-07-29",
                     "markets": []}]
        if params.get("tag_slug") == "fed":
            raise SourceError("HTTP 404")
        return []

    async def history(token, start, end):
        calls.append(("history", token, start, end))
        if token == "c25" and len([c for c in calls if c[0] == "history" and c[1] == "c25"]) == 2:
            raise SourceError("HTTP 404")
        return [(start + 60, 0.5)]

    monkeypatch.setattr(P, "PM_LIMIT", None)
    monkeypatch.setattr(pm, "events", events)
    monkeypatch.setattr(pm, "history", history)
    return calls


async def test_collecte(fake_pm):
    db = LabDB(":memory:")
    _trade(db, "AAA", "2026-10-20", 1.0)
    rep = await P.fetch(db, NOW, 100)
    assert rep["decouverte"]["evenements"] == 1 and rep["decouverte"]["marches"] == 2
    assert rep["decouverte"]["erreurs"] == 2  # étiquette « fed » absente : notée, la découverte continue
    assert db.get("polyfed", "discovery_ts") == NOW.timestamp()
    rows = db.conn.execute("SELECT token, meeting, bps, start_ts FROM pm_markets ORDER BY token").fetchall()
    assert rows == [("c25", "2026-10-28", -25, ny(2026, 9, 16, 20, 0)),
                    ("nc", "2026-10-28", 0, ny(2026, 9, 16, 20, 0))]
    hist = [c for c in fake_pm if c[0] == "history"]
    # Cotes depuis la création du marché (après la veille de la réunion du 16/09), par tranches de 7 jours, jusqu'à
    # maintenant (la réunion est à venir).
    assert hist[0][2] == ny(2026, 9, 16, 20, 0) and all(c[3] - c[2] <= pm.CHUNK_S for c in hist)
    assert max(c[3] for c in hist) == int(NOW.timestamp())
    assert db.conn.execute("SELECT status, attempts FROM pm_markets WHERE token = 'c25'").fetchone() == ("erreur", 1)
    nc = db.conn.execute("SELECT covered_to_ts FROM pm_markets WHERE token = 'nc'").fetchone()[0]
    assert nc == int(NOW.timestamp()) - 3600  # dernière heure relue au passage suivant
    assert rep["erreurs"] == 1


async def test_collecte_budget(fake_pm):
    db = LabDB(":memory:")
    _trade(db, "AAA", "2026-10-20", 1.0)
    assert (await P.fetch(db, NOW, 0))["appels"] == 0
    rep = await P.fetch(db, NOW, 3)
    assert rep["appels"] == 3 and not fake_pm[-1][0] == "history"
    assert db.get("polyfed", "discovery_ts") is None  # découverte inachevée : reprise au passage suivant


def test_budget_du_worker():
    assert W.LAB2_BUDGET["seance"]["pm_calls"] == 0
    assert all("pm_calls" in b for b in W.LAB2_BUDGET.values())
