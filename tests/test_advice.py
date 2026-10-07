"""Tests hors ligne de l'avis sur un actif et du conseil sur les positions ouvertes (sortir, alléger, garder)."""
import sqlite3
from datetime import datetime, timezone

from app.lab import db as lab_db
from app.notify import bot
from app.trading import advice as A
from app.trading import service as S

NOW = datetime(2026, 10, 7, 19, 45, tzinfo=timezone.utc)  # mercredi 15 h 45 à New York, séance ouverte


def bars(closes, step, spread=0.3):
    start = int(NOW.timestamp()) - len(closes) * step - step
    return [{"t": start + i * step, "o": c, "h": c + spread, "l": c - spread, "c": c, "v": 1.0}
            for i, c in enumerate(closes)]


def trade(price, market="crypto", side="long", entry=None, stop_gap=2.0, **over):
    entry = price if entry is None else entry
    sign = 1 if side == "long" else -1
    t = {"id": "t1", "symbol": "BTC", "market": market, "side": side, "entry": entry,
         "stop": entry - sign * stop_gap, "stop_current": entry - sign * stop_gap, "targets_hit": 0,
         "status": "open", "last_price": price, "events": [],
         "targets": [{"name": f"TP{i}", "price": entry + sign * stop_gap * i, "eta_latest": None, "eta_text": "~2 h"}
                     for i in (1, 2, 3)]}
    return t | over


def plan(side, go, composite, expectancies, verdict="Feu vert : déclencheur complet"):
    return {"symbol": "NVDA", "market": "stock", "side": side, "entry": 100.0, "stop": 98.0, "stop_source": "creux",
            "score": {"go": go, "decision": "GO" if go else "NO GO", "composite": composite,
                      "reasons": [] if go else ["note 4 sous 6"]},
            "checklist": {"verdict": verdict, "score": 3, "max_score": 5, "trigger": go,
                          "items": [{"label": "tendance", "ok": go}, {"label": "volume", "ok": False}]},
            "targets": [{"name": f"TP{i}", "expectancy_r": e, "hit_rate": 0.4} for i, e in enumerate(expectancies, 1)]}


def test_avis_entre_du_cote_go():
    v = A.decide([plan("long", True, 7, [0.1, 0.3, -0.2]), plan("short", False, 4, [-0.4])])
    assert v["decision"] == "ENTRER LONG" and v["plan"]["side"] == "long"
    assert any("TP2" in r and "+0.30 R" in r for r in v["reasons"])


def test_avis_go_mais_rejeu_perdant_attend():
    v = A.decide([plan("long", True, 7, [-0.1, -0.3]), plan("short", False, 4, [-0.4])])
    assert v["decision"] == "ATTENDRE" and "aucun objectif ne gagne" in v["reasons"][-1]


def test_avis_deux_no_go_dit_ce_qui_debloquerait():
    v = A.decide([plan("long", False, 5, [0.1]), plan("short", False, 4, [0.2])])
    assert v["decision"] == "ATTENDRE" and v["plan"] is None
    assert v["unlock"].startswith("côté long") and "volume" in v["unlock"]


def test_avis_affiche_que_la_note_n_est_pas_prouvee():
    m = {"symbole": {"GO": {"n": 4, "r": -0.2, "wins": 1}},
         "marche": {"GO": {"n": 500, "r": -0.24, "wins": 150}, "NO GO": {"n": 900, "r": -0.23, "wins": 260}}}
    text = "\n".join(A.measure_lines(m, "NVDA"))
    assert "GO -0.24 R sur 500" in text and "pas encore prouvé" in text
    assert "Labo indisponible" in A.measure_lines(None, "NVDA")[0]


def test_mesure_du_labo_lit_la_table_des_trades(tmp_path):
    path = tmp_path / "lab.db"
    conn = sqlite3.connect(path)
    conn.executescript(lab_db.TRADES_DDL.format(name="trades"))
    for i, (sym, decision, r) in enumerate([("NVDA", "GO", 1.0), ("NVDA", "GO", -1.0), ("NVDA", "NO GO", -1.0),
                                            ("AMD", "GO", 2.0)]):
        conn.execute("INSERT INTO trades (origin, method_version, market, symbol, side, day, planned_at, triggered, "
                     "r_net, decision) VALUES ('replay', 'v1', 'stock', ?, 'long', '2026-01-02', ?, 1, ?, ?)",
                     (sym, i, r, decision))
    conn.commit()
    conn.close()
    m = A.lab_measure("stock", "NVDA", str(path))
    assert m["symbole"]["GO"] == {"n": 2, "r": 0.0, "wins": 1}
    assert m["marche"]["GO"]["n"] == 3 and m["marche"]["NO GO"]["r"] == -1.0
    assert A.lab_measure("stock", "NVDA", str(tmp_path / "absente.db")) is None


def test_garder_quand_la_tendance_porte_vers_l_objectif():
    closes = [100 + 0.2 * i for i in range(300)]
    price = closes[-1]
    a = A.advise(trade(price), price, bars(closes, 3600), None, None, NOW)
    assert a["decision"] == "GARDER" and a["hold"]["expectancy_r"] > 0


def test_sortir_quand_le_stop_vient_plus_souvent_que_l_objectif():
    closes = [200 - 0.2 * i for i in range(300)]
    price = closes[-1]
    a = A.advise(trade(price, stop_gap=1.0), price, bars(closes, 3600), None, None, NOW)
    assert a["decision"] == "SORTIR" and a["hold"]["expectancy_r"] < 0


def test_sortir_avant_des_resultats():
    closes = [100 + 0.05 * i for i in range(300)]
    price = closes[-1]
    a = A.advise(trade(price, market="stock"), price, bars(closes, 900), None, {"hours_to_earnings": 10}, NOW)
    assert a["decision"] == "SORTIR" and "résultats" in a["why"]
    # Résultats dans 10 jours : rien à voir avec la nuit.
    a = A.advise(trade(price, market="stock"), price, bars(closes, 900), None, {"hours_to_earnings": 240}, NOW)
    assert a["decision"] != "SORTIR" or "résultats" not in a["why"]


def test_alleger_quand_les_gaps_sautent_souvent_le_stop():
    closes = [100 + 0.05 * i for i in range(300)]
    price = closes[-1]
    daily = []
    c = 100.0
    for i in range(120):
        o = c * (0.95 if i % 4 == 0 else 1.0)  # une nuit sur 4 : gap de -5 %
        c = o * 1.05 if i % 4 == 0 else o
        daily.append({"t": i * 86400, "o": o, "h": max(o, c), "l": min(o, c), "c": c, "v": 1.0})
    a = A.advise(trade(price, market="stock"), price, bars(closes, 900), daily, None, NOW)
    assert a["gaps"]["through_stop"] >= 0.2
    assert a["decision"] == "ALLÉGER" and "gap" in a["why"]


def test_conseil_garde_un_par_jour_et_bilan():
    t = trade(100.0)
    A.record(t, {"at": "x", "decision": "GARDER", "price": 100.0, "exit_r": 0.0, "why": "a"}, NOW)
    A.record(t, {"at": "y", "decision": "SORTIR", "price": 100.0, "exit_r": 0.0, "why": "b"}, NOW)
    assert [c["decision"] for c in t["conseils"]] == ["SORTIR"]
    t |= {"status": "closed", "events": [{"kind": "stop", "price": 98.0}]}  # garder aurait coûté 1 R
    st = A.advice_stats([t])
    assert st["SORTIR"] == {"n": 1, "right": 1, "avg_delta": -1.0}
    assert "juste 1/1" in A.stats_line(st)


def test_sortie_en_r_compte_les_tiers_deja_pris():
    t = trade(104.0, entry=100.0, targets_hit=1)  # TP1 à 1 R déjà pris
    assert A.exit_r(t, 104.0) == round((1 + 2 * 2) / 3, 2)


def test_message_avant_la_cloture_une_fois_par_seance():
    assert A.closing_due(NOW, None)  # 15 min avant la clôture
    assert not A.closing_due(NOW, "2026-10-07")
    assert not A.closing_due(datetime(2026, 10, 7, 17, 0, tzinfo=timezone.utc), None)  # 13 h à New York
    assert not A.closing_due(datetime(2026, 10, 7, 21, 0, tzinfo=timezone.utc), None)  # marché fermé


def test_commandes_lisent_le_prix_d_entree_et_le_stop():
    assert bot.parse_trade(["nvda", "132,5", "stop", "128"]) == {
        "symbol": "NVDA", "market": "stock", "side": "long", "entry": 132.5, "stop": 128.0}
    req = bot.parse_trade(["eth", "short", "2500"])
    assert req["market"] == "crypto" and req["side"] == "short" and req["entry"] == 2500 and req["stop"] is None
    assert bot.parse_trade(["NVDA"])["entry"] is None


async def test_positions_sans_trade_suivi(monkeypatch):
    from app import scheduler

    monkeypatch.setattr(scheduler, "book", S.Book(None))
    text = await bot.handle("!positions")
    assert "Aucun trade suivi" in text and "!position NVDA" in text
    assert "prix d'entrée" in await bot.handle("!position NVDA")


async def test_avis_complet_sur_des_bougies(monkeypatch):
    closes = [100 + 0.2 * i + (1.5 if i % 7 == 0 else 0) for i in range(400)]
    hourly = bars(closes, 3600, spread=0.8)

    async def build(symbol, market, side, entry=None, stop=None, targets=None, now=None, cat=None, **kw):
        return S.plan_from_bars(symbol, market, side, hourly, None, None, now, cat)

    monkeypatch.setattr(S, "build_plan", build)
    monkeypatch.setattr(A, "lab_measure", lambda market, symbol: None)
    o = await A.opinion("btc", "crypto", NOW)
    assert o["decision"] in ("ENTRER LONG", "ENTRER SHORT", "ATTENDRE") and len(o["plans"]) == 2
    text = A.opinion_text(o)
    assert text.startswith("🧭 **Avis de JARVIS sur BTC**") and "Long :" in text and "Short :" in text


async def test_message_avant_la_cloture_garde_les_conseils(monkeypatch):
    from app import scheduler

    book = S.Book(None)
    book.trades.append(trade(100.0, market="stock"))
    monkeypatch.setattr(scheduler, "book", book)
    monkeypatch.setattr(scheduler.state, "data", {})
    monkeypatch.setattr(scheduler.state, "save", lambda: None)
    sent = []
    monkeypatch.setattr(scheduler, "_queue", lambda text, kind, **kw: sent.append(text))

    async def fake(trades, now):
        return [{"id": "t1", "symbol": "BTC", "side": "long", "decision": "GARDER", "why": "garde", "facts": [],
                 "price": 100.0, "exit_r": 0.0, "at": now.isoformat()}]

    monkeypatch.setattr(A, "advise_book", fake)
    await scheduler.tick_closing(NOW)
    await scheduler.tick_closing(NOW)  # une seule fois par séance
    assert len(sent) == 1 and "Avant la clôture US" in sent[0] and "GARDER" in sent[0]
    assert book.trades[0]["conseils"][0]["decision"] == "GARDER"
