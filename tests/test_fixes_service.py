"""Tests hors ligne des correctifs : suivi par action, crédits Twelve Data, file d'envoi, forward-test,
fenêtres du planificateur, CSRF, clôture à prix 0, journaux, caches bornés."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pytest

from app import scheduler
from app.core import fallback
from app.core.cache import Store
from app.core.http import SourceError
from app.core.outbox import Outbox
from app.core.result import Result
from app.sources import alpaca
from app.trading import paper
from app.trading import service as S

NY = S.NEW_YORK
SESSION = datetime(2026, 10, 6, 11, 0, tzinfo=NY)  # mardi, séance en cours


def plan(symbol="NVDA", entry=100.0, stop=98.0, tps=(102.0, 104.0), created=SESSION - timedelta(minutes=10)):
    targets = [{"name": f"TP{i}", "price": p, "pct": p - entry, "minutes_median": None, "eta_text": "",
                "eta_latest": None, "source": "test"} for i, p in enumerate(tps, start=1)]
    return {"symbol": symbol, "market": "stock", "side": "long", "entry": entry, "stop": stop, "stop_pct": -2.0,
            "targets": targets, "created_at": created.isoformat()}


def minute_bars(start: datetime, prices: list[tuple[float, float]]) -> list[dict]:
    t0 = int(start.timestamp())
    return [{"t": t0 + 60 * i, "o": 100.0, "h": h, "l": lo, "c": (h + lo) / 2, "v": 1.0} for i, (h, lo) in enumerate(prices)]


@pytest.fixture(autouse=True)
def reset_service_state():
    for d in (S._last_stock_check, S._last_plan_bar, S._catchup_done, S._candle_cache):
        d.clear()
    yield
    for d in (S._last_stock_check, S._last_plan_bar, S._catchup_done, S._candle_cache):
        d.clear()


# --- 2 et 3 : un appel par action, rattrapage, rien hors séance ---------------------------------

async def test_deux_trades_meme_action_un_seul_appel(monkeypatch):
    calls = []

    async def fake(symbol, interval, outputsize=5000, apikey=None):
        calls.append((symbol, interval, outputsize))
        if interval == 1:
            return minute_bars(SESSION - timedelta(minutes=5), [(101.0, 99.5), (102.5, 100.0)])
        return []

    monkeypatch.setattr(S.twelvedata, "candles", fake)
    book = S.Book(None)
    a, b = book.add(plan()), book.add(plan(tps=(102.4, 105.0)))
    messages = await S.check_trades(book, SESSION)
    assert [c[1] for c in calls].count(1) == 1
    assert a["targets_hit"] == 1 and b["targets_hit"] == 1 and len([m for m in messages if "TP1" in m]) == 2
    await S.check_trades(book, SESSION + timedelta(seconds=30))  # avant STOCK_CHECK_SECONDS : aucun appel
    assert [c[1] for c in calls].count(1) == 1


async def test_rattrapage_apres_coupure_demande_plus_de_bougies(monkeypatch):
    sizes = []

    async def fake(symbol, interval, outputsize=5000, apikey=None):
        sizes.append(outputsize)
        return minute_bars(SESSION - timedelta(minutes=1), [(100.5, 99.5)])

    monkeypatch.setattr(S.twelvedata, "candles", fake)
    book = S.Book(None)
    later = SESSION + timedelta(hours=3)  # 14 h : 3 h de cotation depuis le dernier passage
    book.add(plan(created=SESSION))
    await S.check_trades(book, later)
    assert sizes[0] >= 180
    S._last_stock_check.clear()
    t = book.trades[0]
    t["last_check"] = int((SESSION - timedelta(days=1)).timestamp())  # coupure d'une séance entière
    await S.check_trades(book, SESSION)
    assert 390 <= sizes[1] <= S.TWELVEDATA_MAX_OUTPUTSIZE


async def test_aucun_appel_un_jour_ferie(monkeypatch):
    async def fail(*a, **k):
        raise AssertionError("pas d'appel Twelve Data un jour férié")

    monkeypatch.setattr(S.twelvedata, "candles", fail)
    christmas = datetime(2026, 12, 25, 11, 0, tzinfo=NY)
    book = S.Book(None)
    t = book.add(plan(created=christmas - timedelta(minutes=10)))
    assert await S.check_trades(book, christmas) == []
    assert t["status"] == "open"


async def test_signaux_de_sortie_une_fois_par_bougie_du_plan(monkeypatch):
    calls = []

    async def fake(symbol, interval, outputsize=5000, apikey=None):
        calls.append(interval)
        if interval == 1:
            return minute_bars(SESSION - timedelta(minutes=1), [(101.0, 100.5)])  # en gain, sans TP
        return minute_bars(SESSION - timedelta(hours=2), [(101.0, 100.0)] * 10)

    monkeypatch.setattr(S.twelvedata, "candles", fake)
    monkeypatch.setattr(S.S, "exit_signals", lambda bars, side: [])
    book = S.Book(None)
    book.add(plan())
    await S.check_trades(book, SESSION + timedelta(minutes=1))
    S._last_stock_check.clear()
    await S.check_trades(book, SESSION + timedelta(minutes=3))  # même bougie de 15 min : pas de relecture
    assert calls.count(15) == 1 and calls.count(1) == 2
    S._last_stock_check.clear()
    S._candle_cache.clear()  # l'horloge du test avance plus vite que celle du cache
    await S.check_trades(book, SESSION + timedelta(minutes=16))
    assert calls.count(15) == 2


async def test_une_reponse_inattendue_n_arrete_pas_les_autres(monkeypatch):
    async def fake(symbol, interval, outputsize=5000, apikey=None):
        if symbol == "BAD":
            raise KeyError("meta")
        return minute_bars(SESSION - timedelta(minutes=1), [(102.5, 100.0)])

    monkeypatch.setattr(S.twelvedata, "candles", fake)
    book = S.Book(None)
    book.add(plan(symbol="BAD"))
    good = book.add(plan(symbol="AAPL"))
    messages = await S.check_trades(book, SESSION)
    assert good["targets_hit"] == 1 and any("AAPL" in m for m in messages)


# --- 4 : caches bornés -------------------------------------------------------------------------

def test_cache_des_bougies_borne(monkeypatch):
    monkeypatch.setattr(S, "CACHE_MAX_ENTRIES", 3)
    for i in range(5):
        S._candle_cache[(f"S{i}", "stock", 1, "")] = (1000.0 + i, [])
    S._candle_cache[("OLD", "stock", 1, "")] = (0.0, [])
    S._prune_cache(1000.0 + S.CACHE_MAX_AGE + 1)
    assert len(S._candle_cache) == 3 and ("OLD", "stock", 1) not in S._candle_cache


def test_store_borne_et_ecritures_espacees(tmp_path):
    path = tmp_path / "last.json"
    store = Store(str(path), max_keys=2, save_every=3600)
    r = Result("x", 1, "A", None, "2026-10-06T00:00:00+00:00")
    store.put("a", r)
    first = path.stat().st_mtime_ns
    store.put("b", r)
    store.put("c", r)
    assert path.stat().st_mtime_ns == first  # pas réécrit à chaque put
    assert store.last_known("a") is None and store.last_known("c")
    store.flush()
    assert "c" in path.read_text()


async def test_verrous_du_repli_bornes(monkeypatch):
    monkeypatch.setattr(fallback, "MAX_LOCKS", 3)
    monkeypatch.setattr(fallback, "store", Store(None))
    fallback._locks.clear()

    async def ok(**_):
        return 1

    for i in range(10):
        await fallback.resolve("x", f"k{i}", [fallback.Source("A", ok)], ttl=60)
    assert len(fallback._locks) <= 3


# --- 5 et 11 : file d'envoi persistante, reprise au morceau en échec ---------------------------

async def test_file_d_envoi_continue_et_reprend(tmp_path):
    box = Outbox(str(tmp_path / "outbox.json"), lambda text: text.split("|"))
    box.add(["p1|p2|p3", "autre"])
    sent, fail_on = [], {"p2"}

    async def send(part):
        if part in fail_on:
            raise RuntimeError("Discord HTTP 500")
        sent.append(part)

    await box.flush(send)
    assert sent == ["p1"]  # panne passagère : on s'arrête pour garder l'ordre
    box = Outbox(str(tmp_path / "outbox.json"), lambda text: text.split("|"))  # redémarrage
    fail_on.clear()
    await box.flush(send)
    assert sent == ["p1", "p2", "p3", "autre"] and box.items == []  # p1 jamais reposté, ordre gardé


async def test_refus_definitif_ne_bloque_pas_la_file(tmp_path):
    from app.notify.discord import DiscordError
    box = Outbox(str(tmp_path / "outbox.json"))
    box.add(["invalide", "a", "b"])
    sent = []

    async def send(part):
        if part == "invalide":
            raise DiscordError("HTTP 400 : message vide", status=400)
        sent.append(part)

    await box.flush(send)
    assert sent == ["a", "b"] and box.items == []


async def test_file_vide_pas_d_ecriture(tmp_path):
    path = tmp_path / "outbox.json"
    box = Outbox(str(path))
    box.add([])
    assert not path.exists()


async def test_message_perime_abandonne(tmp_path):
    box = Outbox(None)
    box.add("brief", expires_at=datetime(2026, 10, 6, 12, 0, tzinfo=NY))

    async def send(part):
        raise AssertionError("trop tard")

    await box.flush(send, now=datetime(2026, 10, 6, 13, 0, tzinfo=NY))
    assert box.items == []


async def test_trades_mis_en_file_avant_sauvegarde(monkeypatch, tmp_path):
    box = Outbox(str(tmp_path / "o.json"))
    monkeypatch.setattr(scheduler, "outbox", box)
    monkeypatch.setattr(scheduler, "book", S.Book(None))
    monkeypatch.setenv("JARVIS_DISCORD_COCKPIT", "off")

    async def check(book, now=None, save=True):
        assert save is False
        return ["🎯 TP1", "🛑 stop"]

    async def down(text):
        raise scheduler.discord.DiscordError("HTTP 503")

    monkeypatch.setattr(scheduler.trading, "check_trades", check)
    monkeypatch.setattr(scheduler.discord, "send", down)
    await scheduler.tick_trades()
    assert [i["parts"][0] for i in Outbox(str(tmp_path / "o.json")).items] == ["🎯 TP1", "🛑 stop"]


# --- 9, 10, 11 : fenêtres et reprises du planificateur ----------------------------------------

class FakeState:
    def __init__(self):
        self.data = {}

    def save(self):
        pass


def test_point_du_matin_borne_a_midi(monkeypatch):
    monkeypatch.setattr(scheduler, "state", FakeState())
    at = lambda h, m=0: datetime(2026, 10, 6, h, m, tzinfo=NY)  # noqa: E731
    assert not scheduler.brief_due(at(8, 59))
    assert scheduler.brief_due(at(9, 0)) and scheduler.brief_due(at(11, 59))
    assert not scheduler.brief_due(at(12, 0)) and not scheduler.brief_due(at(20, 0))
    assert scheduler.brief_due(datetime(2026, 10, 10, 9, 30, tzinfo=NY))  # samedi : envoyé comme avant


async def test_point_du_matin_reprend_au_morceau_en_echec(monkeypatch, tmp_path):
    monkeypatch.setattr(scheduler, "state", FakeState())
    box = Outbox(str(tmp_path / "o.json"), lambda text: text.split("|"))
    monkeypatch.setattr(scheduler, "outbox", box)

    async def brief(now):
        return "partie1|partie2"

    sent, fail = [], [True]

    async def send(text):
        if text == "partie2" and fail[0]:
            raise scheduler.discord.DiscordError("HTTP 500")
        sent.append(text)

    monkeypatch.setattr(scheduler.alerts, "morning_brief", brief)
    monkeypatch.setattr(scheduler.discord, "send", send)
    now = datetime(2026, 10, 6, 9, 1, tzinfo=NY)
    await scheduler.run_brief(now)
    assert not scheduler.brief_due(now + timedelta(minutes=1))  # pas régénéré
    fail[0] = False
    await box.flush(scheduler._send_part, now + timedelta(minutes=1))
    assert sent == ["partie1", "partie2"]


def test_scan_saute_les_jours_feries(monkeypatch):
    monkeypatch.setattr(scheduler.alpaca, "configured", lambda: True)
    monkeypatch.setattr(scheduler, "state", FakeState())
    assert not scheduler.scan_due(datetime(2026, 11, 26, 9, 15, tzinfo=NY))  # Thanksgiving
    assert scheduler.scan_due(datetime(2026, 11, 27, 9, 15, tzinfo=NY))


async def test_scan_en_echec_retente_puis_abandonne(monkeypatch, tmp_path):
    monkeypatch.setattr(scheduler.alpaca, "configured", lambda: True)
    monkeypatch.setattr(scheduler, "state", FakeState())
    monkeypatch.setattr(scheduler, "outbox", Outbox(None))
    now = datetime(2026, 10, 6, 9, 15, tzinfo=NY)
    monkeypatch.setattr(scheduler, "datetime", type("D", (datetime,), {"now": staticmethod(lambda tz=None: now)}))
    calls = []

    async def boom(now):
        calls.append(1)
        raise scheduler.scanner.ScanError("Alpaca HTTP 500")

    async def send(text):
        calls.append(text)

    monkeypatch.setattr(scheduler.scanner, "scan", boom)
    monkeypatch.setattr(scheduler.discord, "send", send)
    for _ in range(5):
        await scheduler.tick_scan()
    await asyncio.gather(*scheduler._background)  # envois Discord lancés en tâche de fond
    assert calls.count(1) == scheduler.MAX_ATTEMPTS["scan"]
    assert scheduler.state.data["scan_date"] == "2026-10-06" and any("impossible" in str(c) for c in calls)


# --- 6 et 7 : forward-test ---------------------------------------------------------------------

def paper_plan():
    p = plan(tps=(102.0, 104.0, 106.0))
    p["score"] = {"decision": "GO", "composite": 7.5, "parts": {"technique": {"score": 7}}}
    return p


@pytest.fixture
def alpaca_ok(monkeypatch):
    monkeypatch.setattr(alpaca, "configured", lambda: True)

    async def account():
        return {"equity": "100000"}

    monkeypatch.setattr(alpaca, "account", account)


async def test_jambe_refusee_annule_les_autres(monkeypatch, alpaca_ok):
    submitted, cancelled = [], []

    async def submit(symbol, qty, side, entry, tp, stop, client_order_id):
        if len(submitted) == 2:
            raise SourceError("HTTP 422")
        submitted.append(f"o{len(submitted)}")
        return {"id": submitted[-1]}

    async def cancel(order_id):
        if order_id == "o1":
            raise SourceError("HTTP 500")
        cancelled.append(order_id)

    monkeypatch.setattr(alpaca, "submit_bracket", submit)
    monkeypatch.setattr(alpaca, "cancel_order", cancel)
    book = paper.PaperBook(None)
    with pytest.raises(SourceError):
        await paper.submit(book, paper_plan(), s={"paper_enabled": True, "paper_risk_pct": 1.0})
    assert cancelled == ["o0"]
    (entry,) = book.entries  # l'ordre non annulé reste suivi, annulation retentée
    assert [leg["order_id"] for leg in entry["legs"]] == ["o1"] and entry["cancel_pending"]


async def test_annulation_et_cloture_retentees(monkeypatch):
    book = paper.PaperBook(None)
    entry = paper.record(paper_plan(), {}, "test")
    entry["created_at"] = datetime(2026, 11, 27, 9, 10, tzinfo=NY).isoformat()  # séance courte
    entry["legs"] = [
        {"name": "TP1", "qty": 1, "target": 102.0, "predicted_minutes": None, "order_id": "a", "filled_at": None,
         "fill_price": None, "exit": None, "exit_price": None, "exit_at": None},
        {"name": "TP2", "qty": 1, "target": 104.0, "predicted_minutes": None, "order_id": "b",
         "filled_at": "2026-11-27T14:40:00Z", "fill_price": 100.0, "exit": None, "exit_price": None, "exit_at": None},
    ]
    book.entries.append(entry)
    fail = {"cancel": True, "close": True}

    async def order(order_id):
        return {"id": order_id, "status": "new" if order_id == "a" else "filled", "legs": []}

    async def cancel(order_id):
        if fail["cancel"]:
            raise SourceError("HTTP 500")

    async def close(symbol, qty=None):
        if fail["close"]:
            raise SourceError("HTTP 500")

    async def snaps(symbols):
        return {"NVDA": {"price": 101.0}}

    monkeypatch.setattr(alpaca, "order", order)
    monkeypatch.setattr(alpaca, "cancel_order", cancel)
    monkeypatch.setattr(alpaca, "close_position", close)
    monkeypatch.setattr(alpaca, "snapshots", snaps)
    s = {"paper_horizon_sessions": 1}
    # 12 h 56 un jour de séance courte : après la limite d'entrée (12 h 55), avant 16 h
    await paper.check(book, datetime(2026, 11, 27, 12, 56, tzinfo=NY), s)
    assert not entry.get("entry_cancelled") and entry["legs"][1]["exit"] is None and entry["status"] == "open"
    fail.update(cancel=False, close=False)
    await paper.check(book, datetime(2026, 11, 27, 12, 58, tzinfo=NY), s)
    assert entry["entry_cancelled"] and entry["legs"][1]["exit"] == "expiré" and entry["status"] == "done"


def test_arrondi_des_prix_alpaca():
    assert alpaca.round_price(12.345) == 12.35 and alpaca.round_price(0.123456) == 0.1235
    assert alpaca.bracket_prices(0.5001, 0.6, 0.45) == (0.5001, 0.6, 0.45)
    with pytest.raises(SourceError):
        alpaca.bracket_prices(10.001, 10.5, 10.004)  # entrée = stop une fois arrondis
    with pytest.raises(SourceError):
        alpaca.bracket_prices(10.0, 10.002, 9.0)


async def test_garde_fou_compte_fictif(monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER", "false")
    with pytest.raises(SourceError):
        alpaca._trading_base()


# --- 8, 12, 13, 14 : API ----------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from app import main
    monkeypatch.setattr(scheduler, "book", S.Book(None))
    return TestClient(main.app)


def test_csrf_refuse_un_autre_site(client):
    r = client.post("/api/trading/proposals/1/approve", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/api/notify/test", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    r = client.delete("/api/trading/trades/x", headers={"Referer": "https://evil.example/page"})
    assert r.status_code == 403


def test_csrf_laisse_passer_le_cockpit_et_curl(client):
    # même origine (le cockpit), Sec-Fetch-Site same-origin, ou aucun en-tête (curl) : la route répond
    assert client.delete("/api/trading/trades/x", headers={"Origin": "http://testserver"}).status_code == 404
    assert client.delete("/api/trading/trades/x", headers={"Sec-Fetch-Site": "same-origin",
                                                           "Origin": "https://jarvis.ts.net"}).status_code == 404
    assert client.delete("/api/trading/trades/x").status_code == 404
    assert client.get("/api/health", headers={"Origin": "https://evil.example"}).status_code == 200


def test_cloture_a_prix_zero(client):
    trade = scheduler.book.add(plan())
    r = client.post(f"/api/trading/trades/{trade['id']}/close?price=0")
    assert r.status_code == 200 and trade["events"][-1]["price"] == 0
    assert S.Book(None).close("absent", None) is None


def test_api_r_net_et_desaccords(client, monkeypatch):
    """La clôture du cockpit note le R net de frais estimés ; l'API des désaccords lit la pente du labo."""
    trade = scheduler.book.add(plan(tps=(102.0, 104.0, 106.0)) | {"score": {"go": False, "decision": "NO GO",
                                                                             "reasons": []}})
    r = client.post(f"/api/trading/trades/{trade['id']}/close?price=99")
    # 11 actions en 3 brackets : trois entrées, puis le tout vendu en un seul ordre (4 $ pour 22 $ de risque).
    assert r.json()["realized_r"] == -0.5 and r.json()["realized_r_net"] == pytest.approx(-0.5 - 4 / 22, abs=1e-3)
    assert trade["realized_r_net"] == r.json()["realized_r_net"]
    assert client.get("/api/trading/trades").json()[0]["realized_r_net"] == trade["realized_r_net"]
    monkeypatch.setattr(S, "note_slopes", lambda: {"stock": -0.02})
    st = client.get("/api/trading/disagreements").json()
    assert st["pente_note"] == {"stock": -0.02} and st["contre"]["closed"] == 1 and st["contre"]["days"] == 1


async def test_routes_prennent_le_verrou(monkeypatch):
    from app import main

    monkeypatch.setattr(scheduler, "book", S.Book(None))

    async def fake_plan(req):
        return plan()

    monkeypatch.setattr(main, "_plan", fake_plan)
    monkeypatch.setattr(scheduler, "enabled", lambda: False)
    async with scheduler._lock:
        import asyncio
        task = asyncio.create_task(main.trading_open(main.TradeRequest(symbol="NVDA", market="stock")))
        await asyncio.sleep(0.01)
        assert not task.done() and scheduler.book.trades == []  # attend la fin du job en cours
    await task
    assert len(scheduler.book.trades) == 1


def test_journaux_sans_cles_d_api():
    from app import main

    main.configure_logging()
    assert logging.getLogger().getEffectiveLevel() <= logging.INFO
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


# --- L5a : le suivi des trades réels (GO confirmé, sortie au prix réel) -----------------------------

async def test_prends_go_reprend_le_go_confirme_du_jour(monkeypatch):
    """`!prends … go` : stop et objectifs du GO confirmé du jour (pas recalculés), trade marqué go_confirme, avec
    JARVIS même si la note recalculée à l'entrée a baissé ; le suivi garde ta règle (stop à l'entrée après TP2)."""
    from app.notify import bot

    book = S.Book(None)
    monkeypatch.setattr(scheduler, "book", book)
    today = datetime.now(timezone.utc).astimezone(NY).date().isoformat()
    monkeypatch.setattr(scheduler, "confirm_state", {"day": today, "closed": False, "gos": {
        "ACME": {"status": "confirmé", "entry": 10.0, "stop": 9.5, "targets": [10.6, 11.0, 11.5], "trigger": 10.21,
                 "composite": 7.4, "last_bar": None, "events": []},
        "BETA": {"status": "annulé", "entry": 5.0, "stop": 4.8, "targets": [5.3, 5.5, 5.8], "composite": 7.1,
                 "last_bar": None, "events": []}}})
    seen = {}

    async def build_plan(symbol, market, side, entry, stop, targets, **kw):
        seen.update(entry=entry, stop=stop, targets=targets)
        p = plan(symbol, entry, stop, tuple(targets))
        return p | {"score": {"go": False, "decision": "NO GO", "reasons": ["R/R 1.2 sous le minimum de 1.5"]}}

    monkeypatch.setattr(bot.trading, "build_plan", build_plan)
    monkeypatch.setattr(bot.trading, "plan_text", lambda p: "PLAN")
    txt = await bot.handle("!prends ACME 10.25 go")
    assert seen == {"entry": 10.25, "stop": 9.5, "targets": [10.6, 11.0, 11.5]}
    tr = book.trades[-1]
    assert tr["policy"] == "go_confirme" and tr["note"] == "via GO confirmé" and not S.against_jarvis(tr)
    assert "pas d'accord" not in txt and "GO confirmé du jour : stop 9.5, objectifs 10.6 · 11 · 11.5" in txt
    assert "La note recalculée à ton entrée est NO GO" in txt
    await bot.handle("!prends ACME go")  # sans prix : l'ordre stop de la confirmation
    assert seen["entry"] == 10.21
    # Sans GO confirmé aujourd'hui : réponse claire, rien n'est suivi.
    n = len(book.trades)
    assert "Pas de GO confirmé aujourd'hui pour BETA (son GO est annulé)" in await bot.handle("!prends BETA 5.1 go")
    assert "n'est pas dans les GO du scanner du jour" in await bot.handle("!prends ZETA 5 go")
    scheduler.confirm_state["day"] = "2026-01-02"
    assert "pas de GO du scanner aujourd'hui" in await bot.handle("!prends ACME 10.25 go")
    assert len(book.trades) == n
    # Le suivi : stop inchangé après TP1, remonté à l'entrée après TP2.
    tr["last_check"] = int(SESSION.timestamp())
    S.apply_bars(tr, minute_bars(SESSION, [(10.65, 10.2)]), SESSION)
    assert tr["targets_hit"] == 1 and tr["stop_current"] == 9.5
    S.apply_bars(tr, minute_bars(SESSION + timedelta(minutes=1), [(11.05, 10.7)]), SESSION)
    assert tr["targets_hit"] == 2 and tr["stop_current"] == 10.25


async def test_sortie_ferme_ou_corrige_au_prix_reel(monkeypatch):
    from app.notify import bot

    book = S.Book(None)
    monkeypatch.setattr(scheduler, "book", book)
    nvda = book.add(plan("NVDA", 132.5, 130.0, (135.0, 137.5, 140.0)))
    txt = await bot.handle("!sortie NVDA 131.2")
    assert nvda["status"] == "closed" and nvda["events"][-1]["price"] == 131.2 and "clôturé à 131.2" in txt
    acme = book.add(plan("ACME", 100.0, 98.0, (102.0, 104.0, 106.0)))
    acme["last_check"] = int(SESSION.timestamp())
    S.apply_bars(acme, minute_bars(SESSION, [(100.2, 97.5)]), SESSION)  # stop noté à 98 par le suivi
    assert S.realized_r(acme) == -1.0
    txt = await bot.handle("!sortie ACME 96.5")
    assert S.realized_r(acme) == -1.75 and acme["events"][-1]["corrige"] and "sortie corrigée à 96.5" in txt
    assert "Aucun trade MSFT suivi" in await bot.handle("!sortie MSFT 400")
    assert "ton prix de sortie réel" in await bot.handle("!sortie ACME")
    assert "!sortie NVDA 131.2" in bot.HELP and "go` = le GO confirmé du jour" in bot.HELP
