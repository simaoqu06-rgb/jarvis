"""Relecture de la fusion du deuxième tour d'avis (groupe W) : archive d'audit en échec visible (W1, W3), audit apparié
sans la séance du direct recalculée (W2), révisions du coût sans fausse alerte (W4, W7), rattrapage du rejeu et du
direct séparés dans !labo (W6), commande de secours de DEPLOY.md (W5)."""
import sqlite3
from datetime import datetime, timezone

import pytest

from app.lab import audit as AUD
from app.lab.db import LabDB


def _row(symbol, r=0.5, strategy="v1", **kw):
    return {"symbol": symbol, "strategy": strategy, "slot": 0, "triggered": 1, "r_net": r, "entry": 10.0,
            "stop": 9.0} | kw


def _disque_plein(monkeypatch):
    """Disque plein réaliste (review/skep-deploiement/full_real.py) : l'archive ne peut plus grandir, SQLite renvoie
    SQLITE_FULL et annule lui-même la transaction."""
    copy = AUD._copy

    def full(conn, *a, **k):
        conn.execute(f"PRAGMA {AUD.ALIAS}.max_page_count = 4")
        return copy(conn, *a, **k)

    monkeypatch.setattr(AUD, "_copy", full)


def _anciennes(db, n=2000):
    """Lignes d'avant la n° 10 (declaration NULL) assez nombreuses pour dépasser 4 pages."""
    db.replace_strat("replay", "stock", "2025-03-10", [_row(f"S{i}") for i in range(n)])
    db.conn.execute("UPDATE strat_trades SET declaration = NULL")


# --- W3 : la vraie cause d'un échec arrive au journal ---------------------------------------------------------------

def test_transaction_annulee_par_sqlite_garde_la_vraie_cause():
    """SQLite annule seul la transaction sur certaines erreurs (disque plein) : le ROLLBACK de db.transaction levait
    « cannot rollback - no transaction is active » à la place de l'erreur d'origine."""
    db = LabDB(":memory:")
    with pytest.raises(ValueError, match="vraie cause"):
        with db.transaction():
            db.set("x", "a", 1)
            db.conn.execute("ROLLBACK")                       # ce que fait SQLite sur SQLITE_FULL
            raise ValueError("vraie cause")
    assert not db.conn.in_transaction and db.get("x", "a") is None
    with db.transaction():                                    # la connexion reste utilisable
        db.set("x", "b", 2)
    assert db.get("x", "b") == 2
    with pytest.raises(KeyError):                             # cas ordinaire : annulée par db.transaction
        with db.transaction():
            db.set("x", "c", 3)
            raise KeyError("c")
    assert db.get("x", "c") is None and not db.conn.in_transaction


def test_archive_sur_disque_plein_dit_disque_plein(tmp_path, monkeypatch):
    db = LabDB(str(tmp_path / "lab.db"))
    _anciennes(db)
    _disque_plein(monkeypatch)
    with pytest.raises(sqlite3.OperationalError, match="full"):
        AUD.step(db)
    assert [r[1] for r in db.conn.execute("PRAGMA database_list")] == ["main"]   # archive détachée
    assert not db.conn.in_transaction and not db.keys(AUD.JOB).get(f"strats:stock:d{AUD.declaration('stock')}")


def test_fichier_d_archive_vide_n_est_pas_un_audit(tmp_path, monkeypatch):
    """Archivage en échec : le fichier reste à 0 octet ; il était publié comme « Audit » (section de !strategies, route
    en 200) alors qu'il n'archive rien."""
    from fastapi.testclient import TestClient

    from app import main

    path = tmp_path / "lab.db"
    monkeypatch.setenv("JARVIS_LAB_DB", str(path))
    monkeypatch.setenv("JARVIS_LAB_API_KEY", "cle")
    db = LabDB(str(path))
    _anciennes(db)
    with monkeypatch.context() as m:
        _disque_plein(m)
        with pytest.raises(sqlite3.OperationalError):
            AUD.step(db)
    archive = AUD.archive_path(db.path, AUD.declaration("stock"))
    assert archive.exists() and archive.stat().st_size == 0
    assert AUD.report(db.conn) is None
    assert AUD.refresh(db, datetime(2026, 10, 11, 3, tzinfo=timezone.utc), 3600) == 0  # pas de cache
    assert AUD.cached(db.conn) is None and AUD.text(AUD.cached(db.conn)) is None
    client = TestClient(main.app)
    assert client.get("/api/lab/strategies/audit", headers={"X-Lab-Key": "cle"}).status_code == 404
    AUD.step(db)                                                    # place libérée : l'archivage passe
    assert AUD.report(db.conn)["archives"][archive.name]["tables"]


# --- W1 : archive d'audit en échec, dite dans !labo -------------------------------------------------------------------

def _passage_calme(monkeypatch):
    """Passage du worker sans réseau : grade, direct, Labo 2, rejeu et rattrapage neutralisés."""
    from app.lab import worker as W

    async def nothing(*a, **k):
        return {}

    monkeypatch.setattr(W.GRADE, "step", lambda db_, now: {})
    monkeypatch.setattr(W, "lab2", nothing)
    monkeypatch.setattr(W.R, "replay", nothing)
    monkeypatch.setattr(W.R, "run_live_day", nothing)
    monkeypatch.setattr(W.RAT, "step", nothing)
    monkeypatch.setattr(W.alpaca, "configured", lambda: True)
    return W


async def test_archive_en_echec_dite_dans_labo_jusqu_au_passage_qui_reussit(tmp_path, monkeypatch):
    """review/skep-cxd (S2) : archive en échec à chaque passage, le rattrapage n° 10, Killmode complet, la n° 9 et le
    recalcul du direct s'arrêtaient sans rien dans !labo ni /api/lab/status (le journal seul le disait)."""
    from datetime import timedelta

    from app.lab import status as STATUS
    from app.lab import strategies as ST

    W = _passage_calme(monkeypatch)
    path = str(tmp_path / "lab.db")
    db = LabDB(path)
    _anciennes(db)
    night = datetime(2026, 10, 11, 3, tzinfo=timezone.utc)            # dimanche : « nuit », séance US fermée
    with monkeypatch.context() as m:
        _disque_plein(m)
        for i in range(2):                                            # deux passages en échec : « depuis » le premier
            report = await W.tick(db, night + timedelta(minutes=10 * i), collect=True)
    assert report["archive_audit"] == "échec : OperationalError" and "rattrapage" not in report
    st = STATUS.status(path)
    assert st["archive_audit"] == {"etat": "echec", "raison": "OperationalError: database or disk is full",
                                   "depuis": "2026-10-11T03:00:00+00:00"}
    alert = (f"⚠️ Archive d'audit en échec depuis le 11/10/2026 à 03:00 UTC (OperationalError: database or disk is "
             f"full) : rattrapage de la n° {ST.LATEST_DECLARATION}, Killmode complet, n° 9 et recalcul du direct à "
             "l'arrêt, rien n'est réécrit (docker compose logs jarvis-lab)")
    assert alert in STATUS.text(st).splitlines()
    report = await W.tick(db, night + timedelta(minutes=30), collect=True)   # place libérée : l'archivage passe
    assert report["archive_audit"] and "rattrapage" in report
    st = STATUS.status(path)
    assert st["archive_audit"] is None and "Archive d'audit" not in STATUS.text(st)


def test_etat_d_archive_illisible_ou_sans_echec():
    from app.lab import status as STATUS

    assert STATUS.archive_line(None) is None and STATUS.archive_line({"etat": "faite"}) is None
    assert STATUS.archive_line({"etat": "echec", "raison": "OSError: x", "depuis": "pas une date"}) == (
        "⚠️ Archive d'audit en échec (OSError: x) : rattrapage, Killmode complet, n° 9 et recalcul du direct à l'arrêt, "
        "rien n'est réécrit (docker compose logs jarvis-lab)")


# --- W2 : audit apparié sans la séance du direct recalculée -----------------------------------------------------------

def _ancienne(db, origin, day, symbol, r):
    db.conn.execute("INSERT INTO strat_trades (origin, market, symbol, day, strategy, slot, triggered, r_net, entry, "
                    "stop) VALUES (?, 'stock', ?, ?, 'v1', 0, 1, ?, 10.0, 9.0)", (origin, symbol, day, r))


def test_audit_sans_la_seance_du_direct_aux_plans_recalcules(tmp_path, monkeypatch):
    """review/skep-cxd (S1) : le direct d'actions du 06/10/2026 est archivé sur les plans d'avant b595c12, puis
    recalcul_v1 réécrit ses plans et le rattrapage refait ses lignes n° 10 sur les nouveaux : ses paires ne sont pas
    « les mêmes trades » (stops 41,753 → 41,755…). Elles sont exclues, et le texte le dit."""
    from app.lab import empreinte as E

    db = LabDB(str(tmp_path / "lab.db"))
    _ancienne(db, "replay", "2025-03-10", "AAA", -0.4)
    _ancienne(db, "live", "2026-10-06", "BBB", -0.4)
    _ancienne(db, "live", "2026-10-07", "CCC", -0.4)               # autre séance du direct : gardée
    assert AUD.step(db)
    db.replace_strat("replay", "stock", "2025-03-10", [_row("AAA", r=0.1)])
    db.replace_strat("live", "stock", "2026-10-06", [_row("BBB", r=0.1, stop=9.01)])
    db.replace_strat("live", "stock", "2026-10-07", [_row("CCC", r=0.1)])
    assert AUD.report(db.conn)["marches"]["stock"]["appariees"] == 3   # conftest : V1_RECALCULS vide, rien d'exclu
    monkeypatch.setattr(E, "V1_RECALCULS", (("stock", "b595c12", "2026-10-06"),))
    rep = AUD.report(db.conn)
    mk = rep["marches"]["stock"]
    assert mk["archivees"] == 3 and mk["appariees"] == 2 and mk["strategies"]["v1"]["paires"] == 2
    assert rep["seances_exclues"] == [{"marche": "stock", "jour": "2026-10-06", "raison": "plans v1 recalculés"}]
    text = AUD.text(rep)
    assert "séance du direct actions du 06/10/2026 exclue (plans v1 recalculés)" in text
    assert "Actions : 2 lignes appariées sur 3 archivées" in text


# --- W4, W7 : lignes n° 10 écrites avant le suivi du coût, sans fausse alerte -------------------------------------------

def test_lignes_sans_revision_du_cout_ne_font_pas_d_alerte():
    """review/skep-deploiement/coutrev2.py et skeptic-invariants-textes/revs.py : les lignes n° 10 écrites par la PR #53
    n'ont pas de cout_rev (colonne ajoutée après), même modèle de coût vérifié ; elles comptaient pour une révision
    « inconnue » et !strategies affichait en permanence « 2 du coût (…, inconnue …) : à vérifier si les chiffres
    bougent »."""
    from app.lab import empreinte as E
    from app.lab import strat_stats as SS

    db = LabDB(":memory:")
    for m in ("stock", "crypto"):
        db.replace_strat("replay", m, "2025-03-10", [_row("AAA"), _row("BBB"), _row("CCC")])
    db.conn.execute("UPDATE strat_trades SET cout_rev = NULL")             # écrites avant la colonne
    for m in ("stock", "crypto"):
        db.replace_strat("replay", m, "2025-03-11", [_row("DDD")])         # écrites après le déploiement
    rep = SS.report(db.conn)
    assert rep["revisions"]["stock"] == {"code": {E.code_rev("strat"): 4},
                                         "cout": {E.strat_cost_rev("stock"): 1, "inconnue": 3}}   # données inchangées
    lines = SS.text(rep).splitlines()
    assert "ℹ️ 3 lignes actions sans révision du coût (écrites avant son suivi)" in lines
    assert "ℹ️ 3 lignes crypto sans révision du coût (écrites avant son suivi)" in lines
    assert not any("à vérifier si les chiffres bougent" in line for line in lines)
    # Une vraie seconde révision du coût reste dite, les lignes sans révision à part.
    rep["revisions"]["stock"]["cout"]["autrecout"] = 2
    assert SS._revisions_text("stock", rep["revisions"]["stock"]) == (
        f"ℹ️ Lignes actions de 1 révision du code ({E.code_rev('strat')} 4) / 2 du coût (autrecout 2, "
        f"{E.strat_cost_rev('stock')} 1) : descriptif, à vérifier si les chiffres bougent · 3 lignes sans révision du "
        "coût (écrites avant son suivi)")
    assert SS._revisions_text("stock", {"code": {"a": 1}, "cout": {"inconnue": 1}}) == (
        "ℹ️ 1 ligne actions sans révision du coût (écrites avant son suivi)")
    assert SS._revisions_text("stock", {"code": {"a": 1}, "cout": {"b": 1}}) is None


# --- W6 : !labo, rattrapage du rejeu et direct en attente séparés -------------------------------------------------------

def test_labo_separe_rattrapage_et_direct_en_attente(tmp_path):
    """skeptic-invariants-textes/status_n10.py : rejeu fini, un jour du direct d'actions attend son calcul (journalier
    du 09/10 en base, curseur au 08/10) ; !labo disait « rattrapage : 1 jours actions » à côté de « n° 10 : complète »."""
    from datetime import date

    from app.lab import collector as CO
    from app.lab import rattrapage as RAT
    from app.lab import status as STATUS
    from app.lab import strategies as ST

    path = str(tmp_path / "lab.db")
    db = LabDB(path)
    job = ST.progress_job("stock")
    db.set("lab", "live_start", "2026-10-05")
    db.set("live:stock", "last_day", "2026-10-09")
    db.set(job, "rattrapage:live", "2026-10-08")
    db.set("replay:stock", "start", "2026-01-05")
    db.set("replay:stock", "last_day", "2026-10-02")
    db.set(job, "rattrapage:replay", "2026-10-02")
    db.put_bars("stock", "SPY", 1440, [{"t": CO.ny_midnight(date(2026, 10, 9)), "o": 1, "h": 1, "l": 1, "c": 1}])
    for day in ("2026-10-07", "2026-10-08"):
        db.replace_strat("live", "stock", day, [_row("AAA")])
    assert RAT.remaining(db.conn, "stock") == {"replay": 0, "live": 1}
    st = STATUS.status(path)
    assert st["n10"]["complete"]
    lines = STATUS.text(st).splitlines()
    assert "Labo v2 : 2 lignes de stratégies (`!strategies`) · direct en attente de calcul : 1 jour actions" in lines
    assert not any("rattrapage :" in line for line in lines)
    assert f"n° {ST.LATEST_DECLARATION} : complète (stratégies, n° 9 et Killmode complet re-mesurés)" in lines
    db.set("replay:stock", "last_day", "2026-10-09")                 # cinq séances du rejeu à rattraper aussi
    lines = STATUS.text(STATUS.status(path)).splitlines()
    assert ("Labo v2 : 2 lignes de stratégies (`!strategies`) · rattrapage : 5 jours actions, 0 jour crypto · direct "
            "en attente de calcul : 1 jour actions") in lines


# --- W5 : commande de secours de DEPLOY.md ----------------------------------------------------------------------------

def test_copie_de_secours_marche_worker_arrete():
    """« En secours (worker arrêté…) » : `docker compose exec` exige un conteneur en marche (« service is not
    running ») ; `docker compose run --rm --no-deps` marche que le worker tourne ou non (même image, .env et data/)."""
    from pathlib import Path

    deploy = (Path(__file__).resolve().parents[1] / "DEPLOY.md").read_text(encoding="utf-8")
    section = deploy.split("### Archive d'audit des mesures remplacées")[1].split("\n### ")[0]
    assert "worker arrêté" in section
    assert "docker compose run --rm --no-deps -T jarvis-lab python - <<'EOF'\n" in section
    assert "docker compose exec" not in section
