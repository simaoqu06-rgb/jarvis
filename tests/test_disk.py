"""Alerte disque : seuils, une alerte par jour et par niveau, rien d'effacé."""
from datetime import datetime, timezone

from app import alerts, config

GB = 1024 ** 3
NOW = datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)


def usage(pct):
    return lambda _folder: (100 * GB, pct * GB, (100 - pct) * GB)


def test_under_threshold_is_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DISK_ALERT_PCT", 75.0)
    assert alerts.check_disk(alerts.State(None), NOW, str(tmp_path), usage(60)) == []


def test_alert_once_per_day_then_critical(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DISK_ALERT_PCT", 75.0)
    (tmp_path / "lab.db").write_bytes(b"x" * 2048)
    state = alerts.State(None)
    first = alerts.check_disk(state, NOW, str(tmp_path), usage(80))
    assert len(first) == 1 and "80 %" in first[0] and "lab.db 2 Ko" in first[0]
    assert alerts.check_disk(state, NOW, str(tmp_path), usage(82)) == []
    critical = alerts.check_disk(state, NOW, str(tmp_path), usage(93))
    assert len(critical) == 1 and "critique" in critical[0]
    assert alerts.check_disk(state, NOW, str(tmp_path), usage(95)) == []
    tomorrow = NOW.replace(day=8)
    assert len(alerts.check_disk(state, tomorrow, str(tmp_path), usage(80))) == 1
    assert (tmp_path / "lab.db").exists()


def test_unreadable_disk_is_silent(tmp_path):
    def broken(_folder):
        raise OSError("absent")
    assert alerts.check_disk(alerts.State(None), NOW, str(tmp_path), broken) == []


def test_size_format():
    assert alerts._size(52 * 1024) == "52 Ko"
    assert alerts._size(1.8 * GB) == "1,8 Go"
