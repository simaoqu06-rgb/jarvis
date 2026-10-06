"""Validateur : format des règles, walk-forward, critères, essais multiples, mémoire des verdicts."""
import json
import random

import pytest

from app.lab import validator as V
from app.lab.db import LabDB
from tests.lab_fixtures import synthetic_trades

SIGNAL_RULE = {"nom": "signal tendu", "exclude": {"field": "context.signal", "op": ">", "value": 0.8}}


@pytest.fixture
def db(tmp_path):
    return LabDB(str(tmp_path / "lab.db"))


def hidden(cut: float = 0.8, penalty: float = -0.6):
    def effect(ctx, rnd):
        return penalty if ctx["signal"] > cut else 0.0
    return effect


# --- Format ---------------------------------------------------------------------------------

def test_regle_canonique_et_empreinte_stable():
    a = V.parse({"nom": "x", "exclude": {"and": [{"field": "context.rvol", "op": ">", "value": 3},
                                                 {"field": "composite", "op": "<", "value": 7}]}})
    b = V.parse({"exclude": {"and": [{"value": 7, "op": "<", "field": "composite"},
                                     {"field": "context.rvol", "op": ">", "value": 3}]}, "description": "autre"})
    assert V.rule_hash(a) == V.rule_hash(b)
    c = V.parse({"exclude": {"and": [{"field": "context.rvol", "op": ">", "value": 4},
                                     {"field": "composite", "op": "<", "value": 7}]}})
    assert V.rule_hash(c) != V.rule_hash(a)
    assert V.parse({"keep": {"or": [{"field": "regime", "op": "==", "value": "haussier_calme"}]}})["cond"] == \
        {"field": "regime", "op": "==", "value": "haussier_calme"}


def test_listes_triees_et_marches():
    a = V.parse({"keep": {"field": "context.setup", "op": "in", "value": ["b", "a", "a"]}, "markets": ["stock"]})
    assert a["cond"]["value"] == ["a", "b"] and a["markets"] == ["stock"] and a["kind"] == "keep"


@pytest.mark.parametrize("field", ["r_net", "tp1_hit", "mfe_r", "result.exits", "exit_reason", "day_range_pct",
                                   "minutes_to_tp2", "triggered", "context.gap_open_pct"])
def test_resultats_refuses_regard_vers_l_avenir(field):
    with pytest.raises(V.RuleError, match="avenir"):
        V.parse({"exclude": {"field": field, "op": ">", "value": 0}})


@pytest.mark.parametrize("raw, message", [
    ({"exclude": {"field": "context.x", "op": "~", "value": 1}}, "opérateur"),
    ({"exclude": {"field": "context.x", "op": ">", "value": 1}, "keep": {"field": "composite", "op": ">", "value": 1}},
     "exactement une"),
    ({}, "exactement une"),
    ({"exclude": {"field": "context.x", "op": ">", "value": 1}, "parametres": {"stop_atr": 2}}, "régénèrent"),
    ({"exclude": {"field": "context.x') OR 1=1 --", "op": ">", "value": 1}}, "chemin illisible"),
    ({"exclude": {"field": "inconnu", "op": ">", "value": 1}}, "champ inconnu"),
    ({"exclude": {"and": [{"field": "context.a", "op": ">", "value": "auto"},
                          {"field": "context.b", "op": ">", "value": 1}]}}, "une seule condition"),
    ({"exclude": {"field": "context.a", "op": "==", "value": "auto"}}, "auto"),
    ({"exclude": {"field": "context.a", "op": "in", "value": 3}}, "liste"),
    ({"exclude": {"field": "context.a", "op": "between", "value": [1]}}, "between"),
    ({"exclude": {"field": "context.a", "op": ">", "value": 1}, "markets": ["forex"]}, "markets"),
    ({"exclude": {"field": "context.a", "op": ">", "value": 1}, "lot": 3}, "clé inconnue"),
    ("pas du json", "JSON"),
])
def test_regles_illisibles(raw, message):
    with pytest.raises(V.RuleError, match=message):
        V.parse(raw)


def test_trop_de_conditions():
    leaves = [{"field": f"context.c{i}", "op": ">", "value": i} for i in range(V.MAX_LEAVES + 1)]
    with pytest.raises(V.RuleError, match="au plus"):
        V.parse({"exclude": {"or": leaves}})


def test_sql_parametre_sans_valeur_collee():
    canon = V.parse({"exclude": {"or": [{"field": "context.setup", "op": "==", "value": "x' OR '1'='1"},
                                        {"not": {"field": "plan.targets[0].r_multiple", "op": "<=", "value": 1.5}}]}})
    sql, params = V.compile_cond(canon["cond"])
    assert "x' OR" not in sql and "x' OR '1'='1" in params and "$.targets[0].r_multiple" in params
    assert sql.count("?") == len(params)


# --- Fenêtres et seuils ---------------------------------------------------------------------

def test_fenetres_chronologiques_sans_recouvrement_avec_embargo():
    days = [f"2024-01-{d:02d}" for d in range(1, 31)] + [f"2024-02-{d:02d}" for d in range(1, 11)]
    wins = V.windows(days, 3, embargo=1)
    assert len(wins) == 3
    tests = [t for _, t in wins]
    for fit, test in wins:
        assert max(fit) < min(test)
        gap = sorted(set(days)).index(min(test)) - sorted(set(days)).index(max(fit))
        assert gap == 2  # un jour d'embargo entre l'ajustement et le test
    assert not (tests[0] & tests[1]) and not (tests[1] & tests[2])
    assert wins[0][0] < wins[1][0] < wins[2][0]  # ajustement qui s'allonge


def test_seuil_monte_avec_le_nombre_d_essais():
    zs = [V.z_threshold(n) for n in (1, 2, 10, 100, 1000, 10 ** 6, 10 ** 18)]
    assert zs == sorted(zs) and zs[0] == pytest.approx(1.645, abs=1e-3) and zs[-2] > 5.0 and zs[-1] > 9.0


def test_jugement_pur():
    cfg = dict(V.DEFAULTS)
    m = {"touches_test": 150, "amelioration_moyenne_r": 0.08, "fenetres": [{}, {}, {}], "fenetres_meilleures": 2,
         "capture_avant": 20, "capture_apres": 19, "p": 0.001}
    assert V.judge(m, cfg, 1)[0] == "validee"
    verdict, fails, _ = V.judge(m | {"capture_apres": 15}, cfg, 1)
    assert verdict == "rejetee" and "potentiel +10" in fails[0]
    verdict, fails, _ = V.judge(m, cfg, 100)
    assert verdict == "rejetee" and "essai n° 100" in fails[0]
    verdict, fails, _ = V.judge(m | {"touches_test": 99, "fenetres_meilleures": 1}, cfg, 1)
    assert len(fails) == 2
    assert V.judge(m | {"capture_avant": 3, "capture_apres": 2}, cfg, 1)[0] == "validee"  # 1 de moins permis


# --- Tests imposés : bruit rejeté, règle cachée trouvée --------------------------------------

def test_rejette_des_regles_au_hasard_sur_du_bruit(db):
    synthetic_trades(db, n_days=200, per_day=30, seed=11)
    rnd = random.Random(5)
    rules = []
    for i in range(12):
        kind = rnd.choice(["signal", "rvol", "composite", "setup", "stop"])
        if kind == "signal":
            cond = {"field": "context.signal", "op": rnd.choice([">", "<"]), "value": round(rnd.uniform(0.2, 0.8), 3)}
        elif kind == "rvol":
            cond = {"field": "context.rvol", "op": ">", "value": round(rnd.uniform(0.6, 2.0), 3)}
        elif kind == "composite":
            cond = {"field": "composite", "op": "<", "value": round(rnd.uniform(5, 8), 1)}
        elif kind == "setup":
            cond = {"field": "context.setup", "op": "==", "value": rnd.choice(["divergence", "rebond_pivot"])}
        else:
            cond = {"field": "plan.stop_pct", "op": "<", "value": round(rnd.uniform(-4, -2), 2)}
        rules.append({"exclude": cond} if i % 2 else {"keep": {"not": cond}})
    rules.append({"exclude": {"field": "context.rvol", "op": ">", "value": "auto"}})
    verdicts = [V.test(db, r)["etat"] for r in rules]
    assert verdicts.count("rejetee") == len(rules), verdicts


def test_trouve_la_regle_gagnante_cachee(db):
    synthetic_trades(db, n_days=200, per_day=30, seed=12, effect=hidden())
    res = V.test(db, SIGNAL_RULE)
    assert res["etat"] == "validee", res["raisons"]
    m = res["mesures"]
    assert m["touches_test"] >= 100 and m["amelioration_moyenne_r"] >= 0.05 and m["fenetres_meilleures"] >= 2
    assert m["p"] < 0.05 and m["essai"] == 1
    # Une règle à côté (seuil trop bas : retire surtout de bons trades) ne passe pas.
    assert V.test(db, {"exclude": {"field": "context.signal", "op": "<", "value": 0.3}})["etat"] == "rejetee"


def test_seuil_auto_trouve_la_coupure_cachee(db):
    synthetic_trades(db, n_days=200, per_day=30, seed=13, effect=hidden(0.7, -1.0))
    res = V.test(db, {"exclude": {"field": "context.signal", "op": ">", "value": "auto"}})
    assert res["etat"] == "validee", res["raisons"]
    seuils = sorted(w["seuil"] for w in res["mesures"]["fenetres"])
    # Au-dessous de 0,7 les trades sont neutres : tout seuil entre ~0,4 et 0,7 se vaut, au bruit près.
    assert 0.35 <= seuils[0] and seuils[-1] <= 0.75


def test_seuil_auto_choisi_sans_voir_les_donnees_de_test(db):
    synthetic_trades(db, n_days=120, per_day=20, seed=14, effect=hidden(0.7))
    canon = V.parse({"exclude": {"field": "context.signal", "op": ">", "value": "auto"}})
    rows = V.load_rows(db.conn, canon, "v1")
    cfg = dict(V.DEFAULTS)
    first = V.walk_forward(rows, set(), cfg, "exclude", ">")
    last_test = V.windows([r[0] for r in rows], 3, 1)[-1][1]
    rng = random.Random(0)
    mutated = [(r[0], r[1], r[2], r[3], (rng.uniform(-5, 5) if r[3] else r[4]) if r[0] in last_test else r[4],
                r[5], r[6]) for r in rows]
    second = V.walk_forward(mutated, set(), cfg, "exclude", ">")
    assert [w["seuil"] for w in first["fenetres"]] == [w["seuil"] for w in second["fenetres"]]
    assert first["fenetres"][2]["amelioration_r"] != second["fenetres"][2]["amelioration_r"]


def test_la_barre_monte_avec_les_essais(db):
    synthetic_trades(db, n_days=200, per_day=30, seed=12, effect=hidden(0.8, -0.35))
    first = V.test(db, SIGNAL_RULE)
    assert first["etat"] == "validee", first["raisons"]
    assert first["mesures"]["z"] < V.z_threshold(10 ** 18)
    db.set("validator", "trials", 10 ** 18)  # 10^18 idées déjà testées : seuil p < 5e-20 (z > 9)
    again = V.test(db, {"exclude": {"field": "context.signal", "op": ">=", "value": 0.8}})
    assert again["etat"] == "rejetee"
    assert len(again["raisons"]) == 1 and "significativité" in again["raisons"][0]
    assert again["essai"] == 10 ** 18 + 1


def test_memoire_des_echecs(db):
    synthetic_trades(db, n_days=120, per_day=20, seed=15)
    rule = {"nom": "a", "exclude": {"or": [{"field": "context.rvol", "op": ">", "value": 2},
                                           {"field": "composite", "op": "<", "value": 5}]}}
    first = V.test(db, rule)
    assert first["etat"] == "rejetee" and db.get("validator", "trials") == 1
    same = V.test(db, {"nom": "b", "exclude": {"or": [{"value": 5, "op": "<", "field": "composite"},
                                                       {"field": "context.rvol", "op": ">", "value": 2}]}})
    assert same["deja_connue"] is True and same["etat"] == "rejetee" and same["mesures"] == first["mesures"]
    assert db.get("validator", "trials") == 1  # aucun nouvel essai
    assert V.rejected(db.conn)[0]["empreinte"] == first["empreinte"][:12]


def test_champ_absent_invalide_sans_consommer_d_essai(db):
    synthetic_trades(db, n_days=80, per_day=10, seed=16)
    res = V.test(db, {"exclude": {"field": "context.rsi", "op": ">", "value": 75}})
    assert res["etat"] == "invalide" and "absent" in res["raisons"][0]
    assert (db.get("validator", "trials") or 0) == 0


def test_historique_trop_court_reste_en_attente(db):
    synthetic_trades(db, n_days=30, per_day=10, seed=17)
    res = V.test(db, SIGNAL_RULE)
    assert res["etat"] == "en_attente" and "jours" in res["raisons"][0]
    assert (db.get("validator", "trials") or 0) == 0


def test_capture_du_potentiel_protegee(db):
    out = synthetic_trades(db, n_days=200, per_day=30, seed=12, effect=hidden())
    # Les gros mouvements du jour sont justement les trades à signal > 0,8 qui ont touché leur TP1.
    movers = [o for o in out if o["trig"] and o["ctx"]["signal"] > 0.8 and o["r"] > 0.3]
    db.conn.executemany("INSERT INTO day_moves (market, day, symbol, up_pct) VALUES ('stock', ?, ?, 12)",
                        [(o["day"], o["symbol"]) for o in movers])
    res = V.test(db, SIGNAL_RULE)
    assert res["etat"] == "rejetee"
    assert any("potentiel +10" in r for r in res["raisons"])
    assert res["mesures"]["capture_avant"] > 10 and res["mesures"]["capture_apres"] == 0


def test_seuils_modifiables_en_base(db):
    synthetic_trades(db, n_days=200, per_day=30, seed=12, effect=hidden())
    V.set_config(db, "min_affected", 100000)
    assert V.config(db.conn)["min_affected"] == 100000
    res = V.test(db, SIGNAL_RULE)
    assert res["etat"] == "rejetee" and "minimum 100000" in res["raisons"][0]
    with pytest.raises(KeyError):
        V.set_config(db, "inconnu", 1)


def test_marches_limites(db):
    synthetic_trades(db, n_days=200, per_day=30, seed=12, effect=hidden())
    synthetic_trades(db, n_days=200, per_day=10, seed=18, market="crypto")
    res = V.test(db, SIGNAL_RULE | {"markets": ["crypto"]})
    assert res["etat"] == "rejetee" and res["mesures"]["trades"] == 2000


# --- File et traitement -----------------------------------------------------------------------

def test_file_de_soumission_puis_traitement(db):
    synthetic_trades(db, n_days=200, per_day=30, seed=12, effect=hidden())
    out = V.enqueue(db.path, SIGNAL_RULE)
    again = V.enqueue(db.path, {"exclude": SIGNAL_RULE["exclude"], "nom": "autre nom"})
    assert out["empreinte"] == again["empreinte"] and out["etat"] == "en_file"
    files = list(V.queue_dir(db.path).glob("*.json"))
    assert len(files) == 1 and json.loads(files[0].read_text())["nom"] == "signal tendu"
    assert V.ingest_queue(db) == 1 and not list(V.queue_dir(db.path).glob("*.json"))
    assert V.counts(db.conn)["en_attente"] == 1
    assert V.process_pending(db, 0) == []
    done = V.process_pending(db, 5)
    assert [r["etat"] for r in done] == ["validee"] and V.counts(db.conn)["validee"] == 1


def test_file_bornee_et_fichier_illisible(db, monkeypatch):
    monkeypatch.setattr(V, "QUEUE_MAX", 2)
    V.enqueue(db.path, {"exclude": {"field": "composite", "op": "<", "value": 1}})
    V.enqueue(db.path, {"exclude": {"field": "composite", "op": "<", "value": 2}})
    with pytest.raises(V.RuleError, match="file pleine"):
        V.enqueue(db.path, {"exclude": {"field": "composite", "op": "<", "value": 3}})
    (V.queue_dir(db.path) / "mauvais.json").write_text('{"exclude": {"field": "r_net", "op": ">", "value": 0}}')
    assert V.ingest_queue(db) == 2 and not list(V.queue_dir(db.path).glob("*.json"))


def test_regle_inconnue_puis_liste(db):
    V.submit(db, SIGNAL_RULE)
    rules = V.list_rules(db.conn)
    assert rules[0]["nom"] == "signal tendu" and rules[0]["etat"] == "en_attente"
    assert V.list_rules(db.conn, "validee") == []


def test_regle_en_attente_de_donnees_ne_bloque_pas_la_file(db):
    synthetic_trades(db, n_days=200, per_day=10, seed=19, market="crypto")
    synthetic_trades(db, n_days=20, per_day=10, seed=20)  # actions : trop peu de jours pour l'instant
    V.submit(db, {"exclude": {"field": "composite", "op": "<", "value": 5}, "markets": ["stock"]})
    V.submit(db, {"exclude": {"field": "composite", "op": "<", "value": 5}, "markets": ["crypto"]})
    first = V.process_pending(db, 1)
    assert [r["etat"] for r in first] == ["en_attente"]
    second = V.process_pending(db, 1)
    assert second[0]["regle"]["markets"] == ["crypto"] and second[0]["etat"] in ("validee", "rejetee")
    assert "gain_total_r" in second[0]["mesures"]
