"""Données synthétiques pour les tests du labo (marche aléatoire déterministe, sans réseau)."""
import json
import random
from datetime import date, timedelta

from app.lab import collector as CO
from app.lab.db import LabDB


def walk_bars(times: list[int], start: float, rnd: random.Random, vol: float = 0.002) -> list[dict]:
    out, p = [], start
    for t in times:
        o = p
        p = max(0.5, p * (1 + rnd.gauss(0.0001, vol)))
        h = max(o, p) * (1 + abs(rnd.gauss(0, vol / 2)))
        lo = min(o, p) * (1 - abs(rnd.gauss(0, vol / 2)))
        out.append({"t": t, "o": round(o, 4), "h": round(h, 4), "l": round(lo, 4), "c": round(p, 4),
                    "v": float(rnd.randint(1000, 9000))})
    return out


def stock_times(first: date, last: date) -> list[int]:
    out = []
    for d in CO.trading_days(first, last):
        o, c = CO.session_ts(d)
        out += list(range(o, c, 300))
    return out


def fill_stock(db: LabDB, symbols: list[str], first: date, last: date, seed: int = 7) -> None:
    """5 min de séance et journalier (agrégé) pour chaque symbole entre deux dates."""
    rnd = random.Random(seed)
    times = stock_times(first, last)
    for i, s in enumerate(symbols):
        bars = walk_bars(times, 20.0 + 10 * i, rnd)
        db.put_bars("stock", s, 5, bars)
        db.put_bars("stock", s, 1440, CO.aggregate(bars, 1440, "stock"))


def fill_crypto(db: LabDB, symbols: list[str], first: date, last: date, seed: int = 11) -> None:
    rnd = random.Random(seed)
    start, end = CO.utc_midnight(first), CO.utc_midnight(last + timedelta(days=1))
    times = list(range(start, end, 300))
    for i, s in enumerate(symbols):
        bars = walk_bars(times, 100.0 * (i + 1), rnd, vol=0.003)
        db.put_bars("crypto", s, 5, bars)
        db.put_bars("crypto", s, 1440, CO.aggregate(bars, 1440, "crypto"))


# --- Trades synthétiques (Labo 2 : statistiques et validateur) --------------------------------

SETUPS = ("cassure_pivot", "rebond_pivot", "divergence", "tendance_sans_declencheur")


def calendar_days(first: date, n: int, weekdays_only: bool = True) -> list[date]:
    out, d = [], first
    while len(out) < n:
        if not weekdays_only or d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def synthetic_trades(db: LabDB, n_days: int = 120, per_day: int = 20, seed: int = 1, market: str = "stock",
                     effect=None, first: date = date(2024, 1, 2), version: str = "v1", trigger_rate: float = 0.8,
                     noise: float = 1.0, base: float = 0.0, origin: str = "replay") -> list[dict]:
    """Trades sans bougies, insérés directement : r_net = base + effet(contexte) + bruit gaussien.

    Contexte : setup, signal (uniforme 0-1, sans effet sauf `effect`), rvol, regime ; plan : délais
    médians de 60, 120 et 240 min. `effect(ctx, rnd)` renvoie le décalage de r_net d'un trade."""
    rnd = random.Random(seed)
    if not db.conn.execute("SELECT 1 FROM method_versions WHERE version = ?", (version,)).fetchone():
        db.conn.execute("INSERT INTO method_versions VALUES (?, '2024-01-01T00:00:00+00:00', '{}')", (version,))
    rows, out = [], []
    for day in calendar_days(first, n_days, weekdays_only=market == "stock"):
        moment = (CO.ny_midnight(day) + 9 * 3600 + 600) if market == "stock" else CO.utc_midnight(day)
        for k in range(per_day):
            symbol = f"S{k:03d}"
            ctx = {"setup": rnd.choice(SETUPS), "signal": round(rnd.random(), 4),
                   "rvol": round(rnd.lognormvariate(0, 0.5), 3), "regime": {"vol20_pct": round(rnd.uniform(5, 40), 2)}}
            composite = round(rnd.uniform(4, 9.9), 1)
            trig = rnd.random() < trigger_rate
            r = None
            tps = [0, 0, 0]
            minutes = [None, None, None]
            if trig:
                r = base + rnd.gauss(0, noise) + (effect(ctx, rnd) if effect else 0.0)
                r = round(max(-1.5, min(3.0, r)), 4)
                tps[0] = int(r > 0.3)
                tps[1] = int(r > 1.0)
                tps[2] = int(r > 2.0)
                minutes = [round(rnd.uniform(5, 300), 1) if tps[i] else None for i in range(3)]
            plan = {"targets": [{"minutes_median": 60}, {"minutes_median": 120}, {"minutes_median": 240}],
                    "stop_pct": -round(rnd.uniform(1, 5), 2)}
            row = (origin, version, market, symbol, "long", day.isoformat(), moment + (k if market == "crypto" else 0),
                   json.dumps(plan), json.dumps(ctx), "{}", int(trig), moment + 1200 if trig else None,
                   "stop" if trig and r < 0 else ("tp3" if trig else "non_declenche"), r, r,
                   *tps, *minutes, None, None, None, "GO" if composite >= 7 else "NO GO", composite)
            rows.append(row)
            out.append({"day": day.isoformat(), "symbol": symbol, "ctx": ctx, "r": r, "trig": trig})
    db.conn.executemany(
        "INSERT INTO trades (origin, method_version, market, symbol, side, day, planned_at, plan, context, result, "
        "triggered, filled_at, exit_reason, r_net, r_gross, tp1_hit, tp2_hit, tp3_hit, minutes_to_tp1, minutes_to_tp2, "
        "minutes_to_tp3, mfe_r, mae_r, day_range_pct, decision, composite) VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    return out
