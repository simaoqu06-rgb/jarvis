"""Labo 3 : filtres partagés labo / direct, décisions, challenger, bascule et retrait (hors ligne)."""
import json
import sqlite3
from datetime import date, datetime, timezone

import pytest

from app.lab import method as M
from app.lab import summary as SUM
from app.lab import validator as V
from app.lab.db import LabDB, generation_version, open_readonly
from tests.lab_fixtures import insert_validated, synthetic_trades, validated_metrics

NOW = datetime(2026, 10, 7, 6, 5, tzinfo=timezone.utc)
SIGNAL = {"field": "context.signal", "op": ">", "value": 0.8}


@pytest.fixture
def db(tmp_path):
    return LabDB(str(tmp_path / "lab.db"))


def kept_ids(db, rules) -> dict:
    expr, params = M.kept_sql(rules)
    return {i: bool(k) for i, k in db.conn.execute(
        f"SELECT t.id, {expr} FROM trades t LEFT JOIN regimes r ON r.market = t.market AND r.day = t.day ORDER BY t.id",
        params)}


def rows_from_db(db) -> list[dict]:
    """Les trades du journal reconstruits en lignes « direct » (même format que live.stock_row)."""
    out = []
    for (tid, market, symbol, side, day, planned, plan, ctx, comp, dec, label, trend, terc) in db.conn.execute(
            "SELECT t.id, t.market, t.symbol, t.side, t.day, t.planned_at, t.plan, t.context, t.composite, t.decision, "
            "r.label, r.trend, r.vol_tercile FROM trades t LEFT JOIN regimes r ON r.market = t.market AND r.day = t.day "
            "ORDER BY t.id"):
        out.append({"id": tid, "market": market, "symbol": symbol, "side": side, "day": day, "planned_at": planned,
                    "plan": json.loads(plan), "context": json.loads(ctx), "composite": comp, "decision": dec,
                    "regime_label": label, "regime_trend": trend, "regime_vol_tercile": terc})
    return out


def rule(cond, kind="exclude", markets=None, name="r"):
    return {"empreinte": "x" * 64, "nom": name, "kind": kind, "cond": cond, "markets": markets}


# --- Filtres : même SQL au labo et en direct ------------------------------------------------

def test_sans_regle_tout_est_garde():
    assert M.kept_sql([]) == ("1", [])
    assert M.evaluate_rows([], [{"market": "stock", "day": "2026-10-07", "planned_at": 0}]) == [{"garde": True, "regles": []}]


PARITY_RULES = [
    rule(SIGNAL),
    rule({"field": "context.setup", "op": "in", "value": ["divergence", "rebond_pivot"]}),
    rule({"field": "context.rvol", "op": ">", "value": 1.2}, kind="keep"),
    rule({"and": [{"field": "composite", "op": "<", "value": 6}, {"field": "plan.stop_pct", "op": "<", "value": -3}]}),
    rule({"or": [{"field": "weekday", "op": "==", "value": 1}, {"field": "hour_utc", "op": ">=", "value": 14}]}),
    rule({"field": "regime", "op": "==", "value": "baissier_agite"}),
    rule({"not": {"field": "context.regime.vol20_pct", "op": "between", "value": [10, 30]}}),
    rule({"field": "plan.targets[0].minutes_median", "op": "not_null"}, kind="keep"),
    rule({"field": "context.absent", "op": ">", "value": 1}),
    rule(SIGNAL, markets=["crypto"]),
]


@pytest.mark.parametrize("r", PARITY_RULES, ids=[f"regle{i}" for i in range(len(PARITY_RULES))])
def test_filtre_identique_au_labo_et_en_direct(db, r):
    synthetic_trades(db, n_days=12, per_day=8, seed=3)
    synthetic_trades(db, n_days=6, per_day=4, seed=4, market="crypto")
    days = [d for (d,) in db.conn.execute("SELECT DISTINCT day FROM trades WHERE market = 'stock'")]
    for i, d in enumerate(days):
        db.conn.execute("INSERT INTO regimes VALUES ('stock', ?, ?, 2, ?, '{}')",
                        (d, "baissier" if i % 2 else "haussier", "baissier_agite" if i % 2 else "haussier_calme"))
    lab = kept_ids(db, [r])
    rows = rows_from_db(db)
    live = M.evaluate_rows([r], rows)
    assert [lab[row["id"]] for row in rows] == [v["garde"] for v in live]
    assert any(lab.values())


def test_champ_absent_en_direct_n_est_pas_verifie():
    row = {"market": "stock", "day": "2026-10-07", "planned_at": 1_790_000_000, "plan": {}, "context": {}}
    assert M.evaluate_rows([rule(SIGNAL)], [row])[0]["garde"] is True          # exclure : pas vérifié → gardé
    out = M.evaluate_rows([rule(SIGNAL, kind="keep", name="garder")], [row])[0]
    assert out == {"garde": False, "regles": ["garder"]}                      # ne garder que : pas vérifié → écarté


def test_regles_limitees_a_un_marche():
    rows = [{"market": m, "day": "2026-10-07", "planned_at": 0, "context": {"signal": 0.9}} for m in ("stock", "crypto")]
    out = M.evaluate_rows([rule(SIGNAL, markets=["crypto"])], rows)
    assert [o["garde"] for o in out] == [True, False]


def test_seuil_auto_fige_a_la_derniere_fenetre():
    r = {"empreinte": "a" * 64, "nom": "auto", "regle": V.parse({"exclude": {"field": "context.rvol", "op": ">", "value": "auto"}}),
         "mesures": {"fenetres": [{"seuil": 2.5}, {"seuil": None}, {"seuil": 3.0}]}}
    assert M.concrete(r)["cond"] == {"field": "context.rvol", "op": ">", "value": 3}
    assert M.concrete(r | {"mesures": {"fenetres": [{"seuil": None}]}}) is None


def test_textes_en_francais():
    cond = V.parse({"exclude": {"and": [{"field": "context.rvol", "op": ">=", "value": 5.5},
                                        {"not": {"field": "context.setup", "op": "in", "value": ["divergence"]}}]}})["cond"]
    assert M.cond_fr(cond) == "rvol ≥ 5,5 et non (setup parmi divergence)"
    assert M.rule_fr({"kind": "keep", "cond": SIGNAL, "markets": ["stock"]}) == "ne garder que signal > 0,8 (actions)"
    assert M.fr(-0.4) == "−0,40 R" and M.fr(0.153, 3) == "+0,153 R" and M.fr(None) == "n/d"


def test_preuve_chiffree(db):
    h = insert_validated(db, "RSI tendu", {"field": "context.rvol", "op": ">", "value": 75})
    text = M.proof_fr(V.get_rule(db.conn, h))
    assert text == ("Les trades où rvol > 75 font −0,40 R en moyenne sur 212 trades ; les exclure donne +0,15 R par "
                    "trade sur 3 périodes de test (meilleur dans 3 sur 3).")


# --- Base : migration et versions -------------------------------------------------------------

def test_migration_idempotente_et_ancienne_base(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("CREATE TABLE method_versions (version TEXT PRIMARY KEY, created_at TEXT NOT NULL, settings TEXT NOT NULL);"
                       "INSERT INTO method_versions VALUES ('v1', '2024-01-01', '{}');")
    conn.commit()
    conn.close()
    ro = open_readonly(str(path))
    assert generation_version(ro) == "v1" and M.champion(ro)["version"] == "v1" and M.challenger(ro) is None
    ro.close()
    db = LabDB(str(path))
    LabDB(str(path)).close()  # deuxième ouverture : rien à refaire
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(rules)")}
    assert {"pourquoi", "council", "decision", "soiree"} <= cols


def test_version_de_generation_ignore_les_versions_filtres(db):
    synthetic_trades(db, n_days=3, per_day=2)
    db.conn.execute("INSERT INTO method_versions (version, created_at, settings, base, rules, role) "
                    "VALUES ('v2', '2030-01-01', '{}', 'v1', '[]', 'challenger')")
    assert generation_version(db.conn) == "v1" and V.current_version(db.conn) == "v1"
    assert db.method_version({"trading": {}})[0] == "v1"
    assert M.next_version(db.conn) == "v3"


# --- Décisions de Simão -----------------------------------------------------------------------

def test_approbation_puis_challenger(db):
    synthetic_trades(db, n_days=3, per_day=2)
    h = insert_validated(db, "signal tendu", SIGNAL)
    M.enqueue_decision(db.path, h, "approuvee", 4)
    assert M.ingest_decisions(db) == 1
    assert V.get_rule(db.conn, h)["decision"] == "approuvee"
    chall = M.ensure_challenger(db, NOW)
    assert chall["version"] == "v2" and chall["role"] == "challenger" and chall["base"] == "v1"
    assert [r["empreinte"] for r in chall["regles"]] == [h] and chall["debut_ts"] == int(NOW.timestamp())
    assert V.get_rule(db.conn, h)["decision"] == "en_essai"
    assert M.champion(db.conn)["version"] == "v1"  # rien ne change en direct avant la bascule
    assert not list((M.queue_root(db.path) / "decisions").glob("*.json"))


def test_refus_memoire_des_echecs(db):
    synthetic_trades(db, n_days=3, per_day=2)
    h = insert_validated(db, "signal tendu", SIGNAL)
    M.enqueue_decision(db.path, h, "refusee")
    M.ingest_decisions(db)
    r = V.get_rule(db.conn, h)
    assert r["etat"] == "rejetee" and r["decision"] == "refusee" and r["raisons"][0] == "refusée par Simão"
    again = V.submit(db, {"nom": "autre nom", "exclude": SIGNAL})
    assert again["deja_connue"] and again["etat"] == "rejetee"  # jamais retestée ni reproposée
    assert any(x["raison"] == "refusée par Simão" for x in V.rejected(db.conn))
    assert M.ensure_challenger(db, NOW) is None


def test_decisions_illisibles_ou_en_double(db):
    h = insert_validated(db, "a", SIGNAL)
    q = M.queue_root(db.path) / "decisions"
    q.mkdir(parents=True)
    (q / "abc.json").write_text("{pas du json")
    (q / ("f" * 64 + ".json")).write_text(json.dumps({"empreinte": "f" * 64, "decision": "approuvee"}))
    M.enqueue_decision(db.path, h, "approuvee")
    assert M.ingest_decisions(db) == 1
    M.enqueue_decision(db.path, h, "refusee")  # déjà décidée : ignorée
    assert M.ingest_decisions(db) == 0 and V.get_rule(db.conn, h)["decision"] == "approuvee"
    assert not list(q.glob("*.json"))
    with pytest.raises(ValueError):
        M.enqueue_decision(db.path, "../../etc", "approuvee")


def test_types_de_catalyseurs_par_la_file(db):
    for i, sym in enumerate(("AAA", "BBB")):
        db.conn.execute("INSERT INTO catalysts (market, day, symbol, kind, news, news_status, created_at) "
                        "VALUES ('stock', '2026-10-06', ?, 'mouvement', '[]', 'ok', 'x')", (sym,))
    M.enqueue_types(db.path, [{"id": 1, "type": "resultats"}, {"id": 2, "type": "inconnu"}, {"id": 99, "type": "macro"}])
    assert M.ingest_types(db) == 1
    types = dict(db.conn.execute("SELECT id, catalyst_type FROM catalysts"))
    assert types == {1: "resultats", 2: None}


# --- Challenger en direct : bascule, retrait --------------------------------------------------

def challenger_ready(db, live_effect, n_days=8, per_day=30, seed=21):
    synthetic_trades(db, n_days=5, per_day=4, seed=1)  # historique (rejeu)
    h = insert_validated(db, "signal tendu", SIGNAL)
    M.enqueue_decision(db.path, h, "approuvee")
    M.ingest_decisions(db)
    M.ensure_challenger(db, NOW)
    synthetic_trades(db, n_days=n_days, per_day=per_day, seed=seed, origin="live", first=date(2026, 10, 8),
                     effect=live_effect)
    return h


def losers(ctx, rnd):
    return -1.0 if ctx["signal"] > 0.8 else 0.1


def winners(ctx, rnd):
    return 1.5 if ctx["signal"] > 0.8 else 0.0


def test_comparaison_sur_le_direct_seulement(db):
    challenger_ready(db, losers, n_days=2, per_day=10)
    cmp = M.compare(db.conn, M.champion(db.conn), M.challenger(db.conn))
    live_trig = db.conn.execute("SELECT COUNT(*) FROM trades WHERE origin = 'live' AND triggered = 1").fetchone()[0]
    assert cmp["champion"]["declenches"] == live_trig
    assert 0 < cmp["challenger"]["declenches"] < live_trig
    assert M.verdict(cmp, V.config(db.conn)) == "attendre"  # moins de 100 trades


def test_bascule_automatique(db):
    h = challenger_ready(db, losers)
    assert M.judge_challenger(db, NOW) == "promouvoir"
    assert M.champion(db.conn)["version"] == "v2" and M.challenger(db.conn) is None
    assert V.get_rule(db.conn, h)["decision"] == "adoptee"
    ev = M.events(db.conn)
    assert len(ev) == 1 and ev[0]["texte"].startswith("🧪 Méthode v2 en place : « signal tendu »")
    assert M.write_champion(db)
    data = json.loads(M.champion_path(db.path).read_text())
    assert data["version"] == "v2" and data["regles"][0]["cond"] == SIGNAL
    assert not M.write_champion(db)  # inchangé : pas réécrit
    # Une nouvelle idée se teste désormais sur les trades que garde v2.
    rows = V.load_rows(db.conn, V.parse({"exclude": {"field": "context.rvol", "op": ">", "value": 4}}), "v1")
    kept = db.conn.execute("SELECT COUNT(*) FROM trades WHERE json_extract(context, '$.signal') <= 0.8").fetchone()[0]
    assert len(rows) == kept < db.count_trades()


def test_retrait_si_pas_mieux(db):
    h = challenger_ready(db, winners, n_days=15, per_day=40)
    assert M.judge_challenger(db, NOW) == "retirer"
    assert M.champion(db.conn)["version"] == "v1" and M.challenger(db.conn) is None
    assert V.get_rule(db.conn, h)["decision"] == "retiree"
    assert "retirée" in M.events(db.conn)[0]["texte"]
    assert M.ensure_challenger(db, NOW) is None  # pas de nouvel essai de la même règle


def test_capture_du_potentiel_protegee(db):
    challenger_ready(db, losers)
    # Les mouvements +10 % capturés seulement par des trades que le challenger retire.
    rows = db.conn.execute("SELECT market, day, symbol FROM trades WHERE origin = 'live' AND triggered = 1 AND tp1_hit = 1 "
                           "AND json_extract(context, '$.signal') > 0.8 LIMIT 5").fetchall()
    for m, d, s in rows:
        db.conn.execute("INSERT OR IGNORE INTO day_moves (market, day, symbol, up_pct) VALUES (?, ?, ?, 12)", (m, d, s))
    cmp = M.compare(db.conn, M.champion(db.conn), M.challenger(db.conn))
    assert cmp["champion"]["capture"] - cmp["challenger"]["capture"] >= 2 and not cmp["capture_ok"]
    assert M.judge_challenger(db, NOW) is None


def test_challenger_complete_le_meme_matin(db):
    synthetic_trades(db, n_days=3, per_day=2)
    a = insert_validated(db, "a", SIGNAL)
    b = insert_validated(db, "b", {"field": "context.rvol", "op": ">", "value": 4})
    M.enqueue_decision(db.path, a, "approuvee")
    M.ingest_decisions(db)
    M.ensure_challenger(db, NOW)
    M.enqueue_decision(db.path, b, "approuvee")
    M.ingest_decisions(db)
    chall = M.ensure_challenger(db, NOW)
    assert chall["version"] == "v2" and [r["nom"] for r in chall["regles"]] == ["a", "b"]


def test_approbation_attend_la_fin_du_challenger(db):
    challenger_ready(db, losers, n_days=2, per_day=10)
    b = insert_validated(db, "b", {"field": "context.rvol", "op": ">", "value": 4})
    M.enqueue_decision(db.path, b, "approuvee")
    M.ingest_decisions(db)
    assert M.ensure_challenger(db, NOW) is None and V.get_rule(db.conn, b)["decision"] == "approuvee"


def test_v3_herite_des_filtres_du_champion(db):
    challenger_ready(db, losers)
    M.judge_challenger(db, NOW)
    b = insert_validated(db, "b", {"field": "context.rvol", "op": ">", "value": 4})
    M.enqueue_decision(db.path, b, "approuvee")
    M.ingest_decisions(db)
    chall = M.ensure_challenger(db, datetime(2026, 11, 1, tzinfo=timezone.utc))
    assert chall["version"] == "v3" and [r["nom"] for r in chall["regles"]] == ["signal tendu", "b"]


def test_etape_du_worker_isole_les_erreurs(db, monkeypatch):
    synthetic_trades(db, n_days=2, per_day=2)
    monkeypatch.setattr(M, "ingest_types", lambda db: 1 / 0)
    report = M.step(db, NOW)
    assert report["types"].startswith("échec") and report["fichier"] is True


def test_etat_lisible_pour_labo_et_cockpit(db):
    challenger_ready(db, losers, n_days=2, per_day=10)
    st = M.state(db.conn)
    assert st["champion"]["version"] == "v1" and st["challenger"]["version"] == "v2"
    line = M.state_line(st)
    assert "challenger v2" in line and "/100 trades en direct" in line


def test_resume_du_soir_garde_le_nom_de_la_version_de_generation(db):
    synthetic_trades(db, n_days=3, per_day=2)
    db.conn.execute("INSERT INTO method_versions (version, created_at, settings, base, rules, role) "
                    "VALUES ('v2', '2030-01-01', '{}', 'v1', '[]', 'challenger')")
    assert SUM.build(db.conn, "2024-01-05")["version"] == "v1"


def test_metriques_de_regle_validee_toutes_faites():
    m = validated_metrics(seuil=2.0)
    assert m["fenetres"][-1]["seuil"] == 2.0


def test_passage_du_worker_inclut_la_methode(db):
    import asyncio

    from app.lab import worker as W

    h = insert_validated(db, "signal tendu", SIGNAL)
    synthetic_trades(db, n_days=3, per_day=2)
    M.enqueue_decision(db.path, h, "approuvee")
    report = asyncio.run(W.lab2(db, NOW, collect=False))
    assert report["methode"]["decisions"] == 1 and report["methode"]["challenger"] == "v2"
    assert json.loads(M.champion_path(db.path).read_text())["challenger"] == "v2"


def test_bascule_exige_une_marge_et_retrait_apres_delai():
    from datetime import datetime, timezone
    cfg = dict(V.DEFAULTS)
    base = {"depuis_ts": int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()), "capture_ok": True,
            "champion": {"declenches": 150, "esperance_r": 0.10}}
    juste = base | {"challenger": {"declenches": 150, "esperance_r": 0.105}, "meilleur": False}
    assert M.verdict(juste, cfg, datetime(2026, 1, 20, tzinfo=timezone.utc)) == "attendre"
    lent = base | {"challenger": {"declenches": 12, "esperance_r": 0.3}, "meilleur": True}
    assert M.verdict(lent, cfg, datetime(2026, 2, 1, tzinfo=timezone.utc)) == "attendre"
    assert M.verdict(lent, cfg, datetime(2026, 4, 2, tzinfo=timezone.utc)) == "retirer"
