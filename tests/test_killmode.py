"""JARVIS Killmode : règle de prix du labo (déclaration n° 5), piliers, interrupteur, journal et bilan."""
from datetime import datetime, timedelta, timezone

import pytest

from app.lab import collector as CO
from app.lab import strategies as ST
from app.notify import bot
from app.trading import killmode as K
from app.trading import service as T
from tests.test_lab_strategies import DAY, OPEN, bars_from, drift

NOW = datetime.fromtimestamp(OPEN + 30 * 60, timezone.utc)  # 10 h New York


def daily(n=60, close=100.0, half_range=3.0):
    """Journalier connu la veille : ATR 6 % (plus haut - plus bas = 6 sur 100)."""
    return [{"t": CO.ny_midnight(DAY) - (n - i) * 86400, "o": close, "h": close + half_range,
             "l": close - half_range, "c": close, "v": 1e6} for i in range(n)]


def hist():
    r1, p = drift(78, 99.0, 0.01)
    r2, _ = drift(78, p, 0.01)
    return (bars_from(CO.session_ts(CO.sessions_back(DAY, 2))[0], r1)
            + bars_from(CO.session_ts(CO.sessions_back(DAY, 1))[0], r2))


RANGE = [(105.0, 106.0, 104.5, 105.8), (105.8, 106.5, 105.5, 106.2), (106.2, 106.4, 105.9, 106.3)]
RUN = [(106.3, 106.6, 106.2, 106.5), (106.5, 106.8, 106.4, 106.7)] + [
    (106.7 + i, 107.8 + i, 106.6 + i, 107.7 + i) for i in range(8)]


def day(rows=RANGE + RUN, d=None):
    return ST.Day("stock", DAY, CO.regular_hours(hist())[-ST.STOCK_WARMUP_BARS:] + bars_from(OPEN, rows), d or daily())


def test_regle_killmode_signal():
    cand, reasons = ST.killmode_check(day())
    assert reasons == []
    assert cand["entry"] == pytest.approx(106.51) and cand["stop"] == pytest.approx(104.49)
    assert cand["decided_at"] == OPEN + 15 * 60
    assert cand["entry_until"] == int(datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc).timestamp())  # 11 h NY
    ctx = ST.kill_context(day())
    assert ctx["gap_pct"] == pytest.approx(5.0) and ctx["atr_pct"] == pytest.approx(6.0, abs=0.01)


def test_regle_killmode_refus():
    small = [(r[0] - 3, r[1] - 3, r[2] - 3, r[3] - 3) for r in RANGE]  # gap +2 %
    cand, reasons = ST.killmode_check(day(small))
    assert cand is None and reasons[0].startswith("gap +2,0 %")
    calm = daily(half_range=1.0)  # ATR 2 %
    assert ST.killmode_check(day(d=calm))[1][0].startswith("ATR 2,0 %")
    filled = RANGE[:2] + [(106.2, 106.4, 99.5, 106.3)]  # le range repasse sous la clôture de la veille
    assert "gap comblé" in ST.killmode_check(day(filled))[1][0]
    weak = RANGE[:2] + [(106.2, 106.3, 104.6, 104.7)]  # clôture du range sous le VWAP
    assert "VWAP" in ST.killmode_check(day(weak))[1][0]
    assert ST.killmode_check(day(RANGE[:2]))[1] == ["range des 15 premières minutes pas encore complet"]


def test_killmode_au_labo():
    assert "killmode" in ST.STRATEGIES and ST.STRATEGIES["killmode"]["markets"] == ("stock",)
    assert ST.DECLARATION >= 5 and len(ST.trials()) == 139  # n° 5 + sorties de Gemini du n° 6
    rows = {r["strategy"]: r for r in ST.strategy_rows(day(), "AAA", 3, 5.0)}
    k = rows["killmode"]
    assert k["triggered"] == 1 and k["signal_at"] == OPEN + 15 * 60 and k["r_net"] > 0
    assert not any(r["strategy"] == "killmode" for r in ST.strategy_rows(ST.Day("crypto", DAY, day().bars, daily()), "BTC"))


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(K, "STATE_PATH", str(tmp_path / "killmode.json"))
    monkeypatch.setattr(K, "CALLS_PATH", str(tmp_path / "calls.json"))
    return tmp_path


async def test_interrupteur_desactive_par_defaut(paths):
    assert not K.active()
    assert "éteint" in await bot.handle("!kill NVDA")
    assert "éteint" in await bot.handle("!kill scan")
    assert "allumé" in await bot.handle("!kill on") and K.active()
    assert "allumé" in await bot.handle("!kill etat")
    assert "éteint" in await bot.handle("!kill off") and not K.active()
    assert "actions US" in await bot.handle("!kill BTC")
    assert "Killmode" in await bot.handle("!kill")
    assert "!kill" in bot.HELP


def news(tone="positive", hours=2, title="Acme wins $2B defense contract"):
    return {"title": title, "url": "https://x", "published_at": (NOW - timedelta(hours=hours)).isoformat(), "tone": tone}


def cat(**kw):
    base = {"available": True, "earnings_known": True, "name": "Acme Corp", "market_cap": 2e9, "company": True,
            "next_earnings": None, "hours_to_earnings": None, "last_earnings": None, "news": [news()],
            "dilution": False, "errors": []}
    return base | kw


def test_pilier_catalyseur():
    assert K.catalyst_pillar(cat(), NOW)[0]
    assert not K.catalyst_pillar(cat(news=[news(hours=72)]), NOW)[0]  # trop vieille
    assert not K.catalyst_pillar(cat(dilution=True), NOW)[0]
    assert "pari binaire" in K.catalyst_pillar(cat(hours_to_earnings=10), NOW)[1][0]
    assert not K.catalyst_pillar(cat(news=[news(), news("negative"), news("negative")]), NOW)[0]
    beat = {"date": "2026-10-06", "days_ago": 0, "eps_surprise_pct": 12.0}
    ok, why = K.catalyst_pillar(cat(news=[], last_earnings=beat), NOW)
    assert ok and "au-dessus des attentes" in why[0]
    assert not K.catalyst_pillar(cat(available=False), NOW)[0]


def sources(monkeypatch, catalyst=None, price=106.7):
    bars5 = CO.regular_hours(hist()) + bars_from(OPEN, RANGE + RUN)

    async def snapshots(symbols, feed="iex"):
        return {"ACME": {"price": price, "prev_close": 100.0}}

    async def bars(symbols, interval, days, now=None, feed="sip"):
        return {"ACME": daily()}

    async def candles(symbol, market, interval, source=None, max_age=None, outputsize=None):
        return bars5

    async def check(symbol, now=None, days=7):
        return catalyst or cat()

    async def profile(symbol):
        return {"symbol": symbol, "name": "Acme Corp", "industry": "Aerospace & Defense", "market_cap": 2e9}

    async def build_plan(*a, **k):
        raise T.PlanError("pas de crédit")

    monkeypatch.setattr(K.alpaca, "configured", lambda: True)
    monkeypatch.setattr(K.alpaca, "snapshots", snapshots)
    monkeypatch.setattr(K.alpaca, "bars", bars)
    monkeypatch.setattr(K.T, "candles", candles)
    monkeypatch.setattr(K.C, "check", check)
    monkeypatch.setattr(K.finnhub, "profile", profile)
    monkeypatch.setattr(K.T, "build_plan", build_plan)


async def test_analyse_setup_complet(paths, monkeypatch):
    sources(monkeypatch)
    a = await K.analyze("acme", NOW)
    assert a["verdict"] == "kill", a["piliers"]
    plan = a["graphique"]["plan"]
    assert plan["entry"] == pytest.approx(106.51) and plan["targets"][0] == pytest.approx(108.53)
    assert plan["declenche_a"] == OPEN + 15 * 60
    txt = K.text(a)
    assert "🔪 **KILL" in txt and "Ordre stop d'achat 106,51" in txt and "stop 104,49" in txt
    assert "Aerospace" in txt and "Acme wins" in txt and "!prends ACME 106.51 stop 104.49" in txt
    assert K.record(a, NOW) and K.record(a, NOW) is None  # une fois par séance


async def test_analyse_refus_et_attente(paths, monkeypatch):
    sources(monkeypatch, price=108.5)  # plus de 0,5 R au-dessus de l'entrée
    a = await K.analyze("ACME", NOW)
    assert a["verdict"] == "non" and a["graphique"]["status"] == "parti"
    assert K.record(a, NOW) is None
    sources(monkeypatch, catalyst=cat(news=[]))
    a = await K.analyze("ACME", NOW)
    assert a["verdict"] == "non" and not a["piliers"]["catalyseur"]["ok"]
    assert "❌ Catalyseur" in K.text(a)
    sources(monkeypatch)
    before = datetime.fromtimestamp(OPEN - 3600, timezone.utc)  # pré-ouverture : gap de pré-ouverture à +6,7 %
    a = await K.analyze("ACME", before)
    assert a["verdict"] == "pret" and "repasse `!kill ACME`" in K.text(a)


async def test_bilan(paths, monkeypatch):
    sources(monkeypatch)
    a = await K.analyze("ACME", NOW)
    call = K.record(a, NOW)
    after = bars_from(OPEN, RANGE + RUN + [(114.7, 115.0, 114.5, 114.8)] * 60)
    res = K.settle(call, after)
    assert res["declenche"] and res["tp"] == [True, True, True] and res["r_ibkr"] > 1  # entrée au-dessus du stop d'achat : le prix était déjà à 107,7

    async def bars_range(symbols, interval, start, end, **kw):
        return {"ACME": after}

    monkeypatch.setattr(K.alpaca, "bars_range", bars_range)
    assert await K.settle_due(NOW) == 0  # séance pas finie
    later = datetime.fromtimestamp(call["close_ts"], timezone.utc) + timedelta(hours=1)
    assert await K.settle_due(later) == 1
    monkeypatch.setattr(K, "lab_line", lambda: None)
    txt = K.bilan_text(K.calls())
    assert "1 déclenchés" in txt or "1 déclenché" in txt
    assert "ACME" in txt
