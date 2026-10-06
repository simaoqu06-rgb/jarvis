"""File d'envoi persistante (data/outbox.json) : un message Discord n'est retiré qu'une fois posté.

Chaque message est découpé en morceaux à l'entrée ; on retient combien sont déjà partis, pour
reprendre au morceau en échec sans reposter les premiers. Les messages partent dans l'ordre : un
échec passager (Discord en panne) arrête le passage, pour qu'un TP1 n'arrive jamais après le stop ;
un refus définitif (message invalide, webhook supprimé) ou trop de tentatives retire le message
pour ne pas bloquer la file. Un message peut avoir une date limite (`expires_at`) : passé ce délai, il
n'a plus de sens (point du matin à midi) et il est abandonné.
"""
import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger(__name__)
MAX_ITEMS = 500
MAX_AGE = timedelta(days=3)
MAX_ATTEMPTS = 120  # environ deux heures à un passage par minute


class Outbox:
    def __init__(self, path: str | None, split: Callable[[str], list[str]] | None = None):
        self.path = Path(path) if path else None
        self.split = split or (lambda text: [text])
        self.items: list[dict] = []
        self._lock = asyncio.Lock()  # deux passages simultanés ne posteraient pas deux fois le même morceau
        if self.path and self.path.exists():
            try:
                self.items = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                log.warning("File d'envoi illisible, on repart de zéro")

    def save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.items, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            log.warning("Impossible d'écrire %s", self.path)

    def add(self, texts: list[str] | str, kind: str = "alerte", expires_at: datetime | None = None,
            key: str | None = None) -> None:
        """Met des messages en file (et sur disque) ; `key` évite d'empiler deux fois le même."""
        if isinstance(texts, str):
            texts = [texts]
        if key and self.pending(key):
            return
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        added = 0
        for text in texts:
            parts = [p for p in self.split(text) if p.strip()]
            if not parts:
                continue
            self.items.append({"id": uuid.uuid4().hex[:8], "kind": kind, "key": key, "parts": parts, "sent": 0,
                               "attempts": 0, "created_at": now,
                               "expires_at": expires_at.isoformat(timespec="seconds") if expires_at else None})
            added += 1
        if not added:
            return
        if len(self.items) > MAX_ITEMS:
            log.warning("File d'envoi pleine : %d messages anciens abandonnés", len(self.items) - MAX_ITEMS)
            self.items = self.items[-MAX_ITEMS:]
        self.save()

    def pending(self, key: str) -> bool:
        return any(i.get("key") == key for i in self.items)

    async def flush(self, send: Callable[[str], Awaitable[object]], now: datetime | None = None) -> int:
        """Envoie ce qui peut l'être, dans l'ordre. Renvoie le nombre de messages terminés."""
        async with self._lock:
            return await self._flush(send, now or datetime.now(timezone.utc))

    async def _flush(self, send: Callable[[str], Awaitable[object]], now: datetime) -> int:
        done = 0
        for item in list(self.items):
            expires = item.get("expires_at")
            created = datetime.fromisoformat(item["created_at"])
            if (expires and now >= datetime.fromisoformat(expires)) or now - created > MAX_AGE:
                log.warning("Message « %s » abandonné après %d tentatives (trop tard)", item["kind"], item["attempts"])
                self.items.remove(item)
                self.save()
                continue
            try:
                while item["sent"] < len(item["parts"]):
                    await send(item["parts"][item["sent"]])
                    item["sent"] += 1
                    self.save()  # le morceau posté ne sera jamais renvoyé, même après un redémarrage
            except Exception as exc:
                item["attempts"] += 1
                log.warning("Envoi « %s » en échec (morceau %d/%d, tentative %d) : %s", item["kind"],
                            item["sent"] + 1, len(item["parts"]), item["attempts"], exc)
                if getattr(exc, "permanent", False) or item["attempts"] >= MAX_ATTEMPTS:
                    log.error("Message « %s » abandonné : %s", item["kind"], exc)
                    self.items.remove(item)
                    self.save()
                    continue  # refus définitif : on passe au suivant
                self.save()
                break  # panne passagère : on garde l'ordre et on réessaie au prochain passage
            self.items.remove(item)
            self.save()
            done += 1
        return done
