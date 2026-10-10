"""Réglages proposés par JARVIS, que Simão approuve ou refuse.

Le bilan du forward-test (paper.py) repère les critères qui ne paient pas et en fait des
propositions numérotées. Rien ne change tant que Simão n'a pas approuvé (boutons sous le message
Discord, `!oui 3` / `!non 3`, ou la route POST /api/trading/proposals/{id}/approve|refuse).
Une proposition refusée n'est pas reproposée avec la même valeur.
Gardées dans data/trading_proposals.json (JARVIS_PROPOSALS_PATH).

Labo 3 : les règles validées par le labo arrivent ici aussi (kind « labo », même numérotation,
mêmes `!oui N` / `!non N`, mêmes boutons, même cockpit). Leur décision ne touche pas aux réglages :
elle est déposée dans la file du labo (data/lab_queue/decisions/) ; approuvée, la règle part en essai
comme challenger ; refusée, elle rejoint la mémoire des échecs et n'est jamais reproposée.

Grade de JARVIS (app/lab/grade.py) : une hausse de lettre du grade (qui augmente le risque proposé)
arrive ici aussi (kind « grade », envoyée tout de suite en privé avec ses boutons). La décision est
déposée dans la file du labo (data/lab_queue/grade/) ; le worker l'applique si la hausse tient encore.

Promotion du challenger (app/lab/method.py) : à un regard fixe où il fait nettement mieux, le labo ne
bascule pas seul ; il propose « mettre en place vX » (kind « promotion », envoyée tout de suite en privé
avec ses boutons). La décision est déposée dans data/lab_queue/decisions/ ; approuvée, le worker met vX
en place si c'est toujours la promotion du dernier regard ; refusée, vX est retirée.
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

    def pending(self, kind: str | None = None) -> list[dict]:
        return [p for p in self.items if p["status"] == "pending" and (kind is None or p.get("kind") == kind)]

    def lab(self, rule_hash: str) -> dict | None:
        return next((p for p in self.items if p.get("kind") == "labo" and p.get("empreinte") == rule_hash), None)

    def propose_lab(self, rule_hash: str, name: str, champion: str | None, proof: str, why: str | None,
                    group: str) -> dict | None:
        """Proposition d'une règle validée par le labo ; None si elle a déjà été proposée (une seule fois)."""
        if self.lab(rule_hash):
            return None
        item = {"id": max((p["id"] for p in self.items), default=0) + 1, "kind": "labo", "key": "méthode",
                "value": f"+ « {name} »", "current": champion or "v1", "why": proof, "pourquoi": why,
                "empreinte": rule_hash, "nom": name, "groupe": group, "offerte": False, "status": "pending",
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "decided_at": None}
        self.items.append(item)
        self.save()
        return item

    def grade(self, hausse_id: str) -> dict | None:
        return next((p for p in self.items if p.get("kind") == "grade" and p.get("hausse") == hausse_id), None)

    def propose_grade(self, hausse: dict, market_fr: str, current: str, target: str) -> dict | None:
        """Hausse de lettre du grade ; None si déjà proposée. Les hausses dépassées sont remplacées."""
        if self.grade(hausse["id"]):
            return None
        for p in self.pending("grade"):
            if p.get("marche") == hausse["id"].split("-")[0]:
                p["status"] = "replaced"
        item = {"id": max((p["id"] for p in self.items), default=0) + 1, "kind": "grade",
                "key": f"grade JARVIS {market_fr}", "value": target, "current": current, "why": hausse["raison"],
                "hausse": hausse["id"], "marche": hausse["id"].split("-")[0], "status": "pending",
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "decided_at": None}
        self.items.append(item)
        self.save()
        return item

    def retire_grade(self, live_ids: set[str]) -> int:
        """Hausses en attente qui ne tiennent plus (baisse, nouvelle méthode) : remplacées."""
        n = 0
        for p in self.pending("grade"):
            if p.get("hausse") not in live_ids:
                p["status"] = "replaced"
                n += 1
        if n:
            self.save()
        return n

    def promotion(self, promo_id: str) -> dict | None:
        return next((p for p in self.items if p.get("kind") == "promotion" and p.get("promotion") == promo_id), None)

    def propose_promotion(self, promo: dict) -> dict | None:
        """Promotion du challenger proposée à un regard ; None si déjà proposée. Celle d'un regard
        précédent est remplacée."""
        if self.promotion(promo["id"]):
            return None
        for p in self.pending("promotion"):
            p["status"] = "replaced"
        item = {"id": max((p["id"] for p in self.items), default=0) + 1, "kind": "promotion", "key": "méthode",
                "value": promo["version"], "current": promo["champion"], "why": promo["texte"],
                "promotion": promo["id"], "status": "pending",
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "decided_at": None}
        self.items.append(item)
        self.save()
        return item

    def retire_promotion(self, live_ids: set[str]) -> int:
        """Promotions en attente que le labo ne propose plus (regard suivant, challenger retiré) : remplacées."""
        n = 0
        for p in self.pending("promotion"):
            if p.get("promotion") not in live_ids:
                p["status"] = "replaced"
                n += 1
        if n:
            self.save()
        return n

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
        if p.get("kind") == "labo":
            from app.lab import db as lab_db
            from app.lab import method as lab_method

            try:  # d'abord la file du labo : si elle est inaccessible, la proposition reste en attente
                lab_method.enqueue_decision(lab_db.db_path(), p["empreinte"], "approuvee" if approve else "refusee", pid)
            except OSError as exc:
                raise ValueError(f"file du labo inaccessible ({exc}), proposition {pid} toujours en attente")
        elif p.get("kind") == "grade":
            from app.lab import db as lab_db
            from app.lab import grade as lab_grade

            try:
                lab_grade.enqueue_decision(lab_db.db_path(), p["hausse"], approve, pid)
            except OSError as exc:
                raise ValueError(f"file du labo inaccessible ({exc}), proposition {pid} toujours en attente")
        elif p.get("kind") == "promotion":
            from app.lab import db as lab_db
            from app.lab import method as lab_method

            try:
                lab_method.enqueue_promotion(lab_db.db_path(), p["promotion"], approve, pid)
            except OSError as exc:
                raise ValueError(f"file du labo inaccessible ({exc}), proposition {pid} toujours en attente")
        elif approve:
            self.settings.update({p["key"]: p["value"]})
        p["status"] = "approved" if approve else "refused"
        p["decided_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.save()
        return p


def label(status: str) -> str:
    return {"pending": "en attente", "approved": "approuvée", "refused": "refusée", "replaced": "remplacée"}[status]


def text(p: dict) -> str:
    if p.get("kind") == "grade":
        return (f"**Proposition {p['id']}** : {p['key']} {p['current']} → **{p['value']}** (le risque proposé "
                f"par trade augmente) · {p['why']}")
    if p.get("kind") == "labo":
        return f"**Proposition {p['id']}** : méthode {p['current']} + « {p['nom']} » · {p['why']}"
    if p.get("kind") == "promotion":
        return (f"**Proposition {p['id']}** : mettre en place la méthode **{p['value']}** à la place de {p['current']} "
                f"(ses filtres s'appliqueront aux plans de JARVIS) · {p['why']}")
    return f"**Proposition {p['id']}** : `{p['key']}` {p['current']} → {p['value']} · {p['why']}"


def decision_text(p: dict) -> str:
    if p.get("kind") == "grade":
        if p["status"] == "approved":
            return (f"✅ Proposition {p['id']} approuvée : {p['key']} passe à {p['value']} au prochain passage du "
                    "labo (si la mesure tient toujours)")
        return f"❌ Proposition {p['id']} refusée : {p['key']} reste à {p['current']}"
    if p.get("kind") == "labo":
        if p["status"] == "approved":
            return f"✅ Proposition {p['id']} approuvée : « {p['nom']} » part en essai au labo (challenger)"
        return f"❌ Proposition {p['id']} refusée : « {p['nom']} » ne sera plus proposée"
    if p.get("kind") == "promotion":
        if p["status"] == "approved":
            return (f"✅ Proposition {p['id']} approuvée : la méthode {p['value']} se met en place au prochain passage du "
                    "labo (si aucun regard plus récent ne l'a remplacée)")
        return f"❌ Proposition {p['id']} refusée : {p['value']} est retirée, {p['current']} reste en place"
    if p["status"] == "approved":
        return f"✅ Proposition {p['id']} approuvée : `{p['key']}` = {p['value']}"
    return f"❌ Proposition {p['id']} refusée : `{p['key']}` reste à {p['current']}"


proposals = Proposals()
