"""Labo 3 : la méthode championne appliquée au scanner en direct, avec le même code que le labo ;
JARVIS marche sans le labo (fichier absent, illisible, erreur d'évaluation)."""
import json
import logging
from datetime import date, datetime, timezone

import pytest

from app.lab import collector as CO
from app.lab import grade as GR
from app.lab import live as LIVE
from app.lab import method as M
from app.lab import regime as REG
from app.lab import universe as U
from app.sources import alpaca
from app.trading import catalyst as C
from app.trading import scanner
from app.trading import score as SC
from app.trading import service as T
from app.trading.settings import Settings
from tests.test_lab_generator import full_db  # noqa: F401  (fixture)
from tests.test_score import plan as score_plan

NOW = datetime(2026, 10, 7, 13, 10, tzinfo=timezone.utc)  # 9 h 10 à New York


def daily_bars(n: int, end: date, start: float = 50.0, step: float = 0.2, vol: float = 1e6) -> list[dict]:
    days = CO.trading_days(date(2023, 1, 3), end)[-n:]
    return [{"t": CO.ny_midnight(d), "o": start + i * step, "h": start + i * step + 1, "l": start + i * step - 1,
             "c": start + i * step + (0.3 if i % 3 else -0.6), "v": vol * (3 if i == n - 1 else 1)}
            for i, d in enumerate(days)]


def fake_plan(symbol: str) -> dict:
    s = Settings(None)
    p = score_plan()
    p["checklist"] |= {"score": 5, "max_score": 6, "trigger": False, "verdict": "Plan valable"}
    for item in p["checklist"]["items"]:
        item["detail"] = "cassé" if item["key"] == "pivot" else ""
    p["targets"] = [t | {"name": f"TP{i}", "price": 100.0 + i, "source": "pivot", "pct": float(i), "stop_rate": 0.3,
                         "minutes_median": 60 * i, "minutes_p25": 30, "minutes_p75": 90}
                    for i, t in enumerate(p["targets"], start=1)]
    p |= {"symbol": symbol, "stop_source": "swing", "stop_pct": -1.5, "created_at": NOW.isoformat(), "method": "m",
          "catalyst": {"news": [], "positive": 3}}
    p["score"] = SC.evaluate(p, None, None, s)
    return p


def write_champion(path, rules, version="v2"):
    path.write_text(json.dumps({"version": version, "regles": rules}), encoding="utf-8")


def rule(cond, kind="exclude", name="r"):
    return {"empreinte": "e" * 64, "nom": name, "kind": kind, "cond": cond, "markets": None}


@pytest.fixture
def champ_file(tmp_path, monkeypatch):
    path = tmp_path / "lab_champion.json"
    monkeypatch.setenv("JARVIS_LAB_CHAMPION", str(path))
    monkeypatch.setattr(LIVE, "_cache", {"key": None, "data": None})
    monkeypatch.setattr(LIVE, "_warned", set())
    monkeypatch.setattr(LIVE, "_index", {})
    return path


# --- Lecture du champion ----------------------------------------------------------------------

def test_sans_fichier_aucun_filtre_une_seule_ligne(champ_file, caplog):
    caplog.set_level(logging.WARNING)
    assert LIVE.current() == LIVE.EMPTY and LIVE.current() == LIVE.EMPTY
    assert caplog.text.count("absent, aucun filtre") == 1


@pytest.mark.parametrize("content", ["{pas du json", json.dumps({"version": "v2", "regles": [
    {"kind": "exclude", "cond": {"field": "result.r_net", "op": ">", "value": 1}}]})])
def test_fichier_illisible_aucun_filtre(champ_file, content):
    champ_file.write_text(content)
    assert LIVE.current() == LIVE.EMPTY


def test_fichier_relu_quand_il_change(champ_file):
    write_champion(champ_file, [rule({"field": "symbol", "op": "==", "value": "AAA"})])
    assert LIVE.current()["version"] == "v2"
    write_champion(champ_file, [], "v3-plus-long")
    assert LIVE.current() == {"version": "v3-plus-long", "regles": []}


def test_erreur_d_evaluation_tout_est_garde(champ_file, monkeypatch):
    monkeypatch.setattr(M, "evaluate_rows", lambda rules, rows: 1 / 0)
    out = LIVE.check([{"market": "stock"}], {"version": "v2", "regles": [rule({"field": "symbol", "op": "==", "value": "A"})]})
    assert out == [{"garde": True, "regles": [], "version": "v2"}]


def test_contexte_du_direct_comme_au_labo():
    daily = daily_bars(80, date(2026, 10, 6))
    spy = daily_bars(400, date(2026, 10, 6), start=400, step=0.5)
    p = fake_plan("AAA")
    row = LIVE.stock_row(p, None, daily, spy, NOW, Settings(None))
    day = date(2026, 10, 7)
    m = U._metrics([r for r in daily if r["t"] < CO.ny_midnight(day)])
    assert row["day"] == "2026-10-07" and row["planned_at"] == int(NOW.timestamp())
    assert row["context"]["rvol"] == m["rvol"] == 3.0 and row["context"]["day_change_pct"] == m["change_pct"]
    assert row["context"]["regime"] == U.regime(spy, CO.ny_midnight(day), 252)
    assert row["regime_label"] == REG.series(spy, "stock", [day])["2026-10-07"]["label"] != "inconnu"
    assert row["context"]["setup"] == "cassure_pivot" and row["context"]["catalyst_replayed"] is False
    # Note recalculée sans catalyseur, comme au labo.
    assert row["plan"]["score"]["parts"]["catalyst"]["score"] is None and row["composite"] == row["plan"]["score"]["composite"]
    assert set(row["plan"]) >= {"entry", "stop", "stop_pct", "targets", "score", "checklist"}


def test_regle_sur_le_regime_et_le_contexte_en_direct():
    daily = daily_bars(80, date(2026, 10, 6))
    spy = daily_bars(400, date(2026, 10, 6), start=400, step=0.5)
    row = LIVE.stock_row(fake_plan("AAA"), None, daily, spy, NOW, Settings(None))
    champ = {"version": "v2", "regles": [rule({"field": "regime_trend", "op": "==", "value": "haussier"}, name="haussier"),
                                         rule({"field": "context.rvol", "op": ">", "value": 2}, name="rvol")]}
    assert LIVE.check([row], champ)[0] == {"garde": False, "regles": ["haussier", "rvol"], "version": "v2"}


def test_verdict_sans_regle_ou_contexte_impossible(champ_file):
    assert LIVE.verdict_for({}, None, None, None, NOW, None)["garde"] is True
    champ = {"version": "v2", "regles": [rule({"field": "symbol", "op": "==", "value": "AAA"})]}
    assert LIVE.verdict_for({"pas": "un plan"}, None, None, None, NOW, None, champ)["garde"] is True


# --- Scanner pré-ouverture ----------------------------------------------------------------------

@pytest.fixture
def scan_env(monkeypatch, tmp_path):
    daily = {s: daily_bars(80, date(2026, 10, 6), start=50 + i) for i, s in enumerate(("AAA", "BBB", "CCC"))}
    spy = daily_bars(400, date(2026, 10, 6), start=400, step=0.5)
    calls = []

    async def bars(symbols, interval, days, now=None, feed="sip"):
        calls.append(tuple(symbols))
        return {"SPY": spy} if symbols == ["SPY"] else {s: daily[s] for s in symbols}

    async def snapshots(symbols):
        return {s: {"price": daily[s][-1]["c"] * 1.02} for s in symbols}

    async def universe(s):
        return ["AAA", "BBB", "CCC"]

    async def safe_check(sym, now):
        return None

    async def build_plan(symbol, *a, **k):
        return fake_plan(symbol)

    async def optional(symbol, market, interval, source=None, max_age=None):
        return daily[symbol]

    monkeypatch.setattr(alpaca, "configured", lambda: True)
    monkeypatch.setattr(alpaca, "bars", bars)
    monkeypatch.setattr(alpaca, "snapshots", snapshots)
    monkeypatch.setattr(scanner, "universe", universe)
    monkeypatch.setattr(scanner, "filter_reasons", lambda f, s: [])
    monkeypatch.setattr(C, "safe_check", safe_check)
    monkeypatch.setattr(T, "build_plan", build_plan)
    monkeypatch.setattr(T, "_optional_candles", optional)
    return calls


async def test_scanner_sans_labo_inchange(champ_file, scan_env):
    s = Settings(None)
    result = await scanner.scan(NOW, s, save=False)
    assert [p["symbol"] for p in result["picks"]] == ["AAA", "BBB", "CCC"][:s["scan_max_results"]]
    assert result["ecartees_methode"] == [] and result["methode"] is None
    assert ("SPY",) not in scan_env  # aucun appel de plus sans filtre
    assert "méthode" not in scanner.text(result)


async def test_scanner_applique_la_methode_championne(champ_file, scan_env):
    write_champion(champ_file, [rule({"field": "symbol", "op": "==", "value": "BBB"}, name="pas BBB"),
                                rule({"field": "regime", "op": "==", "value": "baissier_calme"}, name="régime")])
    result = await scanner.scan(NOW, Settings(None), save=False)
    assert "BBB" not in [p["symbol"] for p in result["picks"]] and "AAA" in [p["symbol"] for p in result["picks"]]
    assert result["ecartees_methode"] == [{"symbol": "BBB", "version": "v2", "regles": ["pas BBB"],
                                           "composite": result["ecartees_methode"][0]["composite"]}]
    assert ("SPY",) in scan_env
    assert "-# Écartées par la méthode v2 du labo : BBB (« pas BBB »)" in scanner.text(result)


async def test_scanner_continue_si_le_regime_manque(champ_file, scan_env, monkeypatch):
    write_champion(champ_file, [rule({"field": "symbol", "op": "==", "value": "CCC"}, name="pas CCC")])

    async def broken(symbols, interval, days, now=None, feed="sip"):
        if symbols == ["SPY"]:
            raise RuntimeError("Alpaca en panne")
        return {s: daily_bars(80, date(2026, 10, 6)) for s in symbols}
    monkeypatch.setattr(alpaca, "bars", broken)
    result = await scanner.scan(NOW, Settings(None), save=False)
    assert [e["symbol"] for e in result["ecartees_methode"]] == ["CCC"]


def test_regle_garder_sur_le_regime_sans_spy_ne_vide_pas_le_scanner():
    """Relecture : SPY indisponible en direct ne doit pas écarter tous les plans via une règle « keep »
    sur le régime (champ inconnu = non vérifié = écarté) ; ces règles ne s'appliquent pas, les autres oui."""
    daily = daily_bars(80, date(2026, 10, 6))
    champ = {"version": "v2", "regles": [
        rule({"field": "regime_trend", "op": "==", "value": "haussier"}, kind="keep", name="haussier seulement"),
        rule({"field": "context.regime.above_ma50", "op": "==", "value": True}, kind="keep", name="au-dessus MM50")]}
    for spy in (None, []):
        assert LIVE.verdict_for(fake_plan("AAA"), None, daily, spy, NOW, Settings(None), champ)["garde"] is True
    champ["regles"].append(rule({"field": "symbol", "op": "==", "value": "AAA"}, name="pas AAA"))
    out = LIVE.verdict_for(fake_plan("AAA"), None, daily, None, NOW, Settings(None), champ)
    assert out == {"garde": False, "regles": ["pas AAA"], "version": "v2"}
    # Avec SPY, la règle sur le régime s'applique bien.
    spy = daily_bars(400, date(2026, 10, 6), start=400, step=-0.05)
    out = LIVE.verdict_for(fake_plan("BBB"), None, daily, spy, NOW, Settings(None), champ)
    assert out["garde"] is False and "haussier seulement" in out["regles"]


async def test_scanner_montre_le_potentiel_plus_10(champ_file, scan_env, monkeypatch, tmp_path):
    """Lot B : sous chaque plan, la part des séances du labo au même gap qui ont touché +8 % (mesure seule)."""
    from app.lab import potentiel as PO

    path = tmp_path / "lab_potentiel.json"
    path.write_text(json.dumps({"par_gap": {"gap +2 à +5 %": {"n": 120, "zone_pct": 9.2, "plus10_pct": 4.1}}}),
                    encoding="utf-8")
    monkeypatch.setenv("JARVIS_LAB_POTENTIEL", str(path))
    PO._cache.update(key=None, data=None)
    result = await scanner.scan(NOW, Settings(None), save=False)
    assert result["picks"] and all(p["potentiel"] for p in result["picks"])   # gap de pré-ouverture +2 %
    assert ("🎯 Potentiel +10 : 9,2 % des séances à gap +2 à +5 % ont touché +8 % depuis l'ouverture "
            "(4,1 % +10 %), sur 120 séances du labo") in scanner.text(result)


# --- Tous les plans (A5) : `!plan`, `!prends`, API, crypto ---------------------------------------

GRADE_A = {"marches": {"stock": {"grade": "A", "rang": GR.rank_of("A", 1), "non_note": False, "risque_grade": 3.0,
                                 "risque_max": 3.0, "mode_survie": False}},
           "verifie_le": NOW.isoformat()}


def crypto_daily(n: int, end: date, start: float = 60000.0, step: float = 50.0) -> list[dict]:
    """Bougies journalières crypto (minuit UTC, tous les jours), jusqu'à `end` inclus."""
    first = CO.utc_midnight(end) - (n - 1) * 86400
    return [{"t": first + i * 86400, "o": start + i * step, "h": start + i * step + 100,
             "l": start + i * step - 100, "c": start + i * step + (30 if i % 3 else -60), "v": 1e3}
            for i in range(n)]


async def test_plan_ecarte_par_la_methode_sans_taille_mais_affiche(champ_file, monkeypatch):
    """Le grade mesure les trades que garde le champion : un plan qu'il écarte ne reçoit pas le risque du grade
    (0 %, taille None), la raison est dite sur les lignes 🏅 et 💰, et le plan reste affiché."""
    write_champion(champ_file, [rule({"field": "symbol", "op": "==", "value": "AAA"}, name="pas AAA")])
    monkeypatch.setattr(GR, "current", lambda: GRADE_A)
    monkeypatch.setenv("JARVIS_CAPITAL", "10000")
    daily = daily_bars(80, date(2026, 10, 6))
    p = fake_plan("AAA") | {"entry": 10.0, "stop": 9.5}
    verdict = await LIVE.plan_verdict(p, daily, NOW, Settings(None))
    assert verdict == {"garde": False, "regles": ["pas AAA"], "version": "v2"}
    T.apply_grade(p, None, None, NOW, trades=[], usd_per_eur=1.15, methode=verdict)
    g = p["grade"]
    assert p["sizing"] is None and p["methode"] == verdict
    assert g["risque_propose"] == 0.0 and g["ecarte"] and g["grade"] == "A" and g["risque_grade"] == 3.0
    assert g["raison"] == "écarté par la méthode v2 (règle « pas AAA »)"
    assert GR.plan_line(g) == "🏅 Grade JARVIS **A** · risque proposé **0 %** : plan écarté par la méthode v2 (règle « pas AAA »)"
    assert T.sizing_lines(p) == ["💰 JARVIS ne propose pas de taille : plan écarté par la méthode v2 (règle « pas AAA »). "
                                 "Ta règle de 20 € : 46 actions pour 460 $ (exception, hors avis de JARVIS)"]
    text = T.plan_text(p | {"targets": [], "checklist": p["checklist"] | {"items": []}})
    assert "**🟢 LONG AAA**" in text and "plan écarté par la méthode v2" in text and "Ta règle de 20 €" in text
    # Gardé : le risque du grade (3 % de 10 000 €, plafonné à 20 €), rien d'écarté.
    q = fake_plan("BBB") | {"entry": 10.0, "stop": 9.5}
    verdict = await LIVE.plan_verdict(q, daily, NOW, Settings(None))
    T.apply_grade(q, None, None, NOW, trades=[], usd_per_eur=1.15, methode=verdict)
    assert verdict["garde"] and q["sizing"]["risk_amount_eur"] == 20.0 and not q["grade"].get("ecarte")
    assert q["grade"]["raison"] == "grade A"
    # Plusieurs règles ; risque demandé explicitement : gardé, hors budget (JARVIS ne propose rien).
    r = fake_plan("AAA") | {"entry": 10.0, "stop": 9.5}
    T.apply_grade(r, None, 1.0, NOW, trades=[], usd_per_eur=1.15,
                  methode={"garde": False, "regles": ["a", "b"], "version": "v3"})
    assert r["grade"]["raison"] == "écarté par la méthode v3 (règles « a », « b »)"
    assert r["sizing"]["risk_amount_eur"] == 20.0 and r["grade"]["hors_budget"]


async def test_plan_verdict_rien_a_evaluer(champ_file, monkeypatch, caplog):
    """None (aucun effet) sans champion, sans règle sur le marché du plan, ou pour un short (le labo ne mesure
    que des longs) ; une erreur garde le plan avec une ligne de journal, jamais d'exception."""
    caplog.set_level(logging.WARNING)
    daily = daily_bars(80, date(2026, 10, 6))
    assert await LIVE.plan_verdict(fake_plan("AAA"), daily, NOW) is None
    crypto_only = rule({"field": "symbol", "op": "==", "value": "AAA"}, name="pas AAA") | {"markets": ["crypto"]}
    write_champion(champ_file, [crypto_only])
    assert await LIVE.plan_verdict(fake_plan("AAA"), daily, NOW) is None
    write_champion(champ_file, [rule({"field": "symbol", "op": "==", "value": "AAA"}, name="pas AAA")])
    assert await LIVE.plan_verdict(fake_plan("AAA") | {"side": "short"}, daily, NOW) is None
    monkeypatch.setattr(M, "market_rules", lambda rules, market: 1 / 0)
    for _ in range(2):
        assert await LIVE.plan_verdict(fake_plan("AAA"), daily, NOW) == {"garde": True, "regles": [], "version": "v2"}
    assert caplog.text.count("verdict du plan impossible") == 1


async def test_plan_verdict_regime_absent_plan_garde_une_ligne(champ_file, monkeypatch, caplog):
    """SPY (actions) ou BTC (crypto) indisponible : les règles sur le régime ne s'appliquent pas, les autres oui,
    une seule ligne de journal par indice."""
    caplog.set_level(logging.WARNING)
    write_champion(champ_file, [
        rule({"field": "regime_trend", "op": "==", "value": "haussier"}, kind="keep", name="haussier seulement"),
        rule({"field": "symbol", "op": "==", "value": "ETH-USDT"}, name="pas ETH")])
    calls = []

    async def no_spy(now):
        calls.append("SPY")
        return None

    async def no_btc(symbol, market, interval, source=None, max_age=None):
        calls.append(symbol)
        return None

    monkeypatch.setattr(scanner, "_index_daily", no_spy)
    monkeypatch.setattr(T, "_optional_candles", no_btc)
    daily = daily_bars(80, date(2026, 10, 6))
    for _ in range(2):
        assert (await LIVE.plan_verdict(fake_plan("AAA"), daily, NOW))["garde"] is True
        assert (await LIVE.plan_verdict(fake_plan("BTC") | {"market": "crypto"}, None, NOW))["garde"] is True
    out = await LIVE.plan_verdict(fake_plan("ETH") | {"market": "crypto"}, None, NOW)
    assert out == {"garde": False, "regles": ["pas ETH"], "version": "v2"}
    assert caplog.text.count("régime de marché (SPY) indisponible") == 1
    assert caplog.text.count("régime de marché (BTC) indisponible") == 1
    assert calls.count("SPY") == 2 and "BTC" in calls  # réessayé : une panne n'est pas gardée pour la journée


def test_ligne_crypto_au_nom_du_labo_avec_le_regime_de_btc():
    """Plan crypto du direct (« BTC », Kraken) : la ligne de lessons.crypto_row, le symbole du labo (« BTC-USDT »)
    et le régime de BTC comme generator.crypto_day."""
    from app.lab import lessons as L

    day = date(2026, 10, 7)
    btc = crypto_daily(600, day)  # bougie du jour en cours comprise : ignorée, comme au labo
    daily = crypto_daily(60, day, start=3000.0, step=5.0)
    p = fake_plan("ETH") | {"market": "crypto"}
    row = LIVE.crypto_row(p, daily, btc, NOW)
    base = L.crypto_row(p, daily, NOW)
    assert row["symbol"] == "ETH-USDT" and LIVE.lab_symbol("btc-usdt") == "BTC-USDT"
    assert {k: v for k, v in row.items() if not k.startswith("regime") and k not in ("symbol", "context")} == \
        {k: v for k, v in base.items() if k not in ("symbol", "context")}
    assert row["context"] == base["context"] | {"regime": U.regime(btc, CO.utc_midnight(day), 365)}
    assert row["context"]["regime"] is not None
    label = REG.series(btc, "crypto", [day])["2026-10-07"]
    assert (row["regime_label"], row["regime_trend"], row["regime_vol_tercile"]) == \
        (label["label"], label["trend"], label["vol_tercile"]) and row["regime_trend"] == "haussier"
    # Sans BTC : pas de régime (les règles qui le lisent ne s'appliquent pas, voir plus haut).
    assert "regime_label" not in LIVE.crypto_row(p, daily, None, NOW)


async def test_plan_crypto_ecarte_sur_le_regime_de_btc(champ_file, monkeypatch):
    """Règle du champion sur le régime, évaluée en direct sur les bougies journalières de BTC (Kraken), lues une
    fois par jour UTC."""
    write_champion(champ_file, [rule({"field": "regime_trend", "op": "==", "value": "haussier"}, name="pas en hausse")
                                | {"markets": ["crypto"]}])
    btc = crypto_daily(600, date(2026, 10, 7))
    calls = []

    async def kraken(symbol, market, interval, source=None, max_age=None):
        calls.append((symbol, market, interval))
        return btc

    monkeypatch.setattr(T, "_optional_candles", kraken)
    for _ in range(2):
        out = await LIVE.plan_verdict(fake_plan("SOL") | {"market": "crypto"}, None, NOW)
        assert out == {"garde": False, "regles": ["pas en hausse"], "version": "v2"}
    assert calls == [("BTC", "crypto", 1440)]
    assert await LIVE.plan_verdict(fake_plan("AAA"), None, NOW) is None  # règle crypto seulement


def test_build_plan_applique_la_methode_a_tous_les_plans(champ_file, full_db, monkeypatch):  # noqa: F811
    """build_plan (`!plan`, `!prends`, API) passe par le même filtre que le scanner : plan écarté → 0 % proposé,
    plan affiché ; sans règle qui le touche, plan inchangé."""
    import asyncio

    from app.lab import generator as G
    from tests.test_lab_generator import DAY as GEN_DAY

    inp = G.stock_inputs(full_db, "AAA", GEN_DAY)
    now = datetime.fromtimestamp(U.stock_decision_ts(GEN_DAY), timezone.utc)
    by_interval = {15: inp["bars"], 1440: inp["daily"], 240: inp["h4"]}

    async def fake_candles(symbol, market, interval, source=None, max_age=None, outputsize=None):
        return by_interval[interval]

    async def no_cat(symbol, now):
        return None

    monkeypatch.setattr(T, "candles", fake_candles)
    monkeypatch.setattr(T.C, "safe_check", no_cat)
    monkeypatch.setattr(GR, "current", lambda: GRADE_A | {"verifie_le": now.isoformat()})
    monkeypatch.setenv("JARVIS_CAPITAL", "10000")
    write_champion(champ_file, [rule({"field": "symbol", "op": "==", "value": "AAA"}, name="pas AAA")])
    plan = asyncio.run(T.build_plan("AAA", "stock", now=now))
    assert plan["methode"] == {"garde": False, "regles": ["pas AAA"], "version": "v2"}
    assert plan["sizing"] is None and plan["grade"]["risque_propose"] == 0.0 and plan["sizing_regle"]["hors_grade"]
    text = T.plan_text(plan)
    assert "**🟢 LONG AAA**" in text and "🏅 Grade JARVIS **A** · risque proposé **0 %** : plan écarté par la " \
        "méthode v2 (règle « pas AAA »)" in text
    write_champion(champ_file, [rule({"field": "symbol", "op": "==", "value": "BBB"}, name="pas BBB")])
    plan = asyncio.run(T.build_plan("AAA", "stock", now=now))
    assert plan["methode"]["garde"] and plan["sizing"]["risk_amount_eur"] == 20.0
    write_champion(champ_file, [])
    assert "methode" not in asyncio.run(T.build_plan("AAA", "stock", now=now))


async def test_scanner_reprend_le_verdict_du_plan(champ_file, scan_env, monkeypatch):
    """Le scanner trie avec le verdict que build_plan a mis sur le plan (le même que le plan affiché)."""
    write_champion(champ_file, [rule({"field": "symbol", "op": "==", "value": "ZZZ"}, name="pas ZZZ")])

    async def build_plan(symbol, *a, **k):
        p = fake_plan(symbol)
        if symbol == "BBB":
            p["methode"] = {"garde": False, "regles": ["vu par build_plan"], "version": "v2"}
        return p

    monkeypatch.setattr(T, "build_plan", build_plan)
    result = await scanner.scan(NOW, Settings(None), save=False)
    assert [e["regles"] for e in result["ecartees_methode"]] == [["vu par build_plan"]]
    assert "BBB" not in [p["symbol"] for p in result["picks"]]


async def test_prends_go_garde_la_taille_du_go_confirme(champ_file, full_db, monkeypatch):  # noqa: F811
    """`!prends … go` : le GO confirmé a été gardé par le champion au scanner ; le plan recalculé au déclenchement
    (stop plus large) n'est pas refiltré, il garde la taille du MP de confirmation et compte avec JARVIS, pas « à
    ta règle » dans `!desaccords`. `!prends` sans `go` passe toujours par le filtre."""
    from app import scheduler
    from app.lab import generator as G
    from app.notify import bot
    from app.trading import confirm as CF
    from tests.test_lab_generator import DAY as GEN_DAY

    inp = G.stock_inputs(full_db, "AAA", GEN_DAY)
    now = datetime.fromtimestamp(U.stock_decision_ts(GEN_DAY), timezone.utc)
    by_interval = {15: inp["bars"], 1440: inp["daily"], 240: inp["h4"]}

    async def fake_candles(symbol, market, interval, source=None, max_age=None, outputsize=None):
        return by_interval[interval]

    async def no_cat(symbol, now):
        return None

    monkeypatch.setattr(T, "candles", fake_candles)
    monkeypatch.setattr(T.C, "safe_check", no_cat)
    monkeypatch.setattr(GR, "current", lambda: GRADE_A | {"verifie_le": now.isoformat()})
    monkeypatch.setenv("JARVIS_CAPITAL", "10000")
    real_build = T.build_plan
    p0 = await real_build("AAA", "stock", now=now)
    tp1 = p0["targets"][0]["price"]
    trigger = round(p0["entry"] + 0.9 * (tp1 - p0["entry"]), 2)  # déclenché sous TP1, stop plus large
    thr = (p0["stop_pct"] - (trigger - p0["stop"]) / trigger * 100) / 2
    write_champion(champ_file, [rule({"field": "plan.stop_pct", "op": "<", "value": thr}, name="stop trop large")])
    p0 = await real_build("AAA", "stock", now=now)
    assert p0["methode"]["garde"]  # le plan du scan est gardé : c'est un pick
    g = GR.risk_now("stock", [], now)
    risk = CF.rule_size(g, T.recommended_eur(g["risque_propose"]), T.rule_eur())
    targets = [t["price"] for t in p0["targets"]]
    mp = CF.event_text("AAA", {"stop": p0["stop"], "targets": targets, "entry": p0["entry"]},
                       {"kind": "confirmé", "trigger": trigger, "vwap": p0["entry"]}, now, risk, 1.15, g)
    book = T.Book(None)
    monkeypatch.setattr(scheduler, "book", book)
    today = datetime.now(timezone.utc).astimezone(T.NEW_YORK).date().isoformat()
    monkeypatch.setattr(scheduler, "confirm_state", {"day": today, "closed": False, "gos": {
        "AAA": {"status": "confirmé", "entry": p0["entry"], "stop": p0["stop"], "targets": targets,
                "trigger": trigger, "composite": p0["score"]["composite"], "last_bar": None, "events": []}}})

    async def build_at_now(*a, **k):
        return await real_build(*a, **(k | {"now": now}))

    monkeypatch.setattr(bot.trading, "build_plan", build_at_now)
    txt = await bot.handle("!prends AAA go")
    tr = book.trades[-1]
    assert "methode" not in tr and not tr["grade"].get("ecarte") and "plan écarté" not in txt
    assert f"**{tr['sizing']['quantity']} actions**" in mp  # la taille du MP de confirmation
    assert not T.at_rule(tr) and not T.against_jarvis(tr)
    st = T.disagreements(book)
    assert st["regle"]["total"] == 0 and st["avec"]["total"] == 1
    # Sans `go`, mêmes prix : le plan passe par le filtre de la méthode, comme tous les plans.
    txt = await bot.handle(f"!prends AAA {trigger:g} stop {p0['stop']:g}")
    tr = book.trades[-1]
    assert tr["methode"] == {"garde": False, "regles": ["stop trop large"], "version": "v2"} and tr["sizing"] is None
    assert "plan écarté par la méthode v2 (règle « stop trop large »)" in txt and T.at_rule(tr)
