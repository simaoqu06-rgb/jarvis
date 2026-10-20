"""Empreinte du comportement du Labo 1 et révisions du code (avis de GPT et Gemini du 10 octobre, I2 et I3)."""
import shutil

import pytest

from app.lab import empreinte as E
from app.lab import simulator as SIM
from app.lab.db import LabDB
from app.trading import service as T


def test_le_labo_1_garde_son_comportement():
    # Ce test casse quand le comportement du Labo 1 change (plans, note, setup, simulation, coûts) : revenir en
    # arrière, ou ouvrir une nouvelle version de méthode et y noter la nouvelle empreinte. Jamais l'inverse.
    assert E.v1_fingerprint() == E.V1_FINGERPRINT


def test_une_autre_empreinte_figee_casse_le_test(monkeypatch):
    monkeypatch.setattr(E, "V1_FINGERPRINT", "0000000000")
    with pytest.raises(AssertionError):
        test_le_labo_1_garde_son_comportement()


def test_empreinte_voit_la_bougie_close_de_la_veille(monkeypatch):
    # b595c12 : à 9 h 10, la bougie de 15 h 45 de la veille n'est plus tenue pour en cours (niveaux, ATR, note)
    monkeypatch.setattr(T, "with_open_bar", lambda bars, interval, now: bars)
    assert E.v1_fingerprint() != E.V1_FINGERPRINT


def test_empreinte_voit_le_cout_et_la_simulation(monkeypatch):
    real = SIM.costs
    monkeypatch.setattr(SIM, "costs", lambda market, price, rank=None: real(market, price, rank) | {"taker": 0.01})
    assert E.v1_fingerprint() != E.V1_FINGERPRINT
    monkeypatch.setattr(SIM, "costs", real)
    simulate = SIM.simulate
    monkeypatch.setattr(SIM, "simulate", lambda *a, **k: simulate(*a, **k) | {"exit_reason": "autre"})
    assert E.v1_fingerprint() != E.V1_FINGERPRINT


def test_un_texte_change_ne_change_pas_l_empreinte(monkeypatch):
    real = T.plan_from_bars

    def reworded(*args, **kwargs):
        plan = real(*args, **kwargs)
        plan["stop_source"] = "autre formulation"
        plan["score"]["reasons"] = ["autre raison"]
        plan["checklist"]["verdict"] = "autre verdict"
        for t in plan["targets"]:
            t["source"] = "autre source"
        return plan

    monkeypatch.setattr(T, "plan_from_bars", reworded)
    assert E.v1_fingerprint() == E.V1_FINGERPRINT


def test_revision_du_code_calculee_une_fois_a_l_import(monkeypatch):
    assert E.missing() == []                                        # tous les noms suivis existent
    v1, strat = E.code_rev(), E.code_rev("strat")
    assert v1 == E.code_rev("v1") == E.CODE_REV["v1"] and len(v1) == 10 and strat != v1

    def boom(*a):
        raise AssertionError("relu")

    monkeypatch.setattr(E, "_parts", boom)
    monkeypatch.setattr(E, "_sha", boom)
    assert E.code_rev() == v1 and E.code_rev("strat") == strat     # rien n'est relu ni recalculé


def test_revision_strat_suit_tout_le_code_du_rattrapage():
    """Relecture : la n° 10 a ajouté au rattrapage (seule source des lignes v2 des actions) _factor, operation et
    plus10.OP_RATIO / scale sans les suivre ; changer le seuil des opérations sur titre ne changeait pas code_rev."""
    import ast

    tracked = {rel: set(names or ()) for rel, names in E.STRAT_CODE}
    tree = ast.parse((E.APP / "lab/rattrapage.py").read_text(encoding="utf-8"))
    top = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    top |= {t.id: n for n in tree.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
    seen, todo, plus10 = set(), ["stock_rows", "crypto_rows"], set()
    while todo:
        name = todo.pop()
        if name in seen or name == "log":
            continue
        seen.add(name)
        for node in ast.walk(top[name]):
            if isinstance(node, ast.Name) and node.id in top:
                todo.append(node.id)
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "P10":
                plus10.add(node.attr)
    assert seen - {"log"} <= tracked["lab/rattrapage.py"], seen - tracked["lab/rattrapage.py"]
    assert plus10 and plus10 <= tracked["lab/plus10.py"], plus10


def test_revision_suit_le_code_utile_pas_les_commentaires(tmp_path, monkeypatch):
    for rel, _ in E.STRAT_CODE:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(E.APP / rel, tmp_path / rel)
    monkeypatch.setattr(E, "APP", tmp_path)
    base = E._code_hash(E.V1_CODE)
    assert base == E.CODE_REV["v1"]
    service = tmp_path / "trading/service.py"
    src = service.read_text(encoding="utf-8")
    service.write_text(src + "\n# un commentaire\n\ndef autre_fonction_du_service():\n    return 1\n", encoding="utf-8")
    assert E._code_hash(E.V1_CODE) == base                        # hors labo : service.py n'est pas haché en entier
    service.write_text(src.replace("return bars + [", "return list(bars) + [", 1), encoding="utf-8")
    assert E._code_hash(E.V1_CODE) != base                        # with_open_bar : code du Labo 1


def test_signature_stable_et_compteurs():
    db = LabDB(":memory:")
    assert E.sig(db.conn, "trades", "strat") == [0, 0, E.STATS_CODE] == E.sig(db.conn, "trades", "strat")
    with db.transaction():
        db.bump("trades")
    assert E.sig(db.conn, "trades", "strat") == [1, 0, E.STATS_CODE]
    assert E.code_changed(None) and E.code_changed([1, 0, "autre"]) and not E.code_changed([1, 0, E.STATS_CODE])
    assert len(E.cost_rev()) == 10 and E.cost_rev() == E.cost_rev()
