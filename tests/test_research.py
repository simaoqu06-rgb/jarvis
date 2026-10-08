"""Lot D : recherche avant l'ouverture (scan lu par le chemin du labo, verdicts gardés et mesurés)."""
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import main, scheduler
from app.trading import paper
from app.trading import research as R

KEY = "cle-de-test-recherche"
H = {"X-Lab-Key": KEY}
NOW = datetime(2026, 10, 8, 13, 15, tzinfo=timezone.utc)   # 9 h 15 à New York


def scan_line(at: str, symbols=("ABC", "XYZ")) -> str:
    return json.dumps({"at": at, "picks": [{"symbol": s, "entry": 10.0, "stop": 9.5} for s in symbols],
                       "skipped": [], "ecartees_methode": []})


@pytest.fixture
def scans(tmp_path, monkeypatch):
    path = tmp_path / "scans.jsonl"
    monkeypatch.setattr(R.scanner, "SCANS_PATH", str(path))
    return path


def verdict(symbol="ABC", v="soutient", **extra):
    return {"symbol": symbol, "verdict": v, "raison": "Contrat signé hier soir, communiqué officiel."} | extra


def test_dernier_scan_et_jour_de_new_york(scans):
    assert R.last_scan() is None and R.scan_view(None, NOW)["du_jour"] is False
    scans.write_text(scan_line("2026-10-07T13:10:00+00:00") + "\n" + scan_line("2026-10-08T13:10:02+00:00") + "\n")
    view = R.scan_view(R.last_scan(), NOW)
    assert view["du_jour"] is True and view["scan"]["jour"] == "2026-10-08"
    # 2 h UTC le 9 = 22 h à New York le 8 : encore le scan du jour ; le 9 au matin, non.
    assert R.scan_view(R.last_scan(), datetime(2026, 10, 9, 2, 0, tzinfo=timezone.utc))["du_jour"] is True
    assert R.scan_view(R.last_scan(), datetime(2026, 10, 9, 13, 0, tzinfo=timezone.utc))["du_jour"] is False


def test_verdicts_controles_et_remplaces(scans, tmp_path):
    scans.write_text(scan_line("2026-10-08T13:10:02+00:00") + "\n")
    book = R.ResearchBook(str(tmp_path / "r.json"))
    scan = R.last_scan()
    with pytest.raises(R.ResearchError, match="pas une valeur retenue"):
        R.record(book, [verdict("NOPE")], scan, NOW)
    with pytest.raises(R.ResearchError, match="permis"):
        R.record(book, [verdict(v="achat")], scan, NOW)
    with pytest.raises(R.ResearchError, match="sources"):
        R.record(book, [verdict(sources=["ftp://x"])], scan, NOW)
    with pytest.raises(R.ResearchError, match="Pas de scan"):
        R.record(book, [verdict()], scan, datetime(2026, 10, 9, 13, 0, tzinfo=timezone.utc))
    assert R.record(book, [verdict(), verdict("xyz", "contre")], scan, NOW) == {"enregistres": 2, "jour": "2026-10-08"}
    R.record(book, [verdict(v="neutre")], scan, NOW)                       # même valeur, même jour : remplacé
    again = R.ResearchBook(str(tmp_path / "r.json"))
    assert sorted((i["symbol"], i["verdict"]) for i in again.day("2026-10-08")) == [("ABC", "neutre"), ("XYZ", "contre")]


def entry(symbol, day, r, status="done", origin="scanner"):
    return {"symbol": symbol, "created_at": f"{day}T13:10:30+00:00", "origin": origin, "status": status,
            "result_r": r}


def test_bilan_contre_le_forward_test():
    book = R.ResearchBook(None)
    book.items = [{"jour": "2026-10-08", "symbol": "A", "verdict": "soutient"},
                  {"jour": "2026-10-08", "symbol": "B", "verdict": "contre"},
                  {"jour": "2026-10-08", "symbol": "C", "verdict": "soutient"},
                  {"jour": "2026-10-09", "symbol": "A", "verdict": "contre"},
                  {"jour": "2026-10-09", "symbol": "D", "verdict": "neutre"},
                  {"jour": "2026-10-09", "symbol": "E", "verdict": "soutient"}]
    entries = [entry("A", "2026-10-08", 1.5), entry("B", "2026-10-08", -1.0), entry("C", "2026-10-08", -0.5),
               entry("A", "2026-10-09", None), entry("D", "2026-10-09", 0.2, status="open"),
               entry("E", "2026-10-09", 2.0, origin="manuel")]
    b = R.bilan(book, entries)
    assert b["par_verdict"]["soutient"] == {"n": 2, "esperance_r": 0.5, "reussite": 0.5, "echantillon_faible": True}
    assert b["par_verdict"]["contre"]["esperance_r"] == -1.0 and b["ecart_soutient_contre_r"] == 1.5
    assert (b["non_declenches"], b["en_attente"], b["sans_forward_test"]) == (1, 1, 1)
    text = R.bilan_text(b)
    assert "soutient : 2 trades, +0,50 R, 50 % gagnants · échantillon faible" in text and "+1,50 R" in text


def test_routes_du_labo(scans, tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_LAB_API_KEY", KEY)
    monkeypatch.setattr(main, "research_book", R.ResearchBook(str(tmp_path / "r.json")))
    monkeypatch.setattr(scheduler, "paper_book", paper.PaperBook(None))
    client = TestClient(main.app)
    assert client.get("/api/lab/scan").status_code == 401
    today = datetime.now(timezone.utc).isoformat(timespec="seconds")
    scans.write_text(scan_line(today) + "\n")
    got = client.get("/api/lab/scan", headers=H).json()
    assert got["du_jour"] is True and [p["symbol"] for p in got["scan"]["picks"]] == ["ABC", "XYZ"]
    assert client.post("/api/lab/recherche", headers=H, json=[verdict("ZZZ")]).status_code == 422
    assert client.post("/api/lab/recherche", headers=H, json={"symbol": "ABC"}).status_code == 422
    r = client.post("/api/lab/recherche", headers=H, json=[verdict()])
    assert r.status_code == 200 and r.json()["enregistres"] == 1
    assert client.get("/api/lab/scan", headers=H).json()["verdicts"][0]["symbol"] == "ABC"
    b = client.get("/api/lab/recherche", headers=H).json()
    assert b["verdicts"] == 1 and b["sans_forward_test"] == 1 and "Recherche avant l'ouverture" in b["text"]
