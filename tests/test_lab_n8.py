"""Labo v2, déclaration n° 8 : coûts (D2 stress de 3 cents et mise de Simão, D3 demi-pas), jugement en t (D4),
variantes FRSH du bras confirmé, résultats publiés depuis la veille et Killmode complet rejoué sur l'archive des
actus (D1)."""
import json
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from app.core.http import SourceError
from app.lab import archive as ARC
from app.lab import collector as CO
from app.lab import cout as COUT
from app.lab import empreinte as E
from app.lab import kill_complet as KC
from app.lab import simulator as SIM
from app.lab import sources as SRC
from app.lab import stats as STATS
from app.lab import strat_stats as SS
from app.lab import strategies as ST
from app.lab import worker as W
from app.lab.db import LabDB
from app.trading import killmode as K
from tests.test_killmode import RANGE, RUN, daily, hist
from tests.test_lab_n7_confirmation import CONFIRMED, PLAN, at, by_name, make
from tests.test_lab_strategies import DAY, OPEN, _rows, bars_from

TARGETS = [103.0, 105.0, 107.0]


# --- D3 : demi-pas ---------------------------------------------------------------------------------

def test_glissement_jamais_sous_un_demi_pas():
    assert SIM.costs("stock", 2.0)["slippage"] == pytest.approx(0.005 / 2.0)    # 15 pb = 0,3 c < 0,5 c
    assert SIM.costs("stock", 6.0)["slippage"] == pytest.approx(0.005 / 6.0)    # 6 pb = 0,36 c < 0,5 c
    assert SIM.costs("stock", 10.0)["slippage"] == pytest.approx(6 / 10000)     # 0,6 c : le palier reste
    assert SIM.costs("stock", 50.0)["slippage"] == pytest.approx(3 / 10000)
    assert SIM.costs("stock", 0.5)["slippage"] == pytest.approx(40 / 10000)     # 2 c ≫ un demi-pas de 0,0001 $
    assert SIM.costs("crypto", 2.0, 3)["slippage"] == pytest.approx(3 / 10000)  # crypto inchangée
    for price in (0.5, 1.5, 2.0, 4.0, 6.0, 7.5, 12.0, 30.0):
        assert SIM.stock_slippage(price) * price == pytest.approx(K._half_tick_slip(price))  # même règle que Killmode


def _trade(db, table, tid, entry, stop, target, old_bps, new_format=True, side="long"):
    old = {"slippage": old_bps / 10000, "maker": 0.0, "taker": 0.0}
    result = {"triggered": True, "fill_price": entry, "order_prices": {"entry": entry, "stop": stop, "targets": [target]},
              "exits": [{"price": target, "part": 1.0, "fee": 0.0, "reason": "tp1", "at": 1}],
              "slippage_bps": old_bps, "entry_liquidity": "taker" if new_format else None}
    result["r_net"] = SIM.net_r(result | {"entry_liquidity": "taker"}, old, side)
    db.conn.execute(f"INSERT INTO {table} (id, origin, method_version, market, symbol, side, day, planned_at, result, "
                    "triggered, r_net) VALUES (?, 'replay', 'v1', 'stock', ?, ?, '2025-11-04', ?, ?, 1, ?)",
                    (tid, f"S{tid}", side, tid, json.dumps(result), result["r_net"]))
    return result


def test_trades_v1_recalcules_une_fois_au_demi_pas():
    db = LabDB(":memory:")
    cheap = _trade(db, "trades", 1, 2.0, 1.9, 2.2, 15.0)              # 1-3,33 $ : 0,3 c → 0,5 c
    dear = _trade(db, "trades", 2, 50.0, 49.0, 52.0, 3.0)             # inchangé
    _trade(db, "trades", 3, 2.0, 1.9, 2.2, 15.0, new_format=False)    # ancien format : laissé tel quel
    short = _trade(db, "trades_sealed", 4, 6.0, 6.2, 5.6, 6.0, side="short")
    db.set("stats", "signature", [3, 3])
    assert COUT.step(db, limit=2) == 1 and db.get("stats", "signature") == [3, 3]    # pas fini : caches gardés
    while not db.get(COUT.JOB, "fini"):
        COUT.step(db, limit=2)
    rows = dict(db.conn.execute("SELECT id, r_net FROM trades"))
    assert rows[1] == SIM.net_r(cheap, SIM.costs("stock", 2.0)) < cheap["r_net"]
    assert rows[2] == dear["r_net"] and rows[3] is not None
    stored = json.loads(db.conn.execute("SELECT result FROM trades WHERE id = 1").fetchone()[0])
    assert stored["slippage_bps"] == 25.0 and stored["r_net"] == rows[1]
    sealed = db.conn.execute("SELECT r_net FROM trades_sealed WHERE id = 4").fetchone()[0]
    assert sealed == SIM.net_r(short, SIM.costs("stock", 6.0), "short") < short["r_net"]
    assert db.get("stats", "signature") is None and db.get("grade_calc", "signature") is None
    assert COUT.step(db) == 0                                                         # une seule fois


# --- D2 : stress de 3 cents et mise de Simão ---------------------------------------------------------

def test_stress_de_3_cents_et_mise_de_simao():
    d = make(CONFIRMED)
    conf = by_name(ST.go_rows(d, "AAA", {"origine": PLAN}, 3))["go_confirme"]
    p = ST._plan(101.01, 98.0, TARGETS)
    res = ST._sim(d, p, OPEN + 20 * 60, at(11, 30), d.close_ts, SIM.costs("stock", 101.01, 3), "stop", 100.9,
                  exit_mode="sans_be")
    v = ST.cost_variants("stock", res)
    assert conf["r_3c"] == v["r_3c"] and conf["r_20e"] == v["r_20e"]
    fill = res["fill_price"] + 0.03
    gain = sum(e["part"] * (e["price"] - (0 if e["reason"].startswith("tp") else 0.03) - fill) for e in res["exits"])
    assert v["r_3c"] == pytest.approx(gain / 3.01 - SS.ibkr_commission_r(101.01, 98.0), abs=1e-4)
    assert v["r_20e"] < conf["r_sans_be"] - 0.2       # 6 ordres à 1 $ minimum pour ~23 $ de risque
    none = dict.fromkeys(("r_3c", "r_20e", "r_20e_deg", "r_20e_1s", "r_20e_2s"))  # n° 10 : variantes descriptives
    assert ST.cost_variants("crypto", res) == none
    assert ST.cost_variants("stock", {"triggered": False, "exits": []}) == none


def test_candidate_exige_le_stress_de_3_cents():
    db = LabDB(":memory:")
    _rows(db, "pullback", 0.3, -0.2)
    item = SS.report(db.conn)["strategies"]["stock"]["pullback"]
    assert item["candidate"] and set(item["robuste"]) >= {"r_3c"} and "r_3c" in item["robustesse"]
    assert item["mise_20e"]["esperance_r"] == pytest.approx(0.2, abs=0.01) and item["rentable_20e"]
    assert item["candidate_compte"] and f"\n✅ {item['nom']} :" in SS.text(SS.report(db.conn))
    db.conn.execute("UPDATE strat_trades SET r_20e = -0.05")
    rep = SS.report(db.conn)
    item = rep["strategies"]["stock"]["pullback"]
    # n° 10 (L3, voie B) : « candidate » garde sa définition (la mise de Simão n'en décide pas), mais le ✅ demande
    # aussi une espérance positive à 20 € (candidate_compte) ; r_20e est affiché avec la robustesse, hors de « tient ».
    assert item["candidate"] and not item["rentable_20e"] and not item["candidate_compte"]
    assert item["robuste"]["r_20e"] < 0 and item["robuste"]["tient"]
    assert f"\n☑️ avantage, perd à 20 € · {item['nom']} :" in SS.text(rep) and f"✅ {item['nom']}" not in SS.text(rep)
    db.conn.execute("UPDATE strat_trades SET r_3c = -0.1")
    item = SS.report(db.conn)["strategies"]["stock"]["pullback"]
    assert not item["candidate"] and item["robuste"]["r_3c"] < 0 and not item["robuste"]["tient"]
    assert SS.robust("crypto") == SS.ROBUST and "r_3c" in SS.robust("stock")
    assert "à 20 € -0,05 R" in SS.text(SS.report(db.conn)).replace("−", "-")


# --- D4 : jugement en t sur les jours ----------------------------------------------------------------

def test_jugement_en_t_sur_les_jours():
    assert STATS.t_quantile(0.975, 10) == pytest.approx(2.228, abs=1e-3)
    assert STATS.t_quantile(0.975, 30) == pytest.approx(2.042, abs=1e-3)
    assert STATS.t_quantile(0.975, 0) == math.inf
    for p, values in K.T_TABLE.items():                       # table exacte de Killmode
        for df, q in enumerate(values, start=1):
            assert STATS.t_quantile(p, df) == pytest.approx(q, rel=2e-3)
    assert SS.candidate_t(30, 148) == 3.81 and SS.candidate_t(100, 148) == 3.51 and SS.candidate_t(1) is None
    assert SS.candidate_t(10**6, 148) == pytest.approx(SS.candidate_z(148), abs=0.01)
    assert SS.passes(3.5, 3.4) and not SS.passes(None, 3.4) and not SS.passes(9.0, None)
    m = SS.Mean()
    for day, r in (("a", 1.0), ("b", 2.0), ("c", 3.0)):
        m.add(day, r)
    res = m.result()
    se = (res["ic95"][1] - res["ic95"][0]) / 2 / 4.303                    # IC à 2 degrés de liberté
    assert se == pytest.approx(2.0 / res["z"], rel=3e-3)                  # z arrondi au centième
    helps = SS._helps({"esperance_r": 0.2, "z": 3.6, "n": 40, "jours": 30}, 148)
    assert helps["seuil_t"] == 3.81 and not helps["aide"]                 # 3,6 suffisait en loi normale
    assert SS._helps({"esperance_r": 0.2, "z": 3.6, "n": 40, "jours": 1000}, 148)["aide"]


# --- Variantes FRSH du bras confirmé ---------------------------------------------------------------

RISING = [(100.0, 100.5, 99.9, 100.4), (100.4, 100.9, 100.3, 100.8), (100.8, 101.3, 100.7, 101.2),
          (101.2, 102.5, 101.1, 102.4)]


def _spy(close_k: float) -> list[dict]:
    return bars_from(OPEN, [(500.0, 500.2, 499.8, 500.0)] * 3 + [(500.0, 501.0, 498.0, close_k)])


def test_filtres_lus_a_la_bougie_de_confirmation():
    d = make(CONFIRMED)
    k = d.d0 + 3                                          # bougie de 9 h 45, celle de la confirmation
    assert ST.go_filter(d, "v2", k) is False             # VWAP qui baisse depuis l'ouverture
    assert ST.go_filter(d, "v3", k) is False             # 100,9 sous le plus haut de 9 h 30 (101,5)
    assert ST.go_filter(d, "v4", k) is None              # sans SPY : pas jugeable
    assert ST.go_filter(d, "v3", d.d0) is None           # première bougie : rien à casser
    d.index_move = ST.index_move(_spy(499.0), d.start_ts)   # SPY −0,2 %, l'action −0,1 %
    assert ST.go_filter(d, "v4", k) is True
    d.index_move = ST.index_move(_spy(500.5), d.start_ts)   # SPY +0,1 %
    assert ST.go_filter(d, "v4", k) is False
    up = make(RISING)
    assert ST.go_filter(up, "v2", up.d0 + 3) is True and ST.go_filter(up, "v3", up.d0 + 3) is True
    assert ST.index_move(_spy(499.0)[1:], d.start_ts) is None   # SPY sans sa bougie d'ouverture
    with pytest.raises(ValueError):
        ST.go_filter(d, "v9", k)


def test_bras_filtres_meme_ordre_ou_rien():
    d = make(CONFIRMED)
    rows = by_name(ST.go_rows(d, "AAA", {"origine": PLAN}, 3))
    assert {n: rows[n]["confirmation"] for n in ("go_confirme_v2", "go_confirme_v3", "go_confirme_v4")} == {
        "go_confirme_v2": "filtre", "go_confirme_v3": "filtre", "go_confirme_v4": "indetermine"}
    for n in ("go_confirme_v2", "go_confirme_v3", "go_confirme_v4"):
        assert rows[n]["triggered"] == 0 and rows[n]["entry"] == rows["go_confirme"]["entry"]
    broke = [(101.0, 101.1, 100.6, 100.8), (100.8, 100.9, 99.8, 100.0), CONFIRMED[2], (99.9, 101.3, 99.8, 101.2)] \
        + CONFIRMED[4:]
    rows = by_name(ST.go_rows(make(broke), "AAA", {"origine": PLAN}, 3))
    v3, conf = rows["go_confirme_v3"], rows["go_confirme"]
    assert v3["confirmation"] == "confirme" and v3["triggered"] == 1
    assert v3["r_sans_be"] == conf["r_sans_be"] and v3["entry"] == conf["entry"] == 101.31
    tp1_first = [(101.0, 103.5, 100.9, 102.0)] + CONFIRMED[1:]          # GO annulé : aucun bras filtré ne trade
    rows = by_name(ST.go_rows(make(tp1_first), "AAA", {"origine": PLAN}))
    assert {rows[n]["confirmation"] for n in ("go_confirme_v2", "go_confirme_v3", "go_confirme_v4")} == {"annule"}
    floor = {"entry": 100.0, "stop": 97.0, "targets": [{"price": 104.0}, {"price": 106.0}, {"price": 108.0}]}
    names = {r["strategy"] for r in ST.go_rows(make(CONFIRMED), "AAA", {"plancher": floor})}
    assert names == {"go_brut_plancher", "go_confirme_plancher"}      # filtres : stop d'origine seulement


def _go_db(days: list[date]) -> LabDB:
    """GO par jour : brut −0,1, confirmé +0,2 ; V3 garde un GO sur deux (+0,5), V4 les refuse tous, V2 ne peut pas
    juger ; résultats publiés avant l'ouverture un jour sur quatre (donc aussi « depuis la veille » le lendemain)."""
    db = LabDB(":memory:")
    for k, day in enumerate(days):
        arms = {"go_brut": ("confirme", -0.1), "go_confirme": ("confirme", 0.2),
                "go_confirme_v2": ("indetermine", None), "go_confirme_v4": ("filtre", None),
                "go_confirme_v3": ("confirme", 0.5) if k % 2 == 0 else ("filtre", None)}
        for name, (status, r) in arms.items():
            db.conn.execute("INSERT INTO strat_trades (origin, market, symbol, day, strategy, slot, triggered, "
                            "r_sans_be, confirmation) VALUES ('replay', 'stock', 'S', ?, ?, 0, ?, ?, ?)",
                            (day.isoformat(), name, int(r is not None), r, status))
    first, last = CO.previous_trading_day(days[0]), days[-1]
    for n in range((last - first).days + 1):
        x = first + timedelta(days=n)
        items = [{"symbol": "S", "date": x.isoformat(), "hour": "bmo"}] if x in days[::4] else []
        db.conn.execute("INSERT INTO earnings_days (day, status, n, items) VALUES (?, 'ok', ?, ?)",
                        (x.isoformat(), len(items), json.dumps(items)))
    return db


def test_tableau_des_filtres_et_des_resultats_publies():
    days = CO.trading_days(date(2025, 11, 3), date(2026, 1, 9))[:40]
    rep = SS.report(_go_db(days).conn)
    res = rep["confirmation_go"]["origine"]
    v3, v4, v2 = res["filtres"]["v3"], res["filtres"]["v4"], res["filtres"]["v2"]
    assert v3["statuts"] == {"confirmé": 20, "refusé par le filtre": 20}
    assert v3["ecart_au_confirme"]["esperance_r"] == pytest.approx(0.05) and v3["ecart_au_confirme"]["n"] == 40
    assert v3["ecart_au_brut"]["esperance_r"] == pytest.approx(0.35)
    assert v4["ecart_au_confirme"]["esperance_r"] == pytest.approx(-0.2) and not v4["ecart_au_confirme"]["aide"]
    assert v2["statuts"] == {"filtre non jugeable": 40} and v2["ecart_au_confirme"]["n"] == 0
    split = res["par_resultats"]
    assert split["résultats publiés depuis la veille"]["go"] == 20 and split["sans résultats publiés"]["go"] == 20
    assert split["sans résultats publiés"]["confirme"]["esperance_r"] == pytest.approx(0.2)
    text = SS.text(rep)
    assert "V3, cassure du plus haut de séance : filtré − confirmé +0,05 R par GO" in text and "20 refusés" in text
    assert "résultats publiés depuis la veille 20" in text


def test_resultats_publies_depuis_la_veille():
    db = LabDB(":memory:")
    assert ARC.results_lookup(db.conn)("AMC", "2025-11-04") is None              # rien d'archivé
    calendar = {"2025-11-03": [{"symbol": "AMC", "hour": "amc"}, {"symbol": "HIER", "hour": "dmh"}],
                "2025-11-04": [{"symbol": "BMO", "hour": "bmo"}, {"symbol": "DMH", "hour": "dmh"}],
                "2025-11-07": [{"symbol": "VEN", "hour": "amc"}], "2025-11-10": []}
    for x, items in calendar.items():
        db.conn.execute("INSERT INTO earnings_days (day, status, items) VALUES (?, 'ok', ?)", (x, json.dumps(items)))
    passed = ARC.results_lookup(db.conn)
    assert passed("AMC", "2025-11-04") and passed("HIER", "2025-11-04") and passed("BMO", "2025-11-04")
    assert passed("DMH", "2025-11-04") is False and passed("ZZZ", "2025-11-04") is False
    assert passed("VEN", "2025-11-10") is None                     # le week-end n'est pas archivé
    for x in ("2025-11-08", "2025-11-09"):
        db.conn.execute("INSERT INTO earnings_days (day, status, items) VALUES (?, 'ok', '[]')", (x,))
    assert ARC.results_lookup(db.conn)("VEN", "2025-11-10") is True
    assert ARC.results_lookup(sqlite3.connect(":memory:")) is None  # base pas migrée


# --- D1 : Killmode complet ---------------------------------------------------------------------------

DECIDE = datetime.fromtimestamp(ARC.cutoff_ts(DAY), timezone.utc)
FLAT_END = (114.7, 114.8, 114.6, 114.7)
GAPPERS = {  # symbole : (titre de l'actu, profil, fenêtre des actus complète)
    "KILL": ("KILL wins $2B defense contract", {"nom": "Killco Corp"}, True),
    "PROP": (None, {"nom": "Propre Inc"}, True),
    "VETO": ("VETO shares plunge after SEC probe", {"nom": "Vetoco Inc"}, True),
    "DATA": ("DATA wins defense contract", {"nom": None, "inconnu": True}, True),
    "COUP": ("COUP wins defense contract", {"nom": "Coupco Inc"}, False),
}


def _news(title: str | None) -> list[dict]:
    if not title:
        return []
    ts = int(DECIDE.timestamp()) - 2 * 3600
    return [{"title": title, "url": "https://example.com/a", "source": "x", "summary": "",
             "published_at": datetime.fromtimestamp(ts, timezone.utc).isoformat(), "published_ts": ts}]


@pytest.fixture
def kill_db(monkeypatch):
    db = LabDB(":memory:")
    for symbol, (title, profile, complete) in GAPPERS.items():
        db.put_bars("stock", symbol, 1440, daily())
        start = DAY - timedelta(days=ARC.NEWS_DAYS if complete else 2)
        db.conn.execute("INSERT INTO gap_news (day, symbol, gap_pct, atr_pct, cutoff_ts, status, news_from, news_to, "
                        "received, news) VALUES (?, ?, 5.0, 6.0, ?, 'ok', ?, ?, 1, ?)",
                        (DAY.isoformat(), symbol, ARC.cutoff_ts(DAY), start.isoformat(), DAY.isoformat(),
                         json.dumps(_news(title))))
        db.set(ARC.PROFILE_JOB, symbol, profile)
    db.set(ARC.POPULATION_JOB, DAY.isoformat(), len(GAPPERS))
    for x in KC._window(DAY):
        db.conn.execute("INSERT INTO earnings_days (day, status, n, items) VALUES (?, 'ok', 0, '[]')", (x.isoformat(),))
    session = bars_from(OPEN, RANGE + RUN + [FLAT_END] * (78 - len(RANGE) - len(RUN)))
    calls = []

    async def fake_bars(symbols, tf, start, end, transport=None, adjustment="split"):
        calls.append(list(symbols))
        db.adjustments.append(adjustment)
        return {s: hist() + session for s in symbols}

    db.adjustments = []

    monkeypatch.setattr(SRC, "alpaca_bars", fake_bars)
    db.alpaca_calls = calls
    return db


def test_empreinte_figee_par_la_declaration():
    assert K.CATALYST_CODE == KC.CATALYST_CODE_N8, (
        "la déclaration n° 8 fige le pilier catalyseur (catalyst.py, MOVE_WORDS) : le changer arrête la mesure "
        "Killmode complet, il faut une nouvelle déclaration")


async def test_killmode_complet_rejoue_le_catalyseur(kill_db):
    assert KC.ready_days(kill_db) == [DAY]
    assert await KC.step(kill_db, 60) == 1 and kill_db.alpaca_calls == [sorted(GAPPERS)]
    assert kill_db.adjustments == ["raw"]                                   # prix réellement négociés
    rows = {r[0]: r[1:] for r in kill_db.conn.execute(
        "SELECT symbol, groupe, codes, motif, triggered, r_20e, r_3c, empreinte, actus FROM kill_complet")}
    assert {s: r[0] for s, r in rows.items()} == {"KILL": "kill", "PROP": "temoin_propre", "VETO": "temoin_autre",
                                                 "DATA": "donnees", "COUP": "fenetre_coupee"}
    assert json.loads(rows["KILL"][1]) == [] and rows["KILL"][2] == "fond"
    assert json.loads(rows["PROP"][1]) == ["pas_de_positive"] and "negatives" in json.loads(rows["VETO"][1])
    assert all(r[3] == 1 and r[4] is not None and r[5] is not None and r[6] == KC.CATALYST_CODE_N8
               for r in rows.values())
    session = bars_from(OPEN, RANGE + RUN + [FLAT_END] * 65)
    scaled, _ = KC.traded_prices(CO.regular_hours(hist()), session, daily(), 5.0)
    assert scaled[-1]["c"] == pytest.approx(CO.regular_hours(hist())[-1]["c"])   # journalier à l'échelle du brut
    lab = ST.strategy_rows(ST.Day("stock", DAY, CO.regular_hours(hist())[-ST.STOCK_WARMUP_BARS:] + session, scaled),
                           "KILL", names=["killmode"])
    assert rows["KILL"][4] == lab[0]["r_20e"] and lab[0]["signal_at"] == int(DECIDE.timestamp())
    assert kill_db.get(KC.JOB, DAY.isoformat()) == {"gappers": 5, "archives": 5, "signaux": 5, "operations": 0,
                                                    "sans_historique": 0}
    assert KC.ready_days(kill_db) == [] and await KC.step(kill_db, 60) == 0
    rep = KC.report(kill_db.conn, 148)
    assert rep["groupes"]["kill"]["declenches"] == 1 and rep["groupes"]["donnees"]["signaux"] == 1
    assert rep["ecart_temoin_propre"]["n"] == 1 and rep["ecart_temoin_propre"]["esperance_r"] == 0
    assert "Killmode complet (n° 8" in SS.text(SS.report(kill_db.conn))


async def test_seances_pretes_seulement_archive_complete(kill_db):
    kill_db.conn.execute("UPDATE gap_news SET status = 'a_chercher' WHERE symbol = 'PROP'")
    assert KC.ready_days(kill_db) == []                                     # une actu encore à chercher
    kill_db.conn.execute("UPDATE gap_news SET status = 'erreur' WHERE symbol = 'PROP'")
    assert KC.ready_days(kill_db) == [DAY]                                   # en erreur : écartée, pas attendue
    kill_db.conn.execute("DELETE FROM earnings_days WHERE day = ?", ((DAY + timedelta(days=30)).isoformat(),))
    assert KC.ready_days(kill_db) == []                                     # calendrier pas encore complet
    kill_db.conn.execute("INSERT INTO earnings_days (day, status) VALUES (?, 'erreur')",
                         ((DAY + timedelta(days=30)).isoformat(),))
    kill_db.conn.execute("DELETE FROM progress WHERE job = ? AND key = 'KILL'", (ARC.PROFILE_JOB,))
    assert KC.ready_days(kill_db) == []                                     # profil pas encore archivé
    kill_db.set(ARC.PROFILE_JOB, "KILL", {"nom": "Killco Corp"})
    assert await KC.step(kill_db, 60) == 1
    groups = {s: g for s, g in kill_db.conn.execute("SELECT symbol, groupe FROM kill_complet")}
    assert "PROP" not in groups and set(groups.values()) == {"donnees"}  # calendrier en erreur : données incomplètes
    assert kill_db.get(KC.JOB, DAY.isoformat())["archives"] == 4


async def test_archive_aux_signatures_de_finnhub():
    cal = {"2025-11-03": {"AAA": [{"symbol": "AAA", "date": "2025-11-03", "hour": "bmo", "eps_actual": 1.2,
                                   "eps_estimate": 1.0, "quarter": 3}]}, "2025-11-04": {}}
    stub = KC.ArchiveFinnhub({"nom": "Acme Corp", "secteur": "Tech"}, cal, _news("AAA wins contract") * 3)
    assert (await stub.profile("AAA"))["name"] == "Acme Corp"
    got = await stub.earnings("AAA", date(2025, 11, 3), date(2025, 11, 4))
    assert got == [{"date": "2025-11-03", "hour": "bmo", "eps_estimate": 1.0, "eps_actual": 1.2,
                    "revenue_estimate": None, "revenue_actual": None}]
    assert len(await stub.company_news("AAA", 2)) == 2
    with pytest.raises(SourceError):
        await stub.earnings("AAA", date(2025, 11, 3), date(2025, 11, 5))   # date non archivée
    with pytest.raises(SourceError, match="profil inconnu"):
        await KC.ArchiveFinnhub({"inconnu": True}, cal, []).profile("AAA")
    from app.trading import catalyst as C

    saved = C.finnhub
    cat = await KC.catalyst("AAA", datetime(2025, 11, 4, 14, 45, tzinfo=timezone.utc),
                            KC.ArchiveFinnhub({"nom": "Acme Corp"}, cal | {
                                (date(2025, 11, 4) + timedelta(days=k)).isoformat(): {} for k in range(-10, 31)
                                if k != -1}, []))
    assert C.finnhub is saved                                               # Finnhub rendu au direct
    assert cat["earnings_known"] and cat["last_earnings"]["eps_surprise_pct"] == 20.0
    assert K.catalyst_codes(cat, datetime(2025, 11, 4, 14, 45, tzinfo=timezone.utc)) == []   # résultats battus hier


async def test_mesure_arretee_si_le_catalyseur_change(kill_db, monkeypatch):
    monkeypatch.setattr(K, "CATALYST_CODE", "autre")
    assert (await KC.step(kill_db, 60)).startswith("arrêté")
    rep = KC.report(kill_db.conn, 148)
    assert rep["figee"] is False and "mesure arrêtée" in KC.text(rep)


async def test_operation_sur_titre_ecartee(kill_db):
    # Gap ajusté de l'archive (+5 %) contre gap brut (~+4,5 %) : concordants. Un regroupement d'actions 1 pour 10
    # entre la veille et la séance donne un gap ajusté sans rapport avec le gap brut : le gapper est écarté.
    kill_db.conn.execute("UPDATE gap_news SET gap_pct = 950.0 WHERE symbol = 'PROP'")
    assert await KC.step(kill_db, 60) == 1
    assert "PROP" not in {s for (s,) in kill_db.conn.execute("SELECT symbol FROM kill_complet")}
    assert kill_db.get(KC.JOB, DAY.isoformat())["operations"] == 1
    rep = KC.report(kill_db.conn, 148)
    assert rep["operations_sur_titre"] == 1 and "1 sur une opération sur titre" in KC.text(rep)
    assert KC.traded_prices([], [], daily(), 5.0) == (None, "donnees")


def test_base_des_prix_dans_l_empreinte(monkeypatch):
    before = KC.cost_fingerprint()
    monkeypatch.setattr(KC, "PRICES", "ajuste")
    assert KC.cost_fingerprint() != before          # des lignes calculées sur une autre base seraient recalculées


def test_revision_du_simulateur_dans_l_empreinte(monkeypatch):
    """Relecture : la n° 10 (simulateur en révision 2) ne recalculait Killmode complet que parce que cost_variants
    avait pris de nouvelles clés ; une révision suivante du simulateur seule laissait les anciennes séances compter."""
    before = KC.cost_fingerprint()
    monkeypatch.setattr(KC.ST, "SIM_REV", KC.ST.SIM_REV + 1)
    assert KC.cost_fingerprint() != before


async def test_seances_recalculees_quand_le_cout_change(kill_db, monkeypatch):
    assert await KC.step(kill_db, 60) == 1
    before = KC.report(kill_db.conn, 148)
    assert before["a_recalculer"] == 0 and before["groupes"]["kill"]["declenches"] == 1
    real_r = K.real_r
    monkeypatch.setattr(K, "real_r", lambda call, res: None if real_r(call, res) is None else real_r(call, res) - 0.1)
    rep = KC.report(kill_db.conn, 148)                      # ex. PR #44 : gain compté en actions entières
    assert rep["cout"] != before["cout"] and rep["a_recalculer"] == 5 and rep["seances"] == 0
    assert rep["groupes"]["kill"]["declenches"] == 0 and "5 signaux à recalculer" in KC.text(rep)
    assert await KC.step(kill_db, 60) == 1                  # la séance repasse au coût en vigueur
    after = KC.report(kill_db.conn, 148)
    assert after["a_recalculer"] == 0 and after["seances"] == 1
    assert after["groupes"]["kill"]["r_20e"]["esperance_r"] == pytest.approx(
        before["groupes"]["kill"]["r_20e"]["esperance_r"] - 0.1, abs=1e-4)


def _kill_rows(db, days, kill_r=0.5, stress=0.4, other_fp=False):
    for k, day in enumerate(days):
        noise = 0.05 * (k % 3 - 1)
        rows = [("K1", "kill", kill_r + noise), ("K2", "kill", kill_r + noise / 2), ("T1", "temoin_propre", noise / 4),
                ("A1", "temoin_autre", -0.3)]
        for symbol, groupe, r in rows:
            db.conn.execute("INSERT INTO kill_complet (day, symbol, groupe, empreinte, triggered, r_20e, r_3c, r_net, "
                            "motif, cout) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)",
                            (day, symbol, groupe, "autre" if other_fp else KC.CATALYST_CODE_N8, r,
                             stress if groupe == "kill" else r, r, "fond" if groupe == "kill" else None,
                             KC.cost_fingerprint()))
        db.set(KC.JOB, day, {"gappers": 4, "archives": 4, "signaux": 4})


def test_tableau_killmode_complet():
    db = LabDB(":memory:")
    assert KC.report(db.conn, 148) is None
    days = [d.isoformat() for d in CO.trading_days(date(2025, 11, 3), date(2026, 1, 9))[:40]]
    _kill_rows(db, days)
    rep = KC.report(db.conn, 148)
    main, gap = rep["principal"], rep["ecart_temoin_propre"]
    assert main["n"] == 80 and main["jours"] == 40 and main["esperance_r"] == pytest.approx(0.5, abs=0.01)
    assert main["seuil_t"] == SS.candidate_t(40, 148) and main["candidate"] and main["tient"]
    assert gap["n"] == 40 and gap["esperance_r"] == pytest.approx(0.5, abs=0.01) and gap["aide"]
    assert rep["groupes"]["temoin_autre"]["r_20e"]["esperance_r"] == pytest.approx(-0.3)
    assert rep["motifs"]["fond"]["n"] == 80 and rep["autre_empreinte"] == 0
    assert "✅ 🔪 KILL +0,50 R à 20 €" in KC.text(rep) and ", ↗ aide (relatif))" in KC.text(rep)
    db.conn.execute("UPDATE kill_complet SET r_3c = -0.05 WHERE groupe = 'kill'")
    assert not KC.report(db.conn, 148)["principal"]["candidate"]           # le stress de 3 c décide aussi
    _kill_rows(db, [d.isoformat() for d in CO.trading_days(date(2026, 1, 12), date(2026, 1, 16))], other_fp=True)
    assert KC.report(db.conn, 148)["autre_empreinte"] == 20


def test_worker_branche_les_etapes_de_la_n8():
    assert {p: b["kill_complet_seconds"] for p, b in W.LAB2_BUDGET.items()} == {"nuit": 600, "jour": 120, "seance": 0}
    assert W.KC is KC and W.COUT is COUT
    db = LabDB(":memory:")
    assert SS.signature(db.conn)[3:7] == [KC.cost_fingerprint(), 0, 0, 0]  # n° 9 : séances de plus10
    # déclarations du council ; I3 : rev:trades, rev:strat, rev:ouverture (n° 12), code
    assert SS.signature(db.conn)[7:] == [0, 0, 0, 0, E.STATS_CODE]
    _kill_rows(db, ["2025-11-03"])
    assert SS.signature(db.conn)[4] == 4
