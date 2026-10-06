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
    pass


def split_message(text: str, limit: int = MAX_LENGTH) -> list[str]:
    """Découpe un long texte en morceaux de moins de `limit` caractères, aux sauts de ligne."""
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current.strip():
        chunks.append(current)
    return chunks


async def send(text: str, webhook_url: str | None = None, transport: httpx.AsyncBaseTransport | None = None) -> int:
    """Envoie le texte (découpé si besoin). Renvoie le nombre de messages postés."""
    url = webhook_url or secret(KEY_NAME)
    if not url:
        raise DiscordError(f"{KEY_NAME} absente")
    sent = 0
    async with httpx.AsyncClient(
        timeout=TIMEOUT_SECONDS, headers={"User-Agent": USER_AGENT}, transport=transport
    ) as client:
        for chunk in split_message(text):
            for attempt in range(3):
                try:
                    response = await client.post(
                        url,
                        params={"wait": "true"},
                        json={"content": chunk, "allowed_mentions": {"parse": []}},
                    )
                except httpx.HTTPError as exc:
                    raise DiscordError(f"erreur réseau ({type(exc).__name__})") from exc
                if response.status_code == 429 and attempt < 2:
                    retry_after = float(response.json().get("retry_after", 1))
                    log.warning("Discord limite les envois, nouvelle tentative dans %.1f s", retry_after)
                    await asyncio.sleep(min(retry_after, 30))
                    continue
                if response.status_code not in (200, 204):
                    raise DiscordError(f"HTTP {response.status_code} : {response.text[:200]}")
                sent += 1
                break
    return sent


async def upsert(text: str, message_id: str | None, webhook_url: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> str:
    """Réécrit le message `message_id` du webhook (ou en poste un nouveau s'il n'existe plus).

    Renvoie l'identifiant du message. Doc : https://discord.com/developers/docs/resources/webhook#edit-webhook-message
    """
    url = webhook_url or secret(KEY_NAME)
    if not url:
        raise DiscordError(f"{KEY_NAME} absente")
    body = {"content": text[:MAX_LENGTH], "allowed_mentions": {"parse": []}}
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
