"""Cache en mémoire avec durée de vie, plus la dernière valeur connue gardée sur disque."""
import json
import logging
import os
import time
from collections import OrderedDict
from pathlib import Path

from app.core.result import Result

log = logging.getLogger(__name__)
MAX_KEYS = 1000          # au-delà, les clés les moins récemment mises à jour sont oubliées
SAVE_EVERY_SECONDS = 30  # last_known.json réécrit au plus toutes les 30 s (et seulement s'il a changé)


class Store:
    def __init__(self, path: str | None, max_keys: int = MAX_KEYS, save_every: float = SAVE_EVERY_SECONDS):
        self._path = Path(path) if path else None
        self._max_keys = max_keys
        self._save_every = save_every
        self._fresh: OrderedDict[str, tuple[float, Result]] = OrderedDict()
        self._last: OrderedDict[str, dict] = OrderedDict()
        self._dirty = False
        self._saved_at = float("-inf")
        if self._path and self._path.exists():
            try:
                self._last = OrderedDict(json.loads(self._path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                log.warning("Fichier de dernières valeurs illisible, on repart de zéro")

    def get_fresh(self, key: str, ttl: float) -> Result | None:
        entry = self._fresh.get(key)
        if entry and time.monotonic() - entry[0] < ttl:
            return entry[1]
        return None

    def put(self, key: str, result: Result) -> None:
        self._fresh[key] = (time.monotonic(), result)
        self._fresh.move_to_end(key)
        while len(self._fresh) > self._max_keys:
            self._fresh.popitem(last=False)
        raw = result.to_dict()
        if self._last.get(key) != raw:
            self._last[key] = raw
            self._dirty = True
        self._last.move_to_end(key)
        while len(self._last) > self._max_keys:
            self._last.popitem(last=False)
            self._dirty = True
        if self._dirty and time.monotonic() - self._saved_at >= self._save_every:
            self._save()

    def flush(self) -> None:
        """Écrit les dernières valeurs pas encore enregistrées (arrêt du serveur)."""
        if self._dirty:
            self._save()

    def last_known(self, key: str) -> Result | None:
        raw = self._last.get(key)
        return Result.from_dict(raw) if raw else None

    def _save(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._last, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._path)
            self._dirty = False
            self._saved_at = time.monotonic()
        except OSError:
            log.warning("Impossible d'écrire %s", self._path)
