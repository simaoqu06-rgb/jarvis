"""Tests hors ligne de l'envoi Discord (aucun message réel n'est envoyé)."""
import httpx
import pytest

from app.notify import discord

URL = "https://discord.com/api/webhooks/1/test"


def transport(*responses):
    calls = []
    queue = list(responses)

    def handler(request):
        calls.append(request)
        return queue.pop(0)

    return httpx.MockTransport(handler), calls


def test_decoupe_sous_2000_caracteres():
    text = "\n".join(f"ligne {i} " + "x" * 90 for i in range(100))
    chunks = discord.split_message(text)
    assert len(chunks) > 1 and all(len(c) <= 2000 for c in chunks)
    assert "".join(chunks) == text


async def test_envoi_simple():
    mock, calls = transport(httpx.Response(200, json={"id": "1"}))
    assert await discord.send("bonjour", URL, mock) == 1
    assert calls[0].url.params["wait"] == "true"
    assert b'"content":"bonjour"' in calls[0].content
    assert b'"allowed_mentions":{"parse":[]}' in calls[0].content


async def test_reessai_apres_429(monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr(discord.asyncio, "sleep", no_sleep)
    mock, calls = transport(httpx.Response(429, json={"retry_after": 0.5}), httpx.Response(200, json={}))
    assert await discord.send("x", URL, mock) == 1
    assert len(calls) == 2


async def test_webhook_inconnu():
    mock, _ = transport(httpx.Response(404, json={"message": "Unknown Webhook", "code": 10015}))
    with pytest.raises(discord.DiscordError, match="404"):
        await discord.send("x", URL, mock)


async def test_sans_webhook(monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    with pytest.raises(discord.DiscordError, match="absente"):
        await discord.send("x")
