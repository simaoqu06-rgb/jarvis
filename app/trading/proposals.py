"""Réglages proposés par JARVIS, que Simão approuve ou refuse.

Le bilan du forward-test (paper.py) repère les critères qui ne paient pas et en fait des
propositions numérotées. Rien ne change tant que Simão n'a pas approuvé (boutons sous le message
Discord, `!oui 3` / `!non 3`, ou la route POST /api/trading/proposals/{id}/approve|refuse).
Une proposition refusée n'est pas reproposée avec la même valeur.
Gardées dans data/trading_proposals.json (JARVIS_PROPOSALS_PATH).
"""
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from app.trading.settings import settings as default_settings

log = logging.getLogger(__name__)
PROPOSALS_PATH = os.getenv("JARVIS_PROPOSALS_PATH", "data/trading_proposals.json")


class Proposals:
    def __init__(self, path: str | None = PROPOSALS_PATH, settings=default_settings):
        self.path = Path(path) if path else None
        self.settings = settings
        self.items: list[dict] = []
        if self.path and self.path.exists():
            try:
                self.items = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                log.warning("Propositions de réglages illisibles, on repart de zéro")

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, pid: int) -> dict | None:
        return next((p for p in self.items if p["id"] == pid), None)

    def pending(self) -> list[dict]:
        return [p for p in self.items if p["status"] == "pending"]

    def propose(self, key: str, value, why: str) -> dict | None:
        """Ajoute une proposition ; None si elle ne changerait rien, attend déjà, ou a été refusée."""
        if self.settings.get(key) == value:
            return None
        for p in self.items:
            if p["key"] == key and p["value"] == value and p["status"] in ("pending", "refused"):
                return None
        for p in self.pending():  # une seule proposition en attente par réglage : la plus récente
            if p["key"] == key:
                p["status"] = "replaced"
        item = {"id": max((p["id"] for p in self.items), default=0) + 1, "key": key, "value": value,
                "current": self.settings.get(key), "why": why, "status": "pending",
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "decided_at": None}
        self.items.append(item)
        self.save()
        return item

    def decide(self, pid: int, approve: bool) -> dict:
        p = self.get(pid)
        if not p:
            raise KeyError(f"proposition {pid} introuvable")
        if p["status"] != "pending":
            raise ValueError(f"proposition {pid} déjà {label(p['status'])}")
        if approve:
            self.settings.update({p["key"]: p["value"]})
        p["status"] = "approved" if approve else "refused"
        p["decided_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.save()
        return p


def label(status: str) -> str:
    return {"pending": "en attente", "approved": "approuvée", "refused": "refusée", "replaced": "remplacée"}[status]


def text(p: dict) -> str:
    return f"**Proposition {p['id']}** : `{p['key']}` {p['current']} → {p['value']} · {p['why']}"


def decision_text(p: dict) -> str:
    if p["status"] == "approved":
        return f"✅ Proposition {p['id']} approuvée : `{p['key']}` = {p['value']}"
    return f"❌ Proposition {p['id']} refusée : `{p['key']}` reste à {p['current']}"


proposals = Proposals()
