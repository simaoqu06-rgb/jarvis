"""Appels HTTP communs : délai de 8 secondes, User-Agent identifiant, erreurs uniformes."""
import time
from typing import Any, Callable

import httpx

from app.config import TIMEOUT_SECONDS, USER_AGENT
from app.core import sources_health


class SourceError(Exception):
    """Une source n'a pas pu fournir de donnée exploitable."""


async def _get(url: str, params: dict | None, headers: dict | None, accept: str) -> httpx.Response:
    """Lecture notée dans la santé des sources (cockpit), par hôte, sans l'URL ni ses paramètres."""
    started = time.monotonic()
    try:
        response = await _get_raw(url, params, headers, accept)
    except SourceError as exc:
        sources_health.fail(url, exc, started)
        raise
    sources_health.ok(url, started)
    return response


async def _get_raw(url: str, params: dict | None, headers: dict | None, accept: str) -> httpx.Response:
    base_headers = {"User-Agent": USER_AGENT, "Accept": accept}
    if headers:
        base_headers.update(headers)
    try:
        async with httpx.AsyncClient(
            timeout=TIMEOUT_SECONDS, headers=base_headers, follow_redirects=True
        ) as client:
            response = await client.get(url, params=params)
    except httpx.TimeoutException as exc:
        raise SourceError(f"délai de {TIMEOUT_SECONDS:.0f} s dépassé") from exc
    except httpx.HTTPError as exc:
        raise SourceError(f"erreur réseau ({type(exc).__name__})") from exc
    if response.status_code != 200:
        raise SourceError(f"HTTP {response.status_code}")
    return response


async def get_json(url: str, params: dict | None = None, headers: dict | None = None,
                   refusal: Callable[[Any], str | None] | None = None) -> Any:
    """JSON d'une source. `refusal(payload)` repère un refus du service caché dans une réponse HTTP 200 (Twelve Data :
    clé refusée, crédits épuisés) : son texte (« HTTP 401 … ») est noté comme un échec dans la santé des sources, puis
    levé en SourceError."""
    if refusal is None:
        response = await _get(url, params, headers, "application/json")
        try:
            return response.json()
        except ValueError as exc:
            raise SourceError("réponse qui n'est pas du JSON") from exc
    started = time.monotonic()
    try:
        response = await _get_raw(url, params, headers, "application/json")
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceError("réponse qui n'est pas du JSON") from exc
        refused = refusal(payload)
        if refused:
            raise SourceError(refused)
    except SourceError as exc:
        sources_health.fail(url, exc, started)
        raise
    sources_health.ok(url, started)
    return payload


async def get_text(url: str, params: dict | None = None, headers: dict | None = None) -> str:
    """Page brute (HTML) : mêmes délai, en-têtes et erreurs que get_json."""
    return (await _get(url, params, headers, "text/html")).text


def to_float(value: Any) -> float | None:
    """Convertit en nombre ; None si la valeur est absente ou illisible."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
