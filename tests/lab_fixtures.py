"""Données synthétiques pour les tests du labo (marche aléatoire déterministe, sans réseau)."""
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
