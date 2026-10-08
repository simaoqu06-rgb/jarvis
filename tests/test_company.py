"""Tests hors ligne de la fiche société (`!societe`, app/trading/company.py)."""
from datetime import date, datetime, time, timedelta, timezone

from app.notify import bot
from app.sources import finnhub
from app.trading import catalyst as C
from app.trading import company as CO
from app.trading import indicators as I
from app.trading import service as S

NOW = datetime(2026, 10, 8, 0, 30, tzinfo=timezone.utc)


def daily_bars(n=300, jumps=None):
    """Séances de 100 $ qui montent doucement, avec quelques fortes séances {indice: variation}."""
    jumps = jumps or {}
    start = date(2025, 7, 1)
    out, c, d = [], 100.0, start
    while len(out) < n:
        if d.weekday() < 5:
            prev = c
            c = prev * (1 + jumps.get(len(out), 0.002))
            t = int(datetime.combine(d, time(0), I.NEW_YORK).timestamp())
            out.append({"t": t, "o": prev, "h": max(prev, c) * 1.01, "l": min(prev, c) * 0.99, "c": c, "v": 1e6})
        d += timedelta(days=1)
    return out


def test_plus_fortes_seances_avec_leur_fenetre_d_actus():
    daily = daily_bars(jumps={250: 0.30, 260: -0.12, 270: 0.08})
    moves = CO.big_moves(daily, 2)
    assert [m["pct"] for m in moves] == [30.0, -12.0]
    m = moves[0]
    assert m["day"] == CO._ny_day(daily[250]["t"]).isoformat()
    assert m["to_ts"] - m["from_ts"] >= 24 * 3600  # de la clôture de la veille à celle du jour


def test_titre_qui_explique_le_mouvement():
    move = {"pct": 30.0, "from_ts": 1000, "to_ts": 5000}
    items = [{"title": "Marvell stock falls in early trade", "url": "u1", "published_ts": 1500},
             {"title": "Marvell shares soar after raising guidance", "url": "u2", "published_ts": 2000},
             {"title": "Marvell shares soar again", "url": "u3", "published_ts": 9000},  # après la clôture du jour
             {"title": "Nvidia rallies", "url": "u4", "published_ts": 1200}]          # pas sur la société
    assert CO.move_headline(items, "MRVL", "Marvell Technology Inc", move)["url"] == "u2"
    assert CO.move_headline(items[:1], "MRVL", "Marvell Technology Inc", move)["url"] == "u1"  # à défaut, la première
    assert CO.move_headline([], "MRVL", None, move) is None


def fake_finnhub(monkeypatch, calls):
    async def profile(sym):
        calls.append("profile")
        return {"symbol": sym, "name": "Marvell Technology Inc", "industry": "Semiconductors", "market_cap": 249e9,
                "shares": 874e6, "exchange": "NASDAQ", "ipo": None, "url": None}

    async def metric(sym):
        return {"beta": 2.26, "revenueGrowthTTMYoy": 37.2, "grossMarginTTM": 51.2, "netProfitMarginTTM": -3.0,
                "peExclExtraTTM": -40.0, "psTTM": 25.9}

    async def recs(sym):
        return [{"period": "2026-10-01", "strong_buy": 12, "buy": 25, "hold": 6, "sell": 1, "strong_sell": 0}]

    async def insiders(sym, a, b):
        return [{"name": "X", "change": -12000.0, "date": "2026-09-12", "code": "S", "price": 250.0},
                {"name": "Y", "change": 5000.0, "date": "2026-09-02", "code": "M", "price": 0.0}]

    async def filings(sym, a, b):
        return [{"form": "8-K", "filed": "2026-10-06", "url": "https://sec.gov/a"},
                {"form": "424B5", "filed": "2026-10-01", "url": "https://sec.gov/b"}]

    async def surprises(sym):
        return [{"period": "2026-07-31", "actual": 0.74, "estimate": 0.73, "surprise_pct": 1.4},
                {"period": "2026-04-30", "actual": 0.60, "estimate": 0.61, "surprise_pct": -1.6}]

    async def calendar(sym, a, b):
        return [{"date": "2026-10-15", "hour": "amc", "eps_estimate": 0.8, "eps_actual": None}]

    async def news_between(sym, a, b):
        t = int(datetime.combine(b, time(14), timezone.utc).timestamp())
        return [{"title": "Marvell shares jump after strong outlook", "url": "https://x/1", "published_ts": t}]

    async def catalyst(sym, now):
        return {"available": True, "positive": 2, "negative": 1, "dilution": True,
                "news": [{"title": "Marvell prices $2B stock offering", "url": "https://x/2", "tone": "dilution"}]}

    async def candles(*a, **k):
        return daily_bars(jumps={280: 0.25})

    for name, fn in (("profile", profile), ("basic_financials", metric), ("recommendations", recs),
                     ("insider_transactions", insiders), ("filings", filings), ("earnings_surprises", surprises),
                     ("earnings", calendar), ("company_news_between", news_between)):
        monkeypatch.setattr(finnhub, name, fn)
    monkeypatch.setattr(C, "safe_check", catalyst)
    monkeypatch.setattr(S, "_optional_candles", candles)
    monkeypatch.setattr(CO, "_cache", {})


async def test_fiche_societe_complete(monkeypatch):
    calls = []
    fake_finnhub(monkeypatch, calls)
    s = await CO.sheet("mrvl", NOW)
    text = CO.sheet_text(s)
    assert text.startswith("🏢 **Fiche Marvell Technology Inc** (MRVL) · Semiconductors · capitalisation 249 Md$")
    assert "Prochains résultats le 15 oct. après la clôture (dans 7 j)" in text
    assert "battu 1 fois sur 2 (+1.4 %, -1.6 %" in text
    assert "+25.0 % le" in text and "[Marvell shares jump after strong outlook](<https://x/1>)" in text
    assert "pertes sur un an" in text and "37 à l'achat (dont 12 achat fort), 6 neutres, 1 à la vente" in text
    assert "1 vente en bourse (-12 000 actions), aucun achat" in text
    assert "[424B5](<https://sec.gov/b>) le 1er oct." in text and "8-K" not in text
    assert "⚠️ augmentation de capital dans les actus" in text
    assert "résultats dans moins de 15 jours" in text and "`!avis` ne propose pas de long" in text
    # Resservie depuis le cache pendant 30 min.
    await CO.sheet("MRVL", NOW)
    assert calls == ["profile"]


async def test_fiche_societe_partielle_et_commande(monkeypatch):
    calls = []
    fake_finnhub(monkeypatch, calls)

    async def down(*a, **k):
        raise CO.SourceError("HTTP 403")

    monkeypatch.setattr(finnhub, "basic_financials", down)
    monkeypatch.setattr(finnhub, "filings", down)
    text = await bot.handle("!societe mrvl")
    assert "Indisponible : chiffres clés : HTTP 403 ; dépôts SEC : HTTP 403" in text and "💵" not in text
    assert "ne concerne que les actions" in await bot.handle("!societe btc")
