"""Étude des 8-K sur plusieurs jours, collecte seulement (etude8k.py) et ses sources (edgar.py, alpaca.py) : index
EDGAR, en-têtes, pages de garde, classement hors séance, collecte complète, état et texte."""
import gzip
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.core.http import SourceError
from app.lab import etude8k as E
from app.lab import sec8k as S
from app.lab import worker as W
from app.lab.db import LabDB
from app.notify.discord import split_message
from app.sources import alpaca, edgar

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 25, 2, 0, tzinfo=timezone.utc)


def ny(y, m, d, hh, mm) -> int:
    return int(datetime(y, m, d, hh, mm, tzinfo=NY).timestamp())


# --- Sources --------------------------------------------------------------------------------

MASTER = """Description:           Master Index of EDGAR Dissemination Feed
Last Data Received:    March 31, 2021

CIK|Company Name|Form Type|Date Filed|Filename
--------------------------------------------------------------------------------
320193|Apple Inc.|8-K|2021-01-27|edgar/data/320193/0001193125-21-020184.txt
320193|Apple Inc.|10-Q|2021-01-28|edgar/data/320193/0000320193-21-000010.txt
99999|Old Corp|25-NSE|2021-02-01|edgar/data/99999/0000876661-21-000123.txt
88888|Some Co|8-K/A|2021-03-02|edgar/data/88888/0000088888-21-000005.txt
"""


def test_index_master_garde_les_8k_et_les_radiations():
    for raw in (MASTER, MASTER.encode("latin-1"), gzip.compress(MASTER.encode("latin-1"))):
        rows = E.edgar.parse_master(raw)
        assert [(r["accession"], r["form"]) for r in rows] == [
            ("0001193125-21-020184", "8-K"), ("0000876661-21-000123", "25-NSE"), ("0000088888-21-000005", "8-K/A")]
    assert rows[0] == {"accession": "0001193125-21-020184", "cik": 320193, "form": "8-K", "date_filed": "2021-01-27",
                       "company": "Apple Inc."}
    assert edgar.master_url(2021, 1) == "https://www.sec.gov/Archives/edgar/full-index/2021/QTR1/master.gz"


def test_entete_acceptance_datetime():
    assert edgar.header_url(320193, "0000320193-23-000064") == \
        "https://www.sec.gov/Archives/edgar/data/320193/000032019323000064/0000320193-23-000064.hdr.sgml"
    head = "<SEC-HEADER>0000320193-23-000064.hdr.sgml : 20230505\n<ACCEPTANCE-DATETIME>20230504180352\n<TYPE>10-Q"
    assert edgar.parse_header(head) == ny(2023, 5, 4, 18, 3) + 52
    assert edgar.parse_header("<SEC-HEADER>rien</SEC-HEADER>") is None and edgar.parse_header(None) is None


def test_symboles_de_la_page_de_garde():
    html = ('<td><ix:nonNumeric contextRef="c-1" name="dei:TradingSymbol" id="f-5">ABC</ix:nonNumeric></td>'
            '<td><ix:nonNumeric name="dei:TradingSymbol" contextRef="c-2"><span>BRK.B</span></ix:nonNumeric></td>'
            '<ix:nonNumeric name="dei:SecurityExchangeName" contextRef="c-1">NASDAQ</ix:nonNumeric>'
            '<ix:nonNumeric name="dei:TradingSymbol" contextRef="c-3">ABC</ix:nonNumeric>')
    assert edgar.parse_symbols(html) == ["ABC", "BRK-B"]
    assert edgar.parse_symbols("<html>pas de XBRL</html>") == [] and edgar.parse_symbols(None) == []
    assert [E.alpaca_symbol(t) for t in ("ABC", "BRK-B", "ABC-WT", "XYZ.PRA", "abcdef", None)] == \
        ["ABC", "BRK.B", None, None, None, None]


def test_submissions_garde_document_principal_et_sic():
    block = {"accessionNumber": ["a1"], "form": ["8-K"], "filingDate": ["2026-07-28"],
             "acceptanceDateTime": ["2026-07-28T10:58:13.000Z"], "items": ["2.02"], "primaryDocument": ["abc.htm"]}
    assert edgar.parse_filings(block)[0]["primary_doc"] == "abc.htm"


async def test_bougies_alpaca_avec_asof(monkeypatch):
    seen = []

    async def request(method, url, params=None, body=None, transport=None):
        seen.append(params)
        return {"bars": {"OLD": [{"t": "2024-03-05T05:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10}]}}

    monkeypatch.setattr(alpaca, "_request", request)
    start, end = datetime(2024, 3, 1, tzinfo=timezone.utc), datetime(2024, 3, 8, tzinfo=timezone.utc)
    await alpaca.bars_range(["OLD"], 1440, start, end, asof="2024-03-05")
    await alpaca.bars_range(["ABC"], 1440, start, end)
    assert seen[0]["asof"] == "2024-03-05" and "asof" not in seen[1]


# --- Fonctions pures ------------------------------------------------------------------------

def test_trimestres_lus():
    qs = E.quarters(date(2026, 10, 25))
    assert qs[0] == (2020, 4) and qs[-1] == (2026, 4) and len(qs) == 25
    assert E.quarter_end(2026, 3) == date(2026, 9, 30) and E.quarter_end(2020, 4) == date(2020, 12, 31)


def test_classement_hors_seance():
    assert E.session_class(ny(2026, 7, 28, 7, 0)) == ("hors", "2026-07-28")
    assert E.session_class(ny(2026, 7, 28, 9, 29)) == ("hors", "2026-07-28")
    assert E.session_class(ny(2026, 7, 28, 9, 30)) == ("seance", "2026-07-28")
    assert E.session_class(ny(2026, 7, 28, 16, 0)) == ("hors", "2026-07-29")   # après la clôture : J0 = lendemain
    assert E.session_class(ny(2026, 7, 31, 18, 0)) == ("hors", "2026-08-03")   # vendredi soir → lundi
    assert E.session_class(ny(2026, 11, 27, 13, 30)) == ("hors", "2026-11-30")  # clôture anticipée à 13 h


def test_heure_ambigue():
    assert E.ambiguous(ny(2026, 7, 28, 11, 0))       # lu à 11 h, peut-être déposé à 7 h
    assert E.ambiguous(ny(2026, 7, 28, 18, 0))       # lu à 18 h, peut-être déposé à 14 h pendant la séance
    assert not E.ambiguous(ny(2026, 7, 28, 20, 30))  # 20 h 30 ou 16 h 30 : hors séance, même J0
    assert not E.ambiguous(ny(2026, 7, 28, 7, 0))    # 3 h : EDGAR fermé, l'heure lue est juste
    assert not E.ambiguous(ny(2026, 7, 28, 15, 0))   # 15 h ou 11 h : pendant la séance dans les deux cas
    assert not E.ambiguous(None)


# --- Collecte -------------------------------------------------------------------------------

FILINGS = {
    10: [{"accession": "a1", "form": "8-K", "items": ["2.02", "9.01"], "accepted_ts": ny(2026, 7, 28, 11, 0),
          "filing_date": "2026-07-28", "suspect": False, "primary_doc": "a1.htm"},
         {"accession": "a2", "form": "8-K", "items": ["1.01"], "accepted_ts": ny(2026, 7, 28, 20, 30),
          "filing_date": "2026-07-29", "suspect": False, "primary_doc": "a2.htm"},
         {"accession": "a3", "form": "8-K", "items": ["8.01"], "accepted_ts": ny(2026, 7, 30, 7, 0),
          "filing_date": "2026-07-30", "suspect": False, "primary_doc": "a3.htm"},
         {"accession": "a4", "form": "8-K", "items": ["5.02"], "accepted_ts": ny(2026, 7, 30, 21, 0),
          "filing_date": "2026-07-30", "suspect": True, "primary_doc": "a4.htm"},
         {"accession": "a5", "form": "10-Q", "items": [], "accepted_ts": ny(2026, 7, 30, 21, 0),
          "filing_date": "2026-07-30", "suspect": True, "primary_doc": "a5.htm"}],
    20: [{"accession": "b1", "form": "8-K", "items": ["2.02"], "accepted_ts": ny(2024, 3, 5, 18, 10),
          "filing_date": "2024-03-05", "suspect": False, "primary_doc": "b1.htm"}],
}
HEADERS = {"a1": ny(2026, 7, 28, 7, 0), "a3": ny(2026, 7, 30, 7, 0), "a4": ny(2026, 7, 30, 17, 0),
           "b1": ny(2024, 3, 5, 18, 10)}


@pytest.fixture
def fake(monkeypatch):
    calls = []

    async def master_index(y, q):
        calls.append(f"index:{y}Q{q}")
        if (y, q) == (2026, 3):
            return [{"accession": "a1", "cik": 10, "form": "8-K", "date_filed": "2026-07-28", "company": "ABC Inc"},
                    {"accession": "c1", "cik": 30, "form": "8-K", "date_filed": "2026-07-28", "company": "Err Co"},
                    {"accession": "x1", "cik": 40, "form": "8-K/A", "date_filed": "2026-07-28", "company": "Amend"}]
        if (y, q) == (2024, 1):
            return [{"accession": "b1", "cik": 20, "form": "8-K", "date_filed": "2024-03-05", "company": "Old Corp"},
                    {"accession": "b9", "cik": 20, "form": "25-NSE", "date_filed": "2024-06-03", "company": "Old Corp"}]
        return []

    async def submissions(cik):
        calls.append(f"sub:{cik}")
        if cik == 30:
            raise SourceError("HTTP 404")
        return {"recent": FILINGS[cik], "files": [], "oldest": "2020-11-01", "name": f"CIK {cik}",
                "tickers": ["ABC", "ABC-WT"] if cik == 10 else [], "exchanges": ["Nasdaq"], "sic": "3571"}

    async def page(name):  # pragma: no cover - aucune page d'archive utile ici
        calls.append(f"page:{name}")
        return []

    async def accepted_header(cik, acc):
        calls.append(f"entete:{acc}")
        return HEADERS.get(acc)

    async def trading_symbols(cik, acc, doc):
        calls.append(f"garde:{acc}")
        return ["OLD"]

    async def bars_range(symbols, tf, start, end, call=None, asof=None, **kw):
        calls.append(f"bougies:{','.join(symbols)}@{asof}")
        bar = {"t": ny(2024, 3, 5, 0, 0), "o": 10.0, "h": 11.0, "l": 9.0, "c": 10.5, "v": 1000.0}
        return {s: ([] if s == "SPY" and asof else [bar]) for s in symbols}

    monkeypatch.setattr(S, "SEC_LIMIT", None)
    monkeypatch.setattr(E, "CALIBRATION_N", 1)
    monkeypatch.setattr(edgar, "master_index", master_index)
    monkeypatch.setattr(edgar, "submissions", submissions)
    monkeypatch.setattr(edgar, "page", page)
    monkeypatch.setattr(edgar, "accepted_header", accepted_header)
    monkeypatch.setattr(edgar, "trading_symbols", trading_symbols)
    monkeypatch.setattr(alpaca, "bars_range", bars_range)
    monkeypatch.setattr(alpaca, "configured", lambda: True)
    monkeypatch.setenv(edgar.KEY_NAME, "Test Labo test@example.com")
    return calls


async def test_collecte_complete_puis_rien_a_relire(fake):
    db = LabDB(":memory:")
    rep = await E.collect(db, NOW, 100, 60)
    assert rep["trimestres"] == 25 and rep["cik"] == 2 and rep["erreurs"] == 1
    assert sum(c.startswith("index:") for c in fake) == 25
    # 8-K/A seul (CIK 40) : pas un déposant de 8-K. Jamais lus d'abord, les plus récents d'abord.
    assert [c for c in fake if c.startswith("sub:")] == ["sub:10", "sub:30", "sub:20"]
    # Contrôles (retard certain a4, heure juste a3), puis les 2.02/1.01 ambigus (a1 lu à 11 h ; b1 lu à 18 h 10,
    # peut-être 13 h 10 pendant la séance). a2 (20 h 30 ou 16 h 30) n'est pas ambigu ; un 10-Q n'est pas gardé.
    assert [c for c in fake if c.startswith("entete:")] == ["entete:a4", "entete:a3", "entete:a1", "entete:b1"]
    assert [c for c in fake if c.startswith("garde:")] == ["garde:b1"]  # CIK 20 sans ticker actuel
    assert db.conn.execute("SELECT COUNT(*) FROM e8k_filings").fetchone()[0] == 5
    assert db.conn.execute("SELECT sic, tickers FROM e8k_ciks WHERE cik = 10").fetchone() == ("3571", '["ABC", "ABC-WT"]')
    assert set(db.conn.execute("SELECT cik, symbol, source, asof FROM e8k_tickers")) == {
        (10, "ABC", "sec_actuel", ""), (20, "OLD", "couverture", "2024-03-05")}
    assert sorted(c for c in fake if c.startswith("bougies:")) == ["bougies:ABC,SPY@None", "bougies:OLD@2024-03-05"]
    assert set(db.conn.execute("SELECT serie, status, n FROM e8k_series")) == {
        ("ABC", "ok", 1), ("SPY", "ok", 1), ("OLD@2024-03-05", "ok", 1)}
    assert db.conn.execute("SELECT d FROM e8k_bars WHERE serie = 'ABC'").fetchone()[0] == date(2024, 3, 5).toordinal()
    st = E.cached(db.conn)
    assert st["entetes"]["fiable"] and st["entetes"]["ambigus_lus"] == 2
    assert st["tickers"]["cik_eligibles"] == 2 and st["tickers"]["cik_eligibles_avec_ticker"] == 2
    assert st["eligibles_par_annee"] == {"2024": {"depots": 1, "cik": 1}, "2026": {"depots": 2, "cik": 1}}
    assert st["reste"] == {"trimestres": 0, "cik": 1, "entetes": 0, "pages_de_garde": 0, "series": 0}
    assert not st["complete"]
    # Deuxième passage : trimestres clos ou relus il y a moins d'une semaine, CIK lus à jour, séries figées.
    fake.clear()
    await E.collect(db, NOW, 100, 60)
    assert fake == ["sub:30"]


async def test_budget_sec_partage(fake):
    db = LabDB(":memory:")
    rep = await E.collect(db, NOW, 10, 0)
    assert rep["appels_sec"] == 10 and rep["trimestres"] == 10 and not any(c.startswith("sub:") for c in fake)


async def test_sans_cle_sec_rien_n_est_appele(fake, monkeypatch):
    monkeypatch.delenv(edgar.KEY_NAME)
    monkeypatch.setattr(alpaca, "configured", lambda: False)
    db = LabDB(":memory:")
    rep = await E.collect(db, NOW, 100, 60)
    assert fake == [] and "absent" in rep["note"]


async def test_controle_rate_arrete_les_entetes(fake):
    HEADERS["a3"] = ny(2026, 7, 30, 9, 0)  # l'en-tête ne confirme pas l'heure juste certaine
    try:
        db = LabDB(":memory:")
        await E.collect(db, NOW, 100, 0)
        assert E.cached(db.conn)["entetes"]["fiable"] is False
        assert E.exact_queue(db.conn) == []
    finally:
        HEADERS["a3"] = ny(2026, 7, 30, 7, 0)


async def test_texte_discord(fake):
    assert "pas encore commencée" in E.text(None)
    db = LabDB(":memory:")
    await E.collect(db, NOW, 100, 60)
    txt = E.text(E.cached(db.conn))
    assert "collecte" in txt and "sans aucun calcul" in txt and "Collecte en cours" in txt
    assert all(len(part) <= 2000 for part in split_message(txt))


def test_budgets_du_worker():
    for period, b in W.LAB2_BUDGET.items():
        assert "e8k_calls" in b and "e8k_seconds" in b
    assert W.LAB2_BUDGET["seance"]["e8k_calls"] == 0 and W.LAB2_BUDGET["seance"]["e8k_seconds"] == 0
