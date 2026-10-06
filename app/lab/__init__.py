"""Labo de performance : des centaines de trades simulés par jour avec le code de JARVIS en direct.

Les plans viennent de app/trading (plan.py, signals.py, score.py, settings.py) via
service.plan_from_bars ; le labo n'ajoute que les données (collector), l'univers du jour
(universe), la génération des plans (generator), l'exécution simulée (simulator) et le journal
SQLite (db). Le labo ne passe jamais d'ordre, ni réel ni fictif.
"""
