"""Battement du worker : fichier lab.heartbeat dans le dossier de la base du labo (JARVIS_LAB_DB).

Rafraîchi par la boucle principale après chaque étape terminée (un lot de bougies, un jour rejoué) :
une boucle bloquée cesse de le rafraîchir et le healthcheck docker passe à « unhealthy ».
Inactif hors du worker (tests, serveur web) : `enabled` n'est mis à True que par le worker.

Le fichier porte aussi l'étape en cours (`stage`, posée par le worker : direct, rejeu d'un marché,
étape du Labo 2…), lue par /api/lab/status et !labo pour voir d'ailleurs où en est le passage.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from app.lab.db import db_path

enabled = False
stage: str | None = None


def path() -> Path:
    return Path(db_path()).parent / "lab.heartbeat"


def beat(where: str | None = None) -> None:
    """Rafraîchit le battement ; `where` change l'étape en cours (gardée pour les battements suivants)."""
    global stage
    if where:
        stage = where
    if not enabled:
        return
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"le": datetime.now(timezone.utc).isoformat(timespec="seconds"), "etape": stage},
                            ensure_ascii=False), encoding="utf-8")


def read(now: datetime | None = None, file: Path | None = None) -> dict | None:
    """Dernier battement vu du serveur : {le, etape, il_y_a_s}, ou None sans fichier."""
    p = file or path()
    try:
        raw = p.read_text(encoding="utf-8").strip()
        mtime = p.stat().st_mtime
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        data = {"le": raw or None, "etape": None}  # ancien format : la date seule
    now = now or datetime.now(timezone.utc)
    return {"le": data.get("le"), "etape": data.get("etape"), "il_y_a_s": max(0, round(now.timestamp() - mtime))}
