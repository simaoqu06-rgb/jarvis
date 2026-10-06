"""Battement du worker : fichier lab.heartbeat dans le dossier de la base du labo (JARVIS_LAB_DB).

Rafraîchi par la boucle principale après chaque étape terminée (un lot de bougies, un jour rejoué) :
une boucle bloquée cesse de le rafraîchir et le healthcheck docker passe à « unhealthy ».
Inactif hors du worker (tests, serveur web) : `enabled` n'est mis à True que par le worker.
"""
from datetime import datetime, timezone
from pathlib import Path

from app.lab.db import db_path

enabled = False


def path() -> Path:
    return Path(db_path()).parent / "lab.heartbeat"


def beat() -> None:
    if not enabled:
        return
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(datetime.now(timezone.utc).isoformat(timespec="seconds"), encoding="utf-8")
