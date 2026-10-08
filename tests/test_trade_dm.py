"""Alertes des trades suivis copiées en message privé : le salon ne mentionne personne, le MP sonne."""
import asyncio

import pytest

from app import scheduler
from app.notify import bot
from app.trading import service as S
from tests.test_advice import NOW, trade


@pytest.fixture
def dm(monkeypatch):
    queue = asyncio.Queue()
    monkeypatch.setattr(bot, "trade_texts", queue)
    monkeypatch.setattr(bot, "enabled", lambda: True)
    monkeypatch.delenv("JARVIS_TRADE_DM", raising=False)
    monkeypatch.setattr(scheduler, "book", S.Book(None))
    monkeypatch.setattr(scheduler, "_queue", lambda texts, kind="alerte", **kw: None)
    monkeypatch.setenv("JARVIS_DISCORD_COCKPIT", "off")

    async def check(book, now=None, save=True):
        return ["🎯 TP1", "⚠️ sortie anticipée"]

    monkeypatch.setattr(scheduler.trading, "check_trades", check)
    return queue


def drain(queue: asyncio.Queue) -> list[str]:
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


async def test_alertes_de_trade_copiees_en_prive(dm):
    await scheduler.tick_trades()
    assert drain(dm) == ["🎯 TP1", "⚠️ sortie anticipée"]


async def test_copie_privee_coupable_et_sans_bot(dm, monkeypatch):
    monkeypatch.setenv("JARVIS_TRADE_DM", "off")
    await scheduler.tick_trades()
    assert drain(dm) == []
    monkeypatch.setenv("JARVIS_TRADE_DM", "on")
    monkeypatch.setattr(bot, "enabled", lambda: False)
    await scheduler.tick_trades()
    assert drain(dm) == []


async def test_conseil_de_cloture_copie_en_prive(dm, monkeypatch):
    from app.trading import advice as A

    scheduler.book.trades.append(trade(100.0, market="stock"))
    monkeypatch.setattr(scheduler.state, "data", {})
    monkeypatch.setattr(scheduler.state, "save", lambda: None)

    async def fake(trades, now):
        return [{"id": "t1", "symbol": "NVDA", "side": "long", "decision": "SORTIR", "why": "résultats", "facts": [],
                 "price": 100.0, "exit_r": 0.0, "at": now.isoformat()}]

    monkeypatch.setattr(A, "advise_book", fake)
    await scheduler.tick_closing(NOW)
    sent = drain(dm)
    assert len(sent) == 1 and "Avant la clôture US" in sent[0] and "SORTIR" in sent[0]


class User:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    async def send(self, text):
        if self.fail:
            raise RuntimeError("MP fermés")
        self.sent.append(text)


async def test_envoi_prive_decoupe_et_continue_apres_un_echec():
    users = {1: User(fail=True), 2: User()}
    fetched = []

    async def fetch(oid):
        fetched.append(oid)
        return users[oid]

    cache: dict = {}
    long_text = "🎯 " + "x" * 2500
    assert await bot.send_private(long_text, {1, 2}, fetch, cache) is False
    assert len(users[2].sent) == 2 and "".join(users[2].sent).replace("\n", "") == long_text
    assert 1 not in cache and 2 in cache  # le compte en échec sera recherché à nouveau
    assert await bot.send_private("🛑 stop", {2}, fetch, cache) is True
    assert fetched == [1, 2] and users[2].sent[-1] == "🛑 stop"
