"""Correctifs de la revue du labo : divisions d'actions, couverture par symbole, limite Alpaca,
pause pendant la séance, clés absentes, battement, limites documentées."""
import logging
from datetime import date, datetime, timedelta, timezone

import pytest

from app.core.http import SourceError
from app.lab import collector as CO
from app.lab import heartbeat
from app.lab import sources as SRC
from app.lab import status as ST
from app.lab import worker as W
from app.lab.db import LabDB

DAY = date(2026, 9, 15)
NOW = datetime(2026, 10, 6, 2, tzinfo=timezone.utc)


def daily_bars(first: date, last: date, close: float) -> list[dict]:
    return [{"t": CO.ny_midnight(d), "o": close, "h": close, "l": close, "c": close, "v": 1000.0}
            for d in CO.trading_days(first, last)]


class FakeAlpaca:
    """Bougies simulées ; `factor` divise les prix (4 = division 4 pour 1), `fail` = symboles en panne."""

    def __init__(self, factor=1.0, fail=()):
        self.factor, self.fail, self.calls = factor, set(fail), []

    async def __call__(self, symbols, tf, start, end, transport=None):
        self.calls.append((tuple(symbols), tf, int(start.timestamp()), int(end.timestamp())))
        if self.fail & set(symbols):
            raise SourceError("HTTP 500")
        a, b = int(start.timestamp()), int(end.timestamp())
        if tf == 1440:
            rows = daily_bars(CO.ny_day(a), CO.ny_day(b - 1), 100.0 / self.factor)
            return {s: [r for r in rows if a <= r["t"] < b] for s in symbols}
        bars = [{"t": t, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": 1.0} for t in range(a, b, 300)]
        return {s: bars for s in symbols}


@pytest.fixture
def db(tmp_path):
    return LabDB(str(tmp_path / "lab.db"))


async def test_division_4_pour_1_recharge_tout_l_historique(db, monkeypatch):
    first, mid = date(2026, 8, 3), date(2026, 9, 1)
    old = FakeAlpaca()
    monkeypatch.setattr(SRC, "alpaca_bars", old)
    await CO.ensure_daily(db, "stock", ["AAA", "BBB"], first, mid, NOW)
    o, c = CO.session_ts(mid)
    await CO.ensure_intraday(db, "stock", ["AAA"], o, c, NOW)
    assert db.get_bars("stock", "AAA", 5, o, c) and db.get("cov5:stock", "AAA")
    split = FakeAlpaca(factor=4)  # Alpaca renvoie désormais tout l'historique ajusté de la division
    monkeypatch.setattr(SRC, "alpaca_bars", split)
    await CO.ensure_daily(db, "stock", ["AAA"], first, DAY, NOW)
    closes = {b["c"] for b in db.get_bars("stock", "AAA", 1440, 0, 2**40)}
    assert closes == {25.0}  # aucun raccord à 100 → 25 : tout rechargé
    assert db.get_bars("stock", "AAA", 5, o, c) == [] and db.get("cov5:stock", "AAA") is None
    assert db.get("covD:stock", "AAA") == [CO.ny_midnight(first), CO.ny_midnight(DAY + timedelta(days=1))]
    assert split.calls[-1][2] == CO.ny_midnight(first)  # rechargement depuis le début de la couverture


async def test_sans_operation_sur_titre_rien_n_est_recharge(db, monkeypatch):
    fake = FakeAlpaca()
    monkeypatch.setattr(SRC, "alpaca_bars", fake)
    await CO.ensure_daily(db, "stock", ["AAA"], date(2026, 8, 3), date(2026, 9, 1), NOW)
    await CO.ensure_daily(db, "stock", ["AAA"], date(2026, 8, 3), DAY, NOW)
    assert len(fake.calls) == 2
    # le prolongement relit 7 jours déjà stockés pour comparer
    assert fake.calls[1][2] == CO.ny_midnight(date(2026, 9, 2)) - CO.OVERLAP_DAYS * 86400
    assert CO.split_detected([{"t": 1, "c": 100.0}], [{"t": 1, "c": 100.5}]) is False
    assert CO.split_detected([{"t": 1, "c": 100.0}], [{"t": 1, "c": 25.0}]) is True


async def test_journalier_symbole_en_echec_ou_nouveau_complete_au_passage_suivant(db, monkeypatch):
    monkeypatch.setattr(CO, "DAILY_BATCH", 1)
    fake = FakeAlpaca(fail={"BBB"})
    monkeypatch.setattr(SRC, "alpaca_bars", fake)
    await CO.ensure_daily(db, "stock", ["AAA", "BBB"], date(2026, 8, 3), DAY, NOW)
    assert db.get("covD:stock", "AAA") and db.get("covD:stock", "BBB") is None
    fake.fail.clear()
    fake.calls.clear()
    await CO.ensure_daily(db, "stock", ["AAA", "BBB", "NEW"], date(2026, 8, 3), DAY, NOW)
    assert sorted(c[0][0] for c in fake.calls) == ["BBB", "NEW"]  # AAA déjà couvert : pas redemandé
    assert db.get_bars("stock", "NEW", 1440, 0, 2**40) and db.get("covD:stock", "BBB")


async def test_5_min_symbole_en_echec_ou_nouveau_complete_au_passage_suivant(db, monkeypatch):
    monkeypatch.setattr(CO, "STOCK_BATCH", 1)
    fake = FakeAlpaca(fail={"BBB"})
    monkeypatch.setattr(SRC, "alpaca_bars", fake)
    start, end = CO.session_ts(date(2026, 9, 14))[0], CO.session_ts(DAY)[1]
    await CO.ensure_intraday(db, "stock", ["AAA", "BBB"], start, end, NOW)
    assert db.get("cov5:stock", "AAA") == [start, end] and db.get("cov5:stock", "BBB") is None
    fake.fail.clear()
    fake.calls.clear()
    await CO.ensure_intraday(db, "stock", ["AAA", "BBB", "NEW"], start, end, NOW)
    assert sorted({c[0][0] for c in fake.calls}) == ["BBB", "NEW"]
    assert db.get("cov5:stock", "NEW") == [start, end]
    assert len(db.get_bars("stock", "NEW", 5, 0, 2**40)) == 2 * 78


async def test_5_min_demandees_seance_par_seance_sans_heures_etendues(db, monkeypatch):
    fake = FakeAlpaca()
    monkeypatch.setattr(SRC, "alpaca_bars", fake)
    friday, tuesday = date(2026, 9, 11), date(2026, 9, 15)
    start, end = CO.ny_midnight(friday), CO.ny_midnight(tuesday + timedelta(days=1))
    await CO.ensure_intraday(db, "stock", ["AAA", "BBB"], start, end, NOW)
    windows = [(c[2], c[3]) for c in fake.calls]
    assert windows == [CO.session_ts(d) for d in (friday, date(2026, 9, 14), tuesday)]  # ni nuit ni week-end
    assert all(c[0] == ("AAA", "BBB") for c in fake.calls)  # un lot de symboles par séance


def test_limite_alpaca_du_labo(monkeypatch):
    monkeypatch.delenv("JARVIS_LAB_ALPACA_PER_MINUTE", raising=False)
    assert SRC._alpaca_per_minute() == 100
    monkeypatch.setenv("JARVIS_LAB_ALPACA_PER_MINUTE", "60")
    assert SRC._alpaca_per_minute() == 60
    monkeypatch.setenv("JARVIS_LAB_ALPACA_PER_MINUTE", "beaucoup")
    assert SRC._alpaca_per_minute() == 100
    assert SRC.ALPACA_LIMIT.period == 60.0


async def test_actions_en_pause_pendant_la_seance_crypto_continue(db, monkeypatch):
    session = datetime(2026, 9, 16, 15, 0, tzinfo=timezone.utc)  # 11 h à New York
    assert W.stock_pause(session) == "séance US en cours"
    monkeypatch.setenv("JARVIS_LAB_REPLAY_DAYS", "2")
    report = await W.tick(db, session, collect=False)
    assert report["paused"] == {"stock": "séance US en cours"}
    assert "stock" not in report["replay"] and "crypto" in report["replay"]
    assert db.get("replay:stock", "last_day") is None and db.get("replay:crypto", "start")


def test_cles_alpaca_absentes_un_avertissement_par_heure(monkeypatch, caplog):
    for name in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    night = datetime(2026, 9, 17, 3, tzinfo=timezone.utc)
    assert W.stock_pause(night, collect=True) == "clés Alpaca absentes"
    assert W.stock_pause(night, collect=False) is None
    monkeypatch.setattr(W, "_key_warned_at", None)
    with caplog.at_level(logging.WARNING, logger="app.lab.worker"):
        assert W.warn_missing_keys(1000.0) is True
        assert W.warn_missing_keys(1000.0 + 1800) is False
        assert W.warn_missing_keys(1000.0 + 3601) is True
    lines = [r for r in caplog.records if "clés Alpaca" in r.getMessage()]
    assert len(lines) == 2 and all(r.exc_info is None for r in lines)  # une ligne, pas de pile d'appels


async def test_cles_absentes_la_crypto_tourne_seule(db, monkeypatch):
    for name in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    calls = []

    async def fake_replay(start, end, market, *a, **k):
        calls.append(market)
        return 0

    async def fake_live(*a, **k):
        return {}

    monkeypatch.setattr(W.R, "replay", fake_replay)
    monkeypatch.setattr(W.R, "run_live_day", fake_live)
    report = await W.tick(db, datetime(2026, 9, 17, 3, tzinfo=timezone.utc), collect=True)
    assert calls == ["crypto"] and report["paused"]["stock"] == "clés Alpaca absentes"


def test_battement_a_cote_de_la_base(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_LAB_DB", str(tmp_path / "sous" / "lab.db"))
    assert heartbeat.path() == tmp_path / "sous" / "lab.heartbeat"
    monkeypatch.setattr(heartbeat, "enabled", False)
    heartbeat.beat()
    assert not heartbeat.path().exists()  # hors du worker : rien n'est écrit
    monkeypatch.setattr(heartbeat, "enabled", True)
    heartbeat.beat()
    assert heartbeat.path().exists()


def test_limites_dans_l_etat(tmp_path):
    st = ST.status(str(tmp_path / "absent.db"))
    assert any("OKX ne liste que les paires cotées" in x for x in st["limitations"])
