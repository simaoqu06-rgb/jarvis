"""Principale, puis secours, puis dernière valeur connue : jamais de réponse cassée."""
import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.config import LAST_KNOWN_PATH, secret
from app.core.cache import Store
from app.core.http import SourceError
from app.core.result import Result, now_iso

log = logging.getLogger(__name__)

store = Store(LAST_KNOWN_PATH)
_locks: dict[str, asyncio.Lock] = {}


@dataclass
class Source:
    name: str
    fetch: Callable[..., Awaitable[Any]]
    attribution: str | None = None
    requires_key: str | None = None  # variable d'environnement nécessaire


async def resolve(need: str, key: str, sources: list[Source], ttl: float, **kwargs) -> Result:
    cached = store.get_fresh(key, ttl)
    if cached:
        return cached
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        cached = store.get_fresh(key, ttl)
        if cached:
            return cached
        errors: list[str] = []
        for source in sources:
            if source.requires_key and not secret(source.requires_key):
                errors.append(f"{source.name} : clé {source.requires_key} absente")
                continue
            try:
                data = await source.fetch(**kwargs)
            except SourceError as exc:
                errors.append(f"{source.name} : {exc}")
                log.warning("%s indisponible pour %s : %s", source.name, key, exc)
                continue
            result = Result(
                need=need,
                data=data,
                source=source.name,
                attribution=source.attribution,
                updated_at=now_iso(),
                errors=errors,
            )
            store.put(key, result)
            return result

        last = store.last_known(key)
        if last:
            last.stale = True
            last.errors = errors
            last.message = (
                "Sources indisponibles pour le moment. Voici la dernière valeur connue, "
                f"mise à jour le {last.updated_at} (UTC)."
            )
            return last
        return Result(
            need=need,
            data=None,
            source=None,
            attribution=None,
            updated_at=None,
            stale=True,
            message="Donnée indisponible pour le moment et aucune valeur connue. Réessaie dans quelques minutes.",
            errors=errors,
        )
