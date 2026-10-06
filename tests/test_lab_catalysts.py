"""Fiches catalyseurs : mouvements du jour, départ, actus et leur position, contre-exemples, classement."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from app.core.http import SourceError
from app.lab import catalysts as CAT
from app.lab import collector as CO
from app.lab import sources as SRC
from app.lab.db import LabDB
from app.sources import finnhub
from tests.lab_fixtures import fill_crypto, fill_stock

DAY = date(2026, 9, 15)
NOW = datetime(2026, 10, 6, 2, tzinfo=timezone.utc)


def bar(t, o, h, low, c, v=1000.0):
    return {"t": t, "o": o, "h": h, "l": low, "c": c, "v": v}


# --- Fonctions pures ------------------------------------------------------------------------

def test_resume_de_journee():
    bars = [bar(0, 10, 10.5, 9.9, 10.4), bar(300, 10.4, 11.5, 10.3, 11.2), bar(600, 11.2, 11.3, 9.5, 9.8)]
    st = CAT.day_stats(bars, 9.0)
    assert st["up_pct"] == 15.0 and st["down_pct"] == -5.0 and st["gap_pct"] == pytest.approx(11.111, abs=1e-3)
    assert st["t_high"] == 300 and st["t_low"] == 600 and st["close"] == 9.8
    assert CAT.day_stats([], 9.0) is None


def test_classement_hausse_baisse_et_gap():
    up = CAT.classify({"open": 10, "high": 11, "low": 9.9, "up_pct": 10.0, "down_pct": -1.0, "prev_close": 10,
                       "t_high": 5, "t_low": 1})
    assert up["direction"] == "hausse" and up["amplitude_pct"] == 10.0 and up["ref"] == 10 and up["extreme_ts"] == 5
    down = CAT.classify({"open": 10, "high": 10.1, "low": 9.0, "up_pct": 1.0, "down_pct": -10.0, "prev_close": 10,
                         "t_high": 5, "t_low": 7})
    assert down["direction"] == "baisse" and down["extreme"] == 9.0
    # Gap de +12 % puis séance calme : fiche quand même, départ mesuré depuis la clôture de la veille.
    gap = CAT.classify({"open": 11.2, "high": 11.4, "low": 11.1, "up_pct": 1.8, "down_pct": -0.9, "prev_close": 10,
                        "t_high": 5, "t_low": 1})
    assert gap["direction"] == "hausse" and gap["ref"] == 10 and gap["amplitude_veille_pct"] == pytest.approx(14.0)
    assert CAT.classify({"open": 10, "high": 10.5, "low": 9.8, "up_pct": 5.0, "down_pct": -2.0, "prev_close": 10,
                         "t_high": 1, "t_low": 2}) is None


def test_depart_au_tiers_de_l_amplitude():
    bars = [bar(0, 10, 10.2, 10, 10.1), bar(300, 10.1, 10.5, 10, 10.4), bar(600, 10.4, 11.2, 10.4, 11.0)]
    assert CAT.departure(bars, 10.0, 11.2) == 300   # tiers de 10 → 11,2 = 10,4 : franchi à 300 (10,5)
    bars[1]["h"] = 10.39
    assert CAT.departure(bars, 10.0, 11.2) == 600
    assert CAT.departure(bars, 10.0, 9.0) is None  # baisse jamais franchie
    assert CAT.departure([bar(0, 10, 10, 9.6, 9.7)], 10.0, 9.0) == 0


def test_valeur_calme():
    assert CAT.quiet({"open": 10, "high": 10.2, "low": 9.9, "up_pct": 2.0, "down_pct": -1.0, "prev_close": 10.0})
    assert not CAT.quiet({"open": 10, "high": 10.2, "low": 9.9, "up_pct": 2.0, "down_pct": -1.0, "prev_close": 9.5})


def test_volume_relatif_des_30_premieres_minutes():
    bars = [bar(t, 1, 1, 1, 1, 100) for t in range(0, 3600, 300)]
    assert CAT.first_volume(bars, 0) == 600
    assert CAT.rvol_first(600, [300] * 20) == 2.0
    assert CAT.rvol_first(600, [300] * 3) is None  # trop peu d'historique


def test_position_des_actus():
    open_ts, start, extreme, close = 1000, 2000, 3000, 9000
    assert CAT.news_position(500, open_ts, start, extreme, close) == "avant_ouverture"
    assert CAT.news_position(1500, open_ts, start, extreme, close) == "avant_depart"
    assert CAT.news_position(2500, open_ts, start, extreme, close) == "pendant"
    assert CAT.news_position(3200, open_ts, start, extreme, close) == "pendant"   # bougie de l'extrême
    assert CAT.news_position(4000, open_ts, start, extreme, close) == "apres"
    assert CAT.news_position(4000, open_ts, None, None, close) == "seance"       # contre-exemple
    assert CAT.news_position(9500, open_ts, None, None, close) == "apres"


def test_tirage_des_contre_exemples_fixe_et_borne():
    days = [(date(2025, 1, 1) + timedelta(days=i)).isoformat() for i in range(1000)]
    picked = [d for d in days if CAT.counter_day(d)]
    assert 150 < len(picked) < 250 and picked == [d for d in days if CAT.counter_day(d)]
    syms = [f"S{i}" for i in range(50)]
    assert CAT.counter_pick("2025-01-01", syms) == CAT.counter_pick("2025-01-01", list(reversed(syms)))
    assert len(CAT.counter_pick("2025-01-01", syms)) == CAT.COUNTER_PER_DAY


def test_selection_des_actus():
    items = [{"title": f"Titre {i}", "published_ts": 1000 + i * 600, "source": "X", "url": "u"} for i in range(12)]
    items.append({"title": "titre 3", "published_ts": 99999, "source": "Y"})  # doublon
    items.append({"title": "", "published_ts": 5})
    out = CAT.select_news(items, 1000, 4000, 5000, 9000, limit=4)
    assert len(out) == 4 and [n["ts"] for n in out] == sorted(n["ts"] for n in out)
    assert {n["titre"] for n in out} == {"Titre 3", "Titre 4", "Titre 5", "Titre 6"}  # plus proches du départ
    assert all(n["position"] in CAT.POSITIONS for n in out)


# --- Base : mouvements et fiches ------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    d = LabDB(str(tmp_path / "lab.db"))
    fill_stock(d, ["AAA", "BBB", "CCC", "DDD"], CO.sessions_back(DAY, 25), DAY)
    o, c = CO.session_ts(DAY)
    bars = d.get_bars("stock", "AAA", 5, o, c)
    p = bars[0]["o"]
    ramp = []
    for i, b in enumerate(bars):
        level = p * (1 + min(i, 30) * 0.004)  # +12 % en 30 bougies, puis plat
        ramp.append({"t": b["t"], "o": level, "h": level * 1.001, "l": level * 0.999, "c": level, "v": 20000.0})
    d.put_bars("stock", "AAA", 5, ramp)
    d.replace_universe("stock", DAY.isoformat(), [{"symbol": s, "rank": i, "reason": "test", "metrics": {}}
                                                  for i, s in enumerate(["AAA", "BBB", "CCC", "DDD"], 1)])
    return d


def test_mouvements_du_jour_et_fiche(db, monkeypatch):
    monkeypatch.setattr(CAT, "counter_day", lambda day, every=5: False)
    out = CAT.compute_day(db, "stock", DAY)
    assert out == {"valeurs": 4, "fiches": 1}
    rows = db.conn.execute("SELECT symbol, up_pct, rvol30 FROM day_moves ORDER BY symbol").fetchall()
    assert [r[0] for r in rows] == ["AAA", "BBB", "CCC", "DDD"] and rows[0][1] > 11.5
    fiche = CAT.query(db.conn)[0]
    assert fiche["symbole"] == "AAA" and fiche["direction"] == "hausse" and fiche["genre"] == "mouvement"
    assert fiche["actus_etat"] == "a_chercher" and fiche["type"] == "à classer"
    o, _ = CO.session_ts(DAY)
    start = db.conn.execute("SELECT start_ts FROM catalysts").fetchone()[0]
    assert start == o + 10 * 300  # tiers de +12,1 % ≈ +4 % : franchi par la 11e bougie
    assert fiche["signaux"]["rvol30"] and fiche["signaux"]["rvol30"] > 1
    assert db.get("moves:stock", DAY.isoformat()) == {"valeurs": 4, "fiches": 1}


def test_contre_exemples_un_jour_tire(db, monkeypatch):
    monkeypatch.setattr(CAT, "counter_day", lambda day, every=5: True)
    monkeypatch.setattr(CAT, "quiet", lambda st, threshold=3.0: True)
    CAT.compute_day(db, "stock", DAY)
    kinds = sorted(r["genre"] for r in CAT.query(db.conn))
    assert kinds.count("mouvement") == 1 and kinds.count("contre_exemple") == 3
    assert all(r["direction"] is None for r in CAT.query(db.conn, kind="contre_exemple"))


def test_recalcul_garde_les_actus_et_le_type(db, monkeypatch):
    monkeypatch.setattr(CAT, "counter_day", lambda day, every=5: False)
    CAT.compute_day(db, "stock", DAY)
    cid = CAT.query(db.conn)[0]["id"]
    db.conn.execute("UPDATE catalysts SET news = '[{\"titre\": \"x\"}]', news_status = 'ok' WHERE id = ?", (cid,))
    assert CAT.set_catalyst_type(db, cid, "resultats")
    CAT.compute_day(db, "stock", DAY)
    fiche = CAT.query(db.conn)[0]
    assert fiche["id"] == cid and fiche["actus_etat"] == "ok" and fiche["type"] == "resultats" and fiche["classe_le"]
    assert db.conn.execute("SELECT COUNT(*) FROM day_moves").fetchone()[0] == 4


def test_classement_du_type(db, monkeypatch):
    monkeypatch.setattr(CAT, "counter_day", lambda day, every=5: False)
    CAT.compute_day(db, "stock", DAY)
    cid = CAT.query(db.conn)[0]["id"]
    with pytest.raises(ValueError, match="type inconnu"):
        CAT.set_catalyst_type(db, cid, "lune")
    assert CAT.set_catalyst_type(db, cid, None) and CAT.query(db.conn, unclassified=True)[0]["id"] == cid
    assert not CAT.set_catalyst_type(db, 999, "macro")


def test_jours_en_attente_et_rattrapage(db, monkeypatch):
    monkeypatch.setattr(CAT, "counter_day", lambda day, every=5: False)
    early = datetime.fromtimestamp(CO.session_ts(DAY)[1] + 60, timezone.utc)
    assert CAT.days_pending(db, "stock", early, 10) == []  # séance pas close depuis 30 min
    assert CAT.days_pending(db, "stock", NOW, 10) == [DAY]
    assert CAT.backfill_moves(db, NOW, 5) == 1
    assert CAT.days_pending(db, "stock", NOW, 10) == []


def test_crypto_sans_source_avec_btc(tmp_path):
    db = LabDB(str(tmp_path / "c.db"))
    fill_crypto(db, ["BTC-USDT", "ETH-USDT"], DAY - timedelta(days=25), DAY + timedelta(days=1))
    start = CO.utc_midnight(DAY)
    bars = db.get_bars("crypto", "ETH-USDT", 5, start, start + 86400)
    p = bars[0]["o"]
    db.put_bars("crypto", "ETH-USDT", 5, [{**b, "o": p * (1 - min(i, 40) * 0.0025), "h": p * (1 - min(i, 40) * 0.0025),
                                            "l": p * (1 - min(i + 1, 40) * 0.0025), "c": p * (1 - min(i + 1, 40) * 0.0025)}
                                           for i, b in enumerate(bars)])
    db.replace_universe("crypto", DAY.isoformat(), [{"symbol": s, "rank": 1, "reason": "t", "metrics": {}}
                                                    for s in ("BTC-USDT", "ETH-USDT")])
    CAT.compute_day(db, "crypto", DAY)
    fiche = CAT.query(db.conn, market="crypto")[0]
    assert fiche["symbole"] == "ETH-USDT" and fiche["direction"] == "baisse"
    assert fiche["actus_etat"] == "sans_source" and fiche["note"] == CAT.CRYPTO_NOTE
    assert "btc_jour_pct" in fiche["signaux"] and "btc_avant_depart_pct" in fiche["signaux"]


# --- Actus Finnhub (simulées) ---------------------------------------------------------------

@pytest.fixture
def news_env(db, monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "test")
    monkeypatch.setattr(CAT, "FINNHUB_LIMIT", SRC.RateLimiter(1000, 60.0))
    monkeypatch.setattr(CAT, "counter_day", lambda day, every=5: False)
    calls = {"news": [], "profile": []}
    o, _ = CO.session_ts(DAY)

    async def news(symbol, start, end):
        calls["news"].append((symbol, start, end))
        return [{"title": "Résultats au-dessus des attentes", "published_ts": o - 3600, "source": "Reuters", "url": "u"},
                {"title": "L'action s'envole", "published_ts": o + 3 * 3600, "source": "Yahoo", "url": "v"}]

    async def profile(symbol):
        calls["profile"].append(symbol)
        return {"industry": "Semiconductors"}

    monkeypatch.setattr(finnhub, "company_news_between", news)
    monkeypatch.setattr(finnhub, "profile", profile)
    CAT.compute_day(db, "stock", DAY)
    return calls


async def test_actus_recuperees_avec_position_et_secteur(db, news_env):
    rep = await CAT.fetch_news(db, NOW, 10)
    assert rep["fiches"] == 1 and rep["appels"] == 2
    fiche = CAT.query(db.conn)[0]
    assert fiche["actus_etat"] == "ok" and [n["position"] for n in fiche["actus"]] == ["avant_ouverture", "apres"]
    assert fiche["actus"][0]["source"] == "Reuters" and fiche["actus"][0]["publie_a"].endswith("+00:00")
    assert fiche["signaux"]["secteur"] == "Semiconductors"
    assert news_env["news"][0] == ("AAA", DAY - timedelta(days=2), DAY + timedelta(days=1))
    assert (await CAT.fetch_news(db, NOW, 10))["appels"] == 0  # plus rien en attente


async def test_actus_sans_cle_ou_sans_budget(db, news_env, monkeypatch):
    assert (await CAT.fetch_news(db, NOW, 0))["appels"] == 0
    monkeypatch.delenv("FINNHUB_API_KEY")
    assert (await CAT.fetch_news(db, NOW, 10))["appels"] == 0
    assert news_env["news"] == []


async def test_actus_hors_profondeur(db, news_env):
    later = NOW + timedelta(days=400)
    rep = await CAT.fetch_news(db, later, 10)
    assert rep["hors_profondeur"] == 1 and news_env["news"] == []
    assert CAT.query(db.conn)[0]["actus_etat"] == "hors_profondeur"


async def test_actus_en_echec_trois_fois(db, news_env, monkeypatch):
    async def broken(symbol, start, end):
        raise SourceError("HTTP 403")

    monkeypatch.setattr(finnhub, "company_news_between", broken)
    for _ in range(3):
        rep = await CAT.fetch_news(db, NOW, 10)
        assert rep["erreurs"] == 1
    assert CAT.query(db.conn)[0]["actus_etat"] == "erreur"
    assert (await CAT.fetch_news(db, NOW, 10))["appels"] == 0


async def test_aucune_actu(db, news_env, monkeypatch):
    async def empty(symbol, start, end):
        return []

    monkeypatch.setattr(finnhub, "company_news_between", empty)
    await CAT.fetch_news(db, NOW, 10)
    assert CAT.query(db.conn)[0]["actus_etat"] == "aucune"


def test_finnhub_actus_entre_deux_dates(monkeypatch):
    seen = {}

    async def fake_get(url, params=None, headers=None):
        seen.update(params)
        return [{"headline": "A", "datetime": 1700000000, "source": "S", "url": "u"}, {"headline": "sans date"}]

    monkeypatch.setenv("FINNHUB_API_KEY", "k")
    monkeypatch.setattr(finnhub, "get_json", fake_get)
    import asyncio

    items = asyncio.run(finnhub.company_news_between("AAA", date(2025, 1, 1), date(2025, 1, 3)))
    assert seen["from"] == "2025-01-01" and seen["to"] == "2025-01-03"
    assert items == [{"title": "A", "url": "u", "published_at": "2023-11-14T22:13:20+00:00", "source": "S",
                      "summary": None, "published_ts": 1700000000}]


def test_signaux_json(db, monkeypatch):
    monkeypatch.setattr(CAT, "counter_day", lambda day, every=5: False)
    CAT.compute_day(db, "stock", DAY)
    sig = json.loads(db.conn.execute("SELECT signals FROM catalysts").fetchone()[0])
    assert {"gap_pct", "rvol30", "secteur", "heure_haut", "ouverture_ts", "cloture_ts"} <= set(sig)
