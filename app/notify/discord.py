"""Envoi de messages sur un salon Discord via un webhook (variable DISCORD_WEBHOOK_URL).

Doc : https://discord.com/developers/docs/resources/webhook#execute-webhook
Un message fait au plus 2 000 caractères ; en cas de 429, Discord indique
le délai d'attente dans le champ `retry_after`.
"""
import asyncio
import logging

import httpx

from app.config import TIMEOUT_SECONDS, USER_AGENT, secret

log = logging.getLogger(__name__)

KEY_NAME = "DISCORD_WEBHOOK_URL"
MAX_LENGTH = 2000


class DiscordError(Exception):
    """Envoi en échec. `sent_count` : nombre de morceaux déjà postés (pour reprendre au suivant)."""

    def __init__(self, message: str, sent_count: int = 0, status: int | None = None):
        super().__init__(message)
        self.sent_count = sent_count
        self.status = status  # code HTTP de Discord quand il a répondu

    @property
    def permanent(self) -> bool:
        """Refus définitif (message invalide, webhook supprimé) : inutile de réessayer."""
        return self.status is not None and 400 <= self.status < 500 and self.status != 429


def split_message(text: str, limit: int = MAX_LENGTH) -> list[str]:
    """Découpe un long texte en morceaux de moins de `limit` caractères, aux sauts de ligne."""
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            if current.strip():  # un morceau vide ou fait d'espaces est refusé par Discord (HTTP 400)
                chunks.append(current)
            current = ""
        current += line
    if current.strip():
        chunks.append(current)
    return [c for c in chunks if c.strip()]


def _retry_after(response: httpx.Response) -> float:
    """Délai demandé par Discord après un 429 ; 2 s si la réponse est illisible."""
    try:
        return max(0.0, float(response.json().get("retry_after", 2)))
    except (ValueError, TypeError, AttributeError):
        pass
    try:
        return max(0.0, float(response.headers.get("Retry-After", 2)))
    except (TypeError, ValueError):
        return 2.0


async def send(text: str, webhook_url: str | None = None, transport: httpx.AsyncBaseTransport | None = None) -> int:
    """Envoie le texte (découpé si besoin). Renvoie le nombre de messages postés."""
    return await send_parts(split_message(text), webhook_url, transport)


async def send_parts(parts: list[str], webhook_url: str | None = None,
                     transport: httpx.AsyncBaseTransport | None = None) -> int:
    """Envoie les morceaux dans l'ordre. Renvoie le nombre de messages postés.

    En cas d'échec, DiscordError.sent_count dit combien de morceaux sont déjà partis :
    on peut reprendre avec `parts[exc.sent_count:]` sans renvoyer les premiers.
    """
    url = webhook_url or secret(KEY_NAME)
    if not url:
        raise DiscordError(f"{KEY_NAME} absente")
    sent = 0
    async with httpx.AsyncClient(
        timeout=TIMEOUT_SECONDS, headers={"User-Agent": USER_AGENT}, transport=transport
    ) as client:
        for chunk in parts:
            if not chunk.strip():
                sent += 1  # rien à envoyer : compté comme fait pour garder les indices de reprise
                continue
            for attempt in range(3):
                try:
                    response = await client.post(
                        url,
                        params={"wait": "true"},
                        json={"content": chunk, "allowed_mentions": {"parse": []}},
                    )
                except httpx.HTTPError as exc:
                    raise DiscordError(f"erreur réseau ({type(exc).__name__})", sent) from exc
                if response.status_code == 429 and attempt < 2:
                    retry_after = _retry_after(response)
                    log.warning("Discord limite les envois, nouvelle tentative dans %.1f s", retry_after)
                    await asyncio.sleep(min(retry_after, 30))
                    continue
                if response.status_code not in (200, 204):
                    raise DiscordError(f"HTTP {response.status_code} : {response.text[:200]}", sent,
                                       response.status_code)
                sent += 1
                break
    return sent


def fit_message(text: str, limit: int = MAX_LENGTH) -> str:
    """Raccourcit le texte sous `limit` caractères en retirant des blocs entiers (un trade par bloc),
    jamais en coupant au milieu d'une ligne, et ajoute « … +N trades (voir le cockpit web) »."""
    if len(text) <= limit:
        return text
    blocks = text.split("\n\n")
    head, rest = blocks[0], blocks[1:]
    if rest and len(head) < limit:
        for keep in range(len(rest) - 1, -1, -1):
            candidate = "\n\n".join([head, *rest[:keep]]) + f"\n\n… +{len(rest) - keep} trades (voir le cockpit web)"
            if len(candidate) <= limit:
                return candidate
    # Pas de blocs (ou en-tête trop long) : on retire des lignes entières.
    lines = text.splitlines()
    for keep in range(len(lines) - 1, 0, -1):
        candidate = "\n".join(lines[:keep]) + f"\n… +{len(lines) - keep} lignes (voir le cockpit web)"
        if len(candidate) <= limit:
            return candidate
    return text[: limit - 1] + "…"


async def upsert(text: str, message_id: str | None, webhook_url: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> str:
    """Réécrit le message `message_id` du webhook (ou en poste un nouveau s'il n'existe plus).

    Renvoie l'identifiant du message. Doc : https://discord.com/developers/docs/resources/webhook#edit-webhook-message
    """
    url = webhook_url or secret(KEY_NAME)
    if not url:
        raise DiscordError(f"{KEY_NAME} absente")
    body = {"content": fit_message(text), "allowed_mentions": {"parse": []}}
    async with httpx.AsyncClient(
        timeout=TIMEOUT_SECONDS, headers={"User-Agent": USER_AGENT}, transport=transport
    ) as client:
        try:
            if message_id:
                response = await client.patch(f"{url.split('?')[0]}/messages/{message_id}", json=body)
                if response.status_code == 200:
                    return message_id
                if response.status_code != 404:
                    raise DiscordError(f"HTTP {response.status_code} : {response.text[:200]}")
            response = await client.post(url, params={"wait": "true"}, json=body)
        except httpx.HTTPError as exc:
            raise DiscordError(f"erreur réseau ({type(exc).__name__})") from exc
    if response.status_code != 200:
        raise DiscordError(f"HTTP {response.status_code} : {response.text[:200]}")
    return response.json()["id"]
