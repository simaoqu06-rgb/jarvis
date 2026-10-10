"""Worker du Labo 2 : étapes isolées, budgets par période, statistiques en cache, résumé quotidien,
indice de référence collecté sans entrer dans l'univers, chiffres de !labo."""
import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.lab import catalysts as CAT
from app.lab import collector as CO
from app.lab import runner as R
from app.lab import stats as STATS
from app.lab import status as ST
from app.lab import universe as U
from app.lab import validator as V
from app.lab import worker as W
from app.lab.db import LabDB
from tests.lab_fixtures import synthetic_trades

PARIS = ZoneInfo("Europe/Paris")
NIGHT = datetime(2026, 10, 6, 23, 0, tzinfo=PARIS)
SESSION = datetime(2026, 10, 6, 17, 0, tzinfo=PARIS)


@pytest.fixture
def db(tmp_path):
    d = LabDB(str(tmp_path / "lab.db"))
    synthetic_trades(d, n_days=30, per_day=5, seed=1, first=date(2026, 8, 3))
    return d


async def test_etapes_isolees(db, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("panne")

    monkeypatch.setattr(CAT, "backfill_moves", boom)
    V.submit(db, {"exclude": {"field": "composite", "op": "<", "value": 5}})
    report = await W.lab2(db, NIGHT, collect=False)
    assert report["mouvements"] == "échec : RuntimeError"
    assert report["regles"] == 1 and report["stats"] == "à jour" and isinstance(report["resume"], int)
    assert "actus" not in report  # collect=False : pas de réseau


async def test_actus_jamais_pendant_la_seance(db, monkeypatch):
    calls = []

    async def fake(db_, now, budget):
        calls.append(budget)
        return {"appels": 0}

    monkeypatch.setattr(CAT, "fetch_news", fake)
    await W.lab2(db, SESSION, collect=True)
    assert calls == []
    await W.lab2(db, NIGHT, collect=True)
    assert calls == [W.LAB2_BUDGET["nuit"]["news_calls"]]


async def test_aucune_regle_validee_pendant_la_seance(db):
    V.submit(db, {"exclude": {"field": "composite", "op": "<", "value": 5}})
    report = await W.lab2(db, SESSION, collect=False)
    assert report["regles"] == 0 and V.counts(db.conn)["en_attente"] == 1


def test_budgets_decroissants():
    b = W.LAB2_BUDGET
    for key in ("moves_days", "news_calls", "rules"):
        assert b["nuit"][key] >= b["jour"][key] >= b["seance"][key]
    assert b["seance"]["news_calls"] == 0 and b["seance"]["rules"] == 0


def test_stats_recalculees_seulement_si_les_trades_changent(db):
    now = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
    assert W.refresh_stats(db, now, 3600) is not None
    assert STATS.cached(db.conn)["trades_base"] == 150
    assert W.refresh_stats(db, now + timedelta(hours=5), 3600) is None  # rien de neuf
    synthetic_trades(db, n_days=2, per_day=5, seed=2, first=date(2026, 9, 14))
    assert W.refresh_stats(db, now + timedelta(minutes=10), 3600) is None  # trop récent
    assert W.refresh_stats(db, now + timedelta(hours=2), 3600) is not None
    assert W.refresh_stats(db, now + timedelta(hours=2), 3600, force=True) is not None
    assert STATS.cached(db.conn)["filtres"] == {"version": "v1"}


async def test_resume_une_fois_par_jour(db):
    await W.lab2(db, NIGHT, collect=False)
    first = db.conn.execute("SELECT day, created_at FROM summaries").fetchall()
    assert [r[0] for r in first] == ["2026-10-06"]
    report = await W.lab2(db, NIGHT + timedelta(minutes=10), collect=False)
    assert "resume" not in report
    early = datetime(2026, 10, 7, 21, 0, tzinfo=PARIS)
    assert "resume" not in await W.lab2(db, early, collect=False)
    assert "resume" in await W.lab2(db, datetime(2026, 10, 7, 22, 40, tzinfo=PARIS), collect=False)


async def test_passage_complet_appelle_le_labo_2(tmp_path, monkeypatch):
    db = LabDB(str(tmp_path / "t.db"))
    seen = []

    async def fake(db_, now, collect=True, hold=False):  # 2e tour d'avis (C1) : hold, archive d'audit en échec
        seen.append(collect)
        return {"ok": True}

    async def nothing(*a, **k):
        return 0

    monkeypatch.setattr(W, "lab2", fake)
    monkeypatch.setattr(R, "replay", nothing)
    report = await W.tick(db, datetime(2026, 10, 6, 21, tzinfo=timezone.utc), collect=False)
    assert report["labo2"] == {"ok": True} and seen == [False]


async def test_indice_collecte_mais_hors_univers(tmp_path, monkeypatch):
    db = LabDB(str(tmp_path / "i.db"))
    asked = {}

    async def symbols(db_, today, transport=None):
        return ["AAA", "BBB"]

    async def ensure(db_, market, syms, first, last, now=None, transport=None, failed_out=None):
        asked[market] = list(syms)
        return 0

    monkeypatch.setattr(CO, "stock_universe_symbols", symbols)
    monkeypatch.setattr(CO, "ensure_daily", ensure)
    await R.ensure_daily_range(db, "stock", date(2026, 9, 15), date(2026, 9, 15))
    assert asked["stock"] == ["AAA", "BBB", "SPY"] and db.get("lab", "index_only:stock") == "SPY"
    day = date(2026, 9, 15)
    for i, sym in enumerate(["AAA", "BBB", "SPY"]):
        rows = [{"t": CO.ny_midnight(d), "o": 10, "h": 10, "l": 10, "c": 10 + i, "v": 1e6 * (i + 1)}
                for d in CO.trading_days(day - timedelta(days=40), day - timedelta(days=1))]
        db.put_bars("stock", sym, 1440, rows)
    assert [u["symbol"] for u in U.build(db, "stock", day, save=False)] == ["BBB", "AAA"]
    # SPY dans la liste des actifs : il reste dans l'univers comme au Labo 1.
    monkeypatch.setattr(CO, "stock_universe_symbols", lambda *a, **k: _async(["AAA", "SPY"]))
    await R.ensure_daily_range(db, "stock", day, day)
    assert db.get("lab", "index_only:stock") is None
    assert "SPY" in [u["symbol"] for u in U.build(db, "stock", day, save=False)]


async def _async(value):
    return value


def test_chiffres_de_labo_dans_l_etat(db, monkeypatch):
    monkeypatch.setenv("JARVIS_LAB_DB", db.path)
    W.refresh_stats(db, datetime(2026, 10, 6, tzinfo=timezone.utc), 0, force=True)
    db.conn.execute("INSERT INTO catalysts (market, day, symbol, kind, news, news_status, created_at) VALUES "
                    "('stock', '2026-08-03', 'S000', 'mouvement', '[]', 'ok', 'x')")
    V.submit(db, {"exclude": {"field": "composite", "op": "<", "value": 5}})
    st = ST.status()
    l2 = st["labo2"]
    assert l2["stats"]["declenches"] > 0 and l2["stats"]["ic95"] and l2["stats"]["version"] == "v1"
    assert l2["fiches"] == {"total": 1, "avec_actus": 1, "a_classer": 1, "actus_a_chercher": 0, "sans_actus": 0} and l2["regles"]["en_attente"] == 1
    txt = ST.text(st)
    assert "Espérance" in txt and "R [" in txt and "Fiches catalyseurs : 1" in txt and "1 en attente" in txt
    assert len(txt) < 600  # message Discord court


def test_etat_d_une_base_du_labo_1(tmp_path, monkeypatch):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE progress (job TEXT, key TEXT, value TEXT);"
                       "CREATE TABLE trades (market TEXT, origin TEXT);"
                       "CREATE TABLE method_versions (version TEXT, created_at TEXT, settings TEXT);")
    conn.close()
    monkeypatch.setenv("JARVIS_LAB_DB", str(path))
    st = ST.status()
    assert "error" not in st and st["labo2"]["regles"] == {"en_attente": 0, "validee": 0, "rejetee": 0, "invalide": 0}
    assert "Espérance" not in ST.text(st)


async def test_mouvements_puis_resume_avec_fiches(tmp_path):
    from tests.lab_fixtures import fill_stock

    db = LabDB(str(tmp_path / "m.db"))
    day = date(2026, 9, 15)
    fill_stock(db, ["AAA", "BBB", "SPY"], CO.sessions_back(day, 125), day)
    await R.replay(day, day, "stock", db, collect=False, now=datetime(2026, 10, 6, tzinfo=timezone.utc))
    report = await W.lab2(db, datetime(2026, 9, 15, 23, 0, tzinfo=PARIS), collect=False)
    assert report["mouvements"] == 1 and report["regimes"] >= 1
    assert db.conn.execute("SELECT COUNT(*) FROM day_moves").fetchone()[0] == 3
    summary = json.loads(db.conn.execute("SELECT data FROM summaries").fetchone()[0])
    assert summary["jour_chiffres"]["stock"]["jour"] == "2026-09-15" and summary["jour_chiffres"]["stock"]["plans"] == 3


async def test_grade_en_tete_de_passage_sans_attendre_le_rejeu(tmp_path, monkeypatch):
    """Le grade est calculé avant le direct et le rejeu : une collecte longue (journalier de tout
    l'univers crypto au premier passage) ne le retarde plus."""
    from app.lab import grade as GR

    monkeypatch.setenv("JARVIS_LAB_DB", str(tmp_path / "g.db"))
    monkeypatch.setattr(GR, "_cache", {"key": None, "data": None})
    db = LabDB(str(tmp_path / "g.db"))
    version, _ = R.method(db)  # version de génération réelle (réglages complets), comme en production
    synthetic_trades(db, version=version, n_days=15, per_day=12, seed=4, first=date(2026, 8, 3), base=-0.3)
    seen = {}

    async def slow_replay(*a, **k):
        seen["grade_pendant_le_rejeu"] = GR.payload(db.conn)["marches"].get("stock", {}).get("grade")
        raise RuntimeError("collecte interrompue")

    async def fake_lab2(*a, **k):
        return {}

    monkeypatch.setattr(R, "replay", slow_replay)
    monkeypatch.setattr(W, "lab2", fake_lab2)
    report = await W.tick(db, datetime(2026, 10, 7, 4, tzinfo=timezone.utc), collect=False)
    assert seen["grade_pendant_le_rejeu"] is not None and seen["grade_pendant_le_rejeu"] != "non noté"
    assert report["grade"]["stock"] == seen["grade_pendant_le_rejeu"]
    assert (tmp_path / "lab_grade.json").exists()


def test_etape_du_worker_dans_l_etat(tmp_path, monkeypatch):
    from app.lab import heartbeat

    monkeypatch.setenv("JARVIS_LAB_DB", str(tmp_path / "lab.db"))
    monkeypatch.setattr(heartbeat, "enabled", True)
    monkeypatch.setattr(heartbeat, "stage", None)
    LabDB(str(tmp_path / "lab.db"))
    assert ST.status(str(tmp_path / "lab.db"))["worker"] is None
    heartbeat.beat("rejeu crypto : journalier de l'univers")
    heartbeat.beat()  # un battement sans étape garde l'étape en cours
    st = ST.status(str(tmp_path / "lab.db"))
    assert st["worker"]["etape"] == "rejeu crypto : journalier de l'univers" and st["worker"]["il_y_a_s"] < 60
    assert "Worker : rejeu crypto : journalier de l'univers · dernier signe de vie à l'instant" in ST.text(st)
    silent = st | {"worker": st["worker"] | {"il_y_a_s": 2 * 3600}}
    assert "⚠️ Worker muet" in ST.text(silent) and "il y a 2,0 h" in ST.text(silent)
    heartbeat.path().write_text("2026-10-07T04:00:00+00:00", encoding="utf-8")  # ancien format
    assert heartbeat.read()["le"] == "2026-10-07T04:00:00+00:00" and heartbeat.read()["etape"] is None
