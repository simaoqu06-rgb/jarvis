"""JARVIS Killmode : règle de prix du labo (déclaration n° 5), piliers, interrupteur, journal et bilan."""
import gzip
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

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


def minutes(rows, start=OPEN):
    """Bougies d'1 min (o, h, l, c) à partir de `start`."""
    return [{"t": start + 60 * i, "o": o, "h": h, "l": lo, "c": c, "v": 200.0} for i, (o, h, lo, c) in enumerate(rows)]


def spread(rows, start=OPEN):
    """Chaque bougie de 5 min en 5 minutes : la première porte ses extrêmes, les 4 suivantes restent à sa clôture
    (regroupées, elles redonnent la bougie de 5 min et son volume)."""
    out = []
    for o, h, lo, c in rows:
        out += [(o, h, lo, c)] + [(c, c, c, c)] * 4
    return minutes(out, start)


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
    assert ST.DECLARATION >= 5 and len(ST.trials()) == 162  # n° 5, sorties de Gemini (n° 6), GO (n° 7), n° 8, 9, 11, 12
    rows = {r["strategy"]: r for r in ST.strategy_rows(day(), "AAA", 3, 5.0)}
    k = rows["killmode"]
    assert k["triggered"] == 1 and k["signal_at"] == OPEN + 15 * 60 and k["r_net"] > 0
    assert not any(r["strategy"] == "killmode" for r in ST.strategy_rows(ST.Day("crypto", DAY, day().bars, daily()), "BTC"))


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(K, "STATE_PATH", str(tmp_path / "killmode.json"))
    monkeypatch.setattr(K, "CALLS_PATH", str(tmp_path / "calls.json"))
    monkeypatch.setattr(K, "SCANS_PATH", str(tmp_path / "scans.json"))
    monkeypatch.setattr(K.T, "TRADES_PATH", str(tmp_path / "trades.json"))
    monkeypatch.setattr(K, "BARS_DIR", str(tmp_path / "bars"))
    monkeypatch.setattr(K, "TICKS_DIR", str(tmp_path / "ticks"))

    async def eurusd():
        return 1.15

    monkeypatch.setattr(K, "eurusd", eurusd)
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
    tick_feed(monkeypatch)


def tick_feed(monkeypatch, order="olhc", fail=()):
    """Transactions d'une minute tirées de sa bougie gardée (ouverture, plus bas, plus haut, clôture, 100 actions
    chacune, sans cotation) ; `fail` : minutes qu'Alpaca refuse."""
    asked = []

    async def minute_ticks(call, m):
        asked.append(m)
        if m in fail:
            raise K.SourceError("HTTP 500 panne")
        with gzip.open(K._bars_file(call), "rt", encoding="utf-8") as f:
            b = next(x for x in json.load(f) if x["t"] == m)
        return {"t": [[m * 10**9 + i, b[k], 100.0, "@", "C", ""] for i, k in enumerate(order)], "q": [], "q0": None}

    monkeypatch.setattr(K, "minute_ticks", minute_ticks)
    return asked


async def test_analyse_setup_complet(paths, monkeypatch):
    sources(monkeypatch)
    a = await K.analyze("acme", NOW)
    assert a["verdict"] == "kill", a["piliers"]
    plan = a["graphique"]["plan"]
    assert plan["entry"] == pytest.approx(106.51) and plan["targets"][0] == pytest.approx(108.53)
    assert plan["declenche_a"] == OPEN + 15 * 60
    txt = K.text(a)
    assert "🔪 **KILL" in txt and "Ordre stop d'achat 106,51" in txt and "stop 104,49" in txt
    assert "Aerospace" in txt and "Acme wins" in txt
    assert "!prends ACME <ton prix d'exécution> stop 104.49 kill tp 108.53 110.55 112.57" in txt
    assert "**11 actions en 3 brackets**" in txt and "**4 actions → TP1 108,53 · 4 actions → TP2 110,55 · " \
        "3 actions → TP3 112,57**" in txt and "stop ajustable IBKR (« Adjustable Stop ») : déclencheur 108,53, " \
        "nouveau stop 106,51" in txt
    assert ("stop d'achat 106,51 en GTD jusqu'à 11:00:00 heure de New York, SL 104,49 et TP liés en ordres du "
            "jour") in txt
    assert "Vers 15 h 59 New York (aujourd'hui 21:59 à Paris), annule les TP et SL restants puis vends le reste" in txt
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
    after = spread(RANGE + RUN + [(114.7, 115.0, 114.5, 114.8)] * 60)
    res = K.settle(call, after)
    # Appel à 10 h, ordre actif à 10 h 01 : le prix est déjà à 108,7, rempli à l'ouverture de la minute ; TP1
    # (108,53), déjà dépassé quand le bracket est posé, part au prix d'entrée.
    assert res["activation"] == call["at"] + 60 and res["remplissage"] == 108.7 and res["mode_entree"] == "ouverture"
    assert res["declenche"] and res["tp"] == [True, True, True] and res["sorties"][0]["prix"] == 108.7
    assert res["r_ibkr"] > 1 and res["methode"] == K.SETTLE_METHOD and res["pas"] == 60  # le labo : 5 min dès 10 h
    asked = []

    async def bars_range(symbols, interval, start, end, **kw):
        asked.append((interval, start, end))
        return {"ACME": after}

    monkeypatch.setattr(K.alpaca, "bars_range", bars_range)
    assert await K.settle_due(NOW) == 0  # séance pas finie
    later = datetime.fromtimestamp(call["close_ts"], timezone.utc) + timedelta(hours=1)
    assert await K.settle_due(later) == 1
    assert asked == [(1, datetime.fromtimestamp(OPEN, timezone.utc),
                      datetime.fromtimestamp(call["close_ts"], timezone.utc))]  # une requête : toute la séance en 1 min
    assert K.calls()[0]["resultat"]["sip"]["meme_ordre"]  # la règle refaite sur les 5 min regroupées
    monkeypatch.setattr(K, "lab_item", lambda: None)
    txt = K.bilan_text(K.calls())
    assert "1 déclenchés" in txt or "1 déclenché" in txt
    assert "ACME" in txt and "Prix mesurés sur les transactions" in txt


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
    assert call["codes"] == ["pas_de_positive"] and K.clean(call)
    assert K.record(a, NOW) is None  # une fois par séance et par groupe
    other = K.record(a | {"symbol": "BETA"}, NOW)  # un autre témoin, sans KILL ce jour-là
    sources(monkeypatch, catalyst=cat(news=[]), price=108.5)  # graphique raté (déjà parti) : ni l'un ni l'autre
    assert K.group(await K.analyze("ACME", NOW)) is None
    assert K.group({"symbol": "X", "verdict": "erreur", "erreur": "boum"}) is None
    after = bars_from(OPEN, RANGE + RUN + [(114.7, 115.0, 114.5, 114.8)] * 60)

    async def bars_range(symbols, interval, start, end, **kw):
        return {s: after for s in symbols}

    monkeypatch.setattr(K.alpaca, "bars_range", bars_range)
    monkeypatch.setattr(K, "lab_item", lambda: None)
    assert await K.settle_due(datetime.fromtimestamp(call["close_ts"] + 3600, timezone.utc)) == 2
    txt = K.bilan_text(K.calls())
    assert "Témoin propre (ni actu positive ni veto) : 2 soldés, 2 déclenchés (100 %)" in txt
    assert "ACME (témoin propre)" in txt and "BETA (témoin propre)" in txt and "rien n'est lu avant 30 KILL" in txt
    # L'actu arrive : ACME devient KILL dans la même séance. Le témoin reste un témoin (groupe figé à l'écriture,
    # jamais retiré sur une info postérieure) ; la comparaison prend le premier appel de chaque action et séance.
    sources(monkeypatch)
    kill = K.record(await K.analyze("ACME", NOW), NOW)
    assert kill["groupe"] == "kill" and other["symbol"] == "BETA"
    assert K.stats(K.calls(), "temoin_catalyseur")["appels"] == 2
    assert [c["groupe"] for c in K.first_calls(K.calls())] == ["temoin_catalyseur", "temoin_catalyseur"]
    assert K.record(a, NOW) is None  # ni témoin après un KILL de la même séance
    txt = K.bilan_text(K.calls())
    assert "1 KILL proposé, 2 témoins catalyseur dont 2 propres" in txt and "ACME (témoin propre)" in txt


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
    """Appels de la version en vigueur, soldés : r = résultat brut (une seule sortie, prix = entrée + r)."""
    return [{"symbol": f"S{i}", "day": f"2026-09-{1 + i % 25:02d}", "at": i, "groupe": groupe, "entry": 10.0,
             "stop": 9.0, "targets": [11.0, 12.0, 13.0], "regle": K.RULE, "seuils": K.seuils(),
             "codes": [] if groupe == "kill" else ["pas_de_positive"],
             "resultat": {"declenche": True, "r_ibkr": r, "tp": [r > 0, False, False], "sortie": "x",
                          "remplissage": 10.0, "sorties": [{"prix": 10.0 + r, "part": 1.0,
                                                            "raison": "tp3" if r > 0 else "stop"}],
                          "eurusd": 1.15, "plan_ibkr": "fixe"}}
            for i, r in enumerate(rs)]


@pytest.fixture
def gross(monkeypatch):
    """La règle de lecture jugée sur le R brut du helper `book` (les frais réels ont leur propre test)."""
    monkeypatch.setitem(K.KEYS, "r_20e", lambda c, res: res["r_ibkr"])


def test_regle_de_lecture(gross):
    few = K.reading(book([1.0] * 29), None)
    assert "rien n'est lu avant 30" in few[0] and "indicatif" in few[0]
    bad = K.reading(book([-1.0, -0.8] * 15), None)
    assert "JARVIS recommande d'éteindre Killmode ; à toi de décider" in bad[0] and "30 premiers KILL" in bad[0]
    assert "Lecture finale à 100 KILL" in bad[0]
    # Au 30e, jamais de verdict positif : un audit.
    good = K.reading(book([1.2, 0.8] * 15), None)
    assert "pas d'alerte" in good[0] and "audit, pas une validation" in good[0] and "positive" not in good[0]
    unsure = K.reading(book([1.0, -1.0] * 15), None)
    assert "pas d'alerte" in unsure[0]
    # Deux regards fixes : des pertes après le 30e KILL ne changent pas la lecture du 30e avant le 100e.
    later = K.reading(book([1.0, -1.0] * 15 + [-1.0, 1.0, -1.0] * 10), None)
    assert "pas d'alerte" in later[0] and "(60 pour l'instant)" in later[0]
    done = K.reading(book([1.0, -1.0, 0.2] * 33 + [1.0]), None)
    assert "JARVIS ne voit pas d'avantage démontré" in done[0] and "100 premiers KILL" in done[0]
    assert "seul un écart d'au moins ≈" in done[0]
    win = K.reading(book([1.2, 0.8, 1.1] * 33 + [1.0]), None)  # 25 jours de septembre : 4 à 5 semaines
    assert "pas une validation : sur" in win[0] and "seulement (il en faut au moins 8)" in win[0]
    ten_weeks = [c | {"day": (datetime(2026, 7, 1) + timedelta(days=i % 70)).date().isoformat()}
                 for i, c in enumerate(book([1.2, 0.8, 1.1] * 33 + [1.0]))]
    win = K.reading(ten_weeks, None)
    assert "avantage démontré" in win[0] and "semaine par semaine" in win[0]
    one_week = [c | {"day": f"2026-09-0{1 + i % 4}"} for i, c in enumerate(book([1.2, 0.8, 1.1] * 33 + [1.0]))]
    assert "pas une validation : concentré sur 1 semaine" in K.reading(one_week, None)[0]
    # L'alerte du 30e écartée : la lecture finale reste faite au 100e, et le pari se mesure sur les KILL 31 à 100.
    bet = K.reading(book([-1.0, -0.8] * 15 + [1.0, -0.5] * 35), None)
    assert "Lecture finale" in bet[0] and "Le pari contre l'avis de JARVIS (KILL 31 à 100" in bet[1]
    lab = {"esperance_ibkr_r": 0.15, "ic95_ibkr": [0.05, 0.25]}
    far = K.reading(book([-1.0, -0.8] * 15), lab)
    assert "Écart labo/direct" in far[-1]
    noisy = K.reading(book([1.0, -1.2] * 15), {"esperance_ibkr_r": -0.02, "ic95_ibkr": [-0.06, 0.02]})
    assert len(noisy) == 1  # −0,10 R contre le labo : dans le bruit, pas d'alerte
    assert K.stats(book([1.0] * 3, "temoin_catalyseur"))["declenches"] == 0  # les groupes ne se mélangent pas


def test_borne_de_degat(gross):
    assert not any("Borne de dégât" in x for x in K.reading(book([-1.0, 0.5] * 5), None))  # −0,25 R : rien
    crash = K.reading(book([-1.0, -1.1, -0.9] * 4), None)  # un bug ou une catastrophe : vu avant le 30e
    assert "Borne de dégât franchie" in crash[-1] and "à toi de décider" in crash[-1]
    assert K.harm(book([-1.0] * 9)) is None  # pas avant 10 KILL déclenchés


def test_lot_fige_et_insoldable(gross):
    b = book([1.0] * 31)
    b[0]["resultat"] = None  # le plus ancien attend son solde : le lot des 30 n'est pas figé
    assert K.judged(b, 30) == []
    b[0]["insoldable"] = True  # sans bougies 7 jours après : hors du lot, compté à part
    assert len(K.judged(b, 30)) == 30 and K.stats(b)["insoldables"] == 1


def test_quantile_de_student():
    assert K.t_quantile(0.975, 1) == 12.706 and K.t_quantile(0.9995, 8) == 5.041
    assert K.t_quantile(0.975, 9) == pytest.approx(2.262, abs=0.005)
    assert K.t_quantile(0.9995, 9) == pytest.approx(4.781, abs=0.01)
    assert K.t_quantile(0.975, 24) == pytest.approx(2.064, abs=0.005)
    assert K.t_quantile(0.975, 10_000) == pytest.approx(1.96, abs=0.001)


def test_bilan_par_regle(paths, monkeypatch, gross):
    monkeypatch.setattr(K, "lab_item", lambda: None)
    assert K.LEGACY_VERSION != K._current()  # les appels de la PR #32 sont d'une version précédente du direct
    old = [c | {"seuils": c["seuils"] | {"gap": 3.0}} for c in book([1.0])]
    txt = K.bilan_text(old + book([-1.0]))
    assert "Ancienne règle n° 5 (gap ≥ +3 %, ATR ≥ 4 %, range 15 min, ordre jusqu'à 11 h New York" in txt
    assert "🔪 KILL : 1 soldé, 1 déclenché (100 %), 0 expiré · espérance −1,00 R" in txt.replace("-1,00", "−1,00")
    olds = book([-1.0] * 30)
    monkeypatch.setattr(K, "RULE", 7)  # une déclaration change la règle : les anciens appels n'en sont pas
    assert "rien n'est lu avant 30 KILL déclenchés et soldés (0 pour l'instant)" in K.bilan_text(olds)


def test_empreinte_du_catalyseur(monkeypatch):
    assert K.seuils()["catalyseur_code"] == K.CATALYST_CODE == K.catalyst_fingerprint()
    monkeypatch.setattr(K.C, "POSITIVE", K.C.POSITIVE + [r"\bmoon"])
    assert K.catalyst_fingerprint() != K.CATALYST_CODE  # un mot de plus dans catalyst.py : une nouvelle version


def test_version_du_journal_figee():
    """Un seuil ou le code du catalyseur qui change ouvre une nouvelle version : les appels déjà notés ne sont plus
    lus par la règle. Ce test oblige à le faire exprès (la méthode du solde, elle, re-solde tout le journal)."""
    assert K.seuils() == {"gap": 4.0, "atr": 4.0, "range_min": 15, "jusqu_a": "11:00", "poursuite_r": 0.5,
                          "catalyseur_h": 48, "resultats_h": 24, "resultats_battus_j": 1, "catalyseur_code": "37f15d7363",
                          "objectifs": [1.0, 2.0, 3.0], "direct": 2, "frais_reels": "20 EUR, brackets IBKR, demi-pas"}


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
    rep["strategies"]["stock"]["killmode"] = it | {"jours_vus_jusqu_au": "2025-01-27", "jours_suivants": None}
    assert "pas encore de trade rejoué sur les jours non vus" in K.lab_line(K.lab_item())


async def test_affichage_honnete(paths, monkeypatch):
    sources(monkeypatch)
    a = await K.analyze("ACME", NOW)
    a["jarvis"] = {"checklist": {"verdict": "GO", "score": 5, "max_score": 7},
                   "score": {"composite": 6.5, "decision": "GO"}, "resistances": []}
    txt = K.text(a, "🧪 Labo, ligne")
    assert "note v1 6,5/10 GO, non prédictive au labo" in txt and "🧪 Labo, ligne" in txt
    assert "Un trade stoppé coûte ≈" in txt and "minimum par ordre compris" in txt
    assert "au moins 3 c de glissement par côté (hypothèse de Gemini, non mesurée)" in txt
    plan = a["graphique"]["plan"] | {"entry": 0.85, "stop": 0.80, "risk": 0.05, "targets": [0.9, 0.95, 1.0]}
    cheap = K.text(a | {"graphique": a["graphique"] | {"plan": plan}, "price": 0.84})
    assert "au moins 1 % du prix de glissement par côté" in cheap and "3 c" not in cheap  # sous 1 $
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


async def test_gap_d_ouverture_avant_les_bougies(paths, monkeypatch):
    at = datetime.fromtimestamp(OPEN + 10 * 60, timezone.utc)  # 9 h 40 : aucune bougie de séance close (Alpaca)

    def setup(open_, price):
        sources(monkeypatch, price=price)

        async def snapshots(symbols, feed="iex"):
            return {"ACME": {"price": price, "prev_close": 100.0, "open": open_, "open_day": DAY.isoformat()}}

        async def candles(symbol, market, interval, source=None, max_age=None, outputsize=None):
            return CO.regular_hours(hist())

        monkeypatch.setattr(K.alpaca, "snapshots", snapshots)
        monkeypatch.setattr(K.T, "candles", candles)

    setup(106.0, 103.0)  # ouverte à +6 %, à +3 % maintenant : jugée sur l'ouverture, comme au labo
    a = await K.analyze("ACME", at)
    assert a["gap_pct"] == pytest.approx(6.0) and a["verdict"] == "pret"
    assert "gap d'ouverture +6,0 %" in a["piliers"]["volatilite"]["raisons"][0]
    setup(102.0, 105.0)  # ouverte à +2 %, à +5 % maintenant : pas un jour Killmode
    a = await K.analyze("ACME", at)
    assert a["verdict"] == "non" and "gap d'ouverture +2,0 %" in a["piliers"]["volatilite"]["raisons"][0]


async def test_bougie_de_9h30_absente(paths, monkeypatch):
    sources(monkeypatch)

    async def candles(symbol, market, interval, source=None, max_age=None, outputsize=None):
        return CO.regular_hours(hist()) + bars_from(OPEN + 300, RANGE + RUN)

    monkeypatch.setattr(K.T, "candles", candles)
    a = await K.analyze("ACME", NOW)
    assert a["verdict"] == "non" and "bougie de 9 h 30 absente" in a["piliers"]["graphique"]["raisons"][0]


def test_source_des_bougies():
    fresh = bars_from(OPEN, RANGE + RUN)
    assert K.candle_source(None, fresh, NOW) == "twelvedata"
    late = [b for b in fresh if b["t"] + 300 <= NOW.timestamp() - 16 * 60]  # retombé sur Alpaca (16 min)
    assert K.candle_source(None, late, NOW) == "alpaca (secours)"
    assert K.candle_source("alpaca", late, NOW) == "alpaca" and K.candle_source(None, [], NOW) is None


def test_historique_court_seulement_s_il_l_est():
    co = {"nom": "Acme", "mesures": None, "seances": 60}
    a = {"symbol": "ACME", "at": NOW.isoformat(), "price": None, "gap_pct": None, "verdict": "non", "societe": co,
         "piliers": {k: {"ok": False, "raisons": ["x"]} for k, _ in K.PILLARS}, "graphique": {"status": "non"}}
    assert "historique court" not in K.text(a)
    assert "historique court (20 séances)" in K.text(a | {"societe": co | {"seances": 20}})


# --- Avis de GPT et Gemini (8 octobre) : défauts du direct, solde, frais réels ---------------------------

async def test_trade_du_labo_deja_fini(paths, monkeypatch):
    """Cassure à 9 h 50 puis stop touché à 9 h 55 : le trade du labo est fini, pas de seconde entrée."""
    sources(monkeypatch)
    stopped = RANGE + [(106.3, 106.6, 106.2, 106.5), (106.5, 106.6, 104.0, 104.2), (104.2, 104.6, 104.1, 104.5)]

    async def candles(symbol, market, interval, source=None, max_age=None, outputsize=None):
        return CO.regular_hours(hist()) + bars_from(OPEN, stopped)

    monkeypatch.setattr(K.T, "candles", candles)
    a = await K.analyze("ACME", NOW)
    assert a["graphique"]["status"] == "fini" and a["verdict"] == "non"
    assert "stop touché" in a["graphique"]["raisons"][0] and K.record(a, NOW) is None


async def test_bougies_closes_et_cache(paths, monkeypatch):
    sources(monkeypatch)
    seen = {}

    async def candles(symbol, market, interval, source=None, max_age=None, outputsize=None):
        seen["max_age"] = max_age
        return CO.regular_hours(hist()) + bars_from(OPEN, RANGE + RUN)

    monkeypatch.setattr(K.T, "candles", candles)
    await K.analyze("ACME", NOW + timedelta(seconds=90))
    assert seen["max_age"] == 90  # le cache ne sert que s'il a été lu pendant la bougie en cours
    # Alpaca (16 min de retard) : la bougie qui chevauche la fin des données n'est pas close.
    bars5 = CO.regular_hours(hist()) + bars_from(OPEN, RANGE + RUN)
    d = K.live_day(bars5, daily(), NOW, NOW - timedelta(minutes=16))
    assert d.t[-1] + 300 <= (NOW - timedelta(minutes=16)).timestamp()
    assert len(K.live_day(bars5, daily(), NOW).t) > len(d.t)


def test_actus_comptees_et_motif():
    assert not K.catalyst_pillar(cat(news=[news(hours=-1)]), NOW)[0]  # publiée après l'analyse : ignorée
    assert K.catalyst_codes(cat(), NOW) == []
    assert K.catalyst_codes(cat(news=[]), NOW) == ["pas_de_positive"]
    assert K.catalyst_codes(cat(dilution=True, hours_to_earnings=5), NOW) == ["dilution", "resultats_24h"]
    assert K.catalyst_codes(cat(available=False), NOW) == ["finnhub_muet"]
    assert K.motif("Acme shares jump 25% premarket") == "mouvement"
    assert K.motif("Acme wins $2B defense contract") == "fond"
    assert K.call_motif(cat(news=[news(title="Acme stock soars")]), NOW) == "mouvement"
    assert K.call_motif(cat(news=[news(title="Acme stock soars"), news()]), NOW) == "fond"


async def test_journal_garde_le_contexte(paths, monkeypatch):
    sources(monkeypatch)
    call = K.record(await K.analyze("ACME", NOW), NOW)
    assert call["decided_at"] == OPEN + 15 * 60 and call["declenche_a"] == OPEN + 15 * 60
    assert call["prev_close"] == 100.0 and call["adv20"] == 1e6 and call["gap_type"] == "d'ouverture"
    assert call["secteur"] == "Aerospace & Defense" and call["motif"] == "fond" and call["codes"] == []
    assert call["actus"][0]["titre"].startswith("Acme wins") and call["actu"].startswith("Acme wins")


def _call(at, price=106.7):
    return {"symbol": "ACME", "day": DAY.isoformat(), "at": at, "price": price, "entry": 106.51, "stop": 104.49,
            "targets": [108.53, 110.55, 112.57], "until": OPEN + 90 * 60, "close_ts": CO.session_ts(DAY)[1],
            "prev_close": 100.0, "adv20": 1e6}


def test_ordre_actif_une_minute_apres_l_appel():
    """Appel à 10 h 02 min 10 s : l'ordre est actif à 10 h 04 (première minute pleine 60 s après) ; ce qui se
    passe avant n'est jamais vu. Prix au-dessus de l'entrée : rempli à l'ouverture de cette minute."""
    at = OPEN + 32 * 60 + 10
    early = [(107.0, 107.1, 106.9, 107.0)] * 34  # 9 h 30 → 10 h 03
    flat = [(104.1, 104.2, 104.0, 104.1)] * 300
    res = K.settle(_call(at), minutes(early + [(107.2, 107.3, 106.0, 106.1), (106.0, 106.1, 104.0, 104.1)] + flat))
    assert res["activation"] == OPEN + 34 * 60 and res["remplissage"] == 107.2 and res["sortie"] == "stop"
    assert res["entree_r"] == pytest.approx((107.2 - 106.51) / 2.02, abs=1e-3) and K.value({"resultat": res}
                                                                                          | _call(at)) < -1
    # La minute d'activation ouvre sous l'entrée et n'y remonte pas : l'ordre stop attend, rien sous l'entrée.
    res = K.settle(_call(at, price=106.4), minutes(early + [(106.3, 106.4, 106.2, 106.3)] * 300))
    assert not res["declenche"] and res["remplissage"] is None


async def test_appel_en_fin_de_bougie_solde(paths, monkeypatch):
    """Le défaut de la méthode 1 : un appel dans la dernière minute d'une bougie de 5 min demandait ses minutes
    avec un début après la fin ; si Alpaca refusait, l'appel n'était jamais soldé et la lecture se figeait."""
    sources(monkeypatch)
    call = K.record(await K.analyze("ACME", NOW), NOW)
    book = K.calls()
    book[0]["at"] = OPEN + 35 * 60 - 30  # 10 h 04 min 30 s
    K._write(K.CALLS_PATH, book)

    async def bars_range(symbols, interval, start, end, **kw):
        if start > end:
            raise K.SourceError("HTTP 422 start can not be after end")
        return {"ACME": spread(RANGE + RUN + [(114.7, 115.0, 114.5, 114.8)] * 60)}

    monkeypatch.setattr(K.alpaca, "bars_range", bars_range)
    later = datetime.fromtimestamp(call["close_ts"], timezone.utc) + timedelta(hours=1)
    assert await K.settle_due(later) == 1 and K.calls()[0]["resultat"]["declenche"]


async def test_erreur_de_solde_jamais_muette(paths, monkeypatch):
    sources(monkeypatch)
    call = K.record(await K.analyze("ACME", NOW), NOW)

    async def broken(symbols, interval, start, end, **kw):
        raise K.SourceError("HTTP 500 panne")

    monkeypatch.setattr(K.alpaca, "bars_range", broken)
    later = datetime.fromtimestamp(call["close_ts"], timezone.utc) + timedelta(hours=1)
    assert await K.settle_due(later) == 0
    assert "HTTP 500" in K.calls()[0]["erreur_solde"]
    monkeypatch.setattr(K, "lab_item", lambda: None)
    assert "1 appel pas encore soldé (ACME" in K.bilan_text(K.calls())

    async def ok(symbols, interval, start, end, **kw):
        return {"ACME": spread(RANGE + RUN + [(114.7, 115.0, 114.5, 114.8)] * 60)}

    monkeypatch.setattr(K.alpaca, "bars_range", ok)
    assert await K.settle_due(later) == 1 and "erreur_solde" not in K.calls()[0]


async def test_resolde_si_la_methode_change(paths, monkeypatch):
    """Les bougies d'1 min sont gardées : un changement de méthode re-solde tout le journal sur les mêmes prix,
    sans requête, en gardant le change et le plan IBKR du premier solde."""
    sources(monkeypatch)
    call = K.record(await K.analyze("ACME", NOW), NOW)
    n = []

    async def bars_range(symbols, interval, start, end, **kw):
        n.append(1)
        return {"ACME": spread(RANGE + RUN + [(114.7, 115.0, 114.5, 114.8)] * 60)}

    monkeypatch.setattr(K.alpaca, "bars_range", bars_range)
    later = datetime.fromtimestamp(call["close_ts"], timezone.utc) + timedelta(hours=1)
    assert await K.settle_due(later) == 1 and await K.settle_due(later) == 0 and len(n) == 1
    book = K.calls()
    book[0]["resultat"] |= {"methode": 1, "eurusd": 1.09}
    K._write(K.CALLS_PATH, book)
    assert await K.settle_due(later) == 1 and len(n) == 1  # bougies gardées, rien redemandé
    c = K.calls()[0]
    assert c["resultat"]["methode"] == K.SETTLE_METHOD and c["resultat"]["eurusd"] == 1.09
    assert c["soldes_anciens"][0]["methode"] == 1


def test_concordance_sip():
    bars = bars_from(OPEN, RANGE + RUN)
    v = K.sip_view(_call(OPEN + 30 * 60), bars)
    assert v["signal"] and v["meme_ordre"] and v["vol15_adv"] == pytest.approx(3000 / 1e6)
    shifted = bars_from(OPEN, [(r[0], r[1] + 0.3, r[2], r[3]) for r in RANGE] + RUN)
    assert not K.sip_view(_call(OPEN + 30 * 60), shifted)["meme_ordre"]  # le SIP a vu un plus haut plus haut
    assert K.sip_view(_call(OPEN + 30 * 60) | {"prev_close": None}, bars) is None


async def test_solde_non_ajuste_et_insoldable(paths, monkeypatch):
    sources(monkeypatch)
    call = K.record(await K.analyze("ACME", NOW), NOW)
    asked = []

    async def bars_range(symbols, interval, start, end, **kw):
        asked.append(kw.get("adjustment"))
        return {"ACME": []}

    monkeypatch.setattr(K.alpaca, "bars_range", bars_range)
    close = datetime.fromtimestamp(call["close_ts"], timezone.utc)
    assert await K.settle_due(close + timedelta(hours=1)) == 0 and asked == ["raw"]  # jamais ajusté des splits
    assert await K.settle_due(close + timedelta(days=8)) == 1 and K.calls()[0]["insoldable"]
    assert K.stats(K.calls())["insoldables"] == 1 and await K.settle_due(close + timedelta(days=9)) == 0


def test_frais_reels_a_20_euros():
    c = {"entry": 10.0, "stop": 9.0, "targets": [11.0, 12.0, 13.0]}
    stopped = {"declenche": True, "remplissage": 10.0, "sorties": [{"prix": 9.0, "part": 1.0, "raison": "stop"}],
               "eurusd": 1.15, "plan_ibkr": "fixe"}
    # 23 actions en 3 brackets (8, 8, 7) : 6 ordres plafonnés à 1 % de leur valeur (0,80 $ pour 8 actions à
    # 10 $) ; glissement du labo de 6 pb par côté (0,6 c à l'entrée, 0,54 c au stop, au-dessus du demi-pas).
    fees = (0.8 + 0.72) + (0.8 + 0.72) + (0.7 + 0.63)
    assert K.real_r(c, stopped) == pytest.approx(-1 - 0.0114 - fees / 23, abs=1e-4)
    cheap = {"entry": 2.0, "stop": 1.9, "targets": [2.1, 2.2, 2.3]}  # 15 pb de 2 $ = 0,3 c < demi-pas 0,5 c
    flat = {"declenche": True, "remplissage": 2.0, "sorties": [{"prix": 2.0, "part": 1.0, "raison": "cloture"}]}
    # 2 demi-pas, 3 entrées au minimum de 1 $ et le reste vendu à la clôture en un seul ordre (230 × 0,005 $)
    assert K.real_r(cheap, flat) == pytest.approx(-0.1 - (3 + 1.15) / 23, abs=1e-3)
    assert K.stressed_r(c, stopped) == pytest.approx(-1 - 0.06 - 0.01, abs=1e-4)  # le labo (PR #43), inchangé
    # r_3c du journal : 3 c par côté avec les mêmes brackets et minimums que r_20e (la lecture finale le demande).
    assert K.stress_20e(c, stopped) == pytest.approx(-1 - 0.06 - fees / 23, abs=1e-4)
    assert K.value({"resultat": stopped} | c, "r_3c") == K.stress_20e(c, stopped) < K.real_r(c, stopped)
    assert K.real_r(c, stopped | {"plan_ibkr": "degressif"}) > K.real_r(c, stopped)
    cost = K.stop_cost(10.0, 9.0)
    assert cost["actions"] == 23 and cost["brackets"] == [8, 8, 7] and cost["reel"] > cost["labo"]
    assert cost["stress"] == pytest.approx(0.06 + fees / 23)
    assert K.value({"resultat": stopped} | c) == K.real_r(c, stopped)


def test_gain_en_actions_entieres():
    """TP1 puis retour à l'entrée, 23 actions en brackets de 8, 8 et 7 : le bracket de TP1 gagne 8/23 de la
    position, pas un tiers exact ; à 2 actions, les brackets portent TP1 et TP3, à 1 action TP2."""
    c = {"entry": 10.0, "stop": 9.0, "targets": [11.0, 12.0, 13.0]}
    be = {"declenche": True, "remplissage": 10.0, "eurusd": 1.15, "plan_ibkr": "fixe",
          "sorties": [{"prix": 11.0, "part": 1 / 3, "raison": "tp1"}, {"prix": 10.0, "part": 2 / 3, "raison": "stop_be"}]}
    slip = K._half_tick_slip(10.0)
    fees = (0.8 + 0.88) + (0.8 + 0.8) + (0.7 + 0.7)  # entrée et sortie de chaque bracket, plafond 1 %
    gross = 8 * (1 - K._half_tick_slip(11.0) - slip) + 15 * (-2 * slip)
    assert K.real_r(c, be) == pytest.approx((gross - fees) / 23, abs=1e-4)
    assert K.bracket_orders(10.0, 9.0, c["targets"]) == [(8, 11.0), (8, 12.0), (7, 13.0)]
    assert K.bracket_orders(300.0, 290.0, [310.0, 320.0, 330.0]) == [(1, 310.0), (1, 330.0)]
    assert K.bracket_orders(300.0, 250.0, [350.0, 400.0, 450.0]) == [(1, 400.0)]


def test_change_du_jour_pour_l_ordre_affiche():
    """11,60 $ de risque par action : 1 action au change par défaut (23 $), 2 au change du jour 1,17 (23,4 $), comme
    le solde du journal (`_at_size` au change de son solde)."""
    tps = [111.6, 123.2, 134.8]
    assert K.bracket_orders(100.0, 88.4, tps) == [(1, 123.2)]
    assert K.bracket_orders(100.0, 88.4, tps, 1.17) == [(1, 111.6), (1, 134.8)]
    assert K.stop_cost(100.0, 88.4, 1.17)["actions"] == 2 and K.stop_cost(100.0, 88.4)["actions"] == 1


async def test_kill_hors_budget_et_hors_grade_dit_en_clair(paths, monkeypatch):
    from app.lab import grade as GR

    data = {"marches": {"stock": {"grade": "F-", "rang": 0, "non_note": False, "risque_grade": 0.0,
                                  "risque_max": 0.0, "mode_survie": True}}, "verifie_le": NOW.isoformat()}
    monkeypatch.setattr(GR, "current", lambda: data)
    sources(monkeypatch)
    a = await K.analyze("ACME", NOW)
    assert a["eurusd"] == 1.15 and a["grade"]["grade"] == "F-" and a["grade"]["risque_propose"] == 0.0
    txt = K.text(a)
    assert ("📋 Chez IBKR, à 20 € de risque hors grade (grade actuel F-, risque proposé 0 %) : **11 actions en 3 "
            "brackets**") in txt and "Hors budget" not in txt
    # Une action risque 50 $ (≈ 43 €) : 1 action forcée (le journal reste comparable), dit en clair.
    plan = a["graphique"]["plan"] | {"entry": 300.0, "stop": 250.0, "targets": [350.0, 400.0, 450.0]}
    over = a | {"graphique": a["graphique"] | {"plan": plan}}
    txt = K.text(over)
    assert "📋 Chez IBKR, 1 action = 43 € de risque (au-delà de 20 €) hors grade (grade actuel F-" in txt
    assert "**1 action en 1 bracket**" in txt
    assert "⚠️ Hors budget : à 20 € il faudrait 0 action ; 1 action, un seul bracket sur TP2." in txt
    assert K.record(over, NOW)["hors_budget"] is True


def test_bilan_compte_les_kill_hors_budget_a_part(paths, monkeypatch, gross):
    monkeypatch.setattr(K, "lab_item", lambda: None)
    calls = book([1.0, -1.0])
    calls[1] |= {"entry": 300.0, "stop": 250.0, "targets": [350.0, 400.0, 450.0]}
    calls[1]["resultat"] |= {"remplissage": 300.0, "sorties": [{"prix": 250.0, "part": 1.0, "raison": "stop"}]}
    assert not K.hors_budget(calls[0]) and K.hors_budget(calls[1])
    txt = K.bilan_text(calls).replace("−", "-")
    assert "Hors budget : 1 KILL soldé où une seule action risquait plus de 20 € (1 action forcée" in txt
    assert "à part : -1,00 R par trade sur 1 déclenché" in txt
    assert "Hors budget" not in K.bilan_text(calls[:1])


async def test_prends_un_kill_garde_sa_taille_de_20_euros(paths, monkeypatch):
    from app import scheduler
    from app.lab import grade as GR

    monkeypatch.setattr(scheduler, "book", T.Book(None))
    monkeypatch.setattr(GR, "current", lambda: None)  # grade pas encore calculé : JARVIS ne propose rien
    monkeypatch.setenv("JARVIS_CAPITAL", "1000")

    async def build_plan(symbol, market, side, entry, stop, targets, **kw):
        p = {"symbol": symbol, "market": market, "side": side, "entry": entry, "stop": stop, "stop_pct": -2.0,
             "stop_source": "KILL", "targets": [], "method": "m", "created_at": NOW.isoformat(), "sizing": None,
             "checklist": {"verdict": "Feu vert", "score": 3, "max_score": 5, "items": []}}
        T.apply_grade(p, None, None, NOW, trades=[], usd_per_eur=1.15)
        assert p["sizing"] is None and p["sizing_regle"]
        return p

    monkeypatch.setattr(bot.trading, "build_plan", build_plan)
    # KILL du jour noté par Killmode (sans lui, `kill` est refusé : test_prends_kill_refuse_hors_actions_ou_sans_kill)
    monkeypatch.setattr(K, "kill_today", lambda symbol, now=None: {"stop": 104.49, "targets": [108.5, 110.5, 112.5]})
    txt = await bot.handle("!prends ACME 106.6 stop 104.49 kill")
    t = scheduler.book.trades[0]
    s = t["sizing"]
    assert t["killmode"] and "sizing_regle" not in t
    assert s["risk_pct"] is None and s["risk_amount_eur"] == 20.0 and s["quantity"] == 10 and s["hors_grade"]
    assert "💰 Risque 20 € (23,0 $) : 10 actions pour 1 066 $ · taille du KILL, hors grade" in txt
    assert GR.open_risk([t]) == 2.0  # 20 € sur 1 000 € : compté dans le budget même sous un grade à 0 %
    await bot.handle("!prends XYZ 300 stop 250 kill")
    s = scheduler.book.trades[1]["sizing"]
    assert s["quantity"] == 1 and s["hors_budget"] and s["risk_amount_eur"] == 43.48  # le vrai risque d'une action

    # Relecture d'intégration : `!rouvre` (stop touché mais pas exécuté chez le courtier) garde le KILL, sa gestion
    # (stop à l'entrée dès TP1, vente à la clôture) et sa taille hors grade, au lieu de la règle du grade.
    scheduler.book.close(t["id"], 104.49)
    t["events"][-1]["kind"] = "stop"
    txt = await bot.handle("!rouvre ACME 106.6 stop 104")
    t = next(x for x in scheduler.book.trades if x["symbol"] == "ACME")
    assert "rouvert" in txt and t["status"] == "open" and t["killmode"] and "sizing_regle" not in t
    assert t["sizing"]["hors_grade"] and t["sizing"]["risk_amount_eur"] == 20.0 and t["sizing"]["quantity"] == 8
    assert "taille du KILL, hors grade" in txt


async def test_prends_kill_refuse_hors_actions_ou_sans_kill(paths, monkeypatch):
    """Scénario 2 de GPT : `kill` sur une crypto imposait 1 BTC entier (870 € de risque enregistrés) ; `kill` sans
    KILL du jour était accepté puis mesuré nulle part. Les deux sont refusés, carnet vide."""
    from app import scheduler

    monkeypatch.setattr(scheduler, "book", T.Book(None))
    asked = []

    async def build_plan(*a, **kw):
        raise AssertionError("aucun plan ne doit être calculé")

    monkeypatch.setattr(bot.trading, "build_plan", build_plan)
    monkeypatch.setattr(K, "kill_today", lambda symbol, now=None: asked.append(symbol))
    txt = await bot.handle("!prends BTC 60000 stop 59000 kill")
    assert "Killmode ne couvre que les actions US" in txt and "`!prends BTC 60000 stop 59000`" in txt
    assert "ajoute `action` : `!prends BTC 60000 stop 59000 action kill`" in txt  # BTC est aussi un ETF
    assert scheduler.book.trades == [] and asked == []
    txt = await bot.handle("!prends ETH 2500 stop 2450 crypto kill")
    assert "Killmode ne couvre que les actions US" in txt and "action kill" not in txt
    txt = await bot.handle("!prends AAPL 230 stop 226 kill")
    assert "Pas de KILL de Killmode aujourd'hui pour AAPL" in txt and "`!prends AAPL 230 stop 226` sans `kill`" in txt
    assert scheduler.book.trades == [] and asked == ["AAPL"]
    txt = await bot.handle("!prends BTC 60000 stop 59000 action kill")  # l'action BTC : cherche son KILL du jour
    assert asked == ["AAPL", "BTC"] and scheduler.book.trades == []
    # Sans KILL du jour : la commande proposée garde `action`, sinon elle suivrait la crypto BTC.
    assert "Pas de KILL de Killmode aujourd'hui pour BTC" in txt and "`!prends BTC 60000 stop 59000 action` sans `kill`" in txt
    assert bot.parse_trade(["BTC", "60000", "stop", "59000", "action"])["market"] == "stock"


def test_rouvre_un_go_confirme_reste_avec_jarvis():
    """Relecture d'intégration : un GO confirmé rouvert dont la note recalculée est NO GO reste « avec JARVIS »."""
    book = T.Book(None)
    plan = {"symbol": "ACME", "market": "stock", "side": "long", "entry": 10.0, "stop": 9.5, "targets": [],
            "created_at": NOW.isoformat(), "score": {"go": True}, "policy": "go_confirme"}
    old = book.add(plan)
    book.close(old["id"], 9.5)
    old["events"][-1]["kind"] = "stop"
    new_plan = {k: v for k, v in plan.items() if k != "policy"} | {"stop": 9.2, "score": {"go": False}}
    assert T.against_jarvis(new_plan)  # le plan recalculé seul serait compté contre JARVIS
    T.reopen(book, "ACME", new_plan)
    t = book.trades[0]
    assert t["status"] == "open" and t["policy"] == "go_confirme" and not T.against_jarvis(t)


def test_rouvre_garde_la_note_et_la_taille_de_l_entree():
    """!rouvre : la note recalculée au niveau du stop ne fait pas passer un trade GO « contre JARVIS », et la quantité
    tenue (donc les frais) reste celle de l'entrée."""
    book = T.Book(None)
    size = {"risk_amount_eur": 20.0, "eurusd": 1.15, "quantity": 46, "risk_amount": 23.0}
    plan = {"symbol": "ACME", "market": "stock", "side": "long", "entry": 10.0, "stop": 9.5, "targets": [],
            "created_at": NOW.isoformat(), "score": {"go": True}, "sizing": size, "grade": {"grade": "D"}}
    old = book.add(plan)
    book.close(old["id"], 9.5)
    old["events"][-1]["kind"] = "stop"
    new_plan = plan | {"stop": 9.2, "score": {"go": False}, "sizing": None, "grade": {"grade": "F"},
                       "sizing_regle": {"quantity": 76}}
    T.reopen(book, "ACME", new_plan)
    t = book.trades[0]
    assert t["status"] == "open" and not T.against_jarvis(t) and t["score"] == {"go": True}
    assert t["sizing"] == size and "sizing_regle" not in t and t["grade"] == {"grade": "D"}


def test_pire_et_meilleur_ordre(paths, monkeypatch):
    """Une minute où le stop et TP1 sont touchés : le pire ordre (stop) décide, le meilleur est affiché."""
    at = OPEN + 30 * 60
    rows = [(106.0, 106.1, 105.9, 106.0)] * 31 + [(106.4, 106.6, 106.3, 106.55), (106.55, 108.7, 104.3, 106.0)]
    res = K.settle(_call(at), minutes(rows + [(106.0, 106.1, 105.9, 106.0)] * 300))
    assert res["sortie"] == "stop" and res["sortie_fav"] == "stop_be"
    assert res["ambigus"] == [{"t": OPEN + 32 * 60, "cas": "stop_et_objectif"}]
    c = _call(at) | {"groupe": "kill", "resultat": res | {"eurusd": 1.15, "plan_ibkr": "fixe"}}
    assert K.value(c) < -1 < 0 < K.value(c, "r_fav")
    line = K.ambiguous_line([c])
    assert "1/1 KILL changent selon l'ordre" in line and "pire de tous les ordres possibles (celui qui compte)" in line
    w = c | {"groupe": "temoin_catalyseur", "codes": ["pas_de_positive"], "symbol": "WIT"}
    gap = K._call_gap_line([c, w])
    assert "toutes sources +0,00 R" in gap and "meilleur ordre des minutes ambiguës +0,00 R" in gap


async def test_bilan_permis_eteint(paths, monkeypatch):
    monkeypatch.setattr(K, "lab_item", lambda: None)
    assert not K.active()
    txt = await K.command(["bilan"], NOW)
    assert "Killmode · bilan" in txt and "Killmode est éteint : le bilan solde" in txt


async def test_prends_un_kill(paths, monkeypatch):
    assert bot.parse_trade(["acme", "106.6", "stop", "104.49", "kill"])["killmode"] is True
    assert "killmode" not in bot.parse_trade(["acme", "106.6", "stop", "104.49"])
    nogo = {"score": {"go": False, "reasons": ["x"]}, "status": "closed", "side": "long", "entry": 10.0, "stop": 9.0,
            "targets": [{"price": 11.0}], "targets_hit": 1, "events": []}
    assert T.against_jarvis(nogo) and not T.against_jarvis(nogo | {"killmode": True})

    assert T.disagreements(SimpleNamespace(trades=[nogo | {"killmode": True}]))["contre"]["total"] == 0  # mesuré par le journal de Killmode, pas ici
    kills = book([1.0])
    kills[0] |= {"symbol": "ACME", "day": "2026-09-01"}
    trades = [{"symbol": "ACME", "killmode": True, "entry": 10.2, "created_at": "2026-09-01T14:00:00+00:00",
               "status": "open"}]
    assert "Tes exécutions : 1 KILL pris, entrée +0,20 R" in K.fills_line(kills, trades)


async def test_prends_un_kill_reprend_ses_objectifs(paths, monkeypatch):
    sources(monkeypatch)
    K.record(await K.analyze("ACME", NOW), NOW)
    assert K.kill_today("acme", NOW)["targets"] == pytest.approx([108.53, 110.55, 112.57])
    assert K.kill_today("ACME", NOW + timedelta(days=1)) is None
    seen = {}

    async def build_plan(symbol, market, side, entry, stop, targets, **kw):
        seen.update(stop=stop, targets=targets)
        raise T.PlanError("stop")

    monkeypatch.setattr(bot.trading, "build_plan", build_plan)
    monkeypatch.setattr(K, "kill_today", lambda symbol, now=None: K.calls()[0])
    await bot.handle("!prends ACME 106.6 kill")
    assert seen["stop"] == pytest.approx(104.49) and seen["targets"] == pytest.approx([108.53, 110.55, 112.57])


async def test_garde_fou_regroupement(paths, monkeypatch):
    sources(monkeypatch, price=1000.0)

    async def quotes(symbols):
        return [{"symbol": "ACME", "previous_close": 1000.0}]

    monkeypatch.setattr(K.finnhub, "stock_quotes", quotes)
    a = await K.analyze("ACME", datetime.fromtimestamp(OPEN - 3600, timezone.utc))  # pré-ouverture : +900 %
    assert "regroupement d'actions possible" in " ".join(a["piliers"]["donnees"]["raisons"])


async def test_scan_garde_les_valeurs_coupees(paths, monkeypatch):
    K.set_active(True, NOW)

    async def scan(now=None):
        return {"at": NOW.isoformat(), "examined": 9, "movers": 8, "analysed": 0, "setups": [], "dropped": [],
                "coupees": [{"symbol": "ZZZ", "gap_ouverture": 9.0, "gap_moment": 7.0, "atr_pct": 8.0}]}

    monkeypatch.setattr(K, "scan", scan)
    await K.command(["scan"], NOW)
    assert K._read(K.SCANS_PATH, [])[0]["coupees"][0]["symbol"] == "ZZZ"


# --- Méthode du solde n° 3 (8 octobre, troisième avis de GPT et Gemini) -------------------------------------

def _ticks(m, trades, quotes=(), q0=None):
    """Données d'une minute : transactions (décalage en ns, prix, conditions, tape, mise à jour) et cotations
    (décalage, bid, ask)."""
    n0 = m * 10**9
    return {"t": [[n0 + d, p, 100.0, c, z, u] for d, p, c, z, u in trades],
            "q": [[n0 + d, b, a] for d, b, a in quotes], "q0": [n0 - 1, *q0] if q0 else None}


def test_transactions_comme_ibkr_les_voit():
    """Règle « Last » d'IBKR : transaction qui fait un dernier cours, dans un NBBO valide à 0,5 % près ; l'ordre au
    marché qui part prend le pire de la cotation en vigueur et de la suivante."""
    m = OPEN + 600
    data = _ticks(m, [(1, 100.01, "@", "C", ""),
                      (2, 99.0, "@I", "C", ""),            # lot incomplet : pas un dernier cours
                      (3, 100.5, " B", "A", ""),           # prix moyen (CTA)
                      (4, 100.01, "@B", "C", ""),          # transaction groupée (UTP) : éligible
                      (5, 101.0, "@", "C", ""),            # à plus de 0,5 % du NBBO : IBKR l'ignore
                      (6, 100.02, "@", "C", "canceled"),   # annulée après coup : IBKR l'a vue, elle compte
                      (7, 100.01, "@", "C", "corrected"),  # correction insérée après coup : IBKR ne l'a pas vue
                      (31, 100.04, "@", "C", ""),          # NBBO sans bid : IBKR ne déclenche pas
                      (41, 100.05, "@F", "C", "")],        # balayage intermarchés, sans cotation après
                  quotes=[(10, 100.01, 100.03), (30, 0.0, 100.05), (40, 100.0, 100.06)], q0=(100.0, 100.02))
    bars = K.tick_bars(m, data)
    assert [(b["o"], b["bid"], b["ask"]) for b in bars] == [(100.01, 100.0, 100.03), (100.01, 100.0, 100.03),
                                                           (100.02, 100.0, 100.03), (100.05, 100.0, 100.06)]
    assert all(b["t"] == m and b["h"] == b["l"] == b["o"] for b in bars)
    # Sans cotation lue, la transaction compte à son prix.
    assert K.tick_bars(m, _ticks(m, [(1, 100.0, "@", "C", "")]))[0]["bid"] is None


def test_achat_stop_sur_un_saut_dans_la_minute():
    """GPT : ordre stop d'achat à 106,51, la minute ouvre dessous et saute à 107,8 sans rien entre les deux. La
    bougie seule remplissait à 106,51 ; la transaction qui déclenche et l'ask d'après donnent 107,9."""
    at = OPEN + 30 * 60
    m = OPEN + 31 * 60
    rows = [(106.0, 106.1, 105.9, 106.0)] * 31 + [(106.2, 108.0, 106.1, 107.9)] + [(107.9,) * 4] * 300
    assert K.settle(_call(at), minutes(rows))["remplissage"] == 106.51
    data = _ticks(m, [(1, 106.2, "@", "C", ""), (2, 106.3, "@", "C", ""), (4, 107.8, "@", "C", ""),
                      (6, 108.0, "@", "C", ""), (8, 107.9, "@", "C", "")],
                  quotes=[(0, 106.19, 106.21), (3, 107.7, 107.82), (5, 107.85, 107.9), (7, 107.88, 108.01)])
    res = K.settle(_call(at), minutes(rows), ticks={str(m): data})
    assert res["remplissage"] == 107.9 and res["remplissage_mesure"] and res["a_lire"] == []
    assert res["entree_r"] == pytest.approx((107.9 - 106.51) / 2.02, abs=1e-3) and res["minutes_lues"] == [m]
    c = _call(at) | {"groupe": "kill", "resultat": res | {"eurusd": 1.15, "plan_ibkr": "fixe"}}
    q = K.shares(2.02)
    # Rien ajouté au prix mesuré ; 3 entrées et la vente à la clôture en un ordre, au minimum de 1 $ chacune.
    assert K.value(c) == pytest.approx(-(q * K._half_tick_slip(107.9) + 4.0) / (q * 2.02), abs=1e-3)


def test_stop_dans_une_minute_qui_s_effondre():
    """Gemini : stop 104,49 touché dans une minute qui tombe à 101. La bougie seule sortait au stop exact ; la
    transaction qui déclenche (104,30) et le bid d'après (103,90) donnent la vraie sortie."""
    at = OPEN + 30 * 60
    fill_m, crash = OPEN + 31 * 60, OPEN + 32 * 60
    rows = ([(107.0, 107.1, 106.9, 107.0)] * 31 + [(107.2, 107.3, 107.0, 107.1), (106.8, 106.9, 101.0, 101.5)]
            + [(101.5,) * 4] * 300)
    assert K.settle(_call(at), minutes(rows))["sorties"][0]["prix"] == 104.49
    data = _ticks(crash, [(1, 106.8, "@", "C", ""), (2, 105.0, "@", "C", ""), (4, 104.3, "@", "C", ""),
                          (6, 101.0, "@", "C", ""), (8, 101.5, "@", "C", "")],
                  quotes=[(0, 106.7, 106.85), (3, 104.2, 104.35), (5, 103.9, 104.1), (7, 100.9, 101.1)])
    res = K.settle(_call(at), minutes(rows), ticks={str(crash): data})
    assert res["sorties"] == [{"prix": 103.9, "part": 1.0, "raison": "stop", "mesure": True}]
    assert res["sous_stop_r"] == pytest.approx((104.49 - 103.9) / 2.02, abs=1e-3)
    assert res["a_lire"] == [fill_m]  # la minute d'achat (ouverte au-dessus de l'entrée) reste à lire
    c = _call(at) | {"groupe": "kill", "resultat": res | {"eurusd": 1.15, "plan_ibkr": "fixe"}}
    assert "1 sortie à 0,25 R ou plus sous le stop actif" in K.risk_line([c])
    line = K.execution_line([c], NOW)
    assert "achat sur 0/1 KILL" in line and "1 stop mesuré, +0,29 R sous le stop actif" in line
    assert "1 minute encore à lire" in line


def test_pire_de_tous_les_ordres():
    """GPT : le pire ordre minute par minute n'est pas le pire résultat. Entrée et stop dans la minute d'entrée : le
    pire ordre stoppe à −1 R ; l'autre ordre (le plus bas avant l'achat) garde le trade jusqu'à un gap à 103."""
    at = OPEN + 30 * 60
    rows = ([(106.0, 106.1, 105.9, 106.0)] * 31 + [(106.0, 106.8, 104.3, 106.6), (106.6, 106.7, 106.3, 106.4),
                                                    (103.0, 103.1, 102.9, 103.0)] + [(103.0,) * 4] * 300)
    res = K.settle(_call(at), minutes(rows))
    assert res["sorties"][0]["prix"] == 103.0 and res["sorties_fav"][0]["prix"] == 104.49
    assert res["ordres_joues"] == 3 and not res["ordres_tronques"]  # pire, autre, objectifs puis entrée
    assert res["ambigus"] == [{"t": OPEN + 31 * 60, "cas": "entree_et_stop"}] and res["a_lire"] == [OPEN + 31 * 60,
                                                                                                    OPEN + 33 * 60]
    c = _call(at) | {"groupe": "kill", "resultat": res | {"eurusd": 1.15, "plan_ibkr": "fixe"}}
    assert K.value(c) < K.value(c, "r_fav") < -1


def test_meilleur_ordre_objectifs_puis_entree():
    """Troisième ordre d'une minute d'entrée touchée des deux côtés : l'achat, les objectifs, puis le plus bas (retour
    à l'entrée). C'est le meilleur ici ; le pire ordre minute par minute et « le plus bas avant l'achat » non."""
    at = OPEN + 30 * 60
    rows = [(106.0, 106.1, 105.9, 106.0)] * 31 + [(106.0, 110.8, 104.3, 106.8)] + [(103.0,) * 4] * 300
    res = K.settle(_call(at), minutes(rows))
    assert res["ordres_joues"] == 3 and res["sorties"][0] == {"prix": 104.49, "part": 1.0, "raison": "stop"}
    assert [(e["raison"], e["prix"]) for e in res["sorties_fav"]] == [("tp1", 108.53), ("tp2", 110.55),
                                                                     ("stop_be", 106.51)]


def test_pire_classe_aux_prix_mesures():
    """Les chemins sont classés sur le chiffre qui décide, aux prix mesurés : deux ordres au même R du simulateur
    finissent loin l'un de l'autre quand le stop à l'entrée part au bid mesuré, très bas."""
    at = OPEN + 30 * 60
    m = [OPEN + k * 60 for k in range(41)]
    rows = ([(106.0, 106.1, 105.9, 106.0)] * 31 + [(106.51, 106.6, 106.5, 106.55), (106.6, 108.6, 106.5, 106.7)]
            + [(106.7, 106.8, 106.6, 106.7)] * 7 + [(106.6, 106.6, 106.51, 106.51)] + [(106.0, 106.1, 105.9, 106.0)] * 300)
    ticks = {str(m[31]): _ticks(m[31], [(1, 106.51, "@", "C", ""), (2, 106.55, "@", "C", "")],
                                quotes=[(0, 106.5, 106.52)]),
             str(m[40]): _ticks(m[40], [(1, 106.55, "@", "C", ""), (3, 106.51, "@", "C", "")],
                                quotes=[(2, 106.5, 106.56), (4, 100.0, 100.1)], q0=(106.54, 106.56))}
    res = K.settle(_call(at), minutes(rows), ticks=ticks, eurusd=1.15, plan_ibkr="fixe")
    assert res["a_lire"] == [m[32]] and res["ambigus"] == [{"t": m[32], "cas": "objectif_puis_entree"}]
    last = res["sorties"][-1]
    assert (last["raison"], last["prix"], last.get("mesure")) == ("stop_be", 100.0, True)
    c = _call(at) | {"groupe": "kill", "resultat": res | {"eurusd": 1.15, "plan_ibkr": "fixe"}}
    assert K.value(c) < -1 < 0 < K.value(c, "r_fav")


async def test_minutes_qui_decident_lues_et_gardees(paths, monkeypatch):
    sources(monkeypatch)
    asked = tick_feed(monkeypatch, fail=())
    at = OPEN + 30 * 60
    rows = minutes([(106.0, 106.1, 105.9, 106.0)] * 31 + [(106.2, 106.9, 106.1, 106.8), (106.8, 106.9, 104.0, 104.2)]
                   + [(104.2,) * 4] * 300)
    path = K._bars_file(_call(at))
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(rows, f)
    res = await K.settle_ticks(_call(at), rows)
    assert asked == [OPEN + 31 * 60, OPEN + 32 * 60] and res["a_lire"] == [] and res["sortie"] == "stop"
    assert res["minutes_lues"] == asked and K.load_ticks(_call(at)).keys() == {str(m) for m in asked}
    # Transactions dans l'ordre ouverture, plus bas, plus haut, clôture : rien d'ambigu, stop à 104,0.
    assert res["sorties"][0]["prix"] == 104.0 and res["ambigus"] == []
    await K.settle_ticks(_call(at), rows)
    assert len(asked) == 2  # gardées : rien redemandé


async def test_minute_refusee_retentee_au_bilan_suivant(paths, monkeypatch):
    sources(monkeypatch)
    call = K.record(await K.analyze("ACME", NOW), NOW)
    after = spread(RANGE + RUN + [(114.7, 115.0, 114.5, 114.8)] * 60)

    async def bars_range(symbols, interval, start, end, **kw):
        return {"ACME": after}

    monkeypatch.setattr(K.alpaca, "bars_range", bars_range)
    fill_m = call["at"] + 60
    tick_feed(monkeypatch, fail=(fill_m,))
    later = datetime.fromtimestamp(call["close_ts"], timezone.utc) + timedelta(hours=1)
    assert await K.settle_due(later) == 1
    res = K.calls()[0]["resultat"]
    assert res["a_lire"] == [fill_m] and not res["remplissage_mesure"]
    monkeypatch.setattr(K, "lab_item", lambda: None)
    assert "1 minute encore à lire" in K.bilan_text(K.calls(), now=later)
    asked = tick_feed(monkeypatch)
    assert await K.settle_due(later) == 1 and asked == [fill_m]
    c = K.calls()[0]
    assert c["resultat"]["a_lire"] == [] and c["resultat"]["minutes_lues"] == [fill_m] and "soldes_anciens" not in c
    assert await K.settle_due(later) == 0
    stale = datetime.fromtimestamp(call["close_ts"], timezone.utc) + timedelta(days=K.UNSETTLED_DAYS + 1)
    book = K.calls()
    book[0]["resultat"]["a_lire"] = [fill_m]
    K._write(K.CALLS_PATH, book)
    assert await K.settle_due(stale) == 0  # au-delà de 7 jours, plus retentée, et le bilan le dit
    text = K.bilan_text(K.calls(), now=stale)
    assert "1 minute jamais lue (plus retentée après 7 jours" in text and "encore à lire" not in text


def test_frais_sans_glissement_sur_un_prix_mesure():
    c = {"entry": 10.0, "stop": 9.0, "targets": [11.0, 12.0, 13.0]}
    res = {"declenche": True, "remplissage": 10.0, "eurusd": 1.15, "plan_ibkr": "fixe",
           "sorties": [{"prix": 9.0, "part": 1.0, "raison": "stop"}]}
    fees = (0.8 + 0.72) + (0.8 + 0.72) + (0.7 + 0.63)
    assert K.real_r(c, res) == pytest.approx(-1 - 0.0114 - fees / 23, abs=1e-4)
    measured = res | {"remplissage_mesure": True, "sorties": [{"prix": 9.0, "part": 1.0, "raison": "stop",
                                                               "mesure": True}]}
    assert K.real_r(c, measured) == pytest.approx(-1 - fees / 23, abs=1e-4)
    assert K.stress_20e(c, measured) == K.stress_20e(c, res)  # le stress de 3 c reste par-dessus


def test_stress_a_deux_pas_affiche():
    rows = book([0.0])
    rows[0]["resultat"] |= {"sorties_2t": [{"prix": 9.0, "part": 1.0, "raison": "stop"}]}
    assert "objectifs exigés à 2 pas au-delà : -1," in K.stress_line(rows)


# --- Méthode du solde n° 4 (9 octobre, quatrième avis de GPT) ----------------------------------------------

def test_ordre_actif_une_minute_apres_l_envoi():
    """L'ordre est actif à la première minute qui commence 60 s ou plus après l'envoi du message (`envoye_a`) ;
    les appels notés avant la méthode 4 gardent le début de l'analyse."""
    c = _call(OPEN + 30 * 60 + 5)
    assert K.activation(c) == OPEN + 32 * 60
    assert K.activation(c | {"envoye_a": OPEN + 31 * 60}) == OPEN + 32 * 60  # 60 s pile : cette minute-là
    assert K.activation(c | {"envoye_a": OPEN + 31 * 60 + 1}) == OPEN + 33 * 60


async def test_envoi_note_au_journal(paths, monkeypatch):
    sources(monkeypatch, catalyst=cat(news=[]))
    a = await K.analyze("ACME", NOW)
    assert K.record(a, NOW, NOW.timestamp() + 59.2)["envoye_a"] == NOW.timestamp() + 60  # à la seconde supérieure
    assert K.activation(K.calls()[0]) == NOW.timestamp() + 120

    async def scan(now=None):
        return {"at": NOW.isoformat(), "examined": 1, "movers": 1, "analysed": 1, "setups": [],
                "dropped": [a | {"symbol": "BETA"}]}

    clock = {"s": 100.0}

    async def slow_scan(now=None):  # le scan dure 47,4 s : le message part après
        clock["s"] += 47.4
        return await scan(now)

    async def slow_analyze(symbol, now=None):
        clock["s"] += 12.2
        return a | {"symbol": "GAMMA"}

    monkeypatch.setattr(K, "scan", slow_scan)
    monkeypatch.setattr(K, "analyze", slow_analyze)
    monkeypatch.setattr(K, "time", SimpleNamespace(monotonic=lambda: clock["s"]))
    K.set_active(True, NOW)
    await K.command(["scan"], NOW)
    assert K.calls()[1]["envoye_a"] == NOW.timestamp() + 48
    await K.command(["gamma"], NOW)
    assert K.calls()[2]["envoye_a"] == NOW.timestamp() + 13


def _release(q0, first_quote, tail, low=104.4, first=108.58):
    """Le prix saute à `first` (108,58 : TP1 108,53 dépassé d'un pas) sur la transaction qui déclenche l'achat, puis
    retombe jusqu'à `low` dans la minute. `first_quote` None : aucune cotation lue dans la minute."""
    at, m = OPEN + 30 * 60, OPEN + 31 * 60
    trades = [(1, first), (5, 107.0), (8, 106.0), (12, 104.4)]
    quotes = [(2, *first_quote), (4, 106.95, 107.05), (7, 105.95, 106.05), (11, 104.35, 104.45)] if first_quote else []
    keep = sum(p >= low for _, p in trades)
    data = _ticks(m, [(d, p, "@", "C", "") for d, p in trades[:keep]], quotes=quotes[:keep], q0=q0)
    rows = [(106.0, 106.1, 105.9, 106.0)] * 31 + [(first, 108.6, low - 0.1, low)] + tail
    return K.settle(_call(at, price=106.9), minutes(rows), ticks={str(m): data})


def test_objectif_depasse_pose_seulement_si_le_bid_l_atteint():
    """GPT : un objectif dépassé quand l'achat se remplit ne part que s'il est exécutable. Bid 108,45 puis 108,48 sous
    TP1 : il reste posé, le prix retombe au stop, tout sort au bid mesuré. Bid à 108,54 : TP1 part sur-le-champ."""
    flat = [(104.4, 104.5, 104.3, 104.4)] * 330
    res = _release((108.45, 108.60), (108.48, 108.62), flat)
    assert res["remplissage"] == 108.62 and [(e["raison"], e["prix"], e["part"]) for e in res["sorties"]] == [
        ("stop", 104.35, 1.0)]
    res = _release((108.55, 108.60), (108.54, 108.62), flat)
    assert [(e["raison"], e["prix"]) for e in res["sorties"]] == [("tp1", 108.54), ("stop_be", 104.35)]
    # Resté posé, il part à son prix quand le prix le dépasse ensuite d'un pas.
    res = _release((108.45, 108.60), (108.48, 108.62), [(106.0, 108.6, 106.0, 108.6)] + [(108.6,) * 4] * 330,
                   low=106.0)
    assert [(e["raison"], e["prix"]) for e in res["sorties"]][0] == ("tp1", 108.53)


def test_objectif_atteint_par_le_bid_ou_sans_cotation():
    """Relecture : transaction pile au TP1 avec les deux bids à 108,53, il part sur-le-champ (avant : resté posé,
    puis stoppé) ; un seul des deux bids au TP1, ou aucune cotation lue dans la minute : elle est ambiguë, posé et
    exécuté sont joués, le pire décide."""
    m, flat = OPEN + 31 * 60, [(104.4, 104.5, 104.3, 104.4)] * 330
    res = _release((108.53, 108.60), (108.53, 108.62), flat, first=108.53)
    assert [(e["raison"], e["prix"]) for e in res["sorties"]] == [("tp1", 108.53), ("stop_be", 104.35)]
    assert not res["ambigus"]
    for q0, quote, first in (((108.53, 108.60), (108.50, 108.62), 108.53), (None, None, 108.58)):
        res = _release(q0, quote, flat, first=first)
        assert {"t": m, "cas": "objectif_pose"} in res["ambigus"] and res["ordres_joues"] == 2, (q0, quote)
        assert [e["raison"] for e in res["sorties"]] == ["stop"], (q0, quote)
        assert [e["raison"] for e in res["sorties_fav"]] == ["tp1", "stop_be"], (q0, quote)


def test_cotations_de_la_pause_ecartees():
    """Après un trou de 5 min, une transaction de réouverture (condition 5) prouve une suspension : les cotations
    jusqu'à elle sont celles de la pause, sans exécution possible, et ne donnent plus le prix de l'ordre au marché
    qui part à la reprise. Sans elle, l'action était seulement calme : son NBBO était exécutable et compte."""
    m = OPEN + 600
    bar = {"o": 107.0, "h": 107.0, "l": 107.0, "c": 107.0, "v": 100.0}
    for cond, gap, ask in (("5", 300, 107.6), ("@", 300, 108.5), ("5", 60, 108.5)):
        data = _ticks(m, [(1000, 107.5, cond, "C", "")], quotes=[(500, 107.0, 108.5), (2000, 107.4, 107.6)])
        out = K.refine([bar | {"t": m - gap}, bar | {"t": m}], {str(m): data})
        assert out[-1]["ask"] == ask, (cond, gap)
    data = _ticks(m, [(1000, 107.5, "5", "C", "")], quotes=[(500, 107.0, 108.5), (2000, 107.4, 107.6)])
    assert [(b["bid"], b["ask"]) for b in K.tick_bars(m, data)] == [(107.0, 108.5)]
    assert [(b["bid"], b["ask"]) for b in K.tick_bars(m, data, reopened=True)] == [(107.4, 107.6)]
    # La pause finit à la première transaction de la minute : une cotation au même instant compte, et une
    # transaction avant celle de réouverture prend les cotations d'après elle.
    data = _ticks(m, [(1000, 107.5, "5", "C", "")], quotes=[(500, 107.0, 108.5), (1000, 107.4, 107.6)])
    assert [(b["bid"], b["ask"]) for b in K.tick_bars(m, data, reopened=True)] == [(107.4, 107.6)]
    data = _ticks(m, [(1000, 107.5, "@", "C", ""), (3000, 107.5, "5", "C", "")],
                  quotes=[(500, 107.0, 108.5), (1500, 107.45, 107.55)])
    assert [(b["bid"], b["ask"]) for b in K.tick_bars(m, data, reopened=True)] == [(107.45, 107.55)] * 2


def test_objectif_execute_ou_pose_les_deux_joues():
    """Seule la cotation suivante a un bid au TP1 : les ordres liés deviennent actifs sous l'une ou l'autre, TP1
    exécuté ou posé ; les deux chemins sont joués et le pire décide (relecture : ne garder que « posé » donnait
    +0,58 R là où l'autre chemin perd)."""
    at, m = OPEN + 30 * 60, OPEN + 31 * 60
    data = _ticks(m, [(1, 108.58, "@", "C", ""), (5, 107.0, "@", "C", ""), (8, 106.0, "@", "C", "")],
                  quotes=[(2, 108.54, 108.62), (4, 106.95, 107.05), (7, 105.95, 106.05)], q0=(108.45, 108.60))
    rows = ([(106.0, 106.1, 105.9, 106.0)] * 31 + [(108.58, 108.6, 105.9, 106.0), (106.2, 107.0, 106.2, 107.0),
                                                    (107.0, 113.0, 107.0, 113.0)] + [(113.0,) * 4] * 300)
    res = K.settle(_call(at, price=106.9), minutes(rows), ticks={str(m): data}, eurusd=1.15, plan_ibkr="fixe")
    assert {"t": m, "cas": "objectif_pose"} in res["ambigus"] and res["ordres_joues"] == 2
    assert [(e["raison"], e["prix"]) for e in res["sorties"]] == [("tp1", 108.53), ("stop_be", 105.95)]
    assert [e["raison"] for e in res["sorties_fav"]] == ["tp1", "tp2", "tp3"]
    c = _call(at) | {"groupe": "kill", "resultat": res | {"eurusd": 1.15, "plan_ibkr": "fixe"}}
    assert K.value(c) < -1 < 0 < K.value(c, "r_fav")


def test_trou_avant_la_cloture_incomplet():
    """Trade ouvert à la clôture sans bougie dans ses deux dernières minutes : données incomplètes (une pause LULD
    des dix dernières minutes ne rouvre pas)."""
    at = OPEN + 30 * 60
    rows = [(106.0, 106.1, 105.9, 106.0)] * 31 + [(106.2, 106.9, 106.1, 106.8)] + [(107.0,) * 4] * 357
    assert len(rows) == 389  # jusqu'à 15 h 58 compris
    assert not K.settle(_call(at), minutes(rows))["donnees_incompletes"]
    assert K.settle(_call(at), minutes(rows[:-1]))["donnees_incompletes"]


def test_ordre_valable_jusqu_a_11h_pile():
    """GTD 11:00:00 New York : une transaction à 10 h 59 min 59,9 s remplit l'achat, une à 11 h 00 pile non."""
    at = OPEN + 30 * 60
    flat = [(106.0, 106.1, 105.9, 106.0)]
    m = OPEN + 89 * 60  # 10 h 59
    data = _ticks(m, [(1, 106.0, "@", "C", ""), (59_900_000_000, 106.6, "@", "C", "")],
                  quotes=[(59_800_000_000, 106.55, 106.62), (59_950_000_000, 106.58, 106.64)], q0=(105.95, 106.05))
    rows = flat * 89 + [(106.0, 106.6, 105.9, 106.6)] + [(106.6, 106.7, 106.5, 106.6)] * 300
    res = K.settle(_call(at), minutes(rows), ticks={str(m): data})
    assert res["declenche"] and res["remplissage"] == 106.64
    rows = flat * 90 + [(106.6, 106.7, 106.5, 106.6)] * 300  # rien avant 11 h, au-dessus de l'entrée dès 11 h
    assert not K.settle(_call(at), minutes(rows))["declenche"]


def test_trous_et_sorties_avant_la_cotation():
    """Diagnostics, sans changer le chiffre : trou de 5 min pendant la position ; sortie déclenchée entre la
    transaction qui déclenche l'achat et la cotation suivante (TP et SL liés peut-être pas encore actifs)."""
    at = OPEN + 30 * 60
    rows = minutes([(106.0, 106.1, 105.9, 106.0)] * 31 + [(106.2, 106.9, 106.1, 106.8)] + [(107.0,) * 4] * 357)
    res = K.settle(_call(at), rows[:40] + rows[47:])
    assert res["trous"] == [[OPEN + 40 * 60, OPEN + 47 * 60]] and not res["enfants_avant_remplissage"]
    assert K.settle(_call(at), rows)["trous"] == []
    fill = {"ns": 10, "seq": 0, "q_ns": 20}
    assert K.early_children({"fill_tick": fill, "exits": [{"ns": 15, "seq": 2}]})
    assert K.early_children({"fill_tick": fill, "exits": [{"ns": 10, "seq": 1}]})  # même horodatage, après l'achat
    assert not K.early_children({"fill_tick": fill, "exits": [{"ns": 10, "seq": 0}]})  # sur la transaction de l'achat
    assert not K.early_children({"fill_tick": fill, "exits": [{"ns": 25, "seq": 3}, {}]})
    # Sans cotation suivante lue : jusqu'à la fin de la minute de l'achat.
    assert K.early_children({"fill_tick": {"ns": 10, "q_ns": None}, "exits": [{"ns": 15}]})
    assert not K.early_children({"fill_tick": {"ns": 10, "q_ns": None}, "exits": [{"ns": 60 * 10**9}]})
    assert not K.early_children({"fill_tick": fill, "exits": [{"ns": 15, "seq": 2, "reason": "cloture"}]})
    # Ordre jamais actif (envoyé dans la dernière minute avant 11 h) : aucun trou ne compte.
    late = _call(OPEN + 89 * 60) | {"envoye_a": OPEN + 90 * 60 - 10}
    res = K.settle(late, rows[:88] + rows[93:])
    assert K.activation(late) > late["until"] and not res["declenche"] and res["trous"] == []
    # Ordre jamais rempli, plus aucune transaction jusqu'à son expiration (11 h) : le trou final compte.
    quiet = minutes([(106.0, 106.1, 105.9, 106.0)] * 90)
    assert K.settle(_call(at), quiet[:40])["trous"] == [[OPEN + 40 * 60, OPEN + 90 * 60]]
    assert K.settle(_call(at), quiet[:86])["trous"] == [[OPEN + 86 * 60, OPEN + 90 * 60]]
    assert K.settle(_call(at), quiet[:87])["trous"] == []
    # Position gardée jusqu'à la clôture, plus aucune bougie après 15 h (suspendue jusqu'au bout).
    res = K.settle(_call(at), rows[:330])
    assert res["sortie"] == "cloture" and res["trous"] == [[OPEN + 330 * 60, _call(at)["close_ts"]]]


def test_indetermines_par_groupe(paths):
    """Quatrième avis de GPT : les résultats indéterminés jamais comptés 0 R ni tus, KILL et témoins."""
    k = book([1.0, -1.0, 0.5, 0.2, 0.0])
    k[0]["insoldable"] = True
    del k[1]["resultat"]
    k[2]["resultat"]["donnees_incompletes"] = True
    k[3]["resultat"]["a_lire"] = [OPEN]
    k[4]["resultat"]["trous"] = [[OPEN, OPEN + 600]]
    w = book([0.3], groupe="temoin_catalyseur")
    line = K.status_line([("KILL", k), ("témoin propre", w), ("autres témoins", [])], NOW)
    assert line == ("-# Indéterminés · KILL : 1 insoldable, 1 en attente, 1 soldé sur des données incomplètes, 1 avec "
                    "des minutes non lues, 1 avec 4 min ou plus sans transaction (suspension ou calme) · témoin "
                    "propre : rien d'indéterminé.")
    assert K.status_line([("KILL", book([1.0])), ("témoin propre", w)], NOW) is None
    two = book([1.0, -1.0])
    for c in two:
        c["insoldable"] = True
    assert "KILL : 2 insoldables" in K.status_line([("KILL", two)], NOW)


def test_stress_jamais_plus_doux_que_le_chiffre():
    """Au-dessus de 100 $, le glissement du labo dépasse 3 c : le stress « avec 3 c » le garde au minimum."""
    for entry in (10.0, 150.0, 400.0):
        c = {"entry": entry, "stop": entry * 0.97, "targets": [entry * 1.03, entry * 1.06, entry * 1.09]}
        res = {"declenche": True, "remplissage": entry, "eurusd": 1.15, "plan_ibkr": "fixe",
               "sorties": [{"prix": entry * 0.97, "part": 1.0, "raison": "stop"}]}
        assert K.stress_20e(c, res) <= K.real_r(c, res)
        measured = res | {"remplissage_mesure": True,
                          "sorties": [{"prix": entry * 0.97, "part": 1.0, "raison": "stop", "mesure": True}]}
        assert K.stress_20e(c, measured) < K.real_r(c, measured)
        cost = K.stop_cost(entry, entry * 0.97)  # le message du KILL suit la même règle
        assert cost["stress"] >= cost["reel"]


def test_ecart_apparie_par_jour(gross):
    """IC de l'écart KILL − témoin en grappes par jour communes : un jour de marché pèse sur les deux groupes. Ici
    les deux bougent ensemble, l'écart est net ; deux groupes indépendants donnaient [−1,84 ; +2,24]."""
    w = book([0.7, -1.2, 0.4, -0.7], groupe="temoin_catalyseur")
    for c in w:
        c["symbol"] = "W" + c["symbol"]
    g = K.call_gap(book([1.0, -1.0, 0.5, -0.5]) + w)
    assert g["m"] == pytest.approx(0.2) and g["ci"] == pytest.approx([0.0701, 0.3299], abs=1e-4)
    # Témoins tous expirés (0 R chacun, erreur type nulle) : l'IC vient des KILL seuls, il ne disparaît pas.
    for c in w:
        c["resultat"]["declenche"] = False
    g = K.call_gap(book([1.0, -1.0, 0.5, -0.5]) + w)
    assert g["m"] == pytest.approx(0.0) and g["ci"] == pytest.approx([-1.4524, 1.4524], abs=1e-4)
    # KILL sur 2 jours, témoins sur 6 : correction et t sur les 2 jours (G = 6 donnait un IC trop étroit).
    w = book([0.5, -0.5, 0.3, -0.3, 0.2, -0.2], groupe="temoin_catalyseur")
    for c in w:
        c["symbol"] = "W" + c["symbol"]
    g = K.call_gap(book([1.0, -1.0]) + w)
    assert g["ci"] == pytest.approx([-10.6979, 10.6979], abs=1e-4)  # avec G = 6 : ±8,29


def _fill_at(first, q0, tail_low=106.9):
    """Plan serré (entrée 106,51, stop 106,11, objectifs 106,91 / 107,31 / 107,71) : l'achat part sur une
    transaction à `first`, cotation en vigueur `q0` puis 106,92 / 106,94 (bid au TP1), puis le prix monte."""
    at, m = OPEN + 30 * 60, OPEN + 31 * 60
    call = _call(at, price=106.3) | {"entry": 106.51, "stop": 106.11, "targets": [106.91, 107.31, 107.71]}
    data = _ticks(m, [(10, first, "@", "C", ""), (500, 106.96, "@", "C", "")], quotes=[(20, 106.92, 106.94)],
                  q0=q0)
    rows = ([(106.3, 106.4, 106.2, 106.3)] * 31 + [(first, 106.96, first, 106.96), (106.96, 107.9, tail_low, 107.8)]
            + [(107.8,) * 4] * 300)
    res = K.settle(call, minutes(rows), ticks={str(m): data}, eurusd=1.15, plan_ibkr="fixe")
    return res, call | {"groupe": "kill", "resultat": res | {"eurusd": 1.15, "plan_ibkr": "fixe"}}


def test_stop_jamais_sur_la_transaction_de_l_achat():
    """Contre-vérification : transaction de l'achat pile à l'entrée, TP1 exécuté sur-le-champ (bids au TP1). Le stop
    remonté à l'entrée n'est actif qu'après : il attend une transaction suivante, il ne sort pas sur celle-là."""
    res, c = _fill_at(106.51, (106.91, 106.95))
    assert [e["raison"] for e in res["sorties"]] == ["tp1", "tp2", "tp3"] and not res["ambigus"]
    assert K.value(c) == K.value(c, "r_fav") > 0


def test_objectif_sur_la_transaction_de_l_achat_au_bid():
    """Un objectif exécuté sur la transaction de l'achat est un prix mesuré (le bid, jamais sous son prix ni
    au-dessus de la transaction), que la transaction soit à son prix ou en dessous (atteint seulement par le bid) :
    avant, en dessous, le bilan y ajoutait le glissement du labo."""
    at_tp, c_tp = _fill_at(106.91, (106.92, 106.95))
    below, c_below = _fill_at(106.90, (106.92, 106.95))
    for res in (at_tp, below):
        assert res["sorties"][0] == {"prix": 106.91, "part": pytest.approx(1 / 3), "raison": "tp1", "mesure": True}
    assert K.value(c_tp) == K.value(c_below)
