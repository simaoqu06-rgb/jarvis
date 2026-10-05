"""Cache en mémoire avec durée de vie, plus la dernière valeur connue gardée sur disque."""
import json
import logging
import os
import time
from pathlib import Path

from app.core.result import Result

log = logging.getLogger(__name__)


class Store:
    def __init__(self, path: str | None):
        self._path = Path(path) if path else None
        self._fresh: dict[str, tuple[float, Result]] = {}
        self._last: dict[str, dict] = {}
        if self._path and self._path.exists():
            try:
                self._last = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                log.warning("Fichier de dernières valeurs illisible, on repart de zéro")

    def get_fresh(self, key: str, ttl: float) -> Result | None:
        entry = self._fresh.get(key)
        if entry and time.monotonic() - entry[0] < ttl:
            return entry[1]
        return None

    def put(self, key: str, result: Result) -> None:
        self._fresh[key] = (time.monotonic(), result)
        self._last[key] = result.to_dict()
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
        except OSError:
            log.warning("Impossible d'écrire %s", self._path)
