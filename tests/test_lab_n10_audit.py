"""Deuxième tour d'avis de GPT et Gemini (10 octobre 2026), partie C : la n° 10 traçable et honnête.
Archive d'audit avant réécriture (C1), audit apparié ancien → nouveau (C2), tableau partiel structurel (C3),
indicateur « n° 10 complète » (C4), coût tracé (C6)."""
import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app.lab import audit as AUD
from app.lab import brief as BRIEF
from app.lab import collector as CO
from app.lab import kill_complet as KC
from app.lab import plus10 as P10
from app.lab import rattrapage as RAT
from app.lab import status as STATUS
from app.lab import strat_stats as SS
from app.lab import strategies as ST
from app.lab import worker as W
from app.lab.db import STRAT_COLUMNS, STRAT_TRACE, LabDB
from app.trading import killmode as K


def _strat(symbol, r=0.5, strategy="pullback", slot=0, **kw):
    return {"symbol": symbol, "strategy": strategy, "slot": slot, "triggered": 1, "r_net": r, "exit_reason": "stop",
            "mfe_r": 0.2, "mae_r": -1.0, "tp1_hit": 0} | kw


def _old(db, market, day, symbols, r=-1.0, decl=None, strategy="pullback", **kw):
    """Lignes d'avant la déclaration en vigueur (declaration NULL : d'avant le suivi)."""
    cols = ["origin", "market", "symbol", "day", "strategy", "slot", "triggered", "r_net", "declaration", *kw]
    db.conn.executemany(f"INSERT INTO strat_trades ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                        [("replay", market, s, day, strategy, 0, 1, r, decl, *kw.values()) for s in symbols])


def _daily(db, *days):
    db.put_bars("stock", "SPY", 1440, [{"t": CO.ny_midnight(date.fromisoformat(d)), "o": 1, "h": 1, "l": 1, "c": 1}
                                       for d in days])


# --- C3 : mélange structurel ------------------------------------------------------------------------------------

def test_ancienne_ligne_au_dela_du_journalier_reste_exclue(tmp_path):
    """verif2/sceptique-s4/melange.py : journalier jusqu'au 11/03, lignes d'avant les 10, 11 et 12/03 ; le rattrapage
    refait le 10 et le 11 et s'arrête (remaining = 0) : la ligne du 12 revenait dans le tableau."""
    db = LabDB(str(tmp_path / "lab.db"))
    job = ST.progress_job("stock")
    db.set("replay:stock", "start", "2025-03-10")
    db.set("replay:stock", "last_day", "2025-03-12")
    _daily(db, "2025-03-10", "2025-03-11")
    _old(db, "stock", "2025-03-12", ["AAA"], r=0.8)
    for day in ("2025-03-10", "2025-03-11"):
        db.replace_strat("replay", "stock", day, [_strat("AAA")])
    db.set(job, "rattrapage:replay", "2025-03-11")
    assert RAT.remaining(db.conn, "stock") == {"replay": 0, "live": 0}
    rep = SS.report(db.conn)
    assert rep["partiel"] == {"stock": {"declaration": ST.DECLARATION, "jours_rattrapage": 0, "lignes_exclues": 1}}
    item = rep["strategies"]["stock"]["pullback"]
    assert item["jours"] == 2 and item["esperance_r"] == 0.5          # la ligne à +0,8 R de l'ancienne mesure exclue
    _daily(db, "2025-03-12")                                          # son journalier arrive : le 12 est dû
    assert SS.partial(db.conn)["stock"]["jours_rattrapage"] == 1


def test_jour_du_direct_qui_attend_ne_rend_pas_le_tableau_partiel(tmp_path):
    """E3 point 3 : en actions, chaque jour du direct passe par le rattrapage (journalier de la séance suivante) ; il ne
    rend pas le tableau « partiel » quand rien d'autre n'est en révision."""
    db = LabDB(str(tmp_path / "lab.db"))
    db.set("lab", "live_start", "2026-10-12")
    db.set("live:stock", "last_day", "2026-10-13")
    _daily(db, "2026-10-12", "2026-10-13")
    db.replace_strat("live", "stock", "2026-10-12", [_strat("AAA")])
    db.set(ST.progress_job("stock"), "rattrapage:live", "2026-10-12")
    assert RAT.remaining(db.conn, "stock") == {"replay": 0, "live": 1}
    assert SS.partial(db.conn) == {}
    rep = SS.report(db.conn)
    assert rep["partiel"] is None and "Tableau partiel" not in SS.text(rep)
    _old(db, "stock", "2026-10-13", ["OLD"])                           # mais une ligne d'avant : partiel, et comptée
    assert SS.partial(db.conn) == {"stock": {"declaration": ST.DECLARATION, "jours_rattrapage": 1, "lignes_exclues": 1}}


def test_lignes_inserees_sans_declaration_hors_rattrapage_restent_lues():
    """Une base sans rattrapage en route (aucun curseur, aucune ligne de la déclaration) n'est pas « partielle »."""
    db = LabDB(":memory:")
    _old(db, "stock", "2025-03-10", ["AAA", "BBB"], r=0.4)
    assert SS.partial(db.conn) == {}
    assert SS.report(db.conn)["strategies"]["stock"]["pullback"]["declenches"] == 2


# --- C4 : « n° 10 complète » -------------------------------------------------------------------------------------

def test_n9_seances_a_recalculer():
    db = LabDB(":memory:")
    assert P10.a_recalculer(db.conn) == 0
    db.set(P10.JOB, "2025-03-10", {"population": 3, "mesure": 9})          # calculée avant la n° 10
    db.set(P10.JOB, "2025-03-11", {"population": 3})                       # d'avant la marque : à recalculer aussi
    db.set(P10.JOB, "2025-03-12", {"population": 3, "mesure": P10.MESURE})
    db.set(P10.JOB, "2025-03-13", {"absente": True})
    db.set(P10.JOB, "2000-03-06", {"population": 3, "mesure": 9})          # bloc scellé (déplacé en 2000 par conftest)
    assert P10.a_recalculer(db.conn) == 2


def test_indicateur_n10_complete_ou_ce_qui_reste(tmp_path):
    path = str(tmp_path / "lab.db")
    db = LabDB(path)
    n = STATUS.declaration_state(db.conn)
    assert n == {"numero": ST.LATEST_DECLARATION, "lignes_v2": ST.DECLARATION, "jours": {"stock": 0, "crypto": 0},
                 "lignes_anciennes": {"stock": 0, "crypto": 0}, "seances_n9": 0, "seances_kill_complet": 0,
                 "seances_n12": 0, "bougies_9h30": {"lues": 0, "utiles": 0}, "complete": True}
    assert STATUS.declaration_line(n) == (f"n° {ST.LATEST_DECLARATION} : complète (stratégies, n° 9 et Killmode complet "
                                         "re-mesurés, n° 12 calculée)")
    db.set("replay:stock", "start", "2025-03-10")                          # deux jours d'actions à rattraper
    db.set("replay:stock", "last_day", "2025-03-11")
    _daily(db, "2025-03-10", "2025-03-11")
    db.set(P10.JOB, "2025-03-10", {"population": 3, "mesure": 9})
    db.conn.execute("INSERT INTO kill_complet (day, symbol, groupe, empreinte, triggered, cout) VALUES "
                    "('2025-03-10', 'AAA', 'kill', 'x', 1, 'ancien'), ('2025-03-10', 'BBB', 'kill', 'x', 1, 'ancien'), "
                    "('2025-03-11', 'AAA', 'kill', 'x', 1, ?)", (KC.cost_fingerprint(),))
    n = STATUS.declaration_state(db.conn)
    assert not n["complete"] and n["jours"] == {"stock": 2, "crypto": 0} and n["seances_n9"] == 1
    assert n["seances_kill_complet"] == 1 and KC.stale_days(db.conn) == 1
    assert STATUS.declaration_line(n) == (f"⏳ n° {ST.LATEST_DECLARATION} : 2 j actions, 0 j crypto à rattraper, 1 séance "
                                          "n° 9 à recalculer, 1 séance Killmode complet à recalculer")
    st = STATUS.status(path)
    assert st["declaration"] == n and STATUS.declaration_line(n) in STATUS.text(st)
    data = BRIEF.build(db.conn, BRIEF.datetime(2026, 10, 10, 20, tzinfo=BRIEF.timezone.utc), path)
    assert data["declaration"]["complete"] is False and data["declaration"]["texte"] == STATUS.declaration_line(n)


def test_indicateur_lignes_d_avant_sans_jour_a_refaire(tmp_path):
    db = LabDB(str(tmp_path / "lab.db"))
    db.set(ST.progress_job("crypto"), "rattrapage:replay", "2025-03-11")
    _old(db, "crypto", "2025-03-12", ["AAA-USDT", "BBB-USDT"])
    n = STATUS.declaration_state(db.conn)
    assert n["lignes_anciennes"]["crypto"] == 2 and not n["complete"]
    assert STATUS.declaration_line(n) == (f"⏳ n° {ST.LATEST_DECLARATION} : 2 lignes d'avant sans jour à refaire "
                                          "(exclues)")


def test_n9_texte_dit_les_seances_a_recalculer():
    rep = {"numero": 9, "seances": 3, "du": "2025-03-10", "au": "2025-03-12", "essais": {}, "a_recalculer": 4,
           "amplitude": {}, "reste_apres_decision": {}, "concordance_direct": {"seances": 0}}
    assert "4 séances encore à recalculer (mesure n° 10)" in P10.text(rep)
    assert "à recalculer" not in P10.text(rep | {"a_recalculer": 0})


# --- C6 : modèle de coût des lignes ----------------------------------------------------------------------------

def test_cout_rev_ecrit_par_replace_strat_et_migre(tmp_path):
    import sqlite3

    from app.lab import db as DB
    from app.lab import empreinte as E

    path = str(tmp_path / "lab.db")
    old = sqlite3.connect(path)
    old.executescript(DB.SCHEMA)                          # schéma sans les colonnes ajoutées par migrate()
    old.close()
    db = LabDB(path)
    for table in ("strat_trades", "strat_trades_sealed", "plus10_trades"):
        assert "cout_rev" in {r[1] for r in db.conn.execute(f"PRAGMA table_info({table})")}, table
    db.replace_strat("replay", "stock", "2025-03-10", [_strat("AAA")])
    db.replace_strat("replay", "crypto", "2025-03-10", [_strat("AAA-USDT")])
    got = dict(db.conn.execute("SELECT market, cout_rev FROM strat_trades"))
    assert got == {"stock": KC.cost_fingerprint(), "crypto": E.strat_cost_rev("crypto")}
    assert got["stock"] != got["crypto"]
    db.close()
    LabDB(path).close()                                   # migration idempotente


def test_cout_rev_suit_le_bareme_et_la_revision(monkeypatch):
    from app.lab import empreinte as E
    from app.trading import killmode as K

    stock, crypto = E.strat_cost_rev("stock"), E.strat_cost_rev("crypto")
    real_r = K.real_r
    monkeypatch.setattr(K, "real_r", lambda call, res: None if real_r(call, res) is None else real_r(call, res) + 0.01)
    assert E.strat_cost_rev("stock") != stock and E.strat_cost_rev("crypto") == crypto   # barème IBKR : actions seules
    monkeypatch.setattr(ST, "SIM_REV", ST.SIM_REV + 1)
    assert E.strat_cost_rev("crypto") != crypto


def test_revisions_du_code_et_du_cout_affichees_quand_plusieurs(monkeypatch):
    from app.lab import empreinte as E

    db = LabDB(":memory:")
    db.replace_strat("replay", "stock", "2025-03-10", [_strat("AAA"), _strat("BBB")])
    rep = SS.report(db.conn)
    assert rep["revisions"] == {"stock": {"code": {E.code_rev("strat"): 2}, "cout": {KC.cost_fingerprint(): 2}}}
    assert "révision" not in SS.text(rep)
    first = E.code_rev("strat")
    monkeypatch.setitem(E.CODE_REV, "strat", "autrecode")                       # un déploiement pendant le rattrapage
    db.replace_strat("replay", "stock", "2025-03-11", [_strat("AAA")])
    db.conn.execute("UPDATE strat_trades SET cout_rev = NULL WHERE symbol = 'BBB'")    # écrite avant la colonne
    rep = SS.report(db.conn)
    assert rep["revisions"]["stock"] == {"code": {first: 2, "autrecode": 1},
                                         "cout": {KC.cost_fingerprint(): 2, "inconnue": 1}}
    text = SS.text(rep)
    assert "ℹ️ Lignes actions de 2 révisions du code" in text and "/ 1 du coût" in text   # « inconnue » n'en est pas une
    assert "· 1 ligne sans révision du coût (écrites avant son suivi)" in text


async def test_cout_rev_des_lignes_de_la_n9(monkeypatch):
    from datetime import datetime, timezone

    db = LabDB(":memory:")
    day = date(2025, 3, 10)

    async def no_inactive(*a, **k):
        return set()

    async def rows(db_, d, inactive, transport=None):
        return [{"day": d.isoformat(), "symbol": "UPA", "essai": "plus10_orb15", "liste": "s10", "rang": 1,
                 "triggered": 1, "r_net": 0.5}], {"listes": {"s10": ["UPA"], "rebond": []}}

    monkeypatch.setattr(P10, "ready_days", lambda db_: [day])
    monkeypatch.setattr(P10, "inactive_symbols", no_inactive)
    monkeypatch.setattr(P10, "day_rows", rows)
    assert await P10.step(db, datetime(2025, 3, 11, tzinfo=timezone.utc), 60) == 1
    assert db.conn.execute("SELECT symbol, cout_rev FROM plus10_trades").fetchall() == [("UPA", KC.cost_fingerprint())]


# --- C1 : archive des anciennes lignes avant réécriture ----------------------------------------------------------

DAYS = ("2025-03-10", "2025-03-11")
SEALED_DAY = "2000-03-06"  # dans le bloc scellé des tests (conftest)


def _archive(db) -> Path:
    return AUD.archive_path(db.path, ST.DECLARATION)


def _read(path: Path, sql: str, args=()) -> list:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _kill(db, day, symbol, cout, r=0.4):
    db.conn.execute("INSERT INTO kill_complet (day, symbol, groupe, empreinte, triggered, r_net, cout) "
                    "VALUES (?, ?, 'catalyseur', 'e', 1, ?, ?)", (day, symbol, r, cout))


def _p10(db, day, symbol, mesure, r=0.2):
    db.conn.execute("INSERT INTO plus10_trades (day, symbol, essai, liste, rang, triggered, r_net) "
                    "VALUES (?, ?, 'plus10_open', 'S+10', 1, 1, ?)", (day, symbol, r))
    db.set(P10.JOB, day, {"mesure": mesure, "lignes": 1} if mesure else {"lignes": 1})


def _base(tmp_path):
    """Base hors scellé : lignes de stratégies d'avant la n° 10 sur deux jours à refaire, une ligne scellée, Killmode
    complet à deux coûts, n° 9 à deux mesures."""
    db = LabDB(str(tmp_path / "lab.db"))
    db.set("replay:stock", "start", DAYS[0])
    db.set("replay:stock", "last_day", DAYS[-1])
    _daily(db, *DAYS)
    for day in DAYS:
        _old(db, "stock", day, ["AAA", "BBB"], r=-0.7, entry=10.0, stop=9.0, temoin_r=0.1, temoin_triggered=1)
    _old(db, "crypto", DAYS[0], ["BTC-USDT"], r=0.9, decl=9)
    db.conn.execute("INSERT INTO strat_trades_sealed (origin, market, symbol, day, strategy, slot, triggered, r_net) "
                    "VALUES ('replay', 'stock', 'SEAL', ?, 'pullback', 0, 1, 5.0)", (SEALED_DAY,))
    _kill(db, DAYS[0], "AAA", None)
    _kill(db, DAYS[1], "AAA", KC.cost_fingerprint())
    _p10(db, DAYS[0], "AAA", 9)
    _p10(db, DAYS[1], "BBB", P10.MESURE)
    return db


async def test_archive_faite_avant_le_premier_replace_strat(tmp_path, monkeypatch):
    db = _base(tmp_path)
    seen = []
    replace = db.replace_strat

    def spy(origin, market, day, rows, symbols=None):
        path = _archive(db)
        seen.append(_read(path, "SELECT COUNT(*) FROM strat_trades")[0][0] if path.exists() else None)
        return replace(origin, market, day, rows, symbols)

    async def rows(db_, origin, day, transport=None, missing=None):
        return [_strat("AAA", r=0.3, entry=10.0, stop=9.0, temoin_r=0.2, temoin_triggered=1)]

    db.replace_strat = spy
    monkeypatch.setattr(RAT, "stock_rows", rows)
    assert await RAT.step(db, "stock", 60) == 2
    # n° 13 : la crypto reste en n° 11, ses lignes vont dans le fichier de sa déclaration ; même déclaration : un seul
    # fichier, 4 actions + 1 crypto.
    same = ST.CRYPTO_DECLARATION == ST.DECLARATION
    assert seen == [4 + same] * 2                                       # dès le 1er appel
    assert db.conn.execute("SELECT COUNT(*) FROM strat_trades WHERE market = 'stock' AND declaration IS NOT ?",
                           (ST.DECLARATION,)).fetchone()[0] == 0          # plus aucune ancienne ligne en base
    path = _archive(db)
    assert path == tmp_path / "archives" / f"labo2-avant-n{ST.DECLARATION}.db"
    archived = _read(path, "SELECT market, symbol, day, r_net, declaration FROM strat_trades ORDER BY market, day, symbol")
    crypto = [("crypto", "BTC-USDT", DAYS[0], 0.9, 9)]
    assert archived == [*(crypto if same else []),
                        *[("stock", s, d, -0.7, None) for d in DAYS for s in ("AAA", "BBB")]]   # intacte, telle quelle
    if not same:
        assert _read(AUD.archive_path(db.path, ST.CRYPTO_DECLARATION),
                     "SELECT market, symbol, day, r_net, declaration FROM strat_trades") == crypto
    assert _read(path, "SELECT day, symbol, cout FROM kill_complet") == [(DAYS[0], "AAA", None)]  # autre coût seul
    assert _read(path, "SELECT day, symbol FROM plus10_trades") == [(DAYS[0], "AAA")]          # autre mesure seule
    assert [json.loads(v)["mesure"] for _, v in _read(path, "SELECT day, value FROM plus10_seances")] == [9]
    info = _read(path, "SELECT cle, tables, remplace, declaration, lignes, note FROM archive_info ORDER BY cle")
    assert [r[0] for r in info] == [f"kill_complet:{KC.cost_fingerprint()}", f"plus10:m{P10.MESURE}",
                                    *([f"strats:crypto:d{ST.CRYPTO_DECLARATION}"] if same else []),
                                    f"strats:stock:d{ST.DECLARATION}"]
    stock = info[-1]
    assert json.loads(stock[2]) == ["inconnue (d'avant le suivi)"] and stock[3] == ST.DECLARATION
    assert json.loads(stock[4]) == {"strat_trades": {"choisies": 4, "ajoutees": 4, "deja_la": 0}}
    # Lignes d'avant le suivi (pas de la déclaration précédente) : la note ne prétend pas au contrôle à ≈ 0.
    assert stock[5] == (f"mesures d'avant la n° {ST.DECLARATION}, pas toutes de la n° {ST.DECLARATION - 1} (voir "
                        "remplace) : l'écart à l'audit mêle leurs effets")
    notes = {r[0]: r[5] for r in info}
    assert notes[f"kill_complet:{KC.cost_fingerprint()}"] == (f"Killmode complet : autre empreinte de coût que "
                                                               f"{KC.cost_fingerprint()}")
    assert notes[f"plus10:m{P10.MESURE}"] == f"n° 9 : séances d'une autre mesure que la n° {P10.MESURE}"
    assert AUD._note({"table": "strat_trades"}, ST.DECLARATION, [ST.DECLARATION - 1]) == AUD.NOTES[ST.DECLARATION]
    assert AUD.NOTES[10] == ("mesures d'avant la n° 10 : simulateur révision 1, témoin sur la journée, 5 tirages, "
                             "prix ajustés")
    # Rien du scellé : ni ses lignes, ni ses tables.
    assert not any("sealed" in t for (t,) in _read(path, "SELECT name FROM sqlite_master WHERE type = 'table'"))
    assert _read(path, "SELECT COUNT(*) FROM strat_trades WHERE day = ? OR symbol = 'SEAL'", (SEALED_DAY,)) == [(0,)]
    # Second appel sans effet : rien d'ajouté, pas d'archivage de plus.
    before = path.read_bytes()
    assert AUD.step(db) == {} and await RAT.step(db, "stock", 60) == 0
    assert path.read_bytes() == before


async def test_archive_en_tete_du_passage_et_rien_de_reecrit_si_elle_echoue(tmp_path, monkeypatch):
    """Ordre du passage : archive, puis grade, Labo 2, rejeu, rattrapage ; en échec, ni rattrapage ni Killmode complet
    ni n° 9 (lab2 reçoit hold), le reste tourne."""
    db = LabDB(str(tmp_path / "lab.db"))
    calls = []

    def archive(db_):
        calls.append("archive")
        if fail:
            raise sqlite3.OperationalError("disk I/O error")
        return {}

    async def lab2(db_, now, collect=True, hold=False):
        calls.append(f"lab2 hold={hold}")
        return {}

    async def replay(start, end, market, *a, **k):
        calls.append(f"rejeu {market}")
        return 0

    async def rattrapage(db_, market, seconds, transport=None):
        calls.append(f"rattrapage {market}")
        return 0

    monkeypatch.setattr(W.AUD, "step", archive)
    monkeypatch.setattr(W.GRADE, "step", lambda db_, now: calls.append("grade"))
    monkeypatch.setattr(W, "lab2", lab2)
    monkeypatch.setattr(W.R, "replay", replay)
    monkeypatch.setattr(W.RAT, "step", rattrapage)
    monkeypatch.setattr(W.alpaca, "configured", lambda: True)
    night = datetime(2026, 10, 11, 3, tzinfo=timezone.utc)           # dimanche : « nuit », séance US fermée
    fail = False
    report = await W.tick(db, night, collect=True)
    assert calls[:2] == ["archive", "grade"] and report["archive_audit"] == {}
    assert "lab2 hold=False" in calls and calls[-2:] == ["rattrapage stock", "rattrapage crypto"]
    calls.clear()
    fail = True
    report = await W.tick(db, night + timedelta(minutes=10), collect=True)
    assert calls[0] == "archive" and "lab2 hold=True" in calls and "rejeu crypto" in calls
    assert not any(c.startswith("rattrapage") for c in calls) and "rattrapage" not in report
    assert report["archive_audit"] == "échec : OperationalError"


async def test_lab2_en_attente_saute_killmode_complet_et_n9(tmp_path, monkeypatch):
    db = LabDB(str(tmp_path / "lab.db"))
    ran = []
    monkeypatch.setattr(W.KC, "step", lambda *a: ran.append("kc"))
    monkeypatch.setattr(W.P10, "step", lambda *a: ran.append("p10"))
    monkeypatch.setattr(W.DER, "collect", lambda *a: ran.append("derives"))
    monkeypatch.setattr(W.CAT, "fetch_news", lambda *a: 0)
    monkeypatch.setattr(W.ARC, "step", lambda *a: 0)
    night = datetime(2026, 10, 11, 3, tzinfo=timezone.utc)
    report = await W.lab2(db, night, collect=True, hold=True)
    assert ran == ["derives"] and "killmode_complet" not in report and "audit" in report
    report = await W.lab2(db, night, collect=True)
    assert ran[1:] == ["kc", "p10", "derives"]


def test_copie_manuelle_respectee(tmp_path):
    """La commande de DEPLOY.md (CREATE TABLE … AS SELECT *, sans archive_info ni index, faite avant le déploiement
    du 2e tour d'avis, donc sans la colonne cout_rev) : tables gardées, rien d'effacé ni de modifié, seules les absentes
    ajoutées."""
    db = _base(tmp_path)
    path = _archive(db)
    path.parent.mkdir()
    cols = [c for c in (*STRAT_COLUMNS, *(c for c, _ in STRAT_TRACE)) if c != "cout_rev"]
    db.conn.execute("ATTACH DATABASE ? AS a", (str(path),))
    db.conn.execute(f"CREATE TABLE a.strat_trades AS SELECT id, {', '.join(cols)} FROM main.strat_trades "
                    "WHERE declaration IS NULL")
    db.conn.execute("CREATE TABLE a.kill_complet AS SELECT * FROM main.kill_complet")
    db.conn.execute("CREATE TABLE a.plus10_trades AS SELECT * FROM main.plus10_trades")
    db.conn.execute("UPDATE a.strat_trades SET exit_reason = 'copie manuelle'")    # marque : jamais réécrite
    db.conn.execute("DETACH DATABASE a")
    db.replace_strat("replay", "stock", DAYS[0], [_strat("AAA", r=0.3)])          # réécrit avant le déploiement
    _old(db, "stock", DAYS[1], ["CCC"], r=-0.5)                                    # absente de la copie
    out = AUD.step(db)
    assert out[f"strats:stock:d{ST.DECLARATION}"] == {"strat_trades": {"choisies": 3, "ajoutees": 1, "deja_la": 2}}
    assert out[f"strats:crypto:d{ST.CRYPTO_DECLARATION}"]["strat_trades"]["ajoutees"] == 1
    assert out[f"kill_complet:{KC.cost_fingerprint()}"] == {"kill_complet": {"choisies": 1, "ajoutees": 0,
                                                                              "deja_la": 1}}
    assert out[f"plus10:m{P10.MESURE}"]["plus10_trades"] == {"choisies": 1, "ajoutees": 0, "deja_la": 1}
    rows = _read(path, "SELECT market, day, symbol, exit_reason FROM strat_trades ORDER BY market, day, symbol")
    same = ST.CRYPTO_DECLARATION == ST.DECLARATION  # n° 13 : la crypto (n° 11) a son propre fichier
    assert rows == [*([("crypto", DAYS[0], "BTC-USDT", None)] if same else []),
                    ("stock", DAYS[0], "AAA", "copie manuelle"), ("stock", DAYS[0], "BBB", "copie manuelle"),
                    ("stock", DAYS[1], "AAA", "copie manuelle"), ("stock", DAYS[1], "BBB", "copie manuelle"),
                    ("stock", DAYS[1], "CCC", None)]      # le 10/03 réécrit en base reste dans l'archive
    assert "cout_rev" not in {r[1] for r in _read(path, "PRAGMA table_info(strat_trades)")}  # schéma gardé
    assert _read(path, "SELECT COUNT(*) FROM kill_complet") == [(2,)]                   # copie complète gardée
    assert len(_read(path, "SELECT * FROM archive_info")) == 3 + same


def test_seances_n9_sans_ligne_archivees(tmp_path):
    """Une séance de la n° 9 sans ligne garde ses mesures (listes, amplitudes) : elles sont réécrites aussi."""
    db = LabDB(str(tmp_path / "lab.db"))
    db.set(P10.JOB, DAYS[0], {"mesure": 9, "listes": {"s10": [], "rebond": []}})
    db.set(P10.JOB, DAYS[1], {"absente": True})
    out = AUD.step(db)
    assert out[f"plus10:m{P10.MESURE}"] == {"plus10_trades": {"choisies": 0, "ajoutees": 0, "deja_la": 0},
                                            "plus10_seances": {"ajoutees": 1}}
    assert [d for d, _ in _read(_archive(db), "SELECT day, value FROM plus10_seances")] == [DAYS[0]]


def test_sans_ancienne_ligne_pas_de_fichier(tmp_path):
    db = LabDB(str(tmp_path / "lab.db"))
    db.replace_strat("replay", "stock", DAYS[0], [_strat("AAA")])
    assert AUD.step(db) == {} and not (tmp_path / "archives").exists()
    assert set(db.keys(AUD.JOB)) == {f"strats:stock:d{ST.DECLARATION}", f"strats:crypto:d{ST.CRYPTO_DECLARATION}",
                                     f"kill_complet:{KC.cost_fingerprint()}", f"plus10:m{P10.MESURE}"}
    assert AUD.step(LabDB(":memory:")) == {} and AUD.report(LabDB(":memory:").conn) is None


def test_archive_jamais_lue_pour_decider(tmp_path):
    """Balayage du code : le fichier et ses tables ne sont nommés que dans audit.py ; seuls le worker, le rattrapage, la
    route et `!strategies` importent le module ; le tableau est le même avec ou sans archive."""
    root = Path(__file__).resolve().parents[1] / "app"
    named, importers = set(), set()
    for f in root.rglob("*.py"):
        src = f.read_text(encoding="utf-8")
        rel = f.relative_to(root.parent).as_posix()
        if any(x in src for x in (AUD.PREFIX, "archive_info", "plus10_seances", AUD.CACHE_KEY)):
            named.add(rel)
        if re.search(r"from app\.lab import audit\b|app\.lab\.audit|import audit\b", src):
            importers.add(rel)
    assert named == {"app/lab/audit.py"}
    assert importers == {"app/lab/worker.py", "app/lab/rattrapage.py", "app/main.py", "app/notify/bot.py"}
    db = _base(tmp_path)
    db.conn.execute("DELETE FROM progress WHERE job = ? AND key = ?", (P10.JOB, DAYS[1]))  # séance n° 9 sans listes
    db.conn.execute("DELETE FROM plus10_trades WHERE day = ?", (DAYS[1],))
    before = SS.report(db.conn)
    AUD.step(db)
    assert _archive(db).exists() and SS.report(db.conn) == before


# --- C2 : audit apparié ancien → nouveau -------------------------------------------------------------------------

def _audit_base(tmp_path):
    """Trois jours, deux actions par jour : ancien pullback à −0,4 R (témoin +0,1), refait à +0,1 R (témoin −0,1) ;
    une ligne refaite non déclenchée ; une ancienne ligne sans pendant."""
    db = LabDB(str(tmp_path / "lab.db"))
    days = ("2025-03-10", "2025-03-11", "2025-03-12")
    for i, day in enumerate(days):
        _old(db, "stock", day, ["AAA", "BBB"], r=-0.4 - 0.1 * i, entry=10.0, stop=9.0, temoin_r=0.1,
             temoin_triggered=1)
    _old(db, "stock", days[0], ["ZZZ"], r=1.0)
    assert AUD.step(db)
    for i, day in enumerate(days):
        db.replace_strat("replay", "stock", day, [
            _strat("AAA", r=0.1, entry=10.0, stop=9.5, temoin_r=-0.1, temoin_triggered=1),
            _strat("BBB", r=0.1, entry=10.0, stop=9.5, temoin_r=-0.1, temoin_triggered=1,
                   triggered=0 if i == 2 else 1)])
    return db, days


def test_audit_apparie_nouveau_moins_ancien(tmp_path):
    db, days = _audit_base(tmp_path)
    rep = AUD.report(db.conn)
    assert rep["declaration"] == ST.DECLARATION  # la déclaration qui a réécrit les lignes (la n° 12 n'en réécrit aucune)
    assert list(rep["archives"]) == [f"labo2-avant-n{ST.DECLARATION}.db"]
    mk = rep["marches"]["stock"]
    assert mk["archivees"] == 7 and mk["appariees"] == 6                  # ZZZ sans pendant
    s = mk["strategies"]["pullback"]
    assert (s["paires"], s["declenchees_avant"], s["declenchees_apres"], s["declenchees_les_deux"]) == (6, 6, 5, 5)
    assert s["sortie"] == ST.ref_exit("pullback") == "r_net"
    # Commission IBKR : 0,01 R avant (risque 1 $), 0,02 R après (risque 0,5 $) ; nouveau − ancien sur les 5 paires.
    olds = [-0.4, -0.4, -0.5, -0.5, -0.6]
    expected = sum((0.1 - 0.02) - (r - 0.01) for r in olds) / 5
    assert abs(s["esperance"]["esperance_r"] - expected) < 1e-4 and s["esperance"]["n"] == 5
    assert abs(s["r_net"]["esperance_r"] - sum(0.1 - r for r in olds) / 5) < 1e-4
    assert abs(s["temoin"]["esperance_r"] + 0.2) < 1e-4
    assert abs(s["ecart"]["esperance_r"] - sum((0.1 + 0.1) - (r - 0.1) for r in olds) / 5) < 1e-4
    assert s["ecart"]["grappes"] == 3 and s["ecart"]["ic95"] is not None   # grappes par jour
    assert mk["ensemble"]["ecart"]["n"] == 5
    info = rep["archives"][f"labo2-avant-n{ST.DECLARATION}.db"]["archivages"]
    assert {i["cle"] for i in info} == {f"strats:stock:d{ST.DECLARATION}"}
    # Rien n'est écrit dans la base par l'audit : l'archive est détachée.
    assert [r[1] for r in db.conn.execute("PRAGMA database_list")] == ["main"]


def test_audit_archive_plus_ancienne_appariee_a_part(tmp_path):
    """Fusion avec la n° 11 : une copie manuelle d'une déclaration plus ancienne (labo2-avant-n10.db, faite avant le
    déploiement) reste lue, appariée à part aux lignes en vigueur ; l'archive automatique garde sa place."""
    db, days = _audit_base(tmp_path)
    older = AUD.archive_path(db.path, ST.DECLARATION - 1)
    conn = sqlite3.connect(older)
    conn.execute("ATTACH DATABASE ? AS cur", (str(_archive(db)),))
    conn.execute("CREATE TABLE strat_trades AS SELECT * FROM cur.strat_trades WHERE symbol = 'AAA'")
    conn.commit()
    conn.close()
    assert AUD.on_disk(db.path) == [ST.DECLARATION - 1, ST.DECLARATION]
    rep = AUD.report(db.conn)
    assert list(rep["archives"]) == [older.name, f"labo2-avant-n{ST.DECLARATION}.db"]
    assert rep["marches"]["stock"]["appariees"] == 6                          # inchangé
    old = rep["archives"][older.name]
    assert old["avant"] == ST.DECLARATION - 1 and old["marches"]["stock"]["archivees"] == 3
    assert old["marches"]["stock"]["appariees"] == 3 and "marches" not in rep["archives"][_archive(db).name]
    text = AUD.text(rep)
    assert f"Mesures d'avant la n° {ST.DECLARATION - 1} ({older.name}), actions : 3 lignes appariées sur 3" in text
    assert AUD.refresh(db, datetime(2026, 10, 20, 3, tzinfo=timezone.utc), 3600) == 9   # 6 + 3 paires
    assert [r[1] for r in db.conn.execute("PRAGMA database_list")] == ["main"]
    older.write_bytes(b"")                                                     # fichier vide : pas une archive
    assert list(AUD.report(db.conn)["archives"]) == [f"labo2-avant-n{ST.DECLARATION}.db"]


def test_audit_en_cache_et_texte(tmp_path):
    db, days = _audit_base(tmp_path)
    now = datetime(2026, 10, 11, 3, tzinfo=timezone.utc)
    assert AUD.refresh(db, now, 3600) == 6
    rep = AUD.cached(db.conn)
    assert rep["marches"]["stock"]["appariees"] == 6
    assert AUD.refresh(db, now, 3600) is None                            # même signature
    db.replace_strat("replay", "stock", days[0], [_strat("AAA", r=0.2)])
    assert AUD.refresh(db, now + timedelta(seconds=60), 3600) is None    # lignes changées, cache trop jeune
    assert AUD.refresh(db, now + timedelta(seconds=3601), 3600) == 5
    text = AUD.text(rep)
    assert text.startswith(f"🔎 **Audit de la n° {ST.DECLARATION} (ancien → nouveau, mêmes trades)** : pour "
                           "audit, jamais pour décider")
    assert "Actions : 6 lignes appariées sur 7 archivées" in text and "espérance +0,57 R [" in text
    assert "(5 trades)" in text and "témoin -0,20 R" in text
    assert AUD.text(None) is None


def test_audit_route_et_strategies(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import main
    from app.notify import bot

    path = tmp_path / "lab.db"
    monkeypatch.setenv("JARVIS_LAB_DB", str(path))
    monkeypatch.setenv("JARVIS_LAB_API_KEY", "cle")
    db = LabDB(str(path))
    client = TestClient(main.app)
    assert client.get("/api/lab/strategies/audit", headers={"X-Lab-Key": "cle"}).status_code == 404
    assert "Audit de la n°" not in bot.strategies_text()
    db.close()
    db, _ = _audit_base(tmp_path)
    r = client.get("/api/lab/strategies/audit", headers={"X-Lab-Key": "cle"})   # calcul direct, en lecture seule
    assert r.status_code == 200 and r.json()["marches"]["stock"]["appariees"] == 6
    assert "Audit de la n°" not in bot.strategies_text()                       # pas de cache : rien dans Discord
    AUD.refresh(db, datetime(2026, 10, 11, 3, tzinfo=timezone.utc), 3600)
    assert f"Audit de la n° {ST.DECLARATION} (ancien → nouveau, mêmes trades)" in bot.strategies_text()


# --- C5 : Killmode, comparaison journal / labo ---------------------------------------------------------------------

def test_solde_rejoue_le_cote_labo_en_revision_du_labo(monkeypatch):
    """Le côté labo (r_ibkr) en révision du simulateur des lignes (n° 10) ; les rejeux à la minute du solde de Simão
    (résolution « prudente », `kw` partagé) restent en révision 1 ; méthode 5 : tout le journal est re-soldé."""
    from tests.test_killmode import OPEN, RANGE, RUN, _call, spread

    seen = []
    real = K.SIM.simulate

    def spy(*a, **k):
        seen.append((k.get("resolution", "v1"), k.get("sim_rev", 1)))
        return real(*a, **k)

    monkeypatch.setattr(K.SIM, "simulate", spy)
    call = _call(OPEN + 30 * 60)
    bars = spread(RANGE + RUN + [(114.7, 115.0, 114.5, 114.8)] * 60)
    res = K.settle(call, bars)
    assert K.SETTLE_METHOD == 5 and res["methode"] == 5
    assert [x for x in seen if x[0] == "v1"] == [("v1", ST.SIM_REV)]
    assert {rev for resolution, rev in seen if resolution == "prudente"} == {1}
    five = [b for b in K.CO.aggregate(bars, K.CO.BASE_TF, "stock") if b["t"] >= OPEN + 30 * 60]
    plan = {"side": "long", "entry": call["entry"], "stop": call["stop"],
            "targets": [{"price": p} for p in call["targets"]]}
    lab = real(plan, five, market="stock", decided_at=OPEN + 30 * 60, entry_until=call["until"],
               exit_at=call["close_ts"], last_price=call["price"], cost=K.SIM.costs("stock", call["entry"]),
               entry_order="stop", sim_rev=ST.SIM_REV)
    assert res["r_ibkr"] == round(lab["r_net"] - SS.ibkr_commission_r(call["entry"], call["stop"]), 4)


def test_ligne_du_labo_dit_le_tableau_partiel(monkeypatch):
    from app.lab import db as lab_db

    class Conn:
        def close(self):
            pass

    it = {"declenches": 40, "jours": 30, "esperance_ibkr_r": 0.2, "ic95_ibkr": [0.05, 0.35], "temoin": {"ecart_r": 0.3}}
    rep = {"strategies": {"stock": {"killmode": it}},
           "partiel": {"stock": {"declaration": 10, "jours_rattrapage": 300, "lignes_exclues": 9000}}}
    monkeypatch.setattr(lab_db, "open_readonly", lambda: Conn())
    monkeypatch.setattr(K.SS, "cached", lambda conn: rep)
    line = K.lab_line(K.lab_item())
    assert line.startswith(f"🧪 Labo, {K.LAB_SCOPE} (tableau partiel, rattrapage en cours) : +0,20 R par trade")
    rep["strategies"]["stock"]["killmode"] = {}
    assert "(tableau partiel, rattrapage en cours) : pas encore de trade rejoué." in K.lab_line(K.lab_item())
    rep["strategies"]["stock"]["killmode"] = it | {"jours_suivants": it | {"declenches": 12}}
    assert "(tableau partiel, rattrapage en cours)" in K.lab_line(K.lab_item())
    for partial in (None, {"crypto": {"declaration": 10, "jours_rattrapage": 3, "lignes_exclues": 0}}):
        rep["partiel"] = partial                                         # rien en actions : la ligne d'avant
        assert "partiel" not in K.lab_line(K.lab_item())


# --- C7 : textes du tableau ----------------------------------------------------------------------------------------

def _helps(m, aide=True, n=60):
    return {"esperance_r": m, "z": 4.0, "seuil_t": 3.5, "n": n, "jours": 40, "aide": aide}


def test_jugements_relatifs_sans_coche_et_ce_que_perd_le_bras_a_20e():
    """✅ réservé à candidate_compte : « la confirmation aide », « le filtre aide » disent « ↗ aide (relatif) », et ce
    que le bras amélioré perd encore à 20 € quand il ne gagne pas."""
    res = {"go": 80, "ecart_par_go": _helps(0.15), "par_statut": {},
           "filtres": {"v2": {"ecart_au_confirme": _helps(0.08), "statuts": {}},
                       "v3": {"ecart_au_confirme": _helps(0.3, aide=False), "statuts": {}}}}
    items = {"go_confirme": {"mise_20e": {"esperance_r": -0.123, "n": 40}},
             "go_confirme_v2": {"mise_20e": {"esperance_r": 0.05, "n": 30}}}
    text = SS._go_text("origine", res, items)
    assert "✅" not in text
    assert "par GO (t 4, seuil 3,5, ↗ aide (relatif), perd encore 0,12 R à 20 €)" in text
    assert "filtré − confirmé +0,08 R par GO (t 4, seuil 3,5, ↗ aide (relatif))" in text     # bras gagnant à 20 €
    assert "filtré − confirmé +0,30 R par GO (t 4, seuil 3,5)" in text                        # n'aide pas
    assert SS._go_text("origine", res).count("↗ aide (relatif)") == 2                       # sans le tableau
    kill = {"figee": True, "seances": 40, "du": "2026-01-05", "au": "2026-03-02", "a_recalculer": 0,
            "groupes": {g: {"signaux": 10, "declenches": 8, "r_20e": {"esperance_r": -0.2}}
                        for g in ("kill", "temoin_propre", "temoin_autre", "donnees", "fenetre_coupee")},
            "principal": {"esperance_r": -0.05, "ic95": [-0.3, 0.2], "n": 50, "jours": 40, "z": 0.5, "seuil_t": 3.5,
                          "r_3c": -0.1, "candidate": False},
            "ecart_temoin_propre": _helps(0.25)}
    line = KC.text(kill)
    assert "✅" not in line and "(t 4, seuil 3,5, ↗ aide (relatif), perd encore 0,05 R à 20 €)" in line


def _item(**kw):
    return {"nom": "Pullback", "declenches": 60, "jours": 40, "reussite": 0.5, "frais_r": 0.05,
            "esperance_ibkr_r": -0.1, "ic95_ibkr": [-0.3, 0.1], "commission_ibkr_r": 0.01, "seuil_t": 3.5,
            "mise_20e": {"esperance_r": -0.2, "n": 60}, "temoin": {"ecart_r": 0.0, "z": 0.1},
            "ecarts_sorties": {"r_vwap": _helps(0.05)}, "candidate": False, "candidate_compte": False} | kw


def test_sorties_de_gemini_legende_et_note_de_rebond_confirme():
    rep = {"declaration": {"essais": 152, "numero": 10},
           "strategies": {"stock": {"pullback": _item(), "orb15": _item(nom="ORB 15",
                                                                           mise_20e={"esperance_r": 0.3, "n": 60}),
                                    "rebond_confirme": _item(nom="Rebond", ecarts_sorties={})}},
           "sorties_gemini": {"ecarts": {"r_vwap": {"esperance_r": 0.05, "z": 2.5, "n": 120}}, "par_vwap": {}},
           "scelle": {"texte": "bloc scellé"}}
    text = SS.text(rep)
    assert ("VWAP après 10 h 30 +0,05 R (z 2,5, ↗ aide (relatif) sur 2 stratégies, dont pullback perd encore "
            "≈ 0,15 R à 20 €)") in text
    assert ("témoin non battu = pas d'avantage d'horaire après le signal ; ne dit rien du choix de l'action ou du "
            "jour") in text.splitlines()[0]
    lines = text.splitlines()
    i = next(k for k, x in enumerate(lines) if x.startswith("Rebond :"))
    assert lines[i + 1] == "   ↳ anticipation connue ≈ +0,01 R sous H0 (bougie ambiguë), correction à déclarer"
    assert sum("anticipation connue" in x for x in lines) == 1
    assert not any(x.startswith("✅") for x in lines)


def test_go_brut_lignes_sans_temoin():
    """go_brut ouvert au-dessus du TP1 : pas de témoin (NO_WITNESS) ; ces lignes sortent de l'écart, pas de
    l'espérance : le tableau les compte."""
    db = LabDB(":memory:")
    for i, day in enumerate(("2025-03-10", "2025-03-11", "2025-03-12")):
        db.replace_strat("replay", "stock", day, [
            _strat("AAA", strategy="go_brut", r_sans_be=0.4, temoin_r=0.1, temoin_triggered=1),
            _strat("BBB", strategy="go_brut", r_sans_be=0.9, **ST.NO_WITNESS),
            _strat("CCC", strategy="pullback", temoin_r=0.1, temoin_triggered=1)])
    rep = SS.report(db.conn)
    item = rep["strategies"]["stock"]["go_brut"]
    assert item["temoin"]["sans_temoin"] == 3 and item["temoin"]["n"] == 3 and item["declenches"] == 6
    assert rep["strategies"]["stock"]["pullback"]["temoin"]["sans_temoin"] == 0
    line = next(x for x in SS.text(rep).splitlines() if x.startswith(item["nom"]))
    assert "· 3 lignes sans témoin (hors de l'écart)" in line
    assert "sans témoin" not in next(x for x in SS.text(rep).splitlines()
                                     if x.startswith(rep["strategies"]["stock"]["pullback"]["nom"]))


# --- C8 : docs ---------------------------------------------------------------------------------------------------

def test_commande_manuelle_de_deploy_puis_archive_automatique(tmp_path):
    """La commande de secours de DEPLOY.md, lancée telle quelle, puis le worker : rien de recréé, rien en double."""
    import os
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    deploy = (root / "DEPLOY.md").read_text(encoding="utf-8")
    script = deploy.split("docker compose run --rm --no-deps -T jarvis-lab python - <<'EOF'\n")[1].split("\nEOF\n")[0]
    db = _base(tmp_path)
    run = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, capture_output=True, text=True, timeout=60,
                         env=os.environ | {"JARVIS_LAB_DB": str(tmp_path / "lab.db"), "PYTHONPATH": str(root)})
    # 4 actions d'avant, plus la crypto n° 9 quand les deux marchés ont la même déclaration (n° 13 : crypto en n° 11)
    same = ST.CRYPTO_DECLARATION == ST.DECLARATION
    n = 4 + same
    assert run.returncode == 0 and f"strat_trades {n} lignes archivées" in run.stdout, run.stderr
    path = _archive(db)                                                        # le fichier de la déclaration en vigueur
    assert _read(path, "SELECT COUNT(*) FROM strat_trades") == [(n,)]
    out = AUD.step(db)
    assert out[f"strats:stock:d{ST.DECLARATION}"]["strat_trades"] == {"choisies": 4, "ajoutees": 0, "deja_la": 4}
    assert out[f"strats:crypto:d{ST.CRYPTO_DECLARATION}"]["strat_trades"]["ajoutees"] == int(not same)
    assert _read(path, "SELECT COUNT(*) FROM strat_trades") == [(n,)]


def test_docs_de_la_n10_limites_et_avant_la_n11():
    root = Path(__file__).resolve().parents[1]
    doc = (root / "docs" / "labo-strategies.md").read_text(encoding="utf-8")
    section = doc.split("## Déclaration n° 10 (10 octobre 2026)")[1].split("## Bloc scellé")[0]
    for text in ("moyenne des 6 bougies qui suivent la bougie d'exécution", "se compte en bougies, pas en minutes",
                 "+0,007 R", "−0,06 R à 1 σ, −0,10 R à 0,75 σ, −0,28 R à 0,5 σ", "+0,013 R [+0,004 ; +0,023]",
                 "`go_brut` n'a pas de témoin", "Limite de régime", "30 pas par bougie", "### Avant la déclaration suivante",
                 "`rattrapage.v1_trades` ne lit pas", "témoin transversal", "labo2-avant-n10.db",
                 "Pour audit, jamais pour décider", "(méthode du solde n° 5)"):
        assert text in section, text
    assert "rejoue encore en révision 1 : à aligner" not in section
    assert "Méthode du solde n° 5" in doc and "### Archive d'audit des mesures remplacées" in (
        root / "DEPLOY.md").read_text(encoding="utf-8")
