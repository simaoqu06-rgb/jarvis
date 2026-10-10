"""Labo 3 : filtres partagés labo / direct, décisions, challenger, bascule et retrait (hors ligne)."""
import json
import random
import sqlite3
from datetime import date, datetime, timezone

import pytest

from app.lab import method as M
from app.lab import summary as SUM
from app.lab import validator as V
from app.lab.db import LabDB, generation_version, open_readonly
from app.trading import proposals as PR
from app.trading.settings import Settings
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

def challenger_ready(db, live_effect, n_days=20, per_day=30, seed=21, markets=None):
    synthetic_trades(db, n_days=5, per_day=4, seed=1)  # historique (rejeu)
    h = insert_validated(db, "signal tendu", SIGNAL, markets=markets)
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
    assert cmp["jours"] == 2 and M.verdict(cmp, V.config(db.conn)) == "attendre"  # avant le 1er regard (20 jours)


def test_bascule_apres_accord_de_simao(db):
    """Ex-test_bascule_automatique : au 1er regard (20 jours), la promotion est PROPOSÉE ; rien ne bascule
    avant l'accord de Simão."""
    h = challenger_ready(db, losers)
    assert M.judge_challenger(db, NOW) == "promouvoir"
    assert M.champion(db.conn)["version"] == "v1" and M.challenger(db.conn)["version"] == "v2"
    promo = M.pending_promotion(db.conn)
    assert promo["id"] == "v2-20" and promo["regard"] == 20
    assert promo["texte"].startswith("v2 = v1 + « signal tendu ». En direct sur 20 jours de trades : +0,")
    assert "Regard 1 sur 3." in promo["texte"] and not M.events(db.conn)
    assert M.judge_challenger(db, NOW) is None  # entre deux regards : rien
    assert M.write_champion(db)
    data = json.loads(M.champion_path(db.path).read_text())
    assert data["version"] == "v1" and data["regles"] == [] and data["promotion"]["id"] == "v2-20"
    M.enqueue_promotion(db.path, "v2-20", True, 7)
    assert M.ingest_decisions(db, NOW) == 1
    assert M.champion(db.conn)["version"] == "v2" and M.challenger(db.conn) is None
    assert V.get_rule(db.conn, h)["decision"] == "adoptee" and M.pending_promotion(db.conn) is None
    ev = M.events(db.conn)
    assert len(ev) == 1 and ev[0]["texte"].startswith("🧪 Méthode v2 en place : « signal tendu »")
    assert "(promotion approuvée par Simão)" in ev[0]["texte"]
    assert M.write_champion(db)
    data = json.loads(M.champion_path(db.path).read_text())
    assert data["version"] == "v2" and data["regles"][0]["cond"] == SIGNAL and data["promotion"] is None
    assert not M.write_champion(db)  # inchangé : pas réécrit
    # Une nouvelle idée se teste désormais sur les trades que garde v2.
    rows = V.load_rows(db.conn, V.parse({"exclude": {"field": "context.rvol", "op": ">", "value": 4}}), "v1")
    kept = db.conn.execute("SELECT COUNT(*) FROM trades WHERE json_extract(context, '$.signal') <= 0.8").fetchone()[0]
    assert len(rows) == kept < db.count_trades()


def test_retrait_si_pas_mieux(db):
    h = challenger_ready(db, winners, per_day=20)
    assert M.judge_challenger(db, NOW) == "attendre"  # 1er regard pas assez net : on continue de mesurer
    assert M.challenger(db.conn)["version"] == "v2" and db.get("methode", "regard:v2") == 20
    assert M.judge_challenger(db, NOW) is None and not M.events(db.conn)
    synthetic_trades(db, n_days=40, per_day=20, seed=22, origin="live", first=date(2026, 11, 5), effect=winners)
    assert M.judge_challenger(db, NOW) == "retirer"  # 40 puis 60 jours d'un coup : jugé une fois, au dernier
    assert M.champion(db.conn)["version"] == "v1" and M.challenger(db.conn) is None
    assert V.get_rule(db.conn, h)["decision"] == "retiree"
    text = M.events(db.conn)[0]["texte"]
    assert text.startswith("🧪 Méthode v2 retirée : pas assez net au dernier regard (60 jours de trades) : écart −0,")
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
    assert cmp["z"] > 3 and M.promotion_fails(cmp, V.config(db.conn)) == [
        f"capture du potentiel +10 en baisse ({cmp['champion']['capture']} → {cmp['challenger']['capture']})"]
    assert M.judge_challenger(db, NOW) == "attendre" and M.pending_promotion(db.conn) is None


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
    M.enqueue_promotion(db.path, "v2-20", True)
    M.ingest_decisions(db, NOW)
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
    assert st["challenger"]["prochain_regard"] == 20 and st["challenger"]["promotion"] is None
    line = M.state_line(st)
    assert "challenger v2 : 2 jours de trades en direct (prochain regard à 20), " in line
    # Seuil du prochain regard (20 jours, t à 19 degrés de liberté) du premier challenger, dit avec son rang.
    assert st["challenger"]["rang"] == 1 and round(st["challenger"]["seuil_z"], 2) == 2.53
    assert ", seuil z 2,53 (challenger n° 1)" in line and "trades en direct" in line


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


def cmp_at(jours, delta=0.05, z=3.0, n=400, capture_ok=True, par_marche=None):
    return {"depuis_ts": int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()), "jours": jours, "delta_r": delta,
            "z": z, "capture_ok": capture_ok, "champion": {"declenches": 500, "esperance_r": 0.10, "capture": 3},
            "challenger": {"declenches": n, "esperance_r": 0.10 + delta, "capture": 3},
            "par_marche": par_marche or {"stock": {"jours": jours, "delta_r": delta, "z": z}}}


def test_bascule_exige_une_marge_et_retrait_apres_delai():
    cfg = dict(V.DEFAULTS)
    assert round(V.challenger_z(1, 20, cfg), 2) == 2.53 and cfg["challenger_max_days"] >= 100
    t = datetime(2026, 1, 30, tzinfo=timezone.utc)
    assert M.verdict(cmp_at(20), cfg, t) == "promouvoir"
    assert M.verdict(cmp_at(20, delta=0.01), cfg, t) == "attendre"              # marge sous +0,02
    assert M.verdict(cmp_at(20, delta=0.04, z=2.0), cfg, t) == "attendre"       # marge tenue, z sous 2,53
    assert M.verdict(cmp_at(20, n=80), cfg, t) == "attendre"                    # moins de 100 trades gardés
    assert M.verdict(cmp_at(20, capture_ok=False), cfg, t) == "attendre"
    assert M.verdict(cmp_at(25), cfg, t, done=20) == "attendre"                 # entre deux regards
    assert M.verdict(cmp_at(41), cfg, t, done=20) == "promouvoir"               # 2e regard
    assert M.verdict(cmp_at(60, z=2.0), cfg, t, done=40) == "retirer"           # dernier regard pas assez net
    assert M.verdict(cmp_at(300, n=5000, z=2.0), cfg, t, done=60) == "attendre"  # regards faits : plus jugé
    lent = cmp_at(12)
    assert M.verdict(lent, cfg, datetime(2026, 3, 1, tzinfo=timezone.utc)) == "attendre"
    assert M.verdict(lent, cfg, datetime(2026, 4, 12, tzinfo=timezone.utc)) == "retirer"  # filet : 100 jours
    assert M.verdict(lent, cfg, datetime(2026, 4, 12, tzinfo=timezone.utc), pending=True) == "attendre"


def test_moins_de_20_jours_attendre_meme_avec_300_trades(db):
    challenger_ready(db, losers, n_days=8, per_day=60)
    cmp = M.compare(db.conn, M.champion(db.conn), M.challenger(db.conn))
    assert cmp["challenger"]["declenches"] >= 300 and cmp["jours"] == 8 and cmp["z"] > 3
    assert M.verdict(cmp, V.config(db.conn), NOW) == "attendre"
    assert M.judge_challenger(db, NOW) is None and M.challenger(db.conn)["version"] == "v2"


def test_marche_touche_en_recul_bloque_la_promotion(db):
    challenger_ready(db, losers, per_day=40)
    synthetic_trades(db, n_days=20, per_day=10, seed=23, market="crypto", origin="live", first=date(2026, 10, 8),
                     effect=winners)
    cmp = M.compare(db.conn, M.champion(db.conn), M.challenger(db.conn))
    assert cmp["marches"] is None and set(cmp["par_marche"]) == {"stock", "crypto"}
    assert cmp["delta_r"] >= 0.02 and cmp["z"] >= 2.13 and cmp["par_marche"]["crypto"]["delta_r"] < 0
    fails = M.promotion_fails(cmp, V.config(db.conn))
    assert len(fails) == 1 and fails[0].startswith("crypto : −0,")
    assert M.judge_challenger(db, NOW) == "attendre"


def test_regle_actions_mesuree_sur_les_actions_seulement(db):
    challenger_ready(db, losers, markets=["stock"])
    synthetic_trades(db, n_days=20, per_day=30, seed=24, market="crypto", origin="live", first=date(2026, 10, 8),
                     effect=winners)
    cmp = M.compare(db.conn, M.champion(db.conn), M.challenger(db.conn))
    stock = db.conn.execute("SELECT COUNT(*), SUM(r_net) FROM trades WHERE origin = 'live' AND triggered = 1 "
                            "AND market = 'stock'").fetchone()
    kept = db.conn.execute("SELECT COUNT(*), SUM(r_net) FROM trades WHERE origin = 'live' AND triggered = 1 "
                           "AND market = 'stock' AND json_extract(context, '$.signal') <= 0.8").fetchone()
    assert cmp["marches"] == ["stock"] and list(cmp["par_marche"]) == ["stock"] and cmp["jours"] == 20
    assert cmp["champion"]["declenches"] == stock[0] and cmp["challenger"]["declenches"] == kept[0]
    assert cmp["delta_r"] == pytest.approx(kept[1] / kept[0] - stock[1] / stock[0], abs=1e-4)
    assert cmp["delta_r"] == cmp["par_marche"]["stock"]["delta_r"]


def test_actions_comparees_a_la_mise_de_simao(db):
    from app.lab import strategies as ST

    res = {"triggered": True, "fill_price": 100.0, "order_prices": {"entry": 100.0, "stop": 98.0, "targets": [104.0]},
           "exits": [{"part": 1.0, "price": 104.0, "reason": "tp1"}]}
    r20 = ST.cost_variants("stock", res)["r_20e"]
    assert r20 is not None and r20 < 1.95
    assert M.compared_r("stock", json.dumps(res), 1.95) == r20
    assert M.compared_r("crypto", json.dumps(res), 1.95) == 1.95 and M.compared_r("stock", "{}", 0.4) == 0.4
    # Relecture d'intégration : un ordre au stop égal à l'entrée garde le R net, comme le grade (cost_r), au
    # lieu de faire échouer la comparaison (bascule du challenger, !labo, statut).
    from app.lab import grade as GR

    flat = json.dumps(res | {"order_prices": {"entry": 100.0, "stop": 100.0, "targets": [104.0]}})
    assert M.compared_r("stock", flat, 0.4) == 0.4 == GR.cost_r("stock", 0.4, flat)
    challenger_ready(db, losers, n_days=3, per_day=10)
    before = M.compare(db.conn, M.champion(db.conn), M.challenger(db.conn))["champion"]
    tid, r = db.conn.execute("SELECT id, r_net FROM trades WHERE origin = 'live' AND triggered = 1 LIMIT 1").fetchone()
    db.conn.execute("UPDATE trades SET result = ? WHERE id = ?", (json.dumps(res), tid))
    after = M.compare(db.conn, M.champion(db.conn), M.challenger(db.conn))["champion"]
    n = before["declenches"]
    assert after["esperance_r"] * n == pytest.approx(before["esperance_r"] * n + r20 - r, abs=1e-3)


def null_challenger_promoted(rnd: random.Random, cfgs: list[dict], per_day=120, n_days=60, removed=0.1) -> list[bool]:
    """Challenger nul : 10 % des trades retirés au hasard, mêmes regards et même code que le worker."""
    day_sums, done, kept, out = {}, [0] * len(cfgs), 0, [False] * len(cfgs)
    for day in range(n_days):
        d = [0.0, 0, 0.0, 0]
        for _ in range(per_day):
            r = max(-1.5, min(3.0, rnd.gauss(0, 1)))
            d[0] += r
            d[1] += 1
            if rnd.random() >= removed:
                d[2] += r
                d[3] += 1
        day_sums[day] = d
        kept += d[3]
        if day + 1 not in M.looks(cfgs[0]):
            continue
        cmp = M.measure_days(day_sums) | {"depuis_ts": 0, "capture_ok": True, "challenger": {"declenches": kept}}
        cmp["par_marche"] = {"stock": M.measure_days(day_sums)}
        for i, cfg in enumerate(cfgs):
            if done[i] is None:
                continue
            v = M.verdict(cmp, cfg, None, done[i])
            out[i] = out[i] or v == "promouvoir"
            done[i] = None if v != "attendre" else day + 1
    return out


def test_challenger_nul_rarement_promu():
    cfg = dict(V.DEFAULTS)
    sans_marge = cfg | {"challenger_min_gain": 0.0}  # le z et les regards fixes seuls
    rnd = random.Random(2026)
    runs = [null_challenger_promoted(rnd, [cfg, sans_marge]) for _ in range(200)]
    assert sum(r[0] for r in runs) / 200 <= 0.07
    assert sum(r[1] for r in runs) / 200 <= 0.07


def test_promotion_proposee_a_simao_par_le_circuit_des_propositions(db, tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_LAB_DB", db.path)
    monkeypatch.delenv("JARVIS_LAB_CHAMPION", raising=False)
    challenger_ready(db, losers)
    report = M.step(db, NOW)
    assert report["bascule"] == "promouvoir" and M.champion(db.conn)["version"] == "v1"
    props = PR.Proposals(str(tmp_path / "p.json"), Settings(None))
    [p] = M.sync_proposals(props)
    assert (p["kind"], p["value"], p["current"], p["promotion"]) == ("promotion", "v2", "v1", "v2-20")
    assert PR.text(p).startswith(f"**Proposition {p['id']}** : mettre en place la méthode **v2** à la place de v1")
    assert p["why"] == M.pending_promotion(db.conn)["texte"] and M.sync_proposals(props) == []
    assert M.state_line(M.state(db.conn)).endswith("· promotion proposée à Simão")
    props.decide(p["id"], True)
    assert PR.decision_text(p).startswith(f"✅ Proposition {p['id']} approuvée : la méthode v2 se met en place")
    assert [f.name for f in (M.queue_root(db.path) / "decisions").glob("*.json")] == ["promotion-v2-20.json"]
    assert M.champion(db.conn)["version"] == "v1"  # rien avant le passage du worker
    assert M.step(db, NOW)["decisions"] == 1
    assert M.champion(db.conn)["version"] == "v2" and M.sync_proposals(props) == []
    assert props.get(p["id"])["status"] == "approved"
    with pytest.raises(ValueError):
        M.enqueue_promotion(db.path, "../v2", True)


def test_promotion_refusee_retire_le_challenger(db):
    h = challenger_ready(db, losers)
    M.judge_challenger(db, NOW)
    M.enqueue_promotion(db.path, "v2-20", False)
    assert M.ingest_decisions(db, NOW) == 1
    assert M.champion(db.conn)["version"] == "v1" and M.challenger(db.conn) is None
    assert V.get_rule(db.conn, h)["decision"] == "retiree" and M.pending_promotion(db.conn) is None
    assert M.events(db.conn)[0]["texte"].startswith("🧪 Méthode v2 retirée : promotion refusée par Simão (")


def test_regard_suivant_remplace_la_promotion_sans_reponse(db):
    challenger_ready(db, losers, per_day=20)
    assert M.judge_challenger(db, NOW) == "promouvoir"
    props = PR.Proposals(None, Settings(None))
    [p] = M.sync_proposals(props, M.champion_payload(db.conn))
    # Pas de réponse ; le challenger reste mesuré et fait moins bien ensuite.
    synthetic_trades(db, n_days=20, per_day=20, seed=25, origin="live", first=date(2026, 11, 5), effect=winners)
    assert M.judge_challenger(db, NOW) == "attendre" and M.pending_promotion(db.conn) is None
    assert M.sync_proposals(props, M.champion_payload(db.conn)) == [] and props.get(p["id"])["status"] == "replaced"
    M.enqueue_promotion(db.path, "v2-20", True)  # réponse arrivée trop tard : ignorée
    assert M.ingest_decisions(db, NOW) == 0 and M.champion(db.conn)["version"] == "v1"
    assert M.challenger(db.conn)["version"] == "v2"


def test_promotion_qui_perd_encore_le_dit():
    cmp = cmp_at(20, delta=0.05) | {"marches": ["stock"]}
    cmp["challenger"]["esperance_r"], cmp["champion"]["esperance_r"] = -0.12, -0.17
    champ = {"version": "v1", "regles": []}
    chall = {"version": "v2", "regles": [{"empreinte": "a" * 64, "nom": "signal tendu"}]}
    text = M.promotion_text(cmp, dict(V.DEFAULTS), champ, chall, 20)
    assert text == ("v2 = v1 + « signal tendu ». En direct sur 20 jours de trades (actions) : +0,050 R par trade de "
                    "mieux que v1, z = 3,00, seuil z 2,53 (challenger n° 1) ; −0,120 R par trade sur 400 trades gardés, "
                    "contre −0,170 R sur "
                    "500 ; potentiel +10 capturé 3 fois (contre 3). Filtre moins perdant, méthode toujours perdante. "
                    "Regard 1 sur 3.")


# --- 2e tour d'avis (chantier A) : seuil du challenger, promotion sans réponse, étiquettes absolues ----------

def test_premier_regard_en_student_a_g_moins_1_degres():
    """A1 : le z en grappes du challenger n'a que 20 grappes (jours) au premier regard : seuil de Student à 19
    degrés de liberté, plus exigeant que le quantile normal (qui laissait passer trop de faux gains)."""
    import math
    from statistics import NormalDist

    from app.lab import stats as STATS

    cfg = dict(V.DEFAULTS)
    level = 6 * 0.05 / (math.pi ** 2) / 3  # alpha du challenger n° 1, réparti sur 3 regards
    assert V.challenger_z(1, 20, cfg) == pytest.approx(STATS.t_quantile(1 - level, 19))
    assert V.challenger_z(1, 20, cfg) > NormalDist().inv_cdf(1 - level) + 0.2
    assert V.challenger_z(1, 60, cfg) < V.challenger_z(1, 40, cfg) < V.challenger_z(1, 20, cfg)
    assert V.challenger_z(1, 1, cfg) == math.inf  # moins de 2 jours : rien n'est jugeable


def test_premier_regard_nul_promu_au_plus_a_son_alpha():
    """A1, simulation (graine fixe) : challenger nul (10 % des trades retirés au hasard), z seul (sans la marge de
    +0,02), premier regard à 20 jours, vrai code (measure_days, verdict) : fausse promotion ≤ son alpha
    (α₁/3 ≈ 1,01 %) + marge de bruit. Le quantile normal, lui, en laisse passer bien plus."""
    from statistics import NormalDist

    cfg = dict(V.DEFAULTS) | {"challenger_min_gain": 0.0}
    alpha = V.trial_level(1, cfg["challenger_alpha"]) / 3
    normal = NormalDist().inv_cdf(1 - alpha)
    rnd = random.Random(7)
    runs, promoted, promoted_normal = 3000, 0, 0
    for _ in range(runs):
        day_sums = {}
        for day in range(20):
            d = [0.0, 0, 0.0, 0]
            for _ in range(30):
                r = max(-1.5, min(3.0, rnd.gauss(0, 1)))
                d[0] += r
                d[1] += 1
                if rnd.random() >= 0.1:
                    d[2] += r
                    d[3] += 1
            day_sums[day] = d
        m = M.measure_days(day_sums)
        cmp = m | {"depuis_ts": 0, "capture_ok": True, "challenger": {"declenches": sum(d[3] for d in day_sums.values())},
                   "par_marche": {"stock": m}}
        promoted += M.verdict(cmp, cfg) == "promouvoir"
        promoted_normal += m["z"] is not None and m["z"] >= normal
    assert promoted / runs <= alpha + 0.005
    assert promoted_normal > promoted


def test_seuil_selon_le_rang_du_challenger():
    """A2 : alpha dépensé sur le rang k du challenger (6·0,05/(π²k²), même série que le validateur), Bonferroni sur
    les 3 regards, Student à g − 1 degrés ; la marge de +0,02 R reste."""
    cfg = dict(V.DEFAULTS)
    assert round(V.challenger_z(1, 20, cfg), 2) == 2.53 and round(V.challenger_z(1, 60, cfg), 2) == 2.39
    assert round(V.challenger_z(10, 20, cfg), 2) == 4.58 and round(V.challenger_z(10, 60, cfg), 2) == 3.96
    t = datetime(2026, 1, 30, tzinfo=timezone.utc)
    first, tenth = cfg | {"challenger_rang": 1}, cfg | {"challenger_rang": 10}
    assert M.verdict(cmp_at(20, z=3.0), first, t) == "promouvoir"
    assert M.verdict(cmp_at(20, z=3.0), tenth, t) == "attendre"
    assert M.promotion_fails(cmp_at(20, z=3.0), tenth) == ["z = 3,00, seuil z 4,58 (challenger n° 10)"]
    assert M.verdict(cmp_at(20, z=5.0, delta=0.01), tenth, t) == "attendre"  # marge de +0,02 R toujours exigée
    # Seuil injecté (judged_cfg) : la décision et le texte disent le même seuil.
    injected = cfg | {"challenger_min_z": 3.5, "challenger_rang": 4}
    assert M.verdict(cmp_at(20, z=3.4), injected, t) == "attendre"
    assert M.threshold_fr(cmp_at(20), injected) == "seuil z 3,50 (challenger n° 4)"


def test_rang_du_challenger_memorise_et_retrait_sans_regard_gratuit(db):
    """A2 : k = 1 + challengers précédents jugés à au moins un regard, mémorisé à la création. Un challenger retiré
    sans aucun regard (filet des 100 jours) ne consomme pas d'alpha."""
    synthetic_trades(db, n_days=5, per_day=4, seed=1)
    a = insert_validated(db, "a", SIGNAL)
    M.enqueue_decision(db.path, a, "approuvee")
    M.ingest_decisions(db)
    v2 = M.ensure_challenger(db, NOW)
    assert M.version_settings(db.conn, "v2")["rang_challenger"] == 1 and M.challenger_rank(db.conn, v2) == 1
    later = datetime(2027, 1, 20, tzinfo=timezone.utc)  # plus de 100 jours sans aucun trade en direct
    assert M.judge_challenger(db, later) == "retirer" and M.challenger(db.conn) is None
    assert "100 jours sans atteindre le dernier regard" in M.events(db.conn)[-1]["texte"]
    # v3 : v2 n'a jamais été jugée, k reste 1.
    rvol = {"field": "context.rvol", "op": ">", "value": 1.5}
    b = insert_validated(db, "b", rvol)
    M.enqueue_decision(db.path, b, "approuvee")
    M.ingest_decisions(db)
    v3 = M.ensure_challenger(db, later)
    assert v3["version"] == "v3" and M.challenger_rank(db.conn, v3) == 1
    synthetic_trades(db, n_days=20, per_day=30, seed=31, origin="live", first=date(2027, 1, 21),
                     effect=lambda ctx, rnd: -1.0 if ctx["rvol"] > 1.5 else 0.1)
    assert M.judge_challenger(db, later) == "promouvoir"  # jugée au premier regard : alpha dépensé
    assert "seuil z 2,53 (challenger n° 1)" in M.pending_promotion(db.conn)["texte"]
    M.enqueue_promotion(db.path, "v3-20", False)
    assert M.ingest_decisions(db, later) == 1 and M.challenger(db.conn) is None
    # v4 : une seule précédente jugée (v3), k = 2, et il ne bouge plus pendant l'essai.
    c = insert_validated(db, "c", {"field": "composite", "op": "<", "value": 5})
    M.enqueue_decision(db.path, c, "approuvee")
    M.ingest_decisions(db)
    v4 = M.ensure_challenger(db, datetime(2027, 3, 1, tzinfo=timezone.utc))
    assert v4["version"] == "v4" and M.version_settings(db.conn, "v4")["rang_challenger"] == 2
    db.set("methode", "regard:v2", 20)  # ce qui arrive ensuite aux autres versions ne change pas son rang
    assert M.judged_challengers(db.conn, "v4") == 2 and M.challenger_rank(db.conn, v4) == 2
    st = M.state(db.conn)["challenger"]
    assert st["rang"] == 2 and st["seuil_texte"] == f"seuil z {M.num_fr(V.challenger_z(2, 20))} (challenger n° 2)"
    # Challenger d'avant ce suivi (rang absent des settings) : calculé de la même façon.
    db.conn.execute("UPDATE method_versions SET settings = '{}' WHERE version = 'v4'")
    assert M.challenger_rank(db.conn, v4) == 3


def test_promotion_du_dernier_regard_sans_reponse_retiree_apres_14_jours(db):
    """A3 : une promotion proposée au dernier regard et restée sans réponse ne bloque plus le labo : retrait après
    14 jours (événement comme les autres retraits), proposition close, nouvelle règle approuvée mise en essai."""
    challenger_ready(db, losers, n_days=60, per_day=10)
    assert M.judge_challenger(db, NOW) == "promouvoir"  # 60 jours d'un coup : proposée au dernier regard
    assert M.pending_promotion(db.conn)["id"] == "v2-60"
    props = PR.Proposals(None, Settings(None))
    [p] = M.sync_proposals(props, M.champion_payload(db.conn))
    b = insert_validated(db, "b", {"field": "context.rvol", "op": ">", "value": 4})
    M.enqueue_decision(db.path, b, "approuvee")
    M.ingest_decisions(db)
    assert M.ensure_challenger(db, NOW) is None  # le challenger en attente bloque la file
    assert M.judge_challenger(db, datetime(2026, 10, 20, 6, 5, tzinfo=timezone.utc)) is None  # 13 jours : attendre
    when = datetime(2026, 10, 21, 6, 5, tzinfo=timezone.utc)
    assert M.judge_challenger(db, when) == "retirer"
    assert M.challenger(db.conn) is None and M.pending_promotion(db.conn) is None
    text = M.events(db.conn)[-1]["texte"]
    assert text.startswith("🧪 Méthode v2 retirée : promotion sans réponse de Simão (14 jours) (")
    assert M.sync_proposals(props, M.champion_payload(db.conn)) == [] and props.get(p["id"])["status"] == "replaced"
    assert M.ensure_challenger(db, when)["version"] == "v3"  # la file repart


def test_promotion_sans_reponse_d_un_regard_intermediaire():
    """A3 : avant le dernier regard, le regard suivant remplace la proposition (comportement gardé) ; passé 14
    jours, elle ne protège plus le challenger du filet des 100 jours."""
    cfg = dict(V.DEFAULTS)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    promo = {"id": "v2-20", "le": "2026-01-29T06:05:00+00:00"}
    assert M.verdict(cmp_at(25), cfg, datetime(2026, 2, 20, tzinfo=timezone.utc), done=20, pending=promo) == "attendre"
    late = datetime(2026, 4, 12, tzinfo=timezone.utc)  # 101 jours depuis le début de l'essai
    assert M.verdict(cmp_at(25), cfg, late, done=20, pending=promo) == "retirer"
    fresh = promo | {"le": "2026-04-01T06:05:00+00:00"}  # Simão a encore le temps de répondre
    assert M.verdict(cmp_at(25), cfg, late, done=20, pending=fresh) == "attendre"
    assert M.verdict(cmp_at(65), cfg, datetime(2026, 2, 20, tzinfo=timezone.utc), done=60, pending=promo) == "retirer"
    assert not M.promotion_expired(True, late, cfg) and start < late


def test_etiquette_absolue():
    """A6 : un écart relatif positif ne dit pas que la méthode gagne."""
    assert M.absolute_label({"esperance_r": -0.12}, 0.05) == "filtre moins perdant, méthode toujours perdante"
    assert M.absolute_label({"esperance_r": -0.20}, -0.03) == "méthode toujours perdante"
    assert M.absolute_label({"esperance_r": 0.05, "ic95": [-0.01, 0.11]}, 0.04) == \
        "méthode positive mais non prouvée (IC95 −0,010 ; +0,110)"
    assert M.absolute_label({"esperance_r": 0.05, "ic95": [0.01, 0.09]}) == ""
    assert M.absolute_label({"esperance_r": 0.05, "ic95": None}) == "" == M.absolute_label({"esperance_r": None})
    assert M.absolute_fr({"esperance_r": -0.12}) == " Filtre moins perdant, méthode toujours perdante."


def test_labo_et_cockpit_disent_methode_toujours_perdante(db, monkeypatch):
    """A6 : challenger à −0,12 contre un champion à −0,17 : !labo (state_line) et le cockpit le disent."""
    from app.lab import page as P

    challenger_ready(db, losers, n_days=2, per_day=10)
    cmp = cmp_at(5, delta=0.05) | {"marches": ["stock"]}
    cmp["challenger"]["esperance_r"], cmp["champion"]["esperance_r"] = -0.12, -0.17
    monkeypatch.setattr(M, "compare", lambda *a, **k: cmp)
    st = M.state(db.conn)
    assert st["challenger"]["etiquette"] == "filtre moins perdant, méthode toujours perdante"
    assert "· filtre moins perdant, méthode toujours perdante" in M.state_line(st)
    h = insert_validated(db, "testée", {"field": "context.rvol", "op": ">", "value": 3})
    html = P.render({"exists": True, "methode": st}, [], [V.get_rule(db.conn, h)], None, False)
    assert "<b>Filtre moins perdant, méthode toujours perdante.</b>" in html
    assert "seuil z 2,53 (challenger n° 1)" in html
    assert "<th>Écart / trade (relatif)</th>" in html and "Gain / trade" not in html


def test_bascule_d_une_methode_toujours_perdante_le_dit(db):
    """A6 : l'événement de mise en place (_promote, relayé le matin) porte l'étiquette absolue."""
    challenger_ready(db, lambda ctx, rnd: -1.0 if ctx["signal"] > 0.8 else -0.3)
    assert M.judge_challenger(db, NOW) == "promouvoir"
    M.enqueue_promotion(db.path, "v2-20", True)
    assert M.ingest_decisions(db, NOW) == 1 and M.champion(db.conn)["version"] == "v2"
    text = M.events(db.conn)[-1]["texte"]
    assert text.startswith("🧪 Méthode v2 en place") and text.endswith(" Filtre moins perdant, méthode toujours perdante.")
    note = json.loads(db.conn.execute("SELECT note FROM method_versions WHERE version = 'v2'").fetchone()[0])
    assert note["rang_challenger"] == 1 and note["seuil_z"] == round(V.challenger_z(1, 20), 3)
    assert note["challenger"]["esperance_r"] < 0 and note["challenger"]["ic95"][1] < 0
