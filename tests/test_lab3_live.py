"""Labo 3 : la méthode championne appliquée au scanner en direct, avec le même code que le labo ;
JARVIS marche sans le labo (fichier absent, illisible, erreur d'évaluation)."""
import json
import logging
from datetime import date, datetime, timezone

import pytest

from app.lab import collector as CO
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
