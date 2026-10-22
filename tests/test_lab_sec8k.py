"""Mesure 8-K (sec8k.py) et source EDGAR (edgar.py) : fenêtre d'avant 9 h 45, groupes, volume égal, collecte, texte."""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.core.http import SourceError
from app.lab import sec8k as S
from app.lab import seal as SEAL
from app.lab import worker as W
from app.lab.db import LabDB
from app.notify.discord import split_message
from app.sources import edgar

NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 22, 2, 0, tzinfo=timezone.utc)


def ny(y, m, d, hh, mm) -> int:
    return int(datetime(y, m, d, hh, mm, tzinfo=NY).timestamp())


# --- Source EDGAR ---------------------------------------------------------------------------

def test_parse_filings_garde_les_8k_et_6k_avec_items_et_heure():
    block = {"accessionNumber": ["a1", "a2", "a3", "a4"], "form": ["8-K", "10-Q", "6-K", "8-K/A"],
             "filingDate": ["2026-07-28", "2026-07-30", "2026-07-30", "2026-07-30"],
             "acceptanceDateTime": ["2026-07-28T10:58:13.000Z", "2026-07-30T20:00:00.000Z", "", None],
             "items": ["2.02,9.01", "", "", "5.02"]}
    out = edgar.parse_filings(block)
    assert [f["accession"] for f in out] == ["a1", "a3", "a4"]
    assert out[0]["items"] == ["2.02", "9.01"]
    assert out[0]["accepted_ts"] == ny(2026, 7, 28, 6, 58) + 13
    assert out[1]["accepted_ts"] is None and out[1]["items"] == []
    assert not out[0]["suspect"]


def test_heure_douteuse_apres_17h30_le_jour_meme():
    # Cas réel recoupé avec le flux Atom : 16 h 30 New York, JSON à 00:30 Z le lendemain.
    ts = edgar.accepted_ts("2026-07-31T00:30:28.000Z")
    assert edgar.suspicious(ts, "2026-07-30")
    assert not edgar.suspicious(edgar.accepted_ts("2026-07-28T10:58:13.000Z"), "2026-07-28")
    assert not edgar.suspicious(None, "2026-07-30")


def test_autre_heure_si_le_json_est_en_retard():
    assert edgar.shifted_back(ny(2026, 10, 19, 11, 0)) == ny(2026, 10, 19, 7, 0)  # été : 4 h
    assert edgar.shifted_back(ny(2026, 1, 12, 12, 0)) == ny(2026, 1, 12, 7, 0)    # hiver : 5 h
    assert edgar.shifted_back(ny(2026, 10, 19, 9, 0)) is None   # 5 h : EDGAR fermé, l'heure lue est juste
    assert edgar.shifted_back(ny(2026, 10, 20, 1, 0)) == ny(2026, 10, 19, 21, 0)
    assert edgar.shifted_back(None) is None


def test_page_index_heure_exacte():
    assert edgar.index_url(320193, "0000320193-25-000071") == \
        "https://www.sec.gov/Archives/edgar/data/320193/000032019325000071/0000320193-25-000071-index.htm"
    html = ('<div class="infoHead">Filing Date</div><div class="info">2025-07-31</div>\n'
            '<div class="infoHead">Accepted</div>\n<div class="info">2025-07-31 16:30:45</div>')
    assert edgar.parse_accepted(html) == ny(2025, 7, 31, 16, 30) + 45
    assert edgar.parse_accepted("<html>rien</html>") is None and edgar.parse_accepted(None) is None


def test_tickers_et_pages():
    raw = {"0": {"cik_str": 320193, "ticker": "AAPL"}, "1": {"cik_str": 1067983, "ticker": "BRK-B"},
           "2": {"cik_str": 999, "ticker": "AAPL"}, "3": {"ticker": "XX"}}
    assert edgar.parse_tickers(raw) == {"AAPL": 320193, "BRK-B": 1067983}
    assert edgar.ticker("brk.b") == "BRK-B"
    files = [{"name": "p1", "filingFrom": "2020-01-01", "filingTo": "2023-12-31"},
             {"name": "p2", "filingFrom": "2024-01-01", "filingTo": "2025-02-01"}, {"name": "bad"}]
    assert edgar.pages_needed(files, date(2024, 10, 1)) == ["p2"]
    assert edgar.accepted_ts("pas une date") is None


def test_user_agent_sans_accent(monkeypatch):
    import httpx
    monkeypatch.setenv(edgar.KEY_NAME, "João Test test@example.com")
    assert edgar.user_agent() == "Joao Test test@example.com"
    httpx.Request("GET", edgar.TICKERS_URL, headers={"User-Agent": edgar.user_agent()})  # ne lève plus


def test_sans_user_agent_aucun_appel(monkeypatch):
    monkeypatch.delenv(edgar.KEY_NAME, raising=False)
    with pytest.raises(SourceError):
        edgar.user_agent()


# --- Fonctions pures ------------------------------------------------------------------------

def test_fenetre_de_la_seance():
    # Lundi 19 octobre 2026. Vendredi 16 h 30 → lundi ; lundi 6 h 58 et 9 h 44 → lundi ; 9 h 45 et pendant la séance → rien.
    assert S.window_day(ny(2026, 10, 16, 16, 30)) == "2026-10-19"
    assert S.window_day(ny(2026, 10, 18, 12, 0)) == "2026-10-19"
    assert S.window_day(ny(2026, 10, 19, 6, 58)) == "2026-10-19"
    assert S.window_day(ny(2026, 10, 19, 9, 44)) == "2026-10-19"
    assert S.window_day(ny(2026, 10, 19, 9, 45)) is None
    assert S.window_day(ny(2026, 10, 19, 13, 0)) is None
    assert S.window_day(ny(2026, 10, 16, 15, 59)) is None
    assert S.window_day(ny(2026, 10, 19, 16, 0)) == "2026-10-20"


def test_groupes_par_priorite():
    assert S.group({"2.02", "1.01", "9.01"}) == "resultats"
    assert S.group({"1.01", "8.01"}) == "contrat"
    assert S.group({"7.01"}) == "communique"
    assert S.group({"5.02", "9.01"}) == "autre"


def test_effectif_requis_du_debat():
    assert S.required_n(1.2, 1.96) == 2258
    assert S.required_n(1.2, S.Z_LABO) == 5227
    assert S.required_n(None, 1.96) is None


def test_ecart_a_volume_egal():
    # Tranche 1 : +1 d'écart (n 2/2), tranche 2 : 0 d'écart (n 2/2) → moyenne pondérée 0,5.
    out = S.stratified([([1.0, 2.0], [0.0, 1.0]), ([0.0, 1.0], [0.0, 1.0]), ([5.0], [0.0, 1.0])])
    assert out["ecart"] == 0.5 and out["n_avec"] == 4 and out["n_sans"] == 4
    assert S.stratified([])["ecart"] is None
    assert S.vol_bucket(0.3) == "vol15 0,25-0,5" and S.vol_bucket(None) == S.VOL_INCONNU


# --- Mesure ---------------------------------------------------------------------------------

def _sym(db, symbol, cik, status="ok", covered="2026-01-01", fetched=None):
    db.conn.execute("INSERT INTO sec_symbols (symbol, cik, status, covered_from, fetched_ts) VALUES (?, ?, ?, ?, ?)",
                    (symbol, cik, status, covered, fetched if fetched is not None else int(NOW.timestamp())))


def _filing(db, acc, cik, ts, items="2.02", form="8-K"):
    db.conn.execute("INSERT INTO sec_filings (accession, cik, form, items, accepted_ts, filing_date, suspect) "
                    "VALUES (?, ?, ?, ?, ?, '2026-10-19', 0)", (acc, cik, form, items, ts))


def _trade(db, symbol, day, gross, vol, strategy="orb15", exit_reason="tp1", mfe=1.5, mae=-0.3, temoin=0.0):
    db.conn.execute("INSERT INTO strat_trades (origin, market, symbol, day, strategy, slot, triggered, r_gross, r_20e, "
                    "temoin_r, mfe_r, mae_r, exit_reason, vol15_adv) VALUES ('live', 'stock', ?, ?, ?, 0, 1, ?, ?, ?, ?, "
                    "?, ?, ?)", (symbol, day, strategy, gross, gross - 0.2, temoin, mfe, mae, exit_reason, vol))


def _move(db, day, symbol, up, gap):
    db.conn.execute("INSERT INTO day_moves (market, day, symbol, up_pct, down_pct, gap_pct) "
                    "VALUES ('stock', ?, ?, ?, -1.0, ?)", (day, symbol, up, gap))


@pytest.fixture
def db():
    db = LabDB(":memory:")
    day = "2026-10-19"
    _sym(db, "AAA", 1)          # 8-K 2.02 lundi 7 h : « resultats »
    _sym(db, "BBB", 2)          # 8-K pendant la séance de lundi : « aucun »
    _sym(db, "CCC", 3, status="sans_cik")
    _sym(db, "DDD", 4)          # que des 6-K : émetteur étranger
    _sym(db, "EEE", 5, covered="2026-10-18")   # couvert trop tard
    _sym(db, "FFF", 6, fetched=ny(2026, 10, 19, 9, 0))  # lu avant le 9 h 45 de la séance
    _sym(db, "HHH", 8)          # 8-K lu à 11 h : peut-être déposé à 7 h (JSON en retard), séance ambiguë
    _filing(db, "a1", 1, ny(2026, 10, 19, 7, 0))
    _filing(db, "b1", 2, ny(2026, 10, 19, 15, 0), items="1.01")  # même en retard (11 h), pendant la séance
    _filing(db, "h1", 8, ny(2026, 10, 19, 11, 0))
    _filing(db, "d1", 4, ny(2026, 10, 19, 7, 0), form="6-K", items="")
    for sym, gross, vol, ex, mfe in (("AAA", 2.0, 0.6, "tp2", 2.5), ("BBB", -1.0, 0.6, "stop", 0.4),
                                     ("CCC", 1.0, 0.6, "tp1", 1.2), ("DDD", 1.0, 0.6, "tp1", 1.2),
                                     ("EEE", 1.0, 0.6, "tp1", 1.2), ("FFF", 1.0, 0.6, "tp1", 1.2),
                                     ("GGG", 1.0, 0.6, "tp1", 1.2), ("HHH", 1.0, 0.6, "tp1", 1.2)):
        _trade(db, sym, day, gross, vol, exit_reason=ex, mfe=mfe)
    _move(db, day, "AAA", 12.0, 3.0)
    _move(db, day, "BBB", 2.0, 3.0)
    _move(db, day, "HHH", 15.0, 3.0)
    lo, _ = SEAL.bounds()
    _move(db, lo, "AAA", 50.0, 3.0)  # bloc scellé : jamais lu
    _filing(db, "a0", 1, int(datetime.fromisoformat(lo + "T07:00:00").replace(tzinfo=NY).timestamp()))
    return db


def test_rapport_groupes_et_exclusions(db):
    data = S.report(db.conn)
    t = data["tradable"]["orb15"]
    assert t["exclus"] == {"sans_cik": 1, "etranger": 1, "non_couvert": 2, "sans_donnee": 1, "heure_ambigue": 1}
    cell = t["tranches"]["vol15 0,5-1"]
    assert cell["resultats"]["n"] == 1 and cell["resultats"]["r_brut"] == 2.0 and cell["resultats"]["r_20e"] == 1.8
    assert cell["aucun"]["n"] == 1 and cell["aucun"]["part_stop"] == 1.0 and cell["aucun"]["part_fausse_cassure"] == 1.0
    assert t["par_groupe"]["contrat"]["n"] == 0  # le 1.01 de BBB est pendant la séance
    assert data["qualite"]["8k"] == 4 and data["qualite"]["8k_fenetre"] == 2 and data["qualite"]["8k_heure_ambigue"] == 1
    base = data["taux_de_base"]["par_gap"]["gap +2 à +5 %"]
    assert base["resultats"] == {"n": 1, "zone_pct": 100.0, "plus10_pct": 100.0}  # la séance scellée n'est pas lue
    assert base["aucun"]["n"] == 1 and base["aucun"]["plus10_pct"] == 0.0  # HHH (+15 %) écarté, pas dans « aucun »
    assert data["taux_de_base"]["exclus"]["heure_ambigue"] == 1


def test_heures_new_york_et_valeurs_a_lire(db):
    data = S.report(db.conn)
    q = data["qualite"]
    assert q["heures_ny"][7] == 2 and q["heures_ny"][11] == 1 and q["heures_ny"][15] == 1  # 6-K non comptés
    assert sum(q["heures_ny"]) == 4
    assert q["heures_ny_meme_jour"][7] == 1 and sum(q["heures_ny_meme_jour"]) == 3  # a0 : déposé un autre jour
    assert data["valeurs"]["a_lire"] == 1  # GGG : tradé, jamais lu
    txt = S.text(data)
    assert "7 h 2 · 11 h 1 · 15 h 1" in txt and "1 jamais lues" in txt and "1 à heure ambiguë" in txt


def test_step_met_en_cache_puis_attend(db):
    assert S.step(db, NOW, 3600) == 4
    assert S.cached(db.conn)["qualite"]["8k"] == 4
    assert S.step(db, NOW, 3600) is None


def test_texte_discord(db):
    assert "pas encore calculée" in S.text(None)
    txt = S.text(S.report(db.conn))
    assert "orb15" in txt and "volume égal" in txt and "sans essai" in txt
    assert all(len(part) <= 2000 for part in split_message(txt))


# --- Collecte -------------------------------------------------------------------------------

@pytest.fixture
def fake_sec(monkeypatch):
    calls = []

    async def tickers():
        calls.append("tickers")
        return {"AAA": 1, "BBB": 2}

    async def submissions(cik):
        calls.append(f"sub:{cik}")
        if cik == 2:
            raise SourceError("HTTP 404")
        return {"recent": [{"accession": "r1", "form": "8-K", "items": ["2.02"],
                            "accepted_ts": ny(2026, 10, 19, 7, 0), "filing_date": "2026-10-19", "suspect": False}],
                "files": [{"name": "old.json", "filingFrom": "2020-01-01", "filingTo": "2026-01-31"}],
                "oldest": "2026-02-01"}

    async def page(name):
        calls.append(f"page:{name}")
        return [{"accession": "o1", "form": "8-K", "items": ["8.01"], "accepted_ts": ny(2025, 3, 3, 7, 0),
                 "filing_date": "2025-03-03", "suspect": False}]

    async def accepted_exact(cik, accession):
        calls.append(f"exact:{accession}")
        return EXACT.get(accession)

    monkeypatch.setattr(S, "SEC_LIMIT", None)
    monkeypatch.setattr(edgar, "accepted_exact", accepted_exact)
    monkeypatch.setattr(edgar, "tickers", tickers)
    monkeypatch.setattr(edgar, "submissions", submissions)
    monkeypatch.setattr(edgar, "page", page)
    monkeypatch.setenv(edgar.KEY_NAME, "Test Labo test@example.com")
    return calls


async def test_collecte(fake_sec):
    db = LabDB(":memory:")
    for sym in ("AAA", "BBB", "ZZZ"):
        _move(db, "2026-01-05", sym, 1.0, 0.0)
        _move(db, "2026-10-19", sym, 1.0, 0.0)
    rep = await S.fetch(db, NOW, 50)
    assert rep == {"appels": 5, "valeurs": 1, "sans_cik": 1, "erreurs": 1, "heures_exactes": 1}
    # r1 (7 h New York) : heure juste certaine, lue comme contrôle ; o1 est d'avant la première séance mesurée.
    assert fake_sec == ["tickers", "sub:1", "page:old.json", "sub:2", "exact:r1"]
    row = db.conn.execute("SELECT cik, status, covered_from FROM sec_symbols WHERE symbol = 'AAA'").fetchone()
    assert row == (1, "ok", "2025-12-29")  # toutes les pages lues : couvert depuis la marge de 7 jours
    assert db.conn.execute("SELECT COUNT(*) FROM sec_filings").fetchone()[0] == 2
    assert db.conn.execute("SELECT status, attempts FROM sec_symbols WHERE symbol = 'BBB'").fetchone() == ("erreur", 1)
    # Deuxième passage : AAA à jour, ZZZ sans CIK depuis la dernière liste, BBB réessayé.
    fake_sec.clear()
    rep = await S.fetch(db, NOW, 50)
    assert fake_sec == ["sub:2"] and rep["erreurs"] == 1
    # Une séance de plus : AAA n'est relue qu'une fois sa lecture vieille de plus de REFRESH_DAYS jours.
    _move(db, "2026-10-23", "AAA", 1.0, 0.0)
    fake_sec.clear()
    await S.fetch(db, NOW.replace(day=24), 50)
    assert "sub:1" not in fake_sec
    fake_sec.clear()
    await S.fetch(db, NOW.replace(day=30), 50)
    assert "sub:1" in fake_sec


EXACT: dict[str, int] = {}


async def test_heures_exactes_controles_puis_ambigus(fake_sec):
    db = LabDB(":memory:")
    _move(db, "2026-10-19", "AAA", 1.0, 0.0)
    _move(db, "2026-10-20", "AAA", 1.0, 0.0)
    _sym(db, "AAA", 1)
    _filing(db, "amb", 1, ny(2026, 10, 19, 11, 0))         # 11 h : peut-être 7 h
    _filing(db, "ret", 1, ny(2026, 10, 19, 20, 30))        # 20 h 30 le jour du dépôt : retard certain
    _filing(db, "jus", 1, ny(2026, 10, 19, 8, 0))          # 8 h : heure juste certaine
    _filing(db, "seance", 1, ny(2026, 10, 19, 15, 0))      # 15 h : pendant la séance de toute façon
    _filing(db, "nuit", 1, ny(2026, 10, 20, 4, 0))         # 4 h : ni 4 h ni minuit possibles, pas un contrôle
    db.conn.execute("UPDATE sec_filings SET suspect = 1 WHERE accession = 'ret'")
    EXACT.update({"amb": ny(2026, 10, 19, 7, 0), "ret": ny(2026, 10, 19, 16, 30), "jus": ny(2026, 10, 19, 8, 0)})
    fake_sec.clear()
    rep = await S.fetch(db, NOW, 8)
    assert fake_sec == ["tickers", "exact:ret", "exact:jus", "exact:amb"] and rep["heures_exactes"] == 3
    rows = dict(db.conn.execute("SELECT accession, kind FROM sec_exact"))
    assert rows == {"ret": "controle_retard", "jus": "controle_juste", "amb": "ambigu"}
    fake_sec.clear()
    assert (await S.fetch(db, NOW, 8))["heures_exactes"] == 0 and fake_sec == []
    # Contrôle complet et raté (pages illisibles) : plus aucun ambigu n'est lu.
    db.conn.execute("DELETE FROM sec_exact WHERE accession = 'amb'")
    for i in range(S.CALIBRATION_N):
        for kind in ("controle_retard", "controle_juste"):
            db.conn.execute("INSERT INTO sec_exact (accession, kind, status) VALUES (?, ?, 'illisible')",
                            (f"{kind}{i}", kind))
    assert S._exact_queue(db.conn) == []
    # Un quart du budget au moins reste aux heures exactes.
    assert S.EXACT_SHARE == 4


def _controls(db, n_late, n_right, bad=0):
    for i in range(n_late):
        acc, ts = f"cr{i}", ny(2026, 9, 1 + i, 20, 30)
        db.conn.execute("DELETE FROM sec_filings WHERE accession = ?", (acc,))
        _filing(db, acc, 99, ts)
        true = ts - 4 * 3600 + (3600 if i < bad else 0)
        db.conn.execute("INSERT INTO sec_exact (accession, kind, accepted_ts, status) VALUES (?, 'controle_retard', ?, "
                        "'ok')", (acc, true))
    for i in range(n_right):
        acc, ts = f"cj{i}", ny(2026, 9, 1 + i, 7, 30)
        db.conn.execute("DELETE FROM sec_filings WHERE accession = ?", (acc,))
        _filing(db, acc, 99, ts)
        db.conn.execute("INSERT INTO sec_exact (accession, kind, accepted_ts, status) VALUES (?, 'controle_juste', ?, "
                        "'ok')", (acc, ts))


def _exact_hhh(db, ts):
    db.conn.execute("INSERT INTO sec_exact (accession, kind, accepted_ts, status) VALUES ('h1', 'ambigu', ?, 'ok')", (ts,))


def test_heure_exacte_avant_ouverture_utilisee(db):
    _controls(db, 10, 10)
    _exact_hhh(db, ny(2026, 10, 19, 7, 0))
    data = S.report(db.conn)
    hx = data["qualite"]["heure_exacte"]
    assert hx["fiable"] and hx["avant_ouverture"] == 1 and data["qualite"]["8k_heure_ambigue"] == 0
    t = data["tradable"]["orb15"]
    assert "heure_ambigue" not in t["exclus"] and t["par_groupe"]["resultats"]["n"] == 2  # AAA et HHH
    assert "1 avant l'ouverture" in S.text(data)


def test_heure_exacte_pendant_seance(db):
    _controls(db, 10, 10)
    _exact_hhh(db, ny(2026, 10, 19, 11, 0) + 30)
    data = S.report(db.conn)
    assert data["qualite"]["heure_exacte"]["pendant_seance"] == 1
    assert data["tradable"]["orb15"]["par_groupe"]["aucun"]["n"] == 2  # BBB et HHH


def test_heure_exacte_ignoree_sans_controle_ou_incoherente(db):
    _controls(db, 9, 10)  # un contrôle de trop peu
    _exact_hhh(db, ny(2026, 10, 19, 7, 0))
    data = S.report(db.conn)
    assert not data["qualite"]["heure_exacte"]["fiable"]
    assert data["tradable"]["orb15"]["exclus"]["heure_ambigue"] == 1 and "pas encore utilisée" in S.text(data)
    db.conn.execute("DELETE FROM sec_exact")
    _controls(db, 10, 10, bad=1)  # 9 retards confirmés sur 10 : sous les 95 %
    _exact_hhh(db, ny(2026, 10, 19, 7, 0))
    assert not S.report(db.conn)["qualite"]["heure_exacte"]["fiable"]
    db.conn.execute("DELETE FROM sec_exact")
    _controls(db, 10, 10)
    _exact_hhh(db, ny(2026, 10, 19, 8, 0))  # ni 11 h ni 7 h
    data = S.report(db.conn)
    assert data["qualite"]["heure_exacte"]["incoherentes"] == 1
    assert data["tradable"]["orb15"]["exclus"]["heure_ambigue"] == 1


async def test_collecte_sans_user_agent(monkeypatch):
    monkeypatch.delenv(edgar.KEY_NAME, raising=False)
    rep = await S.fetch(LabDB(":memory:"), NOW, 50)
    assert rep["appels"] == 0 and "absent" in rep["note"]
    assert (await S.fetch(LabDB(":memory:"), NOW, 0))["appels"] == 0


def test_budget_du_worker():
    assert W.LAB2_BUDGET["seance"]["sec_calls"] == 0
    assert all("sec_calls" in b for b in W.LAB2_BUDGET.values())
