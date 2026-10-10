"""Lot 2, chantier C (deuxième tour d'avis de GPT et Gemini, 10 octobre 2026) : la n° 10 traçable et honnête.
Tableau partiel structurel (C3), indicateur « n° 10 complète » (C4)."""
import json
from datetime import date

from app.lab import brief as BRIEF
from app.lab import collector as CO
from app.lab import kill_complet as KC
from app.lab import plus10 as P10
from app.lab import rattrapage as RAT
from app.lab import status as STATUS
from app.lab import strat_stats as SS
from app.lab import strategies as ST
from app.lab.db import LabDB


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
    assert n == {"numero": ST.LATEST_DECLARATION, "jours": {"stock": 0, "crypto": 0},
                 "lignes_anciennes": {"stock": 0, "crypto": 0}, "seances_n9": 0, "seances_kill_complet": 0,
                 "complete": True}
    assert STATUS.declaration_line(n) == "n° 10 : complète (stratégies, n° 9 et Killmode complet re-mesurés)"
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
    assert STATUS.declaration_line(n) == ("⏳ n° 10 : 2 j actions, 0 j crypto à rattraper, 1 séance n° 9 à recalculer, "
                                          "1 séance Killmode complet à recalculer")
    st = STATUS.status(path)
    assert st["n10"] == n and STATUS.declaration_line(n) in STATUS.text(st)
    data = BRIEF.build(db.conn, BRIEF.datetime(2026, 10, 10, 20, tzinfo=BRIEF.timezone.utc), path)
    assert data["declaration"]["complete"] is False and data["declaration"]["texte"] == STATUS.declaration_line(n)


def test_indicateur_lignes_d_avant_sans_jour_a_refaire(tmp_path):
    db = LabDB(str(tmp_path / "lab.db"))
    db.set(ST.progress_job("crypto"), "rattrapage:replay", "2025-03-11")
    _old(db, "crypto", "2025-03-12", ["AAA-USDT", "BBB-USDT"])
    n = STATUS.declaration_state(db.conn)
    assert n["lignes_anciennes"]["crypto"] == 2 and not n["complete"]
    assert STATUS.declaration_line(n) == "⏳ n° 10 : 2 lignes d'avant sans jour à refaire (exclues)"


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
    assert "ℹ️ Lignes actions de 2 révisions du code" in text and "/ 2 du coût" in text and "inconnue 1" in text


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
