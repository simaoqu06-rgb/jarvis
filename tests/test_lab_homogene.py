"""Homogénéité des données du Labo 1 (deuxième tour d'avis du 10 octobre 2026) : garde à l'exécution (empreinte v1 au
démarrage du worker, pause du Labo 1), recalcul ponctuel de la séance d'actions du direct du 06/10/2026 calculée avant
b595c12, révisions lisibles (stats.code_revisions) et docstring du générateur."""
import json
from datetime import date, datetime, timezone

import pytest

from app.core.http import SourceError
from app.lab import collector as CO
from app.lab import empreinte as E
from app.lab import generator as G
from app.lab import grade as GRADE
from app.lab import method as M
from app.lab import rattrapage as RAT
from app.lab import runner as R
from app.lab import sources as SRC
from app.lab import stats as STATS
from app.lab import status as STATUS
from app.lab import strategies as ST
from app.lab import worker as W
from app.lab.db import LabDB
from app.trading import service as T
from tests.lab_fixtures import fill_stock

DAY = date(2026, 10, 6)                                   # la séance du direct d'avant b595c12
NOW = datetime(2026, 10, 10, 2, tzinfo=timezone.utc)      # nuit (samedi à Paris), séance close depuis longtemps
SYMBOLS = ["AAA", "BBB", "SPY"]
REAL = tuple((r["marche"], r["avant"], d) for r in E.V1_RUPTURES for d in r["direct"])
KEY = "v1:b595c12:stock:live:2026-10-06"


@pytest.fixture
def garde(monkeypatch):
    """Garde du Labo 1 remise à zéro (état de processus) et rendue au même état après le test."""
    monkeypatch.setattr(W, "_v1_guard", None)


def _events(db, kind):
    return [t for (t,) in db.conn.execute("SELECT text FROM lab_events WHERE kind = ? ORDER BY id", (kind,))]


# --- Constantes de la rupture -----------------------------------------------------------------------------------

def test_rupture_b595c12_et_seance_a_recalculer():
    assert E.V1_RUPTURES == ({"marche": "stock", "avant": "b595c12", "rejeu_jusqu_au": "2024-12-13",
                              "direct": ("2026-10-06",)},)
    assert REAL == (("stock", "b595c12", "2026-10-06"),)
    assert W.recalcul_key(*REAL[0]) == KEY
    assert E.V1_RECALCULS == ()                           # tests/conftest.py : vidé hors des tests du recalcul


# --- D1 : garde à l'exécution ----------------------------------------------------------------------------------

def test_garde_conforme_sans_evenement(tmp_path, garde):
    db = LabDB(str(tmp_path / "lab.db"))
    state = W.guard_v1(db, NOW)                           # vraie empreinte (≈ 1 s)
    assert state["etat"] == "ok" and state["calculee"] == E.V1_FINGERPRINT == state["attendue"]
    assert db.get("lab", "garde_v1")["etat"] == "ok" and W.v1_paused() is None
    assert db.get("empreintes_v1", E.code_rev("v1")) == E.V1_FINGERPRINT
    assert db.conn.execute("SELECT COUNT(*) FROM lab_events").fetchone()[0] == 0
    st = STATUS.status(str(tmp_path / "lab.db"))
    assert st["garde_v1"]["etat"] == "ok" and "pause" not in STATUS.text(st)


async def test_comportement_change_le_labo_1_en_pause_le_reste_continue(tmp_path, garde, monkeypatch):
    path = str(tmp_path / "lab.db")
    db = LabDB(path)
    db.set("lab", "live_start", "2026-09-16")
    fill_stock(db, SYMBOLS, CO.sessions_back(date(2026, 9, 15), 125), date(2026, 9, 15))
    monkeypatch.setattr(E, "v1_fingerprint", lambda: "0123456789")
    state = W.guard_v1(db, NOW)
    alert = "⚠️ Labo 1 en pause : le comportement v1 a changé (empreinte 0123456789 au lieu de 4f5ef18e6e)"
    assert state["etat"] == "change" and W.v1_paused(db, NOW) == alert
    assert len(_events(db, "labo1_pause")) == 1 and _events(db, "labo1_pause")[0].startswith(alert)
    assert db.get("empreintes_v1", E.code_rev("v1")) == "0123456789"
    W.guard_v1(db, NOW)                                   # même état au redémarrage : pas de second événement
    assert len(_events(db, "labo1_pause")) == 1
    seen = {"grade": 0, "rattrapage": [], "labo2": 0}

    async def interdit(*a, **k):
        raise AssertionError("ligne v1 écrite pendant la pause")

    async def rattrapage(db_, market, seconds, transport=None):
        seen["rattrapage"].append(market)
        return 0

    async def labo2(*a, **k):
        seen["labo2"] += 1
        return {}

    real_grade = GRADE.step

    def grade(db_, now):
        seen["grade"] += 1
        return real_grade(db_, now)

    monkeypatch.setattr(W.alpaca, "configured", lambda: True)
    monkeypatch.setattr(R, "run_live_day", interdit)
    monkeypatch.setattr(R, "replay", interdit)
    monkeypatch.setattr(RAT, "step", rattrapage)
    monkeypatch.setattr(W, "lab2", labo2)
    monkeypatch.setattr(GRADE, "step", grade)
    report = await W.tick(db, NOW, collect=True)
    assert report["paused"]["labo1"] == alert and report["live"] == {} and report["replay"] == {}
    assert seen == {"grade": 1, "rattrapage": ["stock", "crypto"], "labo2": 1}   # le reste du worker continue
    assert db.count_trades() == 0
    st = STATUS.status(path)
    assert st["garde_v1"]["etat"] == "change" and alert in STATUS.text(st)
    # Code remis d'aplomb (retour en arrière, redéploiement) : la garde lève la pause et le dit.
    monkeypatch.setattr(E, "v1_fingerprint", lambda: E.V1_FINGERPRINT)
    W.guard_v1(db, NOW)
    assert W.v1_paused() is None and len(_events(db, "labo1_reprise")) == 1
    assert "Labo 1 repris : empreinte v1 4f5ef18e6e conforme" in _events(db, "labo1_reprise")[0]
    assert "pause" not in STATUS.text(STATUS.status(path))


async def test_pause_sans_recalcul_des_couts(tmp_path, garde, monkeypatch):
    from app.lab import cout as COUT

    db = LabDB(str(tmp_path / "lab.db"))
    monkeypatch.setattr(E, "v1_fingerprint", lambda: "0123456789")
    W.guard_v1(db, NOW)
    monkeypatch.setattr(COUT, "step", lambda db_: pytest.fail("coûts v1 réécrits pendant la pause"))
    report = await W.lab2(db, NOW, collect=False)
    assert report["cout"] == "en pause (garde du Labo 1)" and "grade" in report


async def test_empreinte_incalculable_pause_puis_reprise_au_passage_suivant(tmp_path, garde, monkeypatch):
    monkeypatch.setenv("JARVIS_LAB_REPLAY_DAYS", "1")    # rejeu : la seule séance du 15/09 (bougies en base)
    db = LabDB(str(tmp_path / "lab.db"))
    db.set("lab", "live_start", "2026-09-16")
    fill_stock(db, SYMBOLS, CO.sessions_back(date(2026, 9, 15), 125), date(2026, 9, 15))

    def boom():
        raise MemoryError("plus de mémoire")

    monkeypatch.setattr(E, "v1_fingerprint", boom)
    state = W.guard_v1(db, NOW)
    assert state["etat"] == "erreur" and state["calculee"] is None and state["erreur"] == "MemoryError: plus de mémoire"
    text = "⚠️ Labo 1 en pause : empreinte v1 incalculable (MemoryError: plus de mémoire), comportement non vérifié"
    assert W.v1_paused(db, NOW) == text                   # retenté : même erreur, toujours en pause
    assert len(_events(db, "labo1_pause")) == 1           # un seul événement pour une erreur qui dure
    assert db.get("empreintes_v1", E.code_rev("v1")) is None

    async def nothing(*a, **k):
        return {}

    monkeypatch.setattr(W, "lab2", nothing)
    report = await W.tick(db, NOW, collect=False)
    assert report["paused"]["labo1"] == text and db.count_trades() == 0
    # Erreur passagère : le passage suivant recalcule l'empreinte, lève la pause et rejoue dans le même passage.
    monkeypatch.setattr(E, "v1_fingerprint", lambda: E.V1_FINGERPRINT)
    report = await W.tick(db, NOW, collect=False)
    assert "labo1" not in report["paused"] and db.count_trades() > 0
    assert len(_events(db, "labo1_reprise")) == 1


async def test_garde_une_fois_au_demarrage_avant_le_premier_passage(tmp_path, garde, monkeypatch):
    from app.lab import heartbeat

    class Stop(BaseException):
        pass

    order = []
    monkeypatch.setenv("JARVIS_LAB_DB", str(tmp_path / "lab.db"))   # battement écrit à côté de la base de test
    db = LabDB(str(tmp_path / "lab.db"))
    monkeypatch.setattr(heartbeat, "enabled", False)
    monkeypatch.setattr(heartbeat, "stage", None)
    monkeypatch.setattr(W, "LabDB", lambda: db)
    monkeypatch.setattr(W, "guard_v1", lambda db_, now=None: order.append("garde"))

    async def tick(db_):
        order.append("passage")
        return {}

    async def stop(seconds):
        if order.count("passage") >= 2:
            raise Stop
    monkeypatch.setattr(W, "tick", tick)
    monkeypatch.setattr(W.asyncio, "sleep", stop)
    with pytest.raises(Stop):
        await W.loop()
    assert order == ["garde", "passage", "passage"]


def test_aucune_garde_hors_du_worker(garde):
    assert W.v1_paused() is None and E.guard_text(None) is None and E.guard_text({"etat": "ok"}) is None


# --- D2 : recalcul ponctuel de la séance du direct du 06/10/2026 ---------------------------------------------

def _old_day(db, monkeypatch):
    """Séance du direct calculée par le code d'avant b595c12 (bougie de 15 h 45 de la veille tenue pour en cours),
    sans trace (comme avant le suivi), bougies de 5 min couvertes."""
    fill_stock(db, SYMBOLS, CO.sessions_back(DAY, 125), DAY)
    db.set("lab", "live_start", "2026-10-06")
    with monkeypatch.context() as m:
        m.setattr(T, "with_open_bar", lambda bars, interval, now: bars)
        n = R.run_day(db, "stock", DAY, "live", progress=("live:stock", "last_day"))
    db.conn.execute("UPDATE trades SET code_rev = NULL, cout_rev = NULL, computed_at = NULL")
    start, end = CO.stock_window(DAY)
    for s in SYMBOLS:
        db.set("cov5:stock", s, [start, end])
    return n


def _plans(db):
    return {s: (i, json.loads(p), c) for s, i, p, c in
            db.conn.execute("SELECT symbol, id, plan, code_rev FROM trades WHERE origin = 'live' AND day = ?",
                            (DAY.isoformat(),))}


def _current(tmp_path):
    """Même séance calculée par le code actuel, sur une autre base."""
    other = LabDB(str(tmp_path / "actuel.db"))
    fill_stock(other, SYMBOLS, CO.sessions_back(DAY, 125), DAY)
    R.run_day(other, "stock", DAY, "live")
    return {s: p for s, (_, p, _) in _plans(other).items()}


async def test_recalcul_de_la_seance_du_direct_avec_le_code_actuel(tmp_path, monkeypatch):
    db = LabDB(str(tmp_path / "lab.db"))
    n = _old_day(db, monkeypatch)
    before = _plans(db)
    assert n == 3 and all(c is None for _, _, c in before.values())
    current = _current(tmp_path)
    assert any(before[s][1]["stop"] != current[s]["stop"] or before[s][1]["targets"] != current[s]["targets"]
               for s in current)                          # la rupture change bien les plans
    monkeypatch.setattr(E, "V1_RECALCULS", REAL)

    async def no_network(*a, **k):
        raise AssertionError("bougies déjà couvertes : rien à demander")

    monkeypatch.setattr(SRC, "alpaca_bars", no_network)
    rev = db.get("lab", "rev:trades")
    out = await W.recalcul_v1(db, NOW)
    state = out[KEY]
    assert state["etat"] == "fait" and state["trades"] == 3 and state["sans_revision_avant"] == 3
    assert state["labo_v2"] == "laissées au rattrapage" and db.get(W.RECALCUL_JOB, KEY) == state
    after = _plans(db)
    assert {s: i for s, (i, _, _) in after.items()} == {s: i for s, (i, _, _) in before.items()}   # mêmes id
    assert {s: p for s, (_, p, _) in after.items()} == current          # plans du code actuel
    assert {c for _, _, c in after.values()} == {E.code_rev("v1")}
    assert db.get("live:stock", "last_day") == "2026-10-06" and db.get(W.RECALCUL_JOB, f"attempts:{KEY}") is None
    # Idempotent : rien n'est relu ni réécrit au passage suivant.
    assert await W.recalcul_v1(db, NOW) == {} and db.get("lab", "rev:trades") == rev + 1
    st = STATUS.status(str(tmp_path / "lab.db"))
    assert st["recalcul_v1"][KEY]["etat"] == "fait" and "non recalculable" not in STATUS.text(st)


async def test_recalcul_refait_les_lignes_n10_si_le_rattrapage_a_passe_le_jour(tmp_path, monkeypatch):
    db = LabDB(str(tmp_path / "lab.db"))
    _old_day(db, monkeypatch)
    job = ST.progress_job("stock")
    db.set(job, "rattrapage:live", "2026-10-07")
    db.set(job, "skipped:live:2026-10-06", "ancien échec")
    db.replace_strat("live", "stock", "2026-10-06", [{"symbol": "AAA", "strategy": "v1", "slot": 0, "stop": -1.0}])
    monkeypatch.setattr(E, "V1_RECALCULS", REAL)
    calls = []

    async def stock_rows(db_, origin, day, transport=None, missing=None):
        calls.append((origin, day, missing))
        plans = RAT.v1_trades(db_, origin, "stock", day.isoformat())   # les plans déjà recalculés
        return [{"symbol": s, "strategy": "v1", "slot": 0, "stop": t[0]["plan"]["stop"]} for s, t in plans.items()]

    monkeypatch.setattr(RAT, "stock_rows", stock_rows)
    state = (await W.recalcul_v1(db, NOW))[KEY]
    assert calls == [("live", DAY, None)] and state["etat"] == "fait" and state["labo_v2"] == "refaites (3 lignes)"
    stops = dict(db.conn.execute("SELECT symbol, stop FROM strat_trades WHERE day = '2026-10-06'"))
    assert stops == {s: p["stop"] for s, (_, p, _) in _plans(db).items()}
    assert db.get(job, "skipped:live:2026-10-06") is None and db.get(job, "rattrapage:live") == "2026-10-07"


async def test_lignes_n10_en_echec_tout_est_annule_puis_sautees_au_dernier_essai(tmp_path, monkeypatch):
    db = LabDB(str(tmp_path / "lab.db"))
    _old_day(db, monkeypatch)
    job = ST.progress_job("stock")
    db.set(job, "rattrapage:live", "2026-10-06")
    db.replace_strat("live", "stock", "2026-10-06", [{"symbol": "AAA", "strategy": "orb15", "slot": 1}])
    monkeypatch.setattr(E, "V1_RECALCULS", REAL)
    before = _plans(db)

    async def down(*a, **k):
        raise SourceError("HTTP 503")

    monkeypatch.setattr(RAT, "stock_rows", down)
    for attempt in (1, 2):
        state = (await W.recalcul_v1(db, NOW))[KEY]
        assert state["etat"] == "en_echec" and state["essais"] == attempt
        assert _plans(db) == before                                       # v1 annulé avec les lignes n° 10
        assert db.conn.execute("SELECT COUNT(*) FROM strat_trades").fetchone()[0] == 1
    state = (await W.recalcul_v1(db, NOW))[KEY]
    assert state["etat"] == "fait" and state["labo_v2"] == "sautées (source en panne)"
    assert {c for _, _, c in _plans(db).values()} == {E.code_rev("v1")}
    assert db.conn.execute("SELECT COUNT(*) FROM strat_trades").fetchone()[0] == 0
    assert db.get(job, "skipped:live:2026-10-06") == "SourceError: HTTP 503"


async def test_bougies_purgees_et_source_en_panne_non_recalculable(tmp_path, monkeypatch):
    path = str(tmp_path / "lab.db")
    db = LabDB(path)
    _old_day(db, monkeypatch)
    start, end = CO.stock_window(DAY)
    CO.prune_intraday(db, "stock", start + 30 * 86400)   # ménage : le début de la fenêtre a disparu
    monkeypatch.setattr(E, "V1_RECALCULS", REAL)
    before = _plans(db)
    asked = []

    async def down(symbols, tf, a, b, transport=None, adjustment="split"):
        asked.append(len(symbols))
        raise SourceError("HTTP 503")

    monkeypatch.setattr(SRC, "alpaca_bars", down)
    for attempt in (1, 2):
        state = (await W.recalcul_v1(db, NOW))[KEY]
        assert state["etat"] == "en_echec" and state["essais"] == attempt and "sans leurs bougies" in state["raison"]
        assert _plans(db) == before                                       # rien n'est effacé
    state = (await W.recalcul_v1(db, NOW))[KEY]
    assert state["etat"] == "non_recalculable" and _plans(db) == before and asked
    assert await W.recalcul_v1(db, NOW) == {}                             # plus jamais retenté
    text = STATUS.text(STATUS.status(path))
    assert ("⚠️ Séance du direct actions du 06/10/2026 non recalculable (SourceError: 3 symboles sans leurs bougies "
            "de 5 min : AAA, BBB, SPY) : ses trades restent ceux d'avant b595c12") in text


async def test_bougies_purgees_redemandees_a_la_source(tmp_path, monkeypatch):
    db = LabDB(str(tmp_path / "lab.db"))
    _old_day(db, monkeypatch)
    start, _ = CO.stock_window(DAY)
    cut = start + 30 * 86400
    kept = {s: db.get_bars("stock", s, 5, start, cut) for s in SYMBOLS}
    CO.prune_intraday(db, "stock", cut)
    monkeypatch.setattr(E, "V1_RECALCULS", REAL)

    async def bars(symbols, tf, a, b, transport=None, adjustment="split"):
        lo, hi = int(a.timestamp()), int(b.timestamp())
        return {s: [x for x in kept[s] if lo <= x["t"] < hi] for s in symbols}

    monkeypatch.setattr(SRC, "alpaca_bars", bars)
    state = (await W.recalcul_v1(db, NOW))[KEY]
    assert state["etat"] == "fait" and {s: p for s, (_, p, _) in _plans(db).items()} == _current(tmp_path)


async def test_sans_objet_si_tout_vient_du_code_suivi(tmp_path, monkeypatch):
    db = LabDB(str(tmp_path / "lab.db"))
    monkeypatch.setattr(E, "V1_RECALCULS", REAL)
    assert (await W.recalcul_v1(db, NOW))[KEY]["etat"] == "sans_objet"   # jour pas encore journalisé
    db2 = LabDB(str(tmp_path / "suivi.db"))
    fill_stock(db2, SYMBOLS, CO.sessions_back(DAY, 125), DAY)
    R.run_day(db2, "stock", DAY, "live")
    rev = db2.get("lab", "rev:trades")
    state = (await W.recalcul_v1(db2, NOW))[KEY]
    assert state["etat"] == "sans_objet" and state["trades"] == 3 and db2.get("lab", "rev:trades") == rev


async def test_recalcul_en_tete_de_passage_avant_le_grade(tmp_path, garde, monkeypatch):
    db = LabDB(str(tmp_path / "lab.db"))
    _old_day(db, monkeypatch)
    monkeypatch.setattr(E, "V1_RECALCULS", REAL)
    seen = {}

    def grade(db_, now):
        seen["revisions_vues_par_le_grade"] = {c for _, _, c in _plans(db_).values()}
        return {}

    async def nothing(*a, **k):
        return {}

    monkeypatch.setattr(W.alpaca, "configured", lambda: True)
    monkeypatch.setattr(GRADE, "step", grade)
    for name in ("run_live_day", "replay"):
        monkeypatch.setattr(R, name, nothing)
    monkeypatch.setattr(RAT, "step", nothing)
    monkeypatch.setattr(W, "lab2", nothing)
    monkeypatch.setattr(W, "prune_cutoff", lambda *a: None)          # le ménage rognerait la fenêtre du 06/10
    session = datetime(2026, 10, 9, 15, tzinfo=timezone.utc)            # séance US : pas de source, pas de recalcul
    report = await W.tick(db, session, collect=True)
    assert "recalcul" not in report and seen["revisions_vues_par_le_grade"] == {None}
    report = await W.tick(db, NOW, collect=True)
    assert report["recalcul"][KEY]["etat"] == "fait" and seen["revisions_vues_par_le_grade"] == {E.code_rev("v1")}
    # Labo 1 en pause : pas de recalcul (il écrit des lignes v1).
    db3 = LabDB(str(tmp_path / "pause.db"))
    _old_day(db3, monkeypatch)
    monkeypatch.setattr(E, "v1_fingerprint", lambda: "0123456789")
    W.guard_v1(db3, NOW)
    report = await W.tick(db3, NOW, collect=True)
    assert "recalcul" not in report and db3.get(W.RECALCUL_JOB, KEY) is None


def test_ids_et_jours_vus_gardes_pour_les_lecons_et_les_regles(tmp_path, monkeypatch):
    """replace_trades réécrit en place : la frontière des leçons et des règles (dernier jour vu par origine et marché)
    ne bouge pas, le jour recalculé reste « vu »."""
    from app.lab import lessons as L

    db = LabDB(str(tmp_path / "lab.db"))
    _old_day(db, monkeypatch)
    seen = L.seen_until(db.conn)
    base = db.conn.execute("SELECT MAX(id) FROM trades").fetchone()[0]
    R.run_day(db, "stock", DAY, "live")
    assert L.seen_until(db.conn) == seen == {"live:stock": "2026-10-06"}
    assert L.seen_until(db.conn, base) == seen and db.conn.execute("SELECT MAX(id) FROM trades").fetchone()[0] == base
    cond, params = L.unseen_sql(seen)
    assert db.conn.execute(f"SELECT COUNT(*) FROM trades t WHERE {cond}", params).fetchone()[0] == 0


# --- D3 : révisions lisibles ------------------------------------------------------------------------------------

def _trade(db, origin, market, day, code_rev=None, symbol="AAA"):
    n = db.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    db.conn.execute("INSERT INTO trades (origin, method_version, market, symbol, side, day, planned_at, code_rev) "
                    "VALUES (?, 'v1', ?, ?, 'long', ?, ?, ?)", (origin, market, symbol, day, n + 1, code_rev))


OLD_REPLAY = "inconnue, avant b595c12 (rejeu actions ≤ 13/12/2024)"
OLD_LIVE = "inconnue, avant b595c12 (direct actions du 06/10/2026)"


def test_inconnues_separees_et_alerte_sur_la_rupture(tmp_path):
    path = str(tmp_path / "lab.db")
    db = LabDB(path)
    for day in ("2024-10-01", "2024-12-13"):
        _trade(db, "replay", "stock", day)               # rejeu d'avant b595c12
    _trade(db, "replay", "stock", "2024-12-16")          # après la borne : plans identiques sur la sonde
    _trade(db, "replay", "crypto", "2024-11-01")         # la crypto n'a pas changé
    _trade(db, "live", "stock", "2026-10-06")            # direct du 06/10, pas encore recalculé
    _trade(db, "live", "crypto", "2026-10-06")
    _trade(db, "live", "stock", "2026-10-07", "aedf763a5a")
    rev = STATS.code_revisions(db.conn)
    assert rev["par_version"] == {"v1": {OLD_REPLAY: 2, STATS.UNKNOWN_PROBE: 3, OLD_LIVE: 1, "aedf763a5a": 1}}
    assert rev["alertes"] == [
        "plusieurs comportements sous la même version : v1 compte 3 trades calculés avant b595c12 (rejeu actions ≤ "
        "13/12/2024 : 2, direct actions du 06/10/2026 : 1) sur 7, d'une autre règle de plan (rupture listée dans "
        "empreinte.V1_RUPTURES)"]
    text = STATUS.text(STATUS.status(path))
    assert f"⚠️ Méthode v1 calculée par 4 révisions du code : {OLD_REPLAY} 2 trades" in text
    assert text.count("· trades d'avant b595c12") == 1
    # La séance du direct recalculée (révision du code actuel) n'est plus « inconnue » ; le rejeu de 2024 le reste.
    db.conn.execute("UPDATE trades SET code_rev = 'aedf763a5a' WHERE origin = 'live'")
    rev = STATS.code_revisions(db.conn)
    assert rev["par_version"] == {"v1": {OLD_REPLAY: 2, STATS.UNKNOWN_PROBE: 2, "aedf763a5a": 3}}
    assert len(rev["alertes"]) == 1 and "2 trades calculés avant b595c12 (rejeu actions ≤ 13/12/2024 : 2)" \
        in rev["alertes"][0]
    # Les filtres de stats.compute s'appliquent : sans les actions du rejeu, plus de rupture en vue.
    assert STATS.code_revisions(db.conn, market="crypto")["alertes"] == []
    assert STATS.code_revisions(db.conn, since="2025-01-01")["alertes"] == []


def test_sans_alerte_pour_des_sources_differentes_ou_seulement_l_ancien_code(tmp_path):
    db = LabDB(str(tmp_path / "lab.db"))
    _trade(db, "replay", "stock", "2024-11-04")
    assert STATS.code_revisions(db.conn) == {"par_version": {"v1": {OLD_REPLAY: 1}}, "alertes": []}   # rien de mêlé
    db.conn.execute("DELETE FROM trades")
    _trade(db, "replay", "stock", "2025-02-03")
    _trade(db, "replay", "stock", "2025-02-04", "181e9d4920")
    _trade(db, "replay", "stock", "2025-02-05", "aedf763a5a")
    db.set("empreintes_v1", "aedf763a5a", E.V1_FINGERPRINT)   # seule révision notée par la garde
    rev = STATS.code_revisions(db.conn)
    assert len(rev["par_version"]["v1"]) == 3 and rev["alertes"] == []  # refactorisations, plans identiques
    assert STATS.revision_mixes(db.conn, rev["par_version"]) == {}


# --- D4 : docstring du générateur ------------------------------------------------------------------------------

def test_bougie_de_15_h_45_close_et_bougie_en_cours_plate(tmp_path):
    """generator.py décrit le comportement depuis b595c12 : à 9 h 10, la dernière bougie connue (15 h 45 la veille) est
    close, et le plan ajoute après elle une bougie en cours plate."""
    from zoneinfo import ZoneInfo

    db = LabDB(str(tmp_path / "lab.db"))
    fill_stock(db, ["AAA"], CO.sessions_back(DAY, 125), DAY)
    inp = G.stock_inputs(db, "AAA", DAY)
    last = datetime.fromtimestamp(inp["bars"][-1]["t"], ZoneInfo("America/New_York"))
    assert (last.date(), last.hour, last.minute) == (date(2026, 10, 5), 15, 45)
    bars = T.with_open_bar(inp["bars"], 15, datetime.fromtimestamp(inp["moment"], timezone.utc))
    c = inp["bars"][-1]["c"]
    assert bars[:-1] == inp["bars"] and bars[-1] | {"t": 0} == {"t": 0, "o": c, "h": c, "l": c, "c": c, "v": 0.0,
                                                               "pending": True}
    assert "with_open_bar" in G.__doc__ and "que le plan tient pour en cours" not in G.__doc__


async def test_recalcul_refuse_si_la_version_de_methode_a_change(tmp_path, monkeypatch):
    """replace_trades remplace les trades d'une version : sous une autre version, la ligne d'avant resterait à côté."""
    db = LabDB(str(tmp_path / "lab.db"))
    _old_day(db, monkeypatch)
    db.conn.execute("UPDATE trades SET method_version = 'v0'")
    monkeypatch.setattr(E, "V1_RECALCULS", REAL)
    before = _plans(db)
    state = (await W.recalcul_v1(db, NOW))[KEY]
    assert state["etat"] == "non_recalculable" and state["raison"] == "version de méthode changée (v0 → v1)"
    assert _plans(db) == before and db.count_trades() == 3
