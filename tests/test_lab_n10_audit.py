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
