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
def change_eur_usd_sans_reseau(monkeypatch):
    """build_plan lit le change EUR→USD du jour (taille en euros) : fixé à 1,15 (défaut de Killmode), sans réseau.
    Les tests du change lui-même gardent la vraie fonction (importée avant ce réglage)."""
    from app.trading import killmode as K
    from app.trading import service as T

    async def rate():
        return K.EURUSD_DEFAULT

    monkeypatch.setattr(T, "usd_per_eur", rate)
