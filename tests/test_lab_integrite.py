"""Intégrité des chiffres du labo (avis de GPT et Gemini du 10 octobre) : version du code par ligne (I2), caches
invalidés par les réécritures et les déploiements (I3), données manquantes (I4) et fenêtres honnêtes (I5)."""
import json
import sqlite3
from datetime import date, datetime, timezone

from app.lab import autopsie as AUT
from app.lab import collector as CO
from app.lab import cout as COUT
from app.lab import db as DB
from app.lab import empreinte as E
from app.lab import grade as GRADE
from app.lab import lessons as L
from app.lab import runner as R
from app.lab import stats as STATS
from app.lab import status as STATUS
from app.lab import strat_stats as SS
from app.lab import strategies as ST
from app.lab import worker as W
from app.lab.db import LabDB
from tests.lab_fixtures import fill_crypto, fill_stock, synthetic_trades
from tests.test_lab_n8 import _trade

START, END = date(2026, 9, 14), date(2026, 9, 16)
NOW = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)


def _row(symbol="AAA", r=1.0, day="2026-03-02", planned_at=1):
    return {"symbol": symbol, "side": "long", "day": day, "planned_at": planned_at, "plan": {}, "context": {},
            "result": {}, "flat": {"triggered": 1, "r_net": r}}


def _strat(symbol="AAA", r=0.5):
    return {"symbol": symbol, "strategy": "pullback", "slot": 0, "triggered": 1, "r_net": r, "exit_reason": "stop",
            "mfe_r": 0.2, "mae_r": -1.0, "tp1_hit": 0}


# --- I2 : version du code, du coût et de la déclaration par ligne ---------------------------------------------

def test_migration_d_une_base_existante(tmp_path):
    path = str(tmp_path / "lab.db")
    old = sqlite3.connect(path)
    old.executescript(DB.SCHEMA)                         # schéma d'avant le suivi : colonnes ajoutées par migrate()
    assert "code_rev" not in {r[1] for r in old.execute("PRAGMA table_info(trades)")}
    old.execute("INSERT INTO trades (origin, method_version, market, symbol, side, day, planned_at, triggered, r_net) "
                "VALUES ('replay', 'v1', 'crypto', 'AAA', 'long', '2026-03-02', 1, 1, 2.0)")
    old.execute("INSERT INTO strat_trades (origin, market, symbol, day, strategy, slot, triggered, r_net) "
                "VALUES ('replay', 'stock', 'AAA', '2026-03-02', 'pullback', 0, 1, 0.5)")
    old.commit()
    old.close()
    db = LabDB(path)
    for table, cols in (("trades", DB.TRADE_TRACE), ("trades_sealed", DB.TRADE_TRACE),
                        ("strat_trades", DB.STRAT_TRACE), ("strat_trades_sealed", DB.STRAT_TRACE)):
        have = {r[1] for r in db.conn.execute(f"PRAGMA table_info({table})")}
        assert {c for c, _ in cols} <= have
    assert db.conn.execute("SELECT code_rev, cout_rev, computed_at FROM trades").fetchone() == (None, None, None)
    assert db.conn.execute("SELECT code_rev, declaration FROM strat_trades").fetchone() == (None, None)
    old_id = db.conn.execute("SELECT id FROM trades").fetchone()[0]
    assert STATS.compute(db.conn)["revisions_code"]["par_version"] == {"v1": {"inconnue": 1}}
    db.replace_trades("replay", "v1", "crypto", [1], [_row(r=1.5)])     # même créneau : mis à jour sur place
    row = db.conn.execute("SELECT id, r_net, code_rev, cout_rev, computed_at FROM trades").fetchone()
    assert row[:4] == (old_id, 1.5, E.code_rev("v1"), E.cost_rev()) and row[4] > 0
    db.close()
    LabDB(path).close()                                  # migration idempotente


async def test_rejeu_ecrit_code_cout_et_declaration(tmp_path):
    db = LabDB(str(tmp_path / "lab.db"))
    fill_stock(db, ["AAA", "BBB", "SPY"], CO.sessions_back(START, 125), END)
    await R.replay(START, START, "stock", db, collect=False, now=NOW)
    revs = db.conn.execute("SELECT DISTINCT code_rev, cout_rev, computed_at IS NOT NULL FROM trades").fetchall()
    assert revs == [(E.code_rev("v1"), E.cost_rev(), 1)]
    # n° 10 : les lignes v2 des actions ne viennent que du rattrapage (prix bruts) ; replace_strat les trace
    assert db.conn.execute("SELECT COUNT(*) FROM strat_trades WHERE market = 'stock'").fetchone()[0] == 0
    db.replace_strat("replay", "stock", "2026-09-14", [_strat()])
    strat = db.conn.execute("SELECT DISTINCT code_rev, declaration, computed_at IS NOT NULL FROM strat_trades").fetchall()
    assert strat == [(E.code_rev("strat"), ST.DECLARATION, 1)]
    db.replace_strat("replay", "crypto", "2026-09-14", [_strat()])
    assert db.conn.execute("SELECT declaration FROM strat_trades WHERE market = 'crypto'").fetchone()[0] \
        == ST.CRYPTO_DECLARATION


async def test_deux_revisions_du_code_sous_la_meme_version(tmp_path, monkeypatch):
    path = str(tmp_path / "lab.db")
    db = LabDB(path)
    fill_stock(db, ["AAA", "BBB", "SPY"], CO.sessions_back(START, 125), END)
    monkeypatch.setitem(E.CODE_REV, "v1", "A")
    await R.replay(START, START, "stock", db, collect=False, now=NOW)
    monkeypatch.setitem(E.CODE_REV, "v1", "B")
    await R.replay(START, END, "stock", db, collect=False, max_days=1, now=NOW)
    assert dict(db.conn.execute("SELECT day, code_rev FROM trades GROUP BY day")) == {"2026-09-14": "A",
                                                                                      "2026-09-15": "B"}
    rev = STATS.compute(db.conn)["revisions_code"]
    assert rev["par_version"] == {"v1": {"A": 3, "B": 3}}
    assert len(rev["alertes"]) == 1 and "plusieurs comportements sous la même version" in rev["alertes"][0]
    st = STATUS.status(path)
    assert st["revisions_code"] == {"v1": {"A": 3, "B": 3}}
    assert "Méthode v1 calculée par 2 révisions du code : A 3 trades, B 3 trades" in STATUS.text(st)


def test_une_seule_revision_sans_alerte():
    db = LabDB(":memory:")
    db.replace_trades("replay", "v1", "crypto", [1], [_row(), _row("BBB")])
    rev = STATS.compute(db.conn)["revisions_code"]
    assert rev == {"par_version": {"v1": {E.code_rev(): 2}}, "alertes": []}


# --- I3 : caches invalidés par une réécriture sur place, le recalcul des coûts et un déploiement ----------------

def test_trade_reecrit_sur_place_change_la_signature():
    db = LabDB(":memory:")
    db.replace_trades("live", "v1", "crypto", [1], [_row("AAA", 1.0), _row("BBB", -1.0)])
    before = W.trades_signature(db)
    db.replace_trades("live", "v1", "crypto", [1], [_row("AAA", 2.5), _row("BBB", 0.7)])   # mêmes id, même COUNT
    after = W.trades_signature(db)
    assert before[:2] == after[:2] == [2, 2] and before != after
    assert db.get("lab", "rev:trades") == 2


def test_dernier_jour_de_strategies_reecrit_change_les_signatures():
    db = LabDB(":memory:")
    db.replace_strat("replay", "stock", "2026-03-02", [_strat("AAA"), _strat("BBB")])
    db.replace_strat("replay", "stock", "2026-03-03", [_strat("AAA"), _strat("BBB")])
    ss, aut = SS.signature(db.conn), AUT.signature(db.conn)
    db.replace_strat("replay", "stock", "2026-03-03", [_strat("AAA", -1.0), _strat("BBB", 2.0)])
    count = db.conn.execute("SELECT COUNT(*), MAX(id) FROM strat_trades").fetchone()
    assert count == (4, 4)                                                    # COUNT et MAX(id) inchangés
    assert SS.signature(db.conn) != ss and AUT.signature(db.conn) != aut
    trades = W.trades_signature(db)
    db.replace_strat("replay", "stock", "2026-03-03", [_strat("AAA")])
    assert W.trades_signature(db) == trades                                   # stats ne relit que trades


def test_cout_invalide_tous_les_caches_et_note_son_modele():
    db = LabDB(":memory:")
    _trade(db, "trades", 1, 2.0, 1.9, 2.2, 15.0)
    now = datetime(2026, 10, 9, 22, tzinfo=timezone.utc)
    for job in ("stats", "grade_calc", "lessons", "strats", "autopsie"):
        db.set(job, "signature", [1, 1])
        db.set(job, "at", now.timestamp())
    while not db.get(COUT.JOB, "fini"):
        COUT.step(db)
    assert db.conn.execute("SELECT cout_rev FROM trades").fetchone()[0] == E.cost_rev()
    assert db.get("lab", "rev:trades") == 1
    for job in ("stats", "grade_calc", "lessons", "strats", "autopsie"):
        assert db.get(job, "signature") is None
    for job in ("stats", "lessons", "strats", "autopsie"):
        assert db.get(job, "at") == 0
    assert W.refresh_strategies(db, now, 3600) is not None
    assert AUT.step(db, now, 3600) is not None


def test_un_deploiement_recalcule_chaque_cache_une_fois(monkeypatch):
    db = LabDB(":memory:")
    synthetic_trades(db, n_days=30, per_day=5)
    now = datetime(2026, 10, 9, 22, tzinfo=timezone.utc)
    assert W.refresh_stats(db, now, 0) is not None
    assert W.refresh_strategies(db, now, 0) is not None
    assert AUT.step(db, now, 0) is not None
    assert "stock" in GRADE.step(db, now)
    L.step(db, now, 0)
    later = datetime(2026, 10, 9, 22, 5, tzinfo=timezone.utc)                   # caches tout neufs, âge maximal 1 h
    assert W.refresh_stats(db, later, 3600) is None and W.refresh_strategies(db, later, 3600) is None
    assert AUT.step(db, later, 3600) is None and "stock" not in GRADE.step(db, later)
    assert "mesurees" not in L.step(db, later, 3600)
    monkeypatch.setattr(E, "STATS_CODE", "autre_code")                          # nouveau code, mêmes données
    assert W.refresh_stats(db, later, 3600) is not None
    assert W.refresh_strategies(db, later, 3600) is not None
    assert AUT.step(db, later, 3600) is not None
    assert "stock" in GRADE.step(db, later)
    assert "mesurees" in L.step(db, later, 3600)
    assert W.refresh_stats(db, later, 3600) is None and AUT.step(db, later, 3600) is None   # une seule fois


def _old_declaration(db, day, symbols, decl):
    db.conn.executemany("INSERT INTO strat_trades (origin, market, symbol, day, strategy, slot, triggered, r_net, "
                        "declaration) VALUES ('replay', 'stock', ?, ?, 'pullback', 0, 1, -1.0, ?)",
                        [(s, day, decl) for s in symbols])


def test_rattrapage_a_moitie_fait_ne_montre_que_la_declaration_en_vigueur(tmp_path):
    path = str(tmp_path / "lab.db")
    db = LabDB(path)
    job = ST.progress_job("stock")
    db.set("replay:stock", "start", "2026-09-14")
    db.set("replay:stock", "last_day", "2026-09-16")         # n° 10 : en actions, le rattrapage va jusqu'au dernier jour
    db.set(job, "inline_from:replay", "2026-09-17")          # traité ; le 17 est déjà de la nouvelle déclaration
    db.put_bars("stock", "SPY", 1440, [{"t": CO.ny_midnight(date(2026, 9, 17)), "o": 1, "h": 1, "l": 1, "c": 1}])
    db.set(job, "rattrapage:replay", "2026-09-14")           # le 14 est refait, restent le 15 et le 16
    db.replace_strat("replay", "stock", "2026-09-14", [_strat("AAA"), _strat("BBB")])
    _old_declaration(db, "2026-09-15", ["AAA", "BBB"], ST.DECLARATION - 1)
    _old_declaration(db, "2026-09-16", ["AAA"], None)       # ligne d'avant le suivi
    db.replace_strat("replay", "stock", "2026-09-17", [_strat("AAA"), _strat("BBB")])
    rep = SS.report(db.conn)
    assert rep["partiel"] == {"stock": {"declaration": ST.DECLARATION, "jours_rattrapage": 2, "lignes_exclues": 3}}
    item = rep["strategies"]["stock"]["pullback"]
    assert item["jours"] == 2 and item["esperance_r"] == 0.5     # les lignes à -1 R sont exclues
    assert "Tableau partiel (actions) : 2 jours en rattrapage, 3 lignes de l'ancienne déclaration exclues" \
        in SS.text(rep)
    assert db.conn.execute("SELECT COUNT(*) FROM strat_trades").fetchone()[0] == 7      # vue temporaire retirée
    ro = DB.open_readonly(path)                                 # serveur web : même tableau en lecture seule
    assert SS.report(ro)["strategies"]["stock"]["pullback"]["jours"] == 2
    ro.close()
    db.set(job, "rattrapage:replay", "2026-09-16")           # rattrapage fini : tout est de la même déclaration
    rep = SS.report(db.conn)
    assert rep["partiel"] is None and rep["strategies"]["stock"]["pullback"]["jours"] == 4
    assert "Tableau partiel" not in SS.text(rep)


def test_tableau_partiel_accorde_au_singulier():
    """Relecture : le dernier jour d'un rattrapage, et en actions le jour du direct entre l'arrivée de son journalier et
    son rattrapage (le tableau est recalculé avant lui dans le passage), s'affichaient « 1 jours en rattrapage,
    0 lignes de l'ancienne déclaration exclues »."""
    rep = SS.report(LabDB(":memory:").conn)
    rep["partiel"] = {"stock": {"declaration": 10, "jours_rattrapage": 1, "lignes_exclues": 0},
                      "crypto": {"declaration": 10, "jours_rattrapage": 1, "lignes_exclues": 1}}
    text = SS.text(rep)
    assert ("Tableau partiel (actions) : 1 jour en rattrapage, aucune ligne d'une autre déclaration (n° 10 seule en "
            "attendant)") in text
    assert "Tableau partiel (crypto) : 1 jour en rattrapage, 1 ligne de l'ancienne déclaration exclue (n° 10" in text


def test_autopsie_pendant_un_rattrapage_ne_lit_que_la_declaration_en_vigueur(tmp_path):
    """Relecture : le grade indicatif des GO confirmés (!grade, brief) et l'autopsie lisaient strat_trades sans le filtre
    du tableau : pendant le rattrapage de la n° 10 ils mêlaient les deux révisions du simulateur (la 1, biaisée en
    faveur de l'ordre stop des GO confirmés)."""
    path = str(tmp_path / "lab.db")
    db = LabDB(path)
    job = ST.progress_job("stock")
    db.set("replay:stock", "start", "2026-09-14")
    db.set("replay:stock", "last_day", "2026-09-16")
    db.put_bars("stock", "SPY", 1440, [{"t": CO.ny_midnight(date(2026, 9, 16)), "o": 1, "h": 1, "l": 1, "c": 1}])
    db.set(job, "rattrapage:replay", "2026-09-14")
    col = ST.ref_exit("go_confirme")
    go = {"strategy": "go_confirme", "slot": 0, "triggered": 1, "r_net": 0.5, col: 0.5, "exit_reason": "stop",
          "mfe_r": 0.6, "mae_r": -0.5, "tp1_hit": 0}
    db.replace_strat("replay", "stock", "2026-09-14", [go | {"symbol": s} for s in ("AAA", "BBB")])
    db.conn.executemany(f"INSERT INTO strat_trades (origin, market, symbol, day, strategy, slot, triggered, r_net, {col}, "
                        "exit_reason, mfe_r, mae_r, tp1_hit, declaration) VALUES ('replay', 'stock', ?, ?, 'go_confirme', "
                        "0, 1, -1.0, -1.0, 'stop', 0.1, -1.0, 0, ?)",
                        [("AAA", "2026-09-15", ST.DECLARATION - 1), ("BBB", "2026-09-16", None)])
    rep = AUT.report(db.conn)
    gi = rep["grade_indicatif"]["go_confirme"]
    assert gi["par_go"]["go"] == 2 and gi["par_go"]["esperance_r"] == 0.5          # les lignes à −1 R sont exclues
    assert rep["strategies"]["stock"]["go_confirme"]["trades"] == 2
    assert rep["partiel"]["stock"]["jours_rattrapage"] == 2 and gi["partiel"] == rep["partiel"]["stock"]
    assert db.conn.execute("SELECT COUNT(*) FROM strat_trades").fetchone()[0] == 4    # vue temporaire retirée
    note = "rattrapage de la déclaration n° 10 en cours (encore 2 jours), seuls les jours déjà refaits comptent"
    assert f"Grade indicatif : {note}" in "\n".join(AUT.indicatif_lines(rep["grade_indicatif"]))
    assert f"Stratégies actions : {note}" in AUT.text(rep) and AUT.compact(rep)["partiel"] == rep["partiel"]
    ro = DB.open_readonly(path)                                 # serveur web (/api/lab/autopsie) : lecture seule
    assert AUT.report(ro)["grade_indicatif"]["go_confirme"]["par_go"]["go"] == 2
    ro.close()
    db.set(job, "rattrapage:replay", "2026-09-16")
    rep = AUT.report(db.conn)
    assert rep["partiel"] is None and rep["grade_indicatif"]["go_confirme"]["par_go"]["go"] == 4
    assert "rattrapage" not in "\n".join(AUT.indicatif_lines(rep["grade_indicatif"]))


# --- I4 : données manquantes ----------------------------------------------------------------------------------

SYMS = ["AAA", "BBB", "SPY"]


def _stock_db(path):
    db = LabDB(path)
    fill_stock(db, SYMS, CO.sessions_back(START, 125), START)
    return db


def _source(db, monkeypatch, fail=(), fail_daily=()):
    """Alpaca factice : sert les bougies déjà en base, lève SourceError pour les symboles en panne (lots d'un
    symbole pour isoler chaque échec)."""
    from app.core.http import SourceError

    stored = {(s, tf): db.get_bars("stock", s, tf, 0, 2 ** 40) for s in SYMS for tf in (5, 1440)}

    async def bars(symbols, tf, start, end, transport=None):
        if set(symbols) & set(fail_daily if tf == 1440 else fail):
            raise SourceError("HTTP 503 (après 5 essais)")
        a, b = start.timestamp(), end.timestamp()
        return {s: [x for x in stored.get((s, tf), []) if a <= x["t"] < b] for s in symbols}

    async def symbols(db_, today, transport=None):
        return list(SYMS)

    monkeypatch.setattr(CO.SRC, "alpaca_bars", bars)
    monkeypatch.setattr(CO, "stock_universe_symbols", symbols)
    monkeypatch.setattr(CO, "STOCK_BATCH", 1)
    monkeypatch.setattr(CO, "DAILY_BATCH", 1)


def _drop_session(db, symbol, day=START, after=None):
    o, c = CO.session_ts(day)
    db.conn.execute("DELETE FROM bars WHERE market = 'stock' AND symbol = ? AND tf = 5 AND ts >= ? AND ts < ?",
                    (symbol, o if after is None else after, c))


async def test_panne_de_la_source_le_jour_n_avance_pas_puis_saute(tmp_path, monkeypatch):
    path = str(tmp_path / "lab.db")
    db = _stock_db(path)
    _source(db, monkeypatch, fail=SYMS, fail_daily=SYMS)
    job = "replay:stock"
    assert await R.replay(START, START, "stock", db, now=NOW) == 0
    assert db.get(job, "last_day") is None and db.get(job, f"attempts:{START}") == 1 and db.count_trades() == 0
    assert await R.replay(START, START, "stock", db, now=NOW) == 0
    assert await R.replay(START, START, "stock", db, now=NOW) == 1     # 3e essai : toujours en panne, sauté
    assert "SourceError" in db.get(job, f"skipped:{START}") and db.count_trades() == 0
    db.close()
    st = STATUS.status(path)
    assert st["replay"]["stock"]["jours_sautes"] == 1
    assert st["replay"]["stock"]["days_done"] == 0 and st["replay"]["stock"]["pct"] < 100
    assert "1 jour(s) sauté(s)" in STATUS.text(st)


async def test_un_symbole_en_panne_reessaye_puis_calcule_sans_lui(tmp_path, monkeypatch):
    path = str(tmp_path / "lab.db")
    db = _stock_db(path)
    _drop_session(db, "BBB")                              # sa séance n'est jamais arrivée
    _source(db, monkeypatch, fail=["BBB"])
    job = "replay:stock"
    for attempt in (1, 2):
        assert await R.replay(START, START, "stock", db, now=NOW) == 0
        assert db.get(job, "last_day") is None and db.get(job, f"attempts:{START}") == attempt
    assert await R.replay(START, START, "stock", db, now=NOW) == 1     # dernier essai : calculé sans BBB, noté
    info = db.get(job, f"day:{START}")
    assert db.get(job, "last_day") == START.isoformat() and db.get(job, f"skipped:{START}") is None
    assert info["missing"] == 1 and info["missing_symbols"] == ["BBB"] and info["trades"] == 2
    assert info["absent_symbols"] == [{"symbol": "BBB", "motif": "séance vide"}]
    db.close()
    st = STATUS.status(path)["replay"]["stock"]
    assert st["jours_partiels"] == 1 and st["absents"] == 1 and st["jours_sautes"] == 0 and st["days_done"] == 1


async def test_journalier_en_echec_pas_d_univers_ampute(tmp_path, monkeypatch):
    import pytest

    from app.core.http import SourceError

    db = _stock_db(str(tmp_path / "lab.db"))
    _source(db, monkeypatch, fail_daily=["BBB"])
    with pytest.raises(SourceError, match="BBB"):
        await R.prepare_day(db, "stock", START, NOW)
    missing: set = set()
    universe = await R.prepare_day(db, "stock", START, NOW, missing_out=missing)   # dernier essai
    assert missing == {"BBB"} and {u["symbol"] for u in universe} == set(SYMS)


def test_valeurs_sans_trade_et_leur_motif(tmp_path, monkeypatch):
    from app.lab import generator as G
    from app.trading import service as T

    db = _stock_db(str(tmp_path / "lab.db"))
    _drop_session(db, "BBB")
    real = G.stock_plans

    def no_plan(symbol, inp, s):
        if symbol == "SPY":
            raise T.PlanError("pas de niveau")
        return real(symbol, inp, s)

    monkeypatch.setattr(G, "stock_plans", no_plan)
    universe = [{"symbol": s, "rank": k + 1, "reason": "dollar_volume", "metrics": {}}
                for k, s in enumerate(["AAA", "BBB", "SPY", "ZZZ"])]
    assert R.run_day(db, "stock", START, "replay", universe) == 1
    info = db.get("replay:stock", f"day:{START}")
    assert info["errors"] == 0 and info["absent"] == 3
    assert info["absent_motifs"] == {"séance vide": 1, "plan impossible": 1, "historique<100": 1}
    assert {a["symbol"]: a["motif"] for a in info["absent_symbols"]} == {
        "BBB": "séance vide", "SPY": "plan impossible", "ZZZ": "historique<100"}


def test_paire_crypto_sans_bougies_absente(tmp_path):
    db = LabDB(str(tmp_path / "lab.db"))
    day = date(2026, 9, 14)
    fill_crypto(db, ["BTC-USDT"], date(2026, 7, 20), date(2026, 9, 16))
    universe = [{"symbol": s, "rank": k + 1, "reason": "volume", "metrics": {}}
                for k, s in enumerate(["BTC-USDT", "ZZZ-USDT"])]
    late = datetime.fromtimestamp(CO.utc_midnight(day) + 44 * 3600, timezone.utc)
    assert R.run_day(db, "crypto", day, "live", universe, now=late) == 6
    info = db.get("live:crypto", f"day:{day}")
    assert info["absent_symbols"] == [{"symbol": "ZZZ-USDT", "motif": "historique<100"}]
    ctx = json.loads(db.conn.execute("SELECT context FROM trades LIMIT 1").fetchone()[0])
    assert ctx["barres_seance"] == 288 and ctx["ouverture_presente"] and ctx["couverture"] == 1.0


def test_couverture_par_trade_et_seance_tronquee(tmp_path):
    db = _stock_db(str(tmp_path / "lab.db"))
    o, c = CO.session_ts(START)
    _drop_session(db, "AAA", after=o + 150 * 60)          # coupée à midi : 30 bougies sur 78
    _drop_session(db, "BBB", after=None)
    db.put_bars("stock", "BBB", 5, [{"t": t, "o": 30.0, "h": 30.1, "l": 29.9, "c": 30.0, "v": 1000.0}
                                    for t in range(o + 3600, c, 300)])    # ouverture manquante
    R.run_day(db, "stock", START, "replay")
    ctx = {s: json.loads(x) for s, x in db.conn.execute("SELECT symbol, context FROM trades")}
    assert ctx["AAA"]["barres_seance"] == 30 and ctx["AAA"]["ouverture_presente"]
    assert ctx["AAA"]["couverture"] == round(30 / 78, 3)
    assert not ctx["BBB"]["ouverture_presente"] and ctx["BBB"]["barres_seance"] == 66
    assert ctx["SPY"]["couverture"] == 1.0
    cov = STATS.compute(db.conn)["couverture_donnees"]
    assert cov == {"stock": {"mesures": 3, "sous_50_pct": 1, "part": round(1 / 3, 4)}}


def test_couverture_interdite_dans_les_regles():
    import pytest

    from app.lab import validator as V

    for field in ("context.couverture", "context.barres_seance", "context.ouverture_presente"):
        with pytest.raises(V.RuleError, match="regard vers l'avenir"):
            V.compile_cond({"field": field, "op": "<", "value": 0.5})


async def test_direct_crypto_creneaux_sans_l_absent_jour_complet_reessaye(tmp_path, monkeypatch):
    from app.core.http import SourceError

    db = LabDB(str(tmp_path / "lab.db"))
    day = date(2026, 9, 14)
    pairs = ["BTC-USDT", "ETH-USDT", "SOL-USDT"]
    fill_crypto(db, pairs, date(2026, 7, 20), date(2026, 9, 16))
    stored = {(s, tf): db.get_bars("crypto", s, tf, 0, 2 ** 40) for s in pairs for tf in (5, 1440)}
    db.conn.execute("DELETE FROM bars WHERE symbol = 'ETH-USDT' AND tf = 5 AND ts >= ?", (CO.utc_midnight(day),))

    async def candles(inst_id, tf, start, end, transport=None, max_calls=100000):
        if inst_id == "ETH-USDT":
            raise SourceError("HTTP 503")
        return [x for x in stored[(inst_id, tf)] if start <= x["t"] < end]

    async def symbols(db_, today, transport=None):
        return list(pairs)

    monkeypatch.setattr(CO.SRC, "okx_candles", candles)
    monkeypatch.setattr(CO, "crypto_universe_symbols", symbols)
    early = datetime.fromtimestamp(CO.utc_midnight(day) + 30 * 3600, timezone.utc)   # créneaux de 0 h et 4 h échus
    out = await R.run_live_day(day, db, ("crypto",), now=early)
    assert out["crypto"] == 4                                    # BTC et SOL, sans attendre ETH
    assert db.get("live:crypto", f"day:{day}")["missing_symbols"] == ["ETH-USDT"]
    assert db.get("live:crypto", "last_day") is None
    late = datetime.fromtimestamp(CO.utc_midnight(day) + 44 * 3600 + 600, timezone.utc)
    assert (await R.run_live_day(day, db, ("crypto",), now=late))["crypto"] == 0       # complet : nouvel essai
    assert db.get("live:crypto", f"attempts:{day}") == 1 and db.get("live:crypto", "last_day") is None


def test_jour_crypto_complet_en_echec_reessaye():
    day = date(2026, 10, 8)
    done = CO.utc_midnight(day) + 20 * 3600                       # tous les créneaux déjà journalisés
    complete = datetime.fromtimestamp(done + 86400 + 600, timezone.utc)
    assert W.crypto_windows_due(day, complete, done)              # le calcul complet a échoué : nouvel essai
    before = datetime.fromtimestamp(done + 86400 - 600, timezone.utc)
    assert not W.crypto_windows_due(day, before, CO.utc_midnight(day) + 16 * 3600)


# --- I5 : fenêtres honnêtes (période récente coupée au trou entre le rejeu et le direct) ------------------------

def _demo(db):
    """Base de démonstration : rejeu du 1er décembre 2025 au 20 mars 2026, puis direct du 6 au 9 octobre 2026."""
    synthetic_trades(db, n_days=80, per_day=5, first=date(2025, 12, 1))
    synthetic_trades(db, n_days=4, per_day=5, first=date(2026, 10, 6), origin="live", seed=2)
    return db.conn.execute("SELECT MAX(day) FROM trades WHERE origin = 'replay'").fetchone()[0]


def test_periode_recente_coupee_au_trou():
    db = LabDB(":memory:")
    last_replay = _demo(db)
    assert last_replay == "2026-03-20"
    spans: dict = {}
    assert STATS._recent_cutoffs(db.conn, {}, spans) == {"stock": "2026-10-06", "crypto": ""}
    gap = (date(2026, 10, 6) - date.fromisoformat(last_replay)).days
    assert spans == {"stock": {"trou_max_jours": gap, "jours_direct": 4}} and gap >= 180
    m = STATS.compute(db.conn)["marches"]["stock"]
    assert m["debut_recent"] == "2026-10-06" and m["trou_max_jours"] == gap and m["jours_direct"] == 4
    assert m["par"]["periode"]["recent"]["plans"] == 20                  # le direct seul


def test_periode_recente_sans_trou_inchangee():
    days = [(date.fromordinal(date(2026, 3, 2).toordinal() + k).isoformat(), 0) for k in range(25)][::-1]
    win = STATS.recent_window(days[:STATS.RECENT_DAYS])
    assert win == {"debut": days[STATS.RECENT_DAYS - 1][0], "trou_max_jours": 1, "jours_direct": 0}
    assert STATS.recent_window(days[:5])["debut"] == ""                  # moins de 20 jours : tout est récent
    assert STATS.recent_window([])["debut"] == ""
    week = [("2026-03-09", 0), ("2026-03-06", 0), ("2026-03-05", 0)]    # week-end : 3 jours, pas un trou
    assert STATS.recent_window(week) == {"debut": "", "trou_max_jours": 3, "jours_direct": 0}


def test_grade_indicatif_annonce_sa_fenetre_discontinue():
    db = LabDB(":memory:")
    rows = []
    for k, day in enumerate([date(2026, 1, 5).toordinal() + i for i in range(70)] +
                            [date(2026, 10, 6).toordinal() + i for i in range(4)]):
        d = date.fromordinal(day).isoformat()
        origin = "live" if d >= "2026-10-06" else "replay"
        rows += [(origin, f"S{j}", d, 0.4 if (k + j) % 2 else -0.2) for j in range(2)]
    db.conn.executemany("INSERT INTO strat_trades (origin, market, symbol, day, strategy, slot, triggered, r_sans_be, "
                        "confirmation, tp1_hit) VALUES (?, 'stock', ?, ?, 'go_confirme', 0, 1, ?, 'confirme', 0)", rows)
    gi = AUT.go_indicatif(db.conn)
    win = gi["go_confirme"]["fenetre"]
    assert win["jours"] == GRADE.FENETRE_JOURS and win["jours_direct"] == 4 and win["au"] == "2026-10-09"
    assert win["trou_max_jours"] == (date(2026, 10, 6) - date(2026, 3, 15)).days
    assert any("fenêtre discontinue : trou de" in x and "4 jours en direct" in x for x in AUT.indicatif_lines(gi))
    db.conn.execute("DELETE FROM strat_trades WHERE origin = 'live'")
    gi = AUT.go_indicatif(db.conn)
    assert gi["go_confirme"]["fenetre"]["trou_max_jours"] == 1
    assert not any("discontinue" in x for x in AUT.indicatif_lines(gi))
