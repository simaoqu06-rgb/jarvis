"""Tests hors ligne de l'avis sur un actif et du conseil sur les positions ouvertes, en prix concrets
(ventes, stop suiveur, renforts, entrées sur repli ou sur cassure)."""
import sqlite3
from datetime import datetime, timezone

from app.lab import db as lab_db
from app.notify import bot
from app.trading import advice as A
from app.trading import levels as L
from app.trading import service as S

NOW = datetime(2026, 10, 7, 19, 45, tzinfo=timezone.utc)      # mercredi 15 h 45 à New York, séance ouverte
EVENING = datetime(2026, 10, 7, 23, 0, tzinfo=timezone.utc)   # 19 h à New York, marché fermé


def wave(step, n=400, now=NOW, low=100.0, high=106.0, period=24, spread=0.2):
    """Bougies qui oscillent entre `low` et `high` : sommets et creux répétés, donc des zones solides."""
    start = int(now.timestamp()) - n * step - step
    half = period // 2
    out = []
    for i in range(n):
        c = low + (high - low) * abs((i % period) - half) / half
        out.append({"t": start + i * step, "o": c, "h": c + spread, "l": c - spread, "c": c, "v": 1000.0})
    return out


def gappy_daily(n=120, every=4, gap=0.95):
    """Une nuit sur `every`, ouverture à `gap` fois la clôture de la veille (puis la séance rattrape)."""
    daily, c = [], 100.0
    for i in range(n):
        o = c * (gap if i % every == 0 else 1.0)
        c = o / gap if i % every == 0 else o
        daily.append({"t": i * 86400, "o": o, "h": max(o, c), "l": min(o, c), "c": c, "v": 1.0})
    return daily


def trade(entry, market="crypto", side="long", stop=95.0, stop_current="same", **over):
    t = {"id": "t1", "symbol": "BTC" if market == "crypto" else "NVDA", "market": market, "side": side,
         "entry": entry, "stop": stop, "stop_current": stop if stop_current == "same" else stop_current,
         "targets": [], "targets_hit": 0, "status": "open", "last_price": entry, "events": []}
    return t | over


def no_average(text):
    assert "moyenne" not in text and "moyen" not in text, text


# --- Positions ouvertes --------------------------------------------------------------------

def test_position_hors_jarvis_ventes_stop_et_renforts_en_prix():
    bars = wave(3600)
    price = bars[-1]["c"]  # 101,5, en remontée depuis le creux de 100
    a = A.advise(trade(100.5, stop=99.0, stop_current=None), price, bars, None, None, NOW)
    assert [s["price"] for s in a["sales"]] == sorted(s["price"] for s in a["sales"]) and a["sales"][0]["price"] > price
    assert a["sales"][0]["zone"] is not None and a["sales"][0]["zone"].strong  # sous la zone des sommets à ~106
    assert a["stop"] < 100 and "dernier creux 1 h" in a["stop_why"]
    assert a["decision"] in ("GARDER", "ALLÉGER")
    # Renfort sur cassure seulement : le support à 100 est sous le prix d'entrée (pas de moyenne à la baisse).
    assert [c["kind"] for c in a["adds"]] in ([], ["cassure"])
    text = A.advice_text(a)
    assert "Vends 1/3 à **" in text and "Stop **" in text and "ne l'éloigne jamais" in text
    no_average(text)
    assert a["orders"].startswith(("vends 1/3 à", "vends la moitié maintenant"))


def test_sortir_quand_le_prix_a_passe_le_stop():
    bars = wave(3600)
    price = bars[-1]["c"]
    a = A.advise(trade(103.0, stop=101.0, stop_current=102.0), price, bars, None, None, NOW)
    assert a["decision"] == "SORTIR" and "a passé le stop" in a["why"]
    assert "Vends" not in A.advice_text(a) and a["orders"].startswith("vends tout")


def test_stop_suiveur_remonte_le_stop_d_un_trade_suivi_et_ne_le_descend_jamais():
    bars = wave(3600)
    price = bars[-1]["c"]
    a = A.advise(trade(100.5, stop=95.0), price, bars, None, None, NOW)
    assert a["stop_raised"] and 99 < a["stop"] < 100 and "remonté depuis 95" in A.advice_text(a)
    kept = A.advise(trade(100.5, stop=95.0, stop_current=100.4), price, bars, None, None, NOW)
    assert kept["stop"] == 100.4 and not kept["stop_raised"] and "garde-le" in A.advice_text(kept)


def test_stop_remonte_par_le_conseil_devient_le_stop_suivi():
    bars = wave(3600)
    t = trade(100.5, stop=95.0)
    a = A.advise(t, bars[-1]["c"], bars, None, None, NOW)
    assert "(JARVIS suit ce nouveau stop)" in A.advice_text(a)
    A.record(t, a, NOW)
    assert t["stop_current"] == a["stop"] and t["stop_moved_at"] == int(NOW.timestamp())
    # La bougie en cours, plus ancienne que le conseil, ne déclenche pas ce nouveau stop ; la suivante, si.
    t |= {"last_check": 0, "created_at": NOW.isoformat(), "late_warned": [], "stop_pct": -1.0}
    low = {"o": 101, "h": 101, "l": a["stop"] - 1, "c": 101}
    S.apply_bars(t, [{"t": int(NOW.timestamp()) - 30} | low], NOW)
    assert t["status"] == "open"
    S.apply_bars(t, [{"t": int(NOW.timestamp()) + 30} | low], NOW)
    assert t["status"] == "closed" and t["events"][-1] == {"kind": "stop", "price": a["stop"], "at": t["events"][-1]["at"]}
    closed = trade(100.5, stop=95.0, status="closed")
    A.record(closed, a, NOW)
    assert closed["stop_current"] == 95.0


def test_sortir_avant_des_resultats():
    bars = wave(900)
    price = bars[-1]["c"]
    a = A.advise(trade(100.5, market="stock"), price, bars, None, {"hours_to_earnings": 10}, NOW)
    assert a["decision"] == "SORTIR" and "résultats" in a["why"]
    # Résultats dans 10 jours : rien à voir avec la nuit.
    a = A.advise(trade(100.5, market="stock"), price, bars, None, {"hours_to_earnings": 240}, NOW)
    assert "résultats" not in a["why"]


def test_sortir_sur_une_augmentation_de_capital():
    bars = wave(900)
    a = A.advise(trade(100.5, market="stock"), bars[-1]["c"], bars, None, {"dilution": True}, NOW)
    assert a["decision"] == "SORTIR" and "augmentation de capital" in a["why"]


def test_alleger_quand_les_gaps_sautent_souvent_le_stop_en_seance_seulement():
    bars = wave(900)
    price = bars[-1]["c"]
    t = trade(102.0, market="stock", stop=99.0)  # perdant : ni signal de retournement ni renfort
    a = A.advise(t, price, bars, gappy_daily(), None, NOW)
    assert a["gaps"]["through_stop"] >= 0.2
    assert a["decision"] == "ALLÉGER" and "nuits sur" in a["why"] and "la moitié" in a["why"]
    assert "Puis, sur le reste : vends" in A.advice_text(a) and a["orders"].startswith("vends la moitié maintenant")
    # Marché fermé : la nuit est déjà là, le fait reste affiché mais ne décide plus.
    evening = wave(900, now=EVENING)
    b = A.advise(t, evening[-1]["c"], evening, gappy_daily(), None, EVENING)
    assert b["decision"] == "GARDER" and any("IBKR ne déclenche pas un stop hors séance" in f for f in b["facts"])
    no_average(A.advice_text(b))


def test_objectifs_suivis_rappeles_quand_la_carte_donne_d_autres_prix():
    bars = wave(3600)
    price = bars[-1]["c"]
    targets = [{"name": "TP1", "price": 102.0, "eta_latest": None}, {"name": "TP2", "price": 103.0, "eta_latest": None}]
    a = A.advise(trade(100.5, stop=99.0, targets=targets), price, bars, None, None, NOW)
    assert len(a["sales"]) == 2 and any(f.startswith("Objectifs suivis par JARVIS : TP1 102") for f in a["facts"])


def test_conseil_garde_un_par_jour_et_bilan_compte():
    t = trade(100.0, stop=98.0, targets=[{"name": f"TP{i}", "price": 100.0 + 2 * i} for i in (1, 2, 3)])
    A.record(t, {"at": "x", "decision": "GARDER", "price": 100.0, "exit_r": 0.0, "why": "a", "orders": "o"}, NOW)
    A.record(t, {"at": "y", "decision": "SORTIR", "price": 100.0, "exit_r": 0.0, "why": "b", "orders": "vends tout"}, NOW)
    assert [(c["decision"], c["orders"]) for c in t["conseils"]] == [("SORTIR", "vends tout")]
    t |= {"status": "closed", "events": [{"kind": "stop", "price": 98.0}]}  # garder aurait coûté 1 R
    st = A.advice_stats([t])
    assert st["SORTIR"]["n"] == 1 and st["SORTIR"]["right"] == 1
    line = A.stats_line(st)
    assert "SORTIR juste 1/1" in line
    no_average(line)


def test_sortie_en_r_compte_les_tiers_deja_pris():
    t = trade(100.0, stop=98.0, targets_hit=1, targets=[{"name": f"TP{i}", "price": 100.0 + 2 * i} for i in (1, 2, 3)])
    assert A.exit_r(t, 104.0) == round((1 + 2 * 2) / 3, 2)


def test_message_avant_la_cloture_une_fois_par_seance():
    assert A.closing_due(NOW, None)  # 15 min avant la clôture
    assert not A.closing_due(NOW, "2026-10-07")
    assert not A.closing_due(datetime(2026, 10, 7, 17, 0, tzinfo=timezone.utc), None)  # 13 h à New York
    assert not A.closing_due(datetime(2026, 10, 7, 21, 0, tzinfo=timezone.utc), None)  # marché fermé


# --- Avis sur un actif ---------------------------------------------------------------------

def entry(kind, side="long", ok=True, rr2=2.5, price=100.0):
    z = None if kind == "maintenant" else L.Zone(98.0, 98.5, ["creux 15 min du 6 oct."], 2, True)
    return {"kind": kind, "side": side, "entry": price, "stop": price - 2, "zone": z, "ok": ok,
            "targets": [{"price": price + 1}, {"price": price + 5}, {"price": price + 8}], "rr": [0.5, rr2, 4.0]}


def plan(side, go, composite, entries, trend=True, cat=None):
    return {"symbol": "NVDA", "market": "stock", "side": side, "entry": 100.0, "stop": 98.0, "catalyst": cat,
            "score": {"go": go, "decision": "GO" if go else "NO GO", "composite": composite,
                      "reasons": [] if go else ["note 5 sous 7"]},
            "checklist": {"verdict": "Feu vert : déclencheur complet", "score": 4, "max_score": 6,
                          "items": [{"key": "trend_daily", "label": "Tendance journalière", "ok": trend,
                                     "detail": "prix au-dessus de la MM 50 jours (90)"}]},
            "entries": entries}


def test_avis_entre_maintenant_si_go_et_rr():
    v = A.decide([plan("long", True, 7.5, [entry("maintenant"), entry("repli")]),
                  plan("short", False, 4, [entry("maintenant", "short")], trend=False)])
    assert v["decision"] == "ENTRER LONG" and v["orders"][0]["kind"] == "maintenant"
    assert any("contre la tendance de fond" in r for r in v["reasons"])


def test_avis_ordres_sur_repli_ou_cassure_si_maintenant_trop_court():
    v = A.decide([plan("long", True, 7.5, [entry("maintenant", ok=False, rr2=1.2), entry("repli"),
                                           entry("cassure", price=101.1)])])
    assert v["decision"] == "LONG SUR REPLI OU CASSURE" and [e["kind"] for e in v["orders"]] == ["repli", "cassure"]
    assert any("trop court" in r for r in v["reasons"])


def test_avis_jamais_contre_la_tendance_ni_avant_les_resultats():
    v = A.decide([plan("short", True, 8, [entry("maintenant", "short")], trend=False)])
    assert v["decision"] == "ATTENDRE" and not v["orders"]
    soon = {"hours_to_earnings": 20}
    v = A.decide([plan("long", True, 8, [entry("maintenant")], cat=soon)])
    assert v["decision"] == "ATTENDRE" and "résultats trimestriels dans 20 h" in v["reasons"][-1]
    v = A.decide([plan("long", True, 8, [entry("maintenant")], cat={"dilution": True})])
    assert v["decision"] == "ATTENDRE" and any("augmentation de capital" in r for r in v["reasons"])


def test_avis_attend_et_dit_la_meilleure_entree():
    v = A.decide([plan("long", False, 5, [entry("maintenant", ok=False, rr2=1.0), entry("repli", ok=False, rr2=1.5)])])
    assert v["decision"] == "ATTENDRE" and "la meilleure : long sur repli à 100, R/R 1.5" in v["reasons"][-1]


def test_note_du_labo_sans_moyenne():
    m = {"symbole": {"GO": {"n": 4, "r": -0.2, "wins": 1}},
         "marche": {"GO": {"n": 500, "r": -0.24, "wins": 150}, "NO GO": {"n": 900, "r": -0.23, "wins": 260}}}
    assert "pas encore prouvé" in A.note_line(m) and "Labo indisponible" in A.note_line(None)
    proven = {"marche": {"GO": {"n": 50, "r": 0.3, "wins": 30}, "NO GO": {"n": 50, "r": -0.2, "wins": 10}}}
    assert A.note_proven(proven) and not A.note_proven(m)


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


async def test_avis_complet_sur_des_bougies(monkeypatch):
    hourly = wave(3600, n=500)

    async def build(symbol, market, side, entry=None, stop=None, targets=None, now=None, cat=None, **kw):
        return S.plan_from_bars(symbol, market, side, hourly, None, None, now, cat)

    async def candles(symbol, market, interval, *a, **k):
        return hourly

    async def nothing(*a, **k):
        return None

    monkeypatch.setattr(S, "build_plan", build)
    monkeypatch.setattr(S, "candles", candles)
    monkeypatch.setattr(S, "_optional_candles", nothing)
    monkeypatch.setattr(A, "lab_measure", lambda market, symbol: None)
    o = await A.opinion("btc", "crypto", NOW)
    assert len(o["plans"]) == 2 and all(p["entries"] for p in o["plans"])
    assert o["decision"].startswith(("ENTRER", "LONG", "SHORT", "ATTENDRE"))
    text = A.opinion_text(o)
    assert text.startswith("🧭 **Avis de JARVIS sur BTC**") and "Long · Maintenant : achat au marché" in text
    assert "Short · " in text and "R/R" in text and "Labo indisponible" in text
    no_average(text)


# --- Commandes Discord ---------------------------------------------------------------------

def test_commandes_lisent_entree_stop_et_objectifs():
    assert bot.parse_trade(["nvda", "132,5", "stop", "128"]) == {
        "symbol": "NVDA", "market": "stock", "side": "long", "entry": 132.5, "stop": 128.0, "targets": None}
    req = bot.parse_trade(["MRVL", "281", "stop", "279.5", "tp", "286.8", "290,5", "299.5"])
    assert req["entry"] == 281 and req["stop"] == 279.5 and req["targets"] == [286.8, 290.5, 299.5]
    req = bot.parse_trade(["eth", "tp", "2400", "2300", "short", "2500"])  # un mot termine la liste des objectifs
    assert req["side"] == "short" and req["targets"] == [2400, 2300] and req["entry"] == 2500 and req["market"] == "crypto"
    assert bot.parse_trade(["NVDA"])["entry"] is None
    for bad in (["NVDA", "stop"], ["NVDA", "stop", "x"], ["NVDA", "tp"]):
        try:
            bot.parse_trade(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


async def test_positions_sans_trade_suivi(monkeypatch):
    from app import scheduler

    monkeypatch.setattr(scheduler, "book", S.Book(None))
    text = await bot.handle("!positions")
    assert "Aucun trade suivi" in text and "!position NVDA" in text
    assert "prix d'entrée" in await bot.handle("!position NVDA")


async def test_position_hors_jarvis_donne_la_commande_pour_la_suivre(monkeypatch):
    bars = wave(3600)

    async def inputs(symbol, market, now):
        return bars, None, None

    monkeypatch.setattr(A, "_inputs", inputs)
    text = await bot.handle("!position btc 100.5")
    assert text.startswith("🟢 **BTC**") and "Vends 1/3 à" in text
    assert "`!prends BTC 100.5 stop " in text and " tp " in text
    no_average(text)


async def test_message_avant_la_cloture_garde_les_conseils(monkeypatch):
    from app import scheduler

    book = S.Book(None)
    book.trades.append(trade(100.0, market="stock"))
    monkeypatch.setattr(scheduler, "book", book)
    monkeypatch.setattr(scheduler.state, "data", {})
    monkeypatch.setattr(scheduler.state, "save", lambda: None)
    sent = []
    monkeypatch.setattr(scheduler, "_queue", lambda text, kind, **kw: sent.append(text))
    bars = wave(900)

    async def fake(trades, now):
        return [A.advise(trades[0], bars[-1]["c"], bars, None, None, now)]

    monkeypatch.setattr(A, "advise_book", fake)
    await scheduler.tick_closing(NOW)
    await scheduler.tick_closing(NOW)  # une seule fois par séance
    assert len(sent) == 1 and "Avant la clôture US" in sent[0] and "les ordres à poser" in sent[0]
    c = book.trades[0]["conseils"][0]
    assert c["decision"] in ("GARDER", "ALLÉGER", "SORTIR") and c["orders"]
