"""Appels HTTP communs : délai de 8 secondes, User-Agent identifiant, erreurs uniformes."""
from typing import Any

import httpx

from app.config import TIMEOUT_SECONDS, USER_AGENT


class SourceError(Exception):
    """Une source n'a pas pu fournir de donnée exploitable."""


async def get_json(url: str, params: dict | None = None, headers: dict | None = None) -> Any:
    base_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
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
    try:
        return response.json()
    except ValueError as exc:
        raise SourceError("réponse qui n'est pas du JSON") from exc


def to_float(value: Any) -> float | None:
    """Convertit en nombre ; None si la valeur est absente ou illisible."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
