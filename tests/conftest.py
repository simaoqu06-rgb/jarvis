"""Réglages communs des tests."""
from datetime import date

import pytest

from app.lab import seal


@pytest.fixture(autouse=True)
def seal_hors_des_dates_de_test(monkeypatch):
    """Les tests du labo rejouent des dates de 2026, dans le vrai bloc scellé : on le déplace en 2000
    pour qu'ils voient leurs trades. Les tests du scellé (test_lab_strategies.py) le remettent."""
    monkeypatch.setattr(seal, "SEALED_FROM", date(2000, 1, 3))
    monkeypatch.setattr(seal, "SEALED_TO", date(2000, 6, 30))


@pytest.fixture(autouse=True)
def aucun_recalcul_v1_ponctuel(monkeypatch):
    """Le worker recalcule une fois la séance d'actions du direct du 06/10/2026 (empreinte.V1_RECALCULS) : beaucoup de
    tests prennent ce jour pour un direct synthétique. Vidé ici ; les tests du recalcul le remettent."""
    from app.lab import empreinte as E

    monkeypatch.setattr(E, "V1_RECALCULS", ())


@pytest.fixture(autouse=True)
def change_eur_usd_sans_reseau(monkeypatch):
    """build_plan lit le change EUR→USD du jour (taille en euros) : fixé à 1,15 (défaut de Killmode), sans réseau.
    Les tests du change lui-même gardent la vraie fonction (importée avant ce réglage)."""
    from app.trading import killmode as K
    from app.trading import service as T

    async def rate():
        return K.EURUSD_DEFAULT

    monkeypatch.setattr(T, "usd_per_eur", rate)


@pytest.fixture(autouse=True)
def labo_sans_autonomie_du_council(monkeypatch):
    """Les tests du Labo 3 suivent l'ancien circuit (Simão approuve) ; ceux de l'autonomie du council
    (test_lab_council.py) la remettent."""
    monkeypatch.setenv("JARVIS_LAB_AUTONOMIE", "off")


@pytest.fixture(autouse=True)
def un_bracket_par_objectif(monkeypatch):
    """Les tests d'origine décrivent un bracket par objectif (JARVIS_IBKR_BRACKETS=3) ; ceux de la sortie unique sur
    TP2, défaut depuis le 25/10/2026 (test_ibkr.py, test_sortie_unique.py), retirent la variable."""
    monkeypatch.setenv("JARVIS_IBKR_BRACKETS", "3")


@pytest.fixture(autouse=True)
def journal_des_recos_en_memoire(monkeypatch):
    """Le journal des recommandations (data/reco.json) n'est jamais écrit par les tests : tick_confirm y note les GO
    confirmés."""
    from app import scheduler
    from app.trading import reco

    monkeypatch.setattr(scheduler, "reco_book", reco.RecoBook(None))


@pytest.fixture(autouse=True)
def journal_des_actions_du_cockpit_coupe(monkeypatch):
    """Le journal des actions du cockpit (data/cockpit_actions.jsonl) n'est jamais écrit par les tests ;
    test_cockpit_actions.py le dirige vers un fichier temporaire."""
    from app.web import cockpit_actions

    monkeypatch.setattr(cockpit_actions, "ACTIONS_LOG", None)
