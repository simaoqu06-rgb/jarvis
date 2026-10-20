"""Validateur : format des règles, walk-forward, critères, essais multiples, mémoire des verdicts."""
import json
import random
from datetime import date, timedelta

import pytest

from app.lab import validator as V
from app.lab.db import LabDB
from tests.lab_fixtures import calendar_days, synthetic_trades

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
    assert zs == sorted(zs) and zs[0] == pytest.approx(1.875, abs=1e-3) and zs[-2] > 5.0 and zs[-1] > 9.0


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
    for r in rules:
        V.submit(db, r)  # soumises avant les trades : tous les jours sont neufs
    synthetic_trades(db, n_days=200, per_day=30, seed=11)
    verdicts = [V.test(db, r)["etat"] for r in rules]
    assert verdicts.count("rejetee") == len(rules), verdicts


def test_trouve_la_regle_gagnante_cachee(db):
    V.submit(db, SIGNAL_RULE)
    other = {"exclude": {"field": "context.signal", "op": "<", "value": 0.3}}
    V.submit(db, other)
    synthetic_trades(db, n_days=200, per_day=30, seed=12, effect=hidden())
    res = V.test(db, SIGNAL_RULE)
    assert res["etat"] == "validee", res["raisons"]
    m = res["mesures"]
    assert m["touches_test"] >= 100 and m["amelioration_moyenne_r"] >= 0.05 and m["fenetres_meilleures"] >= 2
    assert m["p"] < 0.05 and m["essai"] == 1
    # Une règle à côté (seuil trop bas : retire surtout de bons trades) ne passe pas.
    assert V.test(db, other)["etat"] == "rejetee"


def test_seuil_auto_trouve_la_coupure_cachee(db):
    rule = {"exclude": {"field": "context.signal", "op": ">", "value": "auto"}}
    V.submit(db, rule)
    synthetic_trades(db, n_days=200, per_day=30, seed=13, effect=hidden(0.7, -1.0))
    res = V.test(db, rule)
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
    second = {"exclude": {"field": "context.signal", "op": ">=", "value": 0.8}}
    V.submit(db, SIGNAL_RULE)
    V.submit(db, second)
    synthetic_trades(db, n_days=200, per_day=30, seed=12, effect=hidden(0.8, -0.35))
    first = V.test(db, SIGNAL_RULE)
    assert first["etat"] == "validee", first["raisons"]
    assert first["mesures"]["z"] < V.z_threshold(10 ** 18)
    db.set("validator", "trials", 10 ** 18)  # 10^18 idées déjà testées : seuil p < 5e-20 (z > 9)
    again = V.test(db, second)
    assert again["etat"] == "rejetee"
    assert len(again["raisons"]) == 1 and "significativité" in again["raisons"][0]
    assert again["essai"] == 10 ** 18 + 1


def test_memoire_des_echecs(db):
    rule = {"nom": "a", "exclude": {"or": [{"field": "context.rvol", "op": ">", "value": 2},
                                           {"field": "composite", "op": "<", "value": 5}]}}
    V.submit(db, rule)
    synthetic_trades(db, n_days=120, per_day=20, seed=15)
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
    V.submit(db, SIGNAL_RULE)
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
    V.submit(db, SIGNAL_RULE)
    synthetic_trades(db, n_days=200, per_day=30, seed=12, effect=hidden())
    V.set_config(db, "min_affected", 100000)
    assert V.config(db.conn)["min_affected"] == 100000
    res = V.test(db, SIGNAL_RULE)
    assert res["etat"] == "rejetee" and "minimum 100000" in res["raisons"][0]
    with pytest.raises(KeyError):
        V.set_config(db, "inconnu", 1)


def test_marches_limites(db):
    V.submit(db, SIGNAL_RULE | {"markets": ["crypto"]})
    synthetic_trades(db, n_days=200, per_day=30, seed=12, effect=hidden())
    synthetic_trades(db, n_days=200, per_day=10, seed=18, market="crypto")
    res = V.test(db, SIGNAL_RULE | {"markets": ["crypto"]})
    assert res["etat"] == "rejetee" and res["mesures"]["trades"] == 2000


# --- File et traitement -----------------------------------------------------------------------

def test_file_de_soumission_puis_traitement(db):
    out = V.enqueue(db.path, SIGNAL_RULE)
    again = V.enqueue(db.path, {"exclude": SIGNAL_RULE["exclude"], "nom": "autre nom"})
    assert out["empreinte"] == again["empreinte"] and out["etat"] == "en_file"
    files = list(V.queue_dir(db.path).glob("*.json"))
    assert len(files) == 1 and json.loads(files[0].read_text())["nom"] == "signal tendu"
    assert V.ingest_queue(db) == 1 and not list(V.queue_dir(db.path).glob("*.json"))
    assert V.counts(db.conn)["en_attente"] == 1
    synthetic_trades(db, n_days=200, per_day=30, seed=12, effect=hidden())  # les nuits suivantes
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
    V.submit(db, {"exclude": {"field": "composite", "op": "<", "value": 5}, "markets": ["stock"]})
    V.submit(db, {"exclude": {"field": "composite", "op": "<", "value": 5}, "markets": ["crypto"]})
    synthetic_trades(db, n_days=200, per_day=10, seed=19, market="crypto")
    synthetic_trades(db, n_days=20, per_day=10, seed=20)  # actions : trop peu de jours pour l'instant
    first = V.process_pending(db, 1)
    assert [r["etat"] for r in first] == ["en_attente"]
    second = V.process_pending(db, 1)
    assert second[0]["regle"]["markets"] == ["crypto"] and second[0]["etat"] in ("validee", "rejetee")
    assert "gain_total_r" in second[0]["mesures"]


# --- Relecture : régressions ----------------------------------------------------------------

def test_seuil_auto_introuvable_ne_touche_aucun_trade():
    """« keep » + auto sans seuil sur l'ajustement : la fenêtre reste telle quelle (avant : tous les
    trades étaient comptés comme touchés, ce qui gonflait le critère des 100 trades)."""
    rows = [(f"2024-01-{d:02d}", "stock", f"S{k}", 1, 0.1, 0, None) for d in range(1, 29) for k in range(5)]
    cfg = dict(V.DEFAULTS, auto_min_affected=10_000)
    m = V.walk_forward(rows, set(), cfg, "keep", ">")
    assert m["touches_test"] == 0
    assert all(w["seuil"] is None and w["touches"] == 0 for w in m["fenetres"])
    assert all(w["amelioration_r"] == 0 for w in m["fenetres"])


@pytest.mark.parametrize("field", ["context.dollar_volume", "context.universe_rank", "context.universe_reason",
                                   "plan.market_cap", "context.liquidity_score", "context.capitalisation"])
def test_filtres_de_liquidite_et_de_capitalisation_refuses(field):
    with pytest.raises(V.RuleError, match="liquidité"):
        V.parse({"exclude": {"field": field, "op": "<", "value": 1_000_000}})


def test_empreinte_insensible_aux_formes_equivalentes():
    """La mémoire des échecs ne se contourne pas en réécrivant la même règle."""
    a = V.parse({"exclude": {"field": "context.rvol", "op": ">", "value": 5}})
    assert V.rule_hash(V.parse({"exclude": {"field": "context.rvol", "op": ">", "value": 5.0}})) == V.rule_hash(a)
    assert V.rule_hash(V.parse({"exclude": {"field": "context.rvol", "op": ">", "value": 5},
                                "markets": ["stock", "crypto"]})) == V.rule_hash(a)
    b = V.parse({"exclude": {"and": [{"field": "composite", "op": "<", "value": 7},
                                     {"and": [{"field": "context.rvol", "op": ">", "value": 3}]}]}})
    c = V.parse({"exclude": {"and": [{"field": "context.rvol", "op": ">", "value": 3},
                                     {"field": "composite", "op": "<", "value": 7},
                                     {"field": "composite", "op": "<", "value": 7.0}]}})
    assert V.rule_hash(b) == V.rule_hash(c)
    d = V.parse({"exclude": {"field": "context.setup", "op": "in", "value": [2.0, 1, 2]}})
    assert d["cond"]["value"] == [1, 2]
    with pytest.raises(V.RuleError):
        V.parse({"exclude": {"field": "context.rvol", "op": ">", "value": float("nan")}})


def test_champ_absent_mesure_sur_les_marches_de_la_regle(db):
    """Un champ propre aux actions n'est pas « absent » parce que la crypto ne l'a pas."""
    synthetic_trades(db, n_days=10, per_day=5, seed=3, market="stock")
    synthetic_trades(db, n_days=40, per_day=20, seed=4, market="crypto")
    db.conn.execute("UPDATE trades SET context = json_remove(context, '$.signal') WHERE market = 'crypto'")
    canon = V.parse({"exclude": {"field": "context.signal", "op": ">", "value": 0.5}, "markets": ["stock"]})
    assert V.missing_share(db.conn, canon, "v1") == {"context.signal": 0.0}


def test_seuils_cumules_ne_depassent_jamais_alpha():
    """Le risque global de faux positif reste tenu, quel que soit le nombre d'idées testées."""
    from app.lab.validator import trial_level
    assert sum(trial_level(n, 0.05) for n in range(1, 100_001)) <= 0.05
    assert trial_level(2, 0.05) < trial_level(1, 0.05)


# --- Hors échantillon : seuls comptent les jours journalisés après la soumission (P3) -------------

def last_day(db) -> date:
    return date.fromisoformat(db.conn.execute("SELECT MAX(day) FROM trades").fetchone()[0])


def test_effet_cache_avant_la_frontiere_attend_puis_rejetee(db):
    synthetic_trades(db, n_days=120, per_day=30, seed=12, effect=hidden())  # lus par la routine du soir
    frontier = last_day(db)
    rule = V.submit(db, SIGNAL_RULE)
    assert V.frontier(db.conn, rule["empreinte"]) == {"replay:stock": frontier.isoformat()}
    res = V.run_one(db, rule["empreinte"])
    assert res["etat"] == "en_attente" and (db.get("validator", "trials") or 0) == 0
    assert "0 jours de trades neufs" in res["raisons"][0]
    explo = res["mesures"]["exploratoire"]  # le même walk-forward sur les jours déjà vus : affiché, sans effet
    assert explo["jours"] == 120 and explo["amelioration_moyenne_r"] > 0.05 and "sans effet sur le verdict" in res["raisons"][1]
    # Les nuits suivantes : 120 jours neufs, sans effet.
    synthetic_trades(db, n_days=120, per_day=30, seed=40, first=frontier + timedelta(days=1))
    res = V.run_one(db, rule["empreinte"])
    assert res["etat"] == "rejetee" and db.get("validator", "trials") == 1
    m = res["mesures"]
    assert m["jours"] == 120 and min(w["test"][0] for w in m["fenetres"]) > frontier.isoformat()
    assert m["exploratoire"] == explo and m["frontiere"] == {"replay:stock": frontier.isoformat()}


def test_effet_apres_la_frontiere_validee(db):
    synthetic_trades(db, n_days=60, per_day=30, seed=41)
    frontier = last_day(db)
    rule = V.submit(db, SIGNAL_RULE)
    synthetic_trades(db, n_days=200, per_day=30, seed=12, effect=hidden(), first=frontier + timedelta(days=1))
    res = V.run_one(db, rule["empreinte"])
    assert res["etat"] == "validee", res["raisons"]
    assert res["mesures"]["jours"] == 200 and res["mesures"]["exploratoire"]["jours"] == 60


def test_jour_vu_puis_recalcule_ne_compte_pas(db):
    synthetic_trades(db, n_days=80, per_day=30, seed=12, effect=hidden())
    rule = V.submit(db, SIGNAL_RULE)
    db.conn.execute("DELETE FROM trades")  # rattrapage : mêmes jours recalculés, id neufs
    synthetic_trades(db, n_days=80, per_day=30, seed=42, effect=hidden())
    res = V.run_one(db, rule["empreinte"])
    assert res["etat"] == "en_attente" and res["mesures"]["jours"] == 0


def test_regle_d_avant_la_frontiere_garde_tous_les_jours(db):
    synthetic_trades(db, n_days=200, per_day=30, seed=12, effect=hidden())
    rule = V.submit(db, SIGNAL_RULE)
    db.conn.execute("UPDATE rules SET seen_until = NULL WHERE hash = ?", (rule["empreinte"],))
    res = V.run_one(db, rule["empreinte"])
    assert res["etat"] == "validee" and res["mesures"]["jours"] == 200 and "frontiere" not in res["mesures"]


def test_jours_par_categorie_rejeu_non_consulte_et_direct(db):
    """Trois étiquettes de preuve (avis de GPT du 10 octobre) : jours vus, rejeu jamais lu, direct ; en jours
    distincts, dans les mesures, et dites dans la preuve du matin."""
    from app.lab import method as M

    synthetic_trades(db, n_days=60, per_day=30, seed=41)  # lus par la routine
    frontier = last_day(db)
    rule = V.submit(db, SIGNAL_RULE)
    synthetic_trades(db, n_days=70, per_day=30, seed=12, effect=hidden(), first=frontier + timedelta(days=1))
    synthetic_trades(db, n_days=5, per_day=30, seed=13, effect=hidden(), first=date(2026, 10, 5), origin="live")
    res = V.run_one(db, rule["empreinte"])
    m = res["mesures"]
    assert m["jours"] == 75 and m["jours_par_categorie"] == {"historique_exploratoire": 60,
                                                             "historique_non_consulte": 70, "prospectif": 5}
    text = M.proof_fr(res)
    assert "Jours neufs depuis la soumission : 70 historiques jamais lus (rejeu), 5 du direct." in text
    assert "historique seulement" not in text
    if res["etat"] == "validee":  # écart relatif dit comme tel, avec l'espérance des trades gardés
        assert "Écart relatif : les trades gardés font encore" in text and "avant commission IBKR à 20 €" in text
    # Sans jour du direct : validée sur l'historique seulement, le challenger décidera.
    no_live = res | {"mesures": m | {"jours_par_categorie": {"historique_exploratoire": 60,
                                                             "historique_non_consulte": 75, "prospectif": 0}}}
    assert ("Jours neufs depuis la soumission : 75 historiques jamais lus (rejeu), 0 du direct ; validée sur "
            "l'historique seulement : le challenger en direct décidera.") in M.proof_fr(no_live)


def test_jours_par_categorie_sans_walk_forward_exploratoire(db):
    """Le compte des jours vus ne dépend pas de l'exploratoire : sous 60 jours vus (pas d'exploratoire) et au
    2e passage (evaluate appelé sans exploration), il reste juste ; une règle en attente le porte aussi."""
    synthetic_trades(db, n_days=40, per_day=30, seed=1)
    frontier = last_day(db)
    rule = V.submit(db, SIGNAL_RULE)
    synthetic_trades(db, n_days=30, per_day=30, seed=2, first=frontier + timedelta(days=1))
    res = V.run_one(db, rule["empreinte"])
    assert res["etat"] == "en_attente" and "exploratoire" not in res["mesures"]
    assert res["mesures"]["jours_par_categorie"] == {"historique_exploratoire": 40, "historique_non_consulte": 30,
                                                     "prospectif": 0}
    db2 = LabDB(str(db.path).replace("lab.db", "lab2.db"))
    synthetic_trades(db2, n_days=70, per_day=30, seed=1)
    frontier = last_day(db2)
    rule = V.submit(db2, SIGNAL_RULE)
    assert V.run_one(db2, rule["empreinte"])["mesures"]["exploratoire"]["jours"] == 70  # 1er passage
    synthetic_trades(db2, n_days=65, per_day=30, seed=2, first=frontier + timedelta(days=1))
    res = V.run_one(db2, rule["empreinte"])  # 2e passage : evaluate(explore=False)
    assert res["etat"] in ("validee", "rejetee")
    assert res["mesures"]["jours_par_categorie"]["historique_exploratoire"] == 70
    assert res["mesures"]["jours_par_categorie"]["historique_non_consulte"] == 65


def test_regle_sans_frontiere_tout_est_exploratoire(db):
    """seen_until NULL : rien ne prouve que la règle n'a pas vu ses jours, pas de « non consulté »."""
    from app.lab import method as M

    synthetic_trades(db, n_days=60, per_day=30, seed=12, effect=hidden())
    synthetic_trades(db, n_days=5, per_day=30, seed=13, first=date(2026, 10, 5), origin="live")
    rule = V.submit(db, SIGNAL_RULE)
    db.conn.execute("UPDATE rules SET seen_until = NULL WHERE hash = ?", (rule["empreinte"],))
    res = V.run_one(db, rule["empreinte"])
    assert res["mesures"]["jours_par_categorie"] == {"historique_exploratoire": 65, "historique_non_consulte": 0,
                                                     "prospectif": 0}
    assert "65 jours qui ont pu être lus avant la règle (sans frontière) ; validée sur l'historique seulement" in \
        M.proof_fr(res)


def test_jours_par_categorie_ne_se_disent_pas_jours_de_test():
    """Relecture de la fusion (L4) : la catégorie compte TOUS les jours après la frontière, y compris le 1er bloc du
    walk-forward (ajustement seul) et les jours d'embargo, jamais testés : le libellé ne dit pas « Jours de test »."""
    from app.lab import method as M

    cats = {"historique_exploratoire": 60, "historique_non_consulte": 70, "prospectif": 5}
    seen = {"stock": "2026-03-02"}
    assert M.days_fr({"frontiere": seen, "jours_par_categorie": cats}) == \
        "Jours neufs depuis la soumission : 70 historiques jamais lus (rejeu), 5 du direct."
    no_frontier = M.days_fr({"jours_par_categorie": cats | {"historique_non_consulte": 0, "prospectif": 0}})
    assert no_frontier.startswith("Jours de données : 60 jours qui ont pu être lus avant la règle (sans frontière)")
    assert "Jours de test" not in no_frontier


def test_readme_cite_la_formule_du_code():
    """Le seuil de p que lit Simão est celui que le code applique (6·α/(π²·n²), pas 0,05 / n)."""
    from pathlib import Path

    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    assert "p < 6·α/(π²·n²)" in readme and "0,05 / n" not in readme
    assert f"{V.trial_level(1):.2f}".replace(".", ",") == "0,03" and "≈ 0,03 au 1er essai" in readme
    assert round(V.trial_level(10) * 1e4) == 3 and "3·10⁻⁴ au 10e" in readme
    assert "seuls comptent les jours journalisés après la soumission" in readme
    assert "6·alpha / (π²·n²)" in V.__doc__ and "SEULS COMPTENT LES JOURS JOURNALISÉS APRÈS LA SOUMISSION" in V.__doc__


# --- Fenêtres : blocs par marché, trou du calendrier et jours en direct (I5) -----------------------

def test_fenetres_annotees_trou_et_direct():
    replay = [d.isoformat() for d in calendar_days(date(2026, 1, 5), 60)]  # rejeu jusqu'au début d'avril
    live = [d.isoformat() for d in calendar_days(date(2026, 10, 6), 4)]  # puis le direct
    rows = [(d, "stock", f"S{k}", 1, 0.1 * ((k % 3) - 1), 0, k % 2) for d in replay + live for k in range(6)]
    m = V.walk_forward(rows, set(), dict(V.DEFAULTS), "exclude", live={("stock", d) for d in live})
    first, last = m["fenetres"][0], m["fenetres"][-1]
    assert last["trou_max_jours"] >= 180 and last["jours_direct"] == 4 and last["test"][1] == live[-1]
    assert first["trou_max_jours"] <= 4 and first["jours_direct"] == 0
    # Une seule série de jours : mêmes fenêtres qu'avant (blocs du marché).
    wins = V.market_windows(rows, 3, 1)
    assert [{d for _, d in t} for _, t in wins] == [t for _, t in V.windows([r[0] for r in rows], 3, 1)]


def test_blocs_par_marche_quand_les_rejeux_avancent_chacun_de_leur_cote():
    stock = [d.isoformat() for d in calendar_days(date(2025, 1, 6), 80)]
    crypto = [d.isoformat() for d in calendar_days(date(2025, 9, 1), 80, weekdays_only=False)]
    rows = [(d, "stock", "S", 1, 0.0, 0, 0) for d in stock] + [(d, "crypto", "C", 1, 0.0, 0, 0) for d in crypto]
    for fit, test in V.market_windows(rows, 3, 1):
        assert {m for m, _ in test} == {"stock", "crypto"} and {m for m, _ in fit} == {"stock", "crypto"}
        for market in ("stock", "crypto"):
            assert max(d for m, d in fit if m == market) < min(d for m, d in test if m == market)
