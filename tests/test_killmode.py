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
    assert ST.DECLARATION == 5 and len(ST.trials()) == 112
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


def sources(monkeypatch, catalyst=None, price=106.7, sessions=60, iex_prev=100.0):
    bars5 = CO.regular_hours(hist()) + bars_from(OPEN, RANGE + RUN)

    async def snapshots(symbols, feed="iex"):
        return {"ACME": {"price": price, "prev_close": iex_prev}}

    async def bars(symbols, interval, days, now=None, feed="sip"):
        return {"ACME": daily(sessions)}

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
    monkeypatch.setattr(K, "lab_item", lambda: None)
    txt = K.bilan_text(K.calls())
    assert "1 déclenchés" in txt or "1 déclenché" in txt
    assert "ACME" in txt


# --- Alignement sur le labo, journal comparable, témoin catalyseur, règle de lecture ----------------

async def test_direct_aligne_sur_le_labo(paths, monkeypatch):
    before = datetime.fromtimestamp(OPEN - 3600, timezone.utc)
    sources(monkeypatch, iex_prev=97.0)  # clôture IEX de la veille ≠ clôture SIP (100)
    a = await K.analyze("ACME", before)
    assert a["gap_pct"] == pytest.approx(6.7)  # 106,7 / 100 (SIP), pas 106,7 / 97 (IEX)
    sources(monkeypatch, sessions=20)  # introduction récente : 15 séances suffisent, comme au labo
    a = await K.analyze("ACME", NOW)
    assert a["verdict"] == "kill", a["piliers"]
    assert "historique court (20 séances)" in K.text(a)
    sources(monkeypatch, sessions=10)
    a = await K.analyze("ACME", NOW)
    assert a["verdict"] == "non" and "moins de 15 séances" in a["piliers"]["donnees"]["raisons"][0]


async def test_journal_note_la_version(paths, monkeypatch):
    sources(monkeypatch)
    a = await K.analyze("ACME", NOW)
    assert a["vol15_adv"] == pytest.approx(3 * 1000 / 1e6) and a["source"] == "twelvedata"
    call = K.record(a, NOW)
    assert call["groupe"] == "kill" and call["declaration"] == ST.DECLARATION and call["regle"] == K.RULE
    assert call["seuils"]["gap"] == ST.KILL_GAP_PCT and call["seuils"]["jusqu_a"] == "11:00"
    assert call["vol15_adv"] == a["vol15_adv"] and call["source"] == "twelvedata"
    d = K.live_day(CO.regular_hours(hist()) + bars_from(OPEN, RANGE + RUN), daily(), NOW)
    assert K.vol15_adv(d, {"vol15_adv": 1.5}) == 1.5  # celui du labo dès que kill_context le donne


async def test_temoin_catalyseur(paths, monkeypatch):
    sources(monkeypatch, catalyst=cat(news=[]))
    a = await K.analyze("ACME", NOW)
    assert a["verdict"] == "non" and K.group(a) == "temoin_catalyseur"
    call = K.record(a, NOW)
    assert call["groupe"] == "temoin_catalyseur" and "aucune actu positive" in call["catalyseur"][0]
    assert K.record(a, NOW) is None  # une fois par séance et par groupe
    sources(monkeypatch)
    assert K.record(await K.analyze("ACME", NOW), NOW)["groupe"] == "kill"
    sources(monkeypatch, catalyst=cat(news=[]), price=108.5)  # graphique raté (déjà parti) : ni l'un ni l'autre
    assert K.group(await K.analyze("ACME", NOW)) is None
    assert K.group({"symbol": "X", "verdict": "erreur", "erreur": "boum"}) is None
    after = bars_from(OPEN, RANGE + RUN + [(114.7, 115.0, 114.5, 114.8)] * 60)

    async def bars_range(symbols, interval, start, end, **kw):
        return {"ACME": after}

    monkeypatch.setattr(K.alpaca, "bars_range", bars_range)
    monkeypatch.setattr(K, "lab_item", lambda: None)
    assert await K.settle_due(datetime.fromtimestamp(call["close_ts"] + 3600, timezone.utc)) == 2
    txt = K.bilan_text(K.calls())
    assert "🔪 KILL : 1 déclenché" in txt and "Témoin catalyseur (tout réuni sauf le catalyseur) : 1 déclenché" in txt
    assert "ACME (témoin)" in txt and "rien n'est jugé avant 30 KILL" in txt


async def test_scan_note_les_temoins_ecartes(paths, monkeypatch):
    sources(monkeypatch, catalyst=cat(news=[]))
    a = await K.analyze("ACME", NOW)

    async def scan(now=None):
        return {"at": NOW.isoformat(), "examined": 1, "movers": 1, "analysed": 1, "setups": [], "dropped": [a]}

    monkeypatch.setattr(K, "scan", scan)
    K.set_active(True, NOW)
    txt = await K.command(["scan"], NOW)
    assert "ACME (catalyseur" in txt
    assert [c["groupe"] for c in K.calls()] == ["temoin_catalyseur"]


def book(rs, groupe="kill"):
    return [{"symbol": f"S{i}", "day": f"2026-09-{1 + i % 25:02d}", "groupe": groupe, "entry": 10.0, "stop": 9.0,
             "resultat": {"declenche": True, "r_ibkr": r, "tp": [r > 0, False, False], "sortie": "x"}}
            for i, r in enumerate(rs)]


def test_regle_de_lecture():
    few = K.reading(K.stats(book([1.0] * 29)), None)
    assert "rien n'est jugé avant 30" in few[0]
    bad = K.reading(K.stats(book([-1.0, -0.8] * 15)), None)
    assert "JARVIS recommande d'éteindre Killmode ; à toi de décider" in bad[0]
    good = K.reading(K.stats(book([1.2, 0.8] * 15)), None)
    assert "on continue" in good[0] and "positive" in good[0]
    unsure = K.reading(K.stats(book([1.0, -1.0] * 15)), None)
    assert "jusqu'à 100 KILL" in unsure[0]
    done = K.reading(K.stats(book([1.0, -1.0] * 50)), None)
    assert "ne voit pas d'avantage" in done[0]
    gap = K.reading(K.stats(book([-1.0, -0.8] * 15)), {"ic95_ibkr": [0.05, 0.3]})
    assert "Écart labo/direct" in gap[1]
    assert K.stats(book([1.0] * 3, "temoin_catalyseur"))["declenches"] == 0  # les groupes ne se mélangent pas


def test_bilan_par_regle(paths, monkeypatch):
    monkeypatch.setattr(K, "lab_item", lambda: None)
    old = [c | {"regle": 5, "seuils": K.seuils() | {"gap": 3.0}} for c in book([1.0])]
    txt = K.bilan_text(old + book([-1.0]))
    assert "règle n° 5 (gap ≥ +3 %" in txt and "(en vigueur) · KILL : 1 déclenché" in txt


def test_ligne_du_labo(monkeypatch):
    assert K.lab_line(None) is None
    assert "pas encore de trade rejoué" in K.lab_line({})
    it = {"declenches": 40, "jours": 30, "esperance_ibkr_r": 0.2, "ic95_ibkr": [0.05, 0.35],
          "temoin": {"ecart_r": 0.3}, "candidate": True}
    line = K.lab_line(it)
    assert "règle de prix seule, sans catalyseur" in line and "candidate au labo (pas une promotion)" in line

    class Conn:
        def close(self):
            pass

    from app.lab import db as lab_db
    monkeypatch.setattr(lab_db, "open_readonly", lambda: Conn())
    rep = {"strategies": {"stock": {"killmode": it | {"jours_suivants": it | {"declenches": 12}}}}}
    monkeypatch.setattr(K.SS, "cached", lambda conn: rep)
    got = K.lab_item()
    assert got["declenches"] == 12 and "jours non vus au choix de la règle" in K.lab_line(got)


async def test_affichage_honnete(paths, monkeypatch):
    sources(monkeypatch)
    a = await K.analyze("ACME", NOW)
    a["jarvis"] = {"checklist": {"verdict": "GO", "score": 5, "max_score": 7},
                   "score": {"composite": 6.5, "decision": "GO"}, "resistances": []}
    txt = K.text(a, "🧪 Labo, ligne")
    assert "note v1 6,5/10 GO, non prédictive au labo" in txt and "🧪 Labo, ligne" in txt
    assert "Frais estimés ≈" in txt and "minimum IBKR par ordre n'est pas compté" in txt
    a["verdict"] = "non"
    assert "🧪 Labo, ligne" not in K.text(a, "🧪 Labo, ligne")


async def test_scan_prefiltre_comme_le_labo(paths, monkeypatch):
    today = CO.ny_midnight(DAY)

    def day_bar(o):
        return {"t": today, "o": o, "h": o, "l": o, "c": o, "v": 1e5}

    data = {
        "GAPO": (daily() + [day_bar(106.0)], {"price": 103.0, "prev_close": 100.0}),  # ouverte +6 %, à +3 %
        "IEXO": (daily(), {"price": 103.0, "prev_close": 100.0, "open": 107.0, "open_day": DAY.isoformat()}),
        "STALE": (daily(), {"price": 102.0, "prev_close": 100.0, "open": 107.0, "open_day": "2026-10-05"}),
        "IPO": (daily(20), {"price": 106.0, "prev_close": 100.0}),
        "TRAP": (daily(), {"price": 103.0, "prev_close": 95.0}),  # +8,4 % sur la clôture IEX, +3 % sur la SIP
        "CALM": (daily(half_range=1.0), {"price": 110.0, "prev_close": 100.0}),
        "SHORT": (daily(10), {"price": 110.0, "prev_close": 100.0}),
    }

    async def universe(s):
        return list(data)

    async def snapshots(symbols, feed="iex"):
        return {s: data[s][1] for s in symbols}

    async def bars(symbols, interval, days, now=None, feed="sip"):
        return {s: data[s][0] for s in symbols}

    seen = []

    async def analyze(sym, now=None, source=None):
        seen.append(sym)
        return {"symbol": sym, "verdict": "erreur", "erreur": "test"}

    monkeypatch.setattr(K.alpaca, "configured", lambda: True)
    monkeypatch.setattr(K.SC, "universe", universe)
    monkeypatch.setattr(K.alpaca, "snapshots", snapshots)
    monkeypatch.setattr(K.alpaca, "bars", bars)
    monkeypatch.setattr(K, "analyze", analyze)
    r = await K.scan(NOW)
    assert r["movers"] == 3 and seen == ["IEXO", "GAPO", "IPO"]
    pre = datetime.fromtimestamp(OPEN - 600, timezone.utc)  # avant 9 h 30 : pas de gap d'ouverture
    assert K.open_gap(data["GAPO"][0], data["IEXO"][1], 100.0, pre) is None


async def test_snapshot_donne_l_ouverture(monkeypatch):
    from app.sources import alpaca

    async def request(method, url, params=None, body=None, transport=None):
        return {"ACME": {"latestTrade": {"p": 10.5, "t": "2026-10-06T14:00:00Z"},
                         "dailyBar": {"o": 10.2, "c": 10.4, "v": 5, "t": "2026-10-06T04:00:00Z"},
                         "prevDailyBar": {"c": 9.8, "v": 7}}}

    monkeypatch.setattr(alpaca, "_request", request)
    snap = (await alpaca.snapshots(["ACME"]))["ACME"]
    assert snap["open"] == 10.2 and snap["open_day"] == "2026-10-06"
