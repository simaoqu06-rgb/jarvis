"""Chasse aux ratés du potentiel +10 (rates.py) : raisons, taux de base par tranche de gap, bloc scellé, texte Discord."""
import itertools
from datetime import date, datetime, timezone

import pytest

from app.lab import rates as RATES
from app.lab import seal as SEAL
from app.lab.db import LabDB
from app.notify.discord import split_message
from tests.lab_fixtures import calendar_days

NOW = datetime(2026, 10, 18, 20, 0, tzinfo=timezone.utc)
_SEQ = itertools.count(1_000_000)
DAYS = [d.isoformat() for d in calendar_days(date(2026, 9, 1), 25)]


def _db():
    db = LabDB(":memory:")
    db.conn.execute("INSERT INTO method_versions (version, created_at, settings) VALUES "
                    "('v0', '2024-01-01T00:00:00+00:00', '{}'), ('v1', '2025-01-01T00:00:00+00:00', '{}')")
    return db


def _move(db, day, symbol, up, gap, rvol=None):
    db.conn.execute("INSERT INTO day_moves (market, day, symbol, up_pct, down_pct, gap_pct, rvol30) "
                    "VALUES ('stock', ?, ?, ?, -1.0, ?, ?)", (day, symbol, up, gap, rvol))


def _trade(db, day, symbol, triggered, tp1, version="v1", exit_reason=None):
    db.conn.execute("INSERT INTO trades (origin, method_version, market, symbol, side, day, planned_at, triggered, "
                    "tp1_hit, exit_reason, r_net) VALUES ('replay', ?, 'stock', ?, 'long', ?, ?, ?, ?, ?, ?)",
                    (version, symbol, day, next(_SEQ), triggered, tp1,
                     exit_reason, 0.5 if tp1 else -1.0))


def _jeu(db):
    """Dernière séance : quatre +10 (un par raison) et une séance calme ; plus haut, des séances de remplissage."""
    last = DAYS[-1]
    _move(db, last, "AAA", 25.0, 12.0, 3.4)                     # pas de plan, gap ≥ 10 %
    _move(db, last, "BBB", 15.0, 3.0)                           # plan non déclenché
    _trade(db, last, "BBB", 0, 0)
    _move(db, last, "CCC", 12.0, 1.0)                           # déclenché, stop avant TP1
    _trade(db, last, "CCC", 1, 0, exit_reason="stop")
    _move(db, last, "DDD", 11.0, 6.0)                           # TP1 touché (v1) ; un vieux plan v0 sans TP1 ignoré
    _trade(db, last, "DDD", 1, 1, exit_reason="tp3")
    _trade(db, last, "DDD", 1, 0, version="v0", exit_reason="stop")
    _move(db, last, "EEE", 4.0, 0.5)                            # pas un mouvement
    _trade(db, DAYS[-2], "AAA", 1, 1)                           # TP1 un autre jour : ne compte pas pour AAA
    db.conn.execute("INSERT INTO catalysts (market, day, symbol, kind, news_status, catalyst_type, created_at) "
                    "VALUES ('stock', ?, 'AAA', 'mouvement', 'ok', 'resultats', '2026-10-01')", (last,))
    db.conn.execute("INSERT INTO plus10_trades (day, symbol, essai, liste, rang, triggered) "
                    "VALUES (?, 'AAA', 'plus10_orb15', 's10', 2, 0), (?, 'AAA', 'plus10_kill', 's10', 2, 0)",
                    (last, last))


def test_raisons_une_par_mouvement_et_detail_de_la_derniere_seance():
    db = _db()
    _jeu(db)
    rep = RATES.report(db.conn)
    assert rep["version"] == "v1" and rep["mouvements"] == 4 and rep["au"] == DAYS[-1]
    assert rep["par_categorie"] == {"pas_de_plan": 1, "non_declenche": 1, "stop_avant_tp1": 1, "tp1": 1}
    assert rep["taux_capture"] == 0.25
    det = {m["symbole"]: m for m in rep["derniere_seance"]}
    assert [m["symbole"] for m in rep["derniere_seance"]] == ["AAA", "BBB", "CCC", "DDD"]   # hausse décroissante
    assert det["AAA"]["categorie"] == "pas_de_plan" and det["DDD"]["categorie"] == "tp1"
    assert det["CCC"]["sortie"] == "stop"
    a = det["AAA"]
    assert a["avant_ouverture"] == {"gap_pct": 12.0, "tranche_gap": "gap ≥ 10 %", "catalyseur": "resultats"}
    assert a["apres_ouverture"]["rvol30"] == 3.4
    assert a["scanner_plus10"] == {"S+10": 2} and a["scanner_top"]
    assert det["BBB"]["avant_ouverture"]["catalyseur"] == "à classer" and not det["BBB"]["scanner_plus10"]
    assert rep["scanner_plus10"] == {"mouvements_listes": 1, "mouvements_top": 1, "rates_listes": 1, "rates_top": 1}


def test_detail_limite_a_dix_lignes():
    db = _db()
    for k in range(14):
        _move(db, DAYS[-1], f"X{k:02d}", 10.0 + k, 1.0)
    rep = RATES.report(db.conn)
    assert len(rep["derniere_seance"]) == RATES.MAX_DETAIL and rep["derniere_seance"][0]["symbole"] == "X13"


def test_taux_de_base_par_tranche_de_gap_et_tranche_a_regarder():
    db = _db()
    # 20 séances : 40 actions par jour en gap 0-2 % (1 seule à +10 % par jour, avec plan déclenché sans TP1) et
    # 2 actions par jour en gap 5-10 % (une sur deux à +10 %, sans plan).
    for d in DAYS[-20:]:
        for k in range(40):
            _move(db, d, f"C{k:02d}", 12.0 if k == 0 else 2.0, 1.0)
        _trade(db, d, "C00", 1, 0, exit_reason="stop")
        _move(db, d, "G1", 15.0, 7.0)
        _move(db, d, "G2", 3.0, 8.0)
    _move(db, DAYS[0], "VIEUX", 30.0, 7.0)                      # hors des 20 dernières séances
    rep = RATES.report(db.conn)
    assert rep["seances"] == RATES.FENETRE_SEANCES and rep["du"] == DAYS[-20]
    assert rep["mouvements"] == 40 and rep["par_categorie"]["pas_de_plan"] == 20
    tr = rep["sans_plan_par_gap"]
    calme, gap = tr["gap 0-2 %"], tr["gap 5-10 %"]
    assert calme["seances"] == 800 and calme["hausses"] == 20 and calme["taux_base"] == 0.025
    assert calme["rates_sans_plan"] == 0
    assert gap["seances"] == 40 and gap["taux_base"] == 0.5 and gap["rates_sans_plan"] == 20
    assert gap["part_des_rates"] == 1.0 and gap["rates_par_seance"] == 0.5 and not gap["echantillon_faible"]
    assert gap["part_des_seances"] == pytest.approx(40 / 840, abs=1e-4)
    assert rep["a_regarder"]["tranche"] == "gap 5-10 %"
    assert "gap < 0 %" not in tr                                # tranche vide : absente


def test_tranche_trop_petite_jamais_designee():
    db = _db()
    for k in range(5):
        _move(db, DAYS[-1], f"G{k}", 20.0, 7.0)
    rep = RATES.report(db.conn)
    assert rep["sans_plan_par_gap"]["gap 5-10 %"]["echantillon_faible"] and rep["a_regarder"] is None


def test_gap_bucket():
    assert [RATES.gap_bucket(g) for g in (-3, 0, 1.99, 2, 5, 9.9, 10, None)] == \
        ["gap < 0 %", "gap 0-2 %", "gap 0-2 %", "gap 2-5 %", "gap 5-10 %", "gap 5-10 %", "gap ≥ 10 %", "gap inconnu"]


def test_bloc_scelle_exclu(monkeypatch):
    db = _db()
    _jeu(db)
    _move(db, DAYS[-3], "OLD", 40.0, 2.0)
    monkeypatch.setattr(SEAL, "SEALED_FROM", date.fromisoformat(DAYS[-2]))
    monkeypatch.setattr(SEAL, "SEALED_TO", date.fromisoformat(DAYS[-1]))
    rep = RATES.report(db.conn)
    assert rep["au"] == DAYS[-3] and DAYS[-1] not in RATES.window(db.conn)
    assert rep["mouvements"] == 1 and rep["derniere_seance"][0]["symbole"] == "OLD"


def test_step_met_en_cache_et_ne_recalcule_pas_sans_changement():
    db = _db()
    _jeu(db)
    assert RATES.step(db, NOW, 0) == 4
    assert RATES.cached(db.conn)["par_categorie"]["tp1"] == 1
    assert RATES.step(db, NOW, 0) is None
    _move(db, DAYS[-1], "FFF", 30.0, -2.0)
    assert RATES.step(db, NOW, 0) == 5


def test_texte_discord():
    assert "pas encore calculée" in RATES.text(None)
    db = _db()
    assert "pas encore de séance" in RATES.text(RATES.report(db.conn))
    _jeu(db)
    txt = RATES.text(RATES.report(db.conn))
    assert "Chasse aux ratés" in txt and "pas de plan" in txt and "TP1" in txt and "25 %" in txt
    assert "AAA" in txt and "après l'ouverture" in txt and "S+10 rang 2" in txt and "absent du scanner" in txt
    assert 0 < len(txt) < 2000
    db2 = _db()
    for d in DAYS[-20:]:
        for k in range(12):
            _move(db2, d, f"LONGSYMBOL{k:02d}", 10.0 + k, 7.5 * (k % 3) - 1, 2.5)
    big = RATES.text(RATES.report(db2.conn))
    assert all(0 < len(c) <= 2000 for c in split_message(big))
