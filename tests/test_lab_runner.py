"""Rejeu reprenable, journée en direct idempotente, état du labo et décisions du worker."""
import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from app.lab import collector as CO
from app.lab import generator as G
from app.lab import runner as R
from app.lab import worker as W
from app.lab.db import LabDB
from tests.lab_fixtures import fill_crypto, fill_stock

START, END = date(2026, 9, 14), date(2026, 9, 16)  # lundi → mercredi
NOW = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
SYMBOLS = ["AAA", "BBB", "SPY"]


@pytest.fixture
def db(tmp_path):
    d = LabDB(str(tmp_path / "lab.db"))
    fill_stock(d, SYMBOLS, CO.sessions_back(START, 125), END)
    return d


async def test_rejeu_reprend_apres_une_coupure_sans_doublon(db, monkeypatch):
    real = G.stock_day
    calls = []

    def flaky(db_, day, s, universe, errors=None, strat_out=None, absent=None):
        calls.append(day)
        if len(calls) == 2:
            raise RuntimeError("coupure")
        return real(db_, day, s, universe, errors, strat_out, absent)

    monkeypatch.setattr(G, "stock_day", flaky)
    assert await R.replay(START, END, "stock", db, collect=False, now=NOW) == 1  # arrêt au jour en échec
    assert db.get("replay:stock", "last_day") == START.isoformat()
    assert db.get("replay:stock", "attempts:2026-09-15") == 1
    first = db.count_trades(origin="replay")
    assert first == 3 and db.count_trades(day=(START + timedelta(days=1)).isoformat()) == 0
    monkeypatch.setattr(G, "stock_day", real)
    done = await R.replay(START, END, "stock", db, collect=False, now=NOW)
    assert done == 2 and db.count_trades(origin="replay") == 9
    assert await R.replay(START, END, "stock", db, collect=False, now=NOW) == 0  # rien à refaire
    assert db.count_trades() == 9 and len(db.universe("stock", END.isoformat())) == 3


async def test_rejeu_par_morceaux_et_jour_pas_encore_pret(db):
    assert await R.replay(START, END, "stock", db, collect=False, max_days=1, now=NOW) == 1
    assert db.get("replay:stock", "last_day") == START.isoformat()
    early = datetime.fromtimestamp(CO.session_ts(START + timedelta(days=1))[1] + 600, timezone.utc)
    assert await R.replay(START, END, "stock", db, collect=False, now=early) == 0  # clôture + 10 min : trop tôt


async def test_rejeu_borne_en_temps_au_moins_un_jour(db, monkeypatch):
    clock = iter(range(0, 10_000, 400))  # chaque lecture de l'horloge du rejeu avance de 400 s

    class FakeTime:
        @staticmethod
        def monotonic():
            return next(clock)
    monkeypatch.setattr(R, "time", FakeTime)
    # Départ à 0 ; après le 1er jour, 400 s < 600 : on continue ; après le 2e, 800 s : on s'arrête.
    assert await R.replay(START, END, "stock", db, collect=False, now=NOW, max_seconds=600) == 2
    assert await R.replay(START, END, "stock", db, collect=False, now=NOW, max_seconds=0) == 1  # au moins un jour


def test_rejeu_s_arrete_avant_le_resume_du_soir():
    from zoneinfo import ZoneInfo

    paris = ZoneInfo("Europe/Paris")
    at = datetime(2026, 10, 7, 22, 25, tzinfo=paris)
    assert W.replay_seconds(at, None) == 300  # 5 min avant 22 h 30 : le résumé passe avant la routine
    assert W.replay_seconds(at, "2026-10-07") == W.REPLAY_SECONDS["nuit"]  # résumé déjà fait
    assert W.replay_seconds(datetime(2026, 10, 7, 22, 40, tzinfo=paris), None) == W.REPLAY_SECONDS["nuit"]
    assert W.replay_seconds(datetime(2026, 10, 7, 11, 0, tzinfo=paris), None) == W.REPLAY_SECONDS["jour"]


def test_rejeu_borne_par_periode():
    assert W.REPLAY_SECONDS["nuit"] > W.REPLAY_SECONDS["jour"] > W.REPLAY_SECONDS["seance"] > 0
    assert set(W.REPLAY_SECONDS) == set(W.CHUNK_DAYS)


async def test_journee_en_direct_idempotente(db):
    out = await R.run_live_day(END, db, markets=("stock",), collect=False, now=NOW)
    assert out == {"stock": 3}
    rows = db.conn.execute("SELECT id, symbol, r_net FROM trades ORDER BY symbol").fetchall()
    await R.run_live_day(END, db, markets=("stock",), collect=False, now=NOW)
    assert db.count_trades(origin="live") == 3
    assert db.conn.execute("SELECT id, symbol, r_net FROM trades ORDER BY symbol").fetchall() == rows  # mêmes id
    assert db.get("live:stock", "last_day") == END.isoformat()


async def test_crypto_en_direct_complete_les_creneaux(tmp_path):
    db = LabDB(str(tmp_path / "c.db"))
    day = date(2026, 9, 15)
    fill_crypto(db, ["BTC-USDT"], day - timedelta(days=45), day + timedelta(days=2))
    noon_next = datetime.fromtimestamp(CO.utc_midnight(day) + 86400 + 12 * 3600 + 60, timezone.utc)
    await R.run_live_day(day, db, markets=("crypto",), collect=False, now=noon_next)
    assert db.count_trades(market="crypto") == 4 and db.get("live:crypto", "last_day") is None
    first = db.conn.execute("SELECT planned_at, id FROM trades ORDER BY planned_at").fetchall()
    later = datetime.fromtimestamp(CO.utc_midnight(day) + 2 * 86400, timezone.utc)
    await R.run_live_day(day, db, markets=("crypto",), collect=False, now=later)
    assert db.count_trades(market="crypto") == 6 and db.get("live:crypto", "last_day") == day.isoformat()
    # Les créneaux déjà journalisés, réécrits avec le jour, gardent leur id (ordre de première journalisation,
    # lu par les mesures hors échantillon) ; les deux nouveaux viennent après.
    after = db.conn.execute("SELECT planned_at, id FROM trades ORDER BY planned_at").fetchall()
    assert after[:4] == first and min(i for _, i in after[4:]) > max(i for _, i in first)


def test_version_de_methode_figee(tmp_path):
    db = LabDB(str(tmp_path / "v.db"))
    v, s = R.method(db)
    assert v == "v1" and s["go_min"] == 7.0
    snap = db.method_version({"trading": {"go_min": 99}})
    assert snap[0] == "v1" and snap[1]["trading"]["go_min"] == 7.0  # la première version reste
    assert snap[1]["plan"]["stock"]["interval"] == 15 and snap[1]["lab"]["timing_lookback"] == 1500


def test_etat_base_absente_puis_remplie(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main

    path = tmp_path / "lab.db"
    monkeypatch.setenv("JARVIS_LAB_DB", str(path))
    client = TestClient(main.app)
    r = client.get("/api/lab/status").json()
    assert r["exists"] is False and r["trades"] == {} and "pas encore démarré" in r["text"]

    db = LabDB(str(path))
    fill_stock(db, SYMBOLS, CO.sessions_back(START, 125), END)
    asyncio.run(R.replay(START, END, "stock", db, collect=False, now=NOW))
    db.close()
    r = client.get("/api/lab/status").json()
    assert r["exists"] and r["trades"] == {"stock": {"replay": 9}}
    assert r["replay"]["stock"] == {"start": START.isoformat(), "end": END.isoformat(), "last_day": END.isoformat(),
                                    "days_done": 3, "days_total": 3, "pct": 100.0, "jours_sautes": 0,
                                    "jours_partiels": 0, "absents": 0}
    assert r["method_version"] == "v1" and r["db_size_mb"] > 0 and "100 %" in r["text"]


async def test_commande_discord_labo(tmp_path, monkeypatch):
    from app.notify import bot

    monkeypatch.setenv("JARVIS_LAB_DB", str(tmp_path / "absent.db"))
    answer = await bot.handle("!labo")
    assert answer.startswith("🧪") and "!labo" in bot.HELP


def test_creneaux_du_worker():
    paris = W.PARIS
    assert W.period(datetime(2026, 10, 6, 23, 0, tzinfo=paris)) == "nuit"
    assert W.period(datetime(2026, 10, 7, 6, 30, tzinfo=paris)) == "nuit"
    assert W.period(datetime(2026, 10, 10, 15, 0, tzinfo=paris)) == "nuit"  # samedi
    assert W.period(datetime(2026, 10, 6, 17, 0, tzinfo=paris)) == "seance"  # 11 h à New York
    assert W.period(datetime(2026, 10, 6, 11, 0, tzinfo=paris)) == "jour"
    assert W.period(datetime(2026, 11, 26, 17, 0, tzinfo=paris)) == "jour"  # Thanksgiving : pas de séance
    assert W.replay_budget(datetime(2026, 10, 6, 23, 0, tzinfo=paris)) > W.replay_budget(
        datetime(2026, 10, 6, 11, 0, tzinfo=paris)) > W.replay_budget(datetime(2026, 10, 6, 17, 0, tzinfo=paris)) == 1


def test_bornes_du_rejeu_et_jours_en_direct():
    start, end = W.replay_bounds(date(2026, 10, 6), 504)
    assert end == date(2026, 10, 5) and len(CO.trading_days(start, date(2026, 10, 5))) == 504
    live_start = date(2026, 10, 5)
    friday_night = datetime(2026, 10, 9, 21, 0, tzinfo=timezone.utc)  # vendredi 17 h à New York
    assert W.live_days_due("stock", None, live_start, friday_night) == [date(2026, 10, d) for d in (5, 6, 7, 8, 9)]
    before_close = datetime(2026, 10, 9, 20, 10, tzinfo=timezone.utc)
    assert W.live_days_due("stock", date(2026, 10, 8), live_start, before_close) == []
    assert W.live_days_due("crypto", date(2026, 10, 7), live_start, friday_night) == [date(2026, 10, 8)]
    day = date(2026, 10, 8)
    nine = datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc)
    assert W.crypto_windows_due(day, nine, None)
    assert not W.crypto_windows_due(day, nine, CO.utc_midnight(day) + 8 * 3600)
    assert W.crypto_windows_due(day, nine + timedelta(hours=4), CO.utc_midnight(day) + 8 * 3600)


async def test_passage_du_worker_hors_ligne(db, monkeypatch):
    monkeypatch.setenv("JARVIS_LAB_REPLAY_DAYS", "2")
    now = datetime(2026, 9, 17, 3, 0, tzinfo=timezone.utc)  # nuit à Paris, avant l'ouverture du 17
    report = await W.tick(db, now, collect=False)
    assert db.get("lab", "live_start") == "2026-09-16"
    assert report["replay"]["stock"] == 2 and db.count_trades(origin="replay") == 6
    assert report["live"]["stock:2026-09-16"] == 3  # séance du 16 close depuis plus de 30 min
    assert W.enabled() and report["paused"] == {}


async def test_symbole_en_erreur_compte_sans_bloquer_le_jour(db, monkeypatch):
    real = G.stock_trade

    def broken(db_, symbol, *args):
        if symbol == "BBB":
            raise ZeroDivisionError("division par zéro")
        return real(db_, symbol, *args)

    monkeypatch.setattr(G, "stock_trade", broken)
    assert await R.replay(START, START, "stock", db, collect=False, now=NOW) == 1
    assert db.count_trades() == 2
    assert db.get("replay:stock", f"day:{START}") == {"trades": 2, "errors": 1, "error_symbols": ["BBB"],
                                                      "absent": 0, "absent_motifs": {}, "absent_symbols": []}


async def test_jour_en_echec_saute_apres_trois_essais(db, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("données absentes")

    monkeypatch.setattr(G, "stock_day", boom)
    for expected in (0, 0):
        assert await R.replay(START, END, "stock", db, collect=False, now=NOW) == expected
    assert db.get("replay:stock", "last_day") is None
    await R.replay(START, END, "stock", db, collect=False, now=NOW)  # 3e échec du 14 : sauté, puis le 15 échoue
    assert db.get("replay:stock", f"skipped:{START}") == "RuntimeError: données absentes"
    assert db.get("replay:stock", "last_day") == START.isoformat()
    assert db.get("replay:stock", "attempts:2026-09-15") == 1
