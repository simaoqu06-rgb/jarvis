"""Bloc scellé du labo : les 6 derniers mois du rejeu, mis de côté pour le test final.

Avis de GPT et council du 7 octobre 2026 : une routine qui regarde chaque soir les résultats, puis
invente l'idée suivante, finit par apprendre sur ses données « hors échantillon ». Le seul remède
est un bloc que personne ne voit avant le jour où une stratégie a passé tout le reste.

- Les trades rejoués dont le jour tombe dans [SEALED_FROM, SEALED_TO] sont calculés comme les autres,
  mais rangés dans des tables à part (trades_sealed, strat_trades_sealed). Aucune route, aucune
  statistique, aucun brief du soir, aucun grade, aucun validateur ne lit ces tables : ils lisent
  trades et strat_trades, où ces jours n'existent pas.
- Le direct (origin = live) n'est jamais scellé : il commence après SEALED_TO.
- On n'ouvre le bloc qu'une fois, pour confirmer une stratégie avant le forward-test, par une
  décision explicite de Simão (pas de route prévue pour l'instant).
- Limite assumée : les mouvements du jour et les fiches catalyseurs de ces dates restent visibles
  (ils disent quelles valeurs ont bougé, pas ce qu'une stratégie y aurait gagné).
"""
from datetime import date

SEALED_FROM = date(2026, 4, 6)
SEALED_TO = date(2026, 10, 5)
DECIDED_ON = "2026-10-07"


def sealed(origin: str, day: str | date) -> bool:
    """Ce trade va-t-il dans le bloc scellé ?"""
    if origin != "replay":
        return False
    d = day if isinstance(day, date) else date.fromisoformat(str(day)[:10])
    return SEALED_FROM <= d <= SEALED_TO


def bounds() -> tuple[str, str]:
    return SEALED_FROM.isoformat(), SEALED_TO.isoformat()


def describe() -> dict:
    return {"du": SEALED_FROM.isoformat(), "au": SEALED_TO.isoformat(), "decide_le": DECIDED_ON,
            "texte": f"Rejeu du {SEALED_FROM:%d/%m/%Y} au {SEALED_TO:%d/%m/%Y} scellé : calculé, jamais montré."}
