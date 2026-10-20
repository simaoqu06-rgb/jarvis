"""Déclaration n° 8, D1 : archive des actus Finnhub des gappers, sans rien en lire."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from app.core.http import SourceError
from app.lab import archive as ARC
from app.lab import collector as CO
from app.lab import seal as SEAL
from app.lab import sources as SRC
from app.lab import status as STATUS
from app.lab.db import LabDB
from app.sources import finnhub

D = date(2025, 11, 4)                                         # mardi, dans la profondeur de Finnhub
NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)         # samedi : hors fenêtre calme


def _daily_bars(day_: date, closes: list[float], gap_open: float | None, half_range: float) -> list[dict]:
    sessions = [CO.sessions_back(day_, k) for k in range(len(closes), 0, -1)]
    bars = [{"t": CO.ny_midnight(s), "o": c, "h": c + half_range, "l": c - half_range, "c": c, "v": 1e6}
            for s, c in zip(sessions, closes)]
    if gap_open is not None:
        bars.append({"t": CO.ny_midnight(day_), "o": gap_open, "h": gap_open, "l": gap_open, "c": gap_open, "v": 1e6})
    return bars


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(ARC, "LIMIT", SRC.RateLimiter(1000, 60.0))
    monkeypatch.setenv("FINNHUB_API_KEY", "test")
    db = LabDB(str(tmp_path / "lab.db"))
    db.put_bars("stock", "GAP5", 1440, _daily_bars(D, [10.0] * 20, 10.5, 0.3))     # gap +5 %, ATR 6 %
    db.put_bars("stock", "GAP38", 1440, _daily_bars(D, [10.0] * 20, 10.38, 0.3))   # +3,8 % : dans la marge
    db.put_bars("stock", "GAP3", 1440, _daily_bars(D, [10.0] * 20, 10.3, 0.3))     # +3 % : dehors
    db.put_bars("stock", "PLAT", 1440, _daily_bars(D, [10.0] * 20, 10.5, 0.05))    # ATR 1 %
    yield db
    db.close()


def _only_d(db, monkeypatch):
    """Ne liste que la séance D (les autres séances de la profondeur n'ont pas de journalier dans ce test)."""
    monkeypatch.setattr(ARC, "days_to_populate", lambda db_, now, limit: [D])
    assert ARC.populate(db, NOW) == 1


def test_population_des_gappers(db, monkeypatch):
    _only_d(db, monkeypatch)
    rows = db.conn.execute("SELECT symbol, gap_pct, atr_pct, prev_close, open, cutoff_ts, status FROM gap_news "
                           "ORDER BY symbol").fetchall()
    assert [r[0] for r in rows] == ["GAP38", "GAP5"] and {r[6] for r in rows} == {"a_chercher"}
    assert rows[1][1] == pytest.approx(5.0) and rows[1][2] == pytest.approx(6.0) and rows[1][3:5] == (10.0, 10.5)
    assert datetime.fromtimestamp(rows[0][5], CO.NEW_YORK).strftime("%Y-%m-%d %H:%M") == "2025-11-04 09:45"
    assert db.get(ARC.POPULATION_JOB, D.isoformat()) == 2


def test_seances_a_lister(db, monkeypatch):
    monkeypatch.setattr(SEAL, "SEALED_FROM", date(2026, 4, 6))   # le vrai bloc (conftest le déplace en 2000)
    monkeypatch.setattr(SEAL, "SEALED_TO", date(2026, 10, 5))
    db.put_bars("stock", "GAP5", 1440, [{"t": CO.ny_midnight(date(2026, 5, 5)), "o": 1, "h": 1, "l": 1, "c": 1,
                                         "v": 1}])  # journalier jusqu'au bloc scellé
    days = ARC.days_to_populate(db, NOW, 1000)
    assert days[0] == date(2025, 10, 10) and D in days                       # profondeur : un an avant
    assert not any(date(2026, 4, 6) <= d <= date(2026, 10, 5) for d in days)  # jamais le bloc scellé
    assert days[-1] == date(2026, 4, 2)                                      # 3 avril : Vendredi saint
    assert ARC.populate(db, NOW, limit=1) == 1 and db.get(ARC.POPULATION_JOB, "2025-10-10") == 0  # sans gapper
    assert ARC.days_to_populate(db, NOW, 1)[0] == date(2025, 10, 13)
    hole = date(2025, 11, 5)                                  # journalier absent alors que la suite est là
    monkeypatch.setattr(ARC, "days_to_populate", lambda db_, now, limit: [hole])
    assert ARC.populate(db, NOW) == 1 and db.get(ARC.POPULATION_JOB, hole.isoformat(), "x") is None
    assert ARC.counts(db.conn)["sans_journalier"] == 1 and ARC.earnings_dates(db, NOW) == []


@pytest.fixture
def news(db, monkeypatch):
    _only_d(db, monkeypatch)
    cut = ARC.cutoff_ts(D)
    calls = []

    async def fake(symbol, start, end):
        calls.append((symbol, start, end))
        return [{"title": "après 9 h 45", "published_ts": cut + 60, "url": "c"},
                {"title": "la veille", "published_ts": cut - 86400, "url": "a"},
                {"title": "à 9 h 45 pile", "published_ts": cut, "url": "b"}]

    monkeypatch.setattr(finnhub, "company_news_between", fake)
    return calls


async def test_actus_d_avant_9h45(db, news):
    rep = await ARC.fetch_news(db, NOW, ARC._Budget(60))
    assert rep["archives"] == 2 and rep["appels"] == 2
    assert news[0] == ("GAP38", D - timedelta(days=7), D)
    row = db.conn.execute("SELECT status, news_from, news_to, received, news FROM gap_news WHERE symbol = 'GAP5'").fetchone()
    assert row[:4] == ("ok", "2025-10-28", "2025-11-04", 3)
    assert [n["url"] for n in json.loads(row[4])] == ["b", "a"]  # plus récente d'abord, rien d'après 9 h 45
    assert (await ARC.fetch_news(db, NOW, ARC._Budget(60)))["appels"] == 0


async def test_fenetre_coupee_a_la_profondeur_puis_perdue(db, news):
    edge = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)  # profondeur au 8 nov. 2025 : D n'y est plus
    near = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
    assert ARC.news_window(D, near) == (date(2025, 10, 31), D)
    await ARC.fetch_news(db, near, ARC._Budget(60))
    assert news[0][1] == date(2025, 10, 31)
    db.conn.execute("UPDATE gap_news SET status = 'a_chercher'")
    rep = await ARC.fetch_news(db, edge, ARC._Budget(60))
    assert rep["hors_profondeur"] == 2 and rep["appels"] == 0


async def test_echecs_repris_au_passage_suivant(db, monkeypatch):
    _only_d(db, monkeypatch)
    calls = []

    async def broken(symbol, start, end):
        calls.append(symbol)
        raise SourceError("HTTP 403")

    monkeypatch.setattr(finnhub, "company_news_between", broken)
    for k in range(ARC.ATTEMPTS):
        rep = await ARC.fetch_news(db, NOW, ARC._Budget(60))
        assert rep["erreurs"] == 2 and len(calls) == 2 * (k + 1)   # un essai par gapper et par passage
    assert {r[0] for r in db.conn.execute("SELECT status FROM gap_news")} == {"erreur"}
    db.conn.execute("UPDATE gap_news SET status = 'a_chercher', attempts = 0")
    monkeypatch.setattr(ARC, "STOP_AFTER_ERRORS", 1)              # panne : on arrête le passage
    assert (await ARC.fetch_news(db, NOW, ARC._Budget(60)))["appels"] == 1


async def test_calendrier_des_resultats(db, monkeypatch):
    _only_d(db, monkeypatch)
    calls = []

    async def fake(start, end):
        calls.append(start)
        assert start == end
        return [{"symbol": "GAP5", "date": start.isoformat(), "hour": "bmo", "eps_actual": 0.2, "eps_estimate": 0.1}]

    monkeypatch.setattr(finnhub, "earnings_calendar", fake)
    assert ARC.earnings_dates(db, NOW)[0] == D - timedelta(days=10) and len(ARC.earnings_dates(db, NOW)) == 41
    rep = await ARC.fetch_earnings(db, NOW, ARC._Budget(60))
    assert rep["dates"] == 41 and calls[-1] == D + timedelta(days=30)
    n, items = db.conn.execute("SELECT n, items FROM earnings_days WHERE day = ?", (D.isoformat(),)).fetchone()
    assert n == 1 and json.loads(items)[0]["symbol"] == "GAP5"
    assert ARC.earnings_dates(db, NOW) == []
    recent = datetime(2025, 11, 7, 12, tzinfo=timezone.utc)      # dates pas encore passées de deux jours
    db.conn.execute("DELETE FROM earnings_days")
    assert ARC.earnings_dates(db, recent)[-1] == date(2025, 11, 5)


async def test_profils(db, monkeypatch):
    _only_d(db, monkeypatch)

    async def profile(symbol):
        if symbol == "GAP38":
            raise SourceError(f"{symbol} : profil inconnu")
        return {"name": "Gap Five Inc", "industry": "Biotechnology", "market_cap": 3e8}

    monkeypatch.setattr(finnhub, "profile", profile)
    rep = await ARC.fetch_profiles(db, ARC._Budget(60))
    assert rep["profils"] == 2
    assert db.get(ARC.PROFILE_JOB, "GAP5")["nom"] == "Gap Five Inc"
    assert db.get(ARC.PROFILE_JOB, "GAP38")["inconnu"] is True
    assert (await ARC.fetch_profiles(db, ARC._Budget(60)))["appels"] == 0


async def test_profils_des_seances_completes_d_abord(db, monkeypatch):
    _only_d(db, monkeypatch)
    later = (D + timedelta(days=1)).isoformat()
    db.conn.execute("UPDATE gap_news SET status = 'ok'")                         # séance D : actus complètes
    db.conn.execute("INSERT INTO gap_news (day, symbol, gap_pct, atr_pct, cutoff_ts, status) "
                    "VALUES (?, 'LATE', 6.0, 7.0, 0, 'a_chercher')", (later,))   # séance suivante : en cours
    asked = []

    async def profile(symbol):
        asked.append(symbol)
        return {"name": f"{symbol} Inc", "industry": None, "market_cap": None}

    monkeypatch.setattr(finnhub, "profile", profile)
    await ARC.fetch_profiles(db, ARC._Budget(60), ready_only=True)
    assert sorted(asked) == ["GAP38", "GAP5"]                                    # Killmode complet peut rejouer D
    await ARC.fetch_profiles(db, ARC._Budget(60))
    assert asked[-1] == "LATE"


def test_fenetre_calme():
    assert ARC.quiet(datetime(2026, 10, 8, 14, tzinfo=timezone.utc))          # 10 h New York, jeudi
    assert ARC.quiet(datetime(2026, 10, 8, 12, 45, tzinfo=timezone.utc))      # 8 h 45 : scanner de 9 h 10
    assert not ARC.quiet(datetime(2026, 10, 8, 21, tzinfo=timezone.utc))      # 17 h
    assert not ARC.quiet(NOW)                                                 # samedi
    assert ARC.seconds_before_quiet(datetime(2026, 10, 8, 12, 20, tzinfo=timezone.utc)) == 600
    assert ARC.seconds_before_quiet(NOW) == float("inf")


async def test_etape_du_worker(db, monkeypatch):
    monkeypatch.setattr(ARC, "days_to_populate", lambda db_, now, limit: [D])
    called = []

    async def fake(*args):
        called.append(args)
        return []

    for name in ("company_news_between", "earnings_calendar", "profile"):
        monkeypatch.setattr(finnhub, name, fake)
    rep = await ARC.step(db, datetime(2026, 10, 8, 14, tzinfo=timezone.utc), 600)   # séance : base seule
    assert rep == {"seances": 1} and called == []
    rep = await ARC.step(db, NOW, 0)                                                   # pas de durée
    assert "actus" not in rep and called == []
    monkeypatch.delenv("FINNHUB_API_KEY")
    assert "actus" not in await ARC.step(db, NOW, 600)                                 # pas de clé
    monkeypatch.setenv("FINNHUB_API_KEY", "test")
    rep = await ARC.step(db, NOW, 600)
    assert rep["actus"]["archives"] == 2 and rep["resultats"]["dates"] == 41 and rep["profils"]["appels"] == 2


async def test_comptes_sans_contenu(db, news, tmp_path):
    assert ARC.counts(db.conn)["archives"] == 0
    await ARC.fetch_news(db, NOW, ARC._Budget(60))
    c = ARC.counts(db.conn)
    assert c == {"seances": 1, "du": D.isoformat(), "au": D.isoformat(), "sans_journalier": 0, "gappers": 2,
                 "archives": 2, "a_chercher": 0, "erreurs": 0, "hors_profondeur": 0, "prochaine_seance": None,
                 "dates_resultats": 0, "dates_resultats_erreurs": 0, "profils": 0, "societes": 2, "rejouees": 0}
    assert "Archive des actus (n° 8) : 2/2 gappers sur 1 séances" in ARC.line(c) and "rejoué" not in ARC.line(c)
    db.set(ARC.KILL_JOB, D.isoformat(), {"gappers": 2, "archives": 2, "signaux": 0})
    assert ARC.line(ARC.counts(db.conn)).endswith("Killmode complet rejoué sur 1 séances")
    st = STATUS.status(str(tmp_path / "lab.db"))
    assert st["archive_actus"]["archives"] == 2 and "Archive des actus" in STATUS.text(st)
    assert ARC.counts(LabDB(":memory:").conn) is None


async def test_calendrier_finnhub_toutes_societes(monkeypatch):
    seen = {}

    async def get_json(url, params=None, headers=None):
        seen.update(params)
        return {"earningsCalendar": [{"symbol": "ACME", "date": "2025-11-04", "hour": "amc", "epsActual": "0.5",
                                      "epsEstimate": 0.4, "quarter": 3, "year": 2025}, {"date": "2025-11-04"}]}

    monkeypatch.setenv("FINNHUB_API_KEY", "test")
    monkeypatch.setattr(finnhub, "get_json", get_json)
    got = await finnhub.earnings_calendar(D, D)
    assert "symbol" not in seen and seen["from"] == seen["to"] == "2025-11-04"
    assert got == [{"symbol": "ACME", "date": "2025-11-04", "hour": "amc", "quarter": 3, "year": 2025,
                    "eps_estimate": 0.4, "eps_actual": 0.5, "revenue_estimate": None, "revenue_actual": None}]

    async def error(url, params=None, headers=None):
        return {"error": "You don't have access to this resource."}

    monkeypatch.setattr(finnhub, "get_json", error)
    with pytest.raises(SourceError):
        await finnhub.earnings_calendar(D, D)
