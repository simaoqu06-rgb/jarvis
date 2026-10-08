"""JARVIS Killmode : règle de prix du labo (déclaration n° 5), piliers, interrupteur, journal et bilan."""
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
    assert ST.DECLARATION >= 5 and len(ST.trials()) == 143  # n° 5, sorties de Gemini (n° 6), GO (n° 7)
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
        "3 actions → TP3 112,57**" in txt and "remonte les SL restants à l'entrée" in txt
    assert "vends-le avant la clôture (aujourd'hui 22:00, 16 h New York)" in txt
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
    assert "ACME" in txt and "Minutes ambiguës" in txt


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
    win = K.reading(book([1.2, 0.8, 1.1] * 33 + [1.0]), None)
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
    assert "3 c de glissement par côté (hypothèse de Gemini, non mesurée)" in txt
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
    assert K.real_r(cheap, flat) == pytest.approx(-0.1 - 6 / 23, abs=1e-3)  # 2 demi-pas + 6 minimums de 1 $
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
    assert "1/1 KILL changent selon l'ordre" in line and "pire ordre (celui qui compte)" in line
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
