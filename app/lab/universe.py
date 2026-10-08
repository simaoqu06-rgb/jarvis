"""Univers du jour du labo : ~300 actions US et ~50 cryptos, sans filtre de liquidité ni de capitalisation.

Tout se calcule avec les seules données connues à l'heure de décision (aucun regard vers l'avenir) :

Actions (décision à 9 h 10, New York, comme le scanner) : bougies journalières jusqu'à la veille (D-1).
Les 200 plus gros volumes en dollars de la veille, puis ~100 valeurs « en mouvement » prises tour à
tour parmi les plus fortes variations absolues de la veille et les plus forts volumes relatifs
(volume de la veille / moyenne des 20 séances d'avant). En direct, le scanner part du screener
Alpaca (plus actives et plus fortes hausses, pré-ouverture comprise), qui ne se rejoue pas :
c'est l'approximation la plus proche sans biais.

Crypto (jour UTC) : les 50 paires USDT d'OKX au plus gros volume en dollars sur la journée UTC
précédente (clôture × volume de la bougie journalière), hors stablecoins, jetons enveloppés et à levier.
"""
import math
import sqlite3
from datetime import date, datetime, time

from app.lab import collector as CO
from app.lab import seal as SEAL
from app.lab import strategies as ST
from app.lab.db import LabDB

STOCK_TOP_DOLLAR = 200
STOCK_TOP_MOVERS = 100
CRYPTO_TOP = 50
RVOL_DAYS = 20
DECISION_NY = time(9, 10)


def stock_decision_ts(day: date) -> int:
    return int(datetime.combine(day, DECISION_NY, CO.NEW_YORK).timestamp())


def _metrics(rows: list[dict]) -> dict | None:
    prev = rows[-1]
    if prev["c"] <= 0 or not prev["v"]:
        return None
    before = rows[-RVOL_DAYS - 1:-1]
    avg = sum(r["v"] for r in before) / len(before) if len(before) == RVOL_DAYS else None
    change = (prev["c"] / rows[-2]["c"] - 1) * 100 if len(rows) >= 2 and rows[-2]["c"] > 0 else None
    return {"close": prev["c"], "dollar_volume": round(prev["c"] * prev["v"], 2),
            "change_pct": round(change, 3) if change is not None else None,
            "rvol": round(prev["v"] / avg, 3) if avg else None}


def stock_universe(daily: dict[str, list[dict]], day: date, top_dollar: int = STOCK_TOP_DOLLAR,
                   top_movers: int = STOCK_TOP_MOVERS) -> list[dict]:
    """Univers de la séance `day` à partir des bougies journalières (minuit New York) des séances
    précédentes ; les bougies du jour même ou plus récentes sont ignorées."""
    cutoff = CO.ny_midnight(day)
    prev_day = CO.previous_trading_day(day)
    stats = {}
    for symbol, rows in daily.items():
        rows = [r for r in rows if r["t"] < cutoff]
        if not rows or CO.ny_day(rows[-1]["t"]) != prev_day:
            continue  # pas d'échange la veille
        m = _metrics(rows)
        if m:
            stats[symbol] = m
    chosen: list[dict] = []
    seen: set[str] = set()

    def take(symbol: str, reason: str) -> None:
        seen.add(symbol)
        chosen.append({"symbol": symbol, "rank": len(chosen) + 1, "reason": reason, "metrics": stats[symbol]})

    for symbol in sorted(stats, key=lambda s: (-stats[s]["dollar_volume"], s))[:top_dollar]:
        take(symbol, "volume_dollars")
    movers = [sorted((s for s in stats if stats[s]["change_pct"] is not None),
                     key=lambda s: (-abs(stats[s]["change_pct"]), s)),
              sorted((s for s in stats if stats[s]["rvol"] is not None), key=lambda s: (-stats[s]["rvol"], s))]
    reasons = ("variation", "rvol")
    added, idx = 0, [0, 0]
    while added < top_movers and (idx[0] < len(movers[0]) or idx[1] < len(movers[1])):
        for k in (0, 1):
            while idx[k] < len(movers[k]) and movers[k][idx[k]] in seen:
                idx[k] += 1
            if idx[k] < len(movers[k]) and added < top_movers:
                take(movers[k][idx[k]], reasons[k])
                added += 1
    return chosen


def crypto_universe(daily: dict[str, list[dict]], day: date, top: int = CRYPTO_TOP) -> list[dict]:
    """Univers du jour UTC `day` : volume en dollars de la journée UTC précédente (bougie close)."""
    cutoff = CO.utc_midnight(day)
    prev_ts = cutoff - 86400
    stats = {}
    for symbol, rows in daily.items():
        rows = [r for r in rows if r["t"] < cutoff]
        if not rows or rows[-1]["t"] != prev_ts:
            continue
        m = _metrics(rows)
        if m:
            stats[symbol] = m
    ranked = sorted(stats, key=lambda s: (-stats[s]["dollar_volume"], s))[:top]
    return [{"symbol": s, "rank": i, "reason": "volume_dollars", "metrics": stats[s]}
            for i, s in enumerate(ranked, start=1)]


def build(db: LabDB, market: str, day: date, save: bool = True) -> list[dict]:
    """Calcule l'univers du jour depuis la base (bougies journalières antérieures au jour) et l'enregistre."""
    cutoff = CO.ny_midnight(day) if market == "stock" else CO.utc_midnight(day)
    daily = db.daily_cross_section(market, cutoff - 40 * 86400, cutoff)
    index_only = db.get("lab", f"index_only:{market}")
    if index_only:
        daily.pop(index_only, None)  # collecté pour le régime seulement (pas dans la liste des actifs)
    rows = stock_universe(daily, day) if market == "stock" else crypto_universe(daily, day)
    if save:
        db.replace_universe(market, day.isoformat(), rows)
    return rows


# Déclaration n° 6 : couverture de Killmode par l'univers du labo (comptage seulement, aucun résultat lu).
COVERAGE_JOB = "killcov:stock"
PRICE_BUCKETS = ((0.0, 1.0, "< 1 $"), (1.0, 5.0, "1-5 $"), (5.0, 20.0, "5-20 $"), (20.0, 1e18, "≥ 20 $"))


def gappers(conn: sqlite3.Connection, day: date, gap_min: float = ST.KILL_GAP_PCT,
            atr_min: float = ST.KILL_ATR_PCT) -> list[dict] | None:
    """Lecture seule. Actions de toute la base (journalier Alpaca, pas seulement l'univers du labo) ouvertes le
    jour `day` en gap ≥ `gap_min` % avec un ATR de Wilder 14 j ≥ `atr_min` % (séances d'avant, 15 au moins) :
    [{symbol, gap_pct, atr_pct, prev_close, open}]. None si le journalier de la séance n'est pas en base."""
    cutoff = CO.ny_midnight(day)
    series: dict[str, list[tuple]] = {}
    for symbol, *bar in conn.execute(
            "SELECT symbol, ts, o, h, l, c FROM bars WHERE market = 'stock' AND tf = 1440 AND ts >= ? AND ts < ? "
            "ORDER BY symbol, ts", (cutoff - 40 * 86400, cutoff + 86400)):
        series.setdefault(symbol, []).append(bar)
    out: list[dict] = []
    seen_day = False
    for symbol, rows in series.items():
        if CO.ny_day(rows[-1][0]) != day:
            continue
        seen_day = True
        before, today = rows[:-1], rows[-1]
        prev = before[-1][4] if before else 0
        if len(before) <= ST.ATR_LEN or prev <= 0:
            continue
        gap = (today[1] / prev - 1) * 100
        if gap < gap_min:
            continue
        a = ST.atr([{"h": h, "l": lo, "c": c} for _, _, h, lo, c in before])[-1]
        if a is None or a / prev * 100 < atr_min:
            continue
        out.append({"symbol": symbol, "gap_pct": round(gap, 3), "atr_pct": round(a / prev * 100, 3),
                    "prev_close": prev, "open": today[1]})
    return out if seen_day else None


def killmode_coverage(conn: sqlite3.Connection, days: list[date]) -> dict[str, dict]:
    """Lecture seule. Pour chaque séance D : actions de toute la base (journalier Alpaca) ouvertes en gap
    ≥ KILL_GAP_PCT avec un ATR de Wilder 14 j ≥ KILL_ATR_PCT (séances d'avant D, 15 au moins), et combien
    sont dans l'univers du labo de D, par prix de la veille : {jour: {tranche: [actions, dans l'univers]}}.
    Une séance dont le journalier n'est pas encore en base est sautée."""
    out: dict[str, dict] = {}
    for day in days:
        found = gappers(conn, day)
        if found is None:
            continue
        listed = {s for (s,) in conn.execute("SELECT symbol FROM universe WHERE market = 'stock' AND day = ?",
                                             (day.isoformat(),))}
        counts = {label: [0, 0] for _, _, label in PRICE_BUCKETS}
        for g in found:
            cell = counts[next(label for lo, hi, label in PRICE_BUCKETS if lo <= g["prev_close"] < hi)]
            cell[0] += 1
            cell[1] += g["symbol"] in listed
        out[day.isoformat()] = counts
    return out


def coverage_step(db: LabDB, limit: int) -> int:
    """Worker : compte la couverture des séances d'actions de l'univers pas encore comptées (hors bloc scellé),
    `limit` séances par passage. Renvoie le nombre de séances comptées."""
    done = db.keys(COVERAGE_JOB)
    last = db.conn.execute("SELECT MAX(ts) FROM bars WHERE market = 'stock' AND tf = 1440").fetchone()[0]
    if last is None:
        return 0
    known = CO.ny_day(last).isoformat()  # pas de séance dont le journalier n'est pas encore collecté
    days = [date.fromisoformat(d) for (d,) in db.conn.execute(
        "SELECT DISTINCT day FROM universe WHERE market = 'stock' AND day <= ? ORDER BY day", (known,))
        if d not in done and not SEAL.sealed("replay", d)][:limit]
    got = killmode_coverage(db.conn, days)
    with db.transaction():
        for day in days:  # séance antérieure au dernier journalier mais absente de la base : notée vide
            if day.isoformat() in got or day.isoformat() < known:
                db.set(COVERAGE_JOB, day.isoformat(), got.get(day.isoformat(), {}))
    return len(got)


def regime(daily: list[dict], cutoff: int, annual_days: int) -> dict | None:
    """Tendance de l'indice de référence (SPY ou BTC) avant la décision : écart à la MM 50 jours et
    volatilité réalisée sur 20 jours (annualisée, en %)."""
    rows = [r for r in daily if r["t"] < cutoff]
    if len(rows) < 51:
        return None
    closes = [r["c"] for r in rows]
    ma50 = sum(closes[-50:]) / 50
    rets = [math.log(b / a) for a, b in zip(closes[-21:-1], closes[-20:]) if a > 0 and b > 0]
    mean = sum(rets) / len(rets) if rets else 0.0
    var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1) if len(rets) > 1 else 0.0
    return {"above_ma50": closes[-1] > ma50, "dist_ma50_pct": round((closes[-1] / ma50 - 1) * 100, 2),
            "vol20_pct": round(math.sqrt(var) * math.sqrt(annual_days) * 100, 1),
            "change_5d_pct": round((closes[-1] / closes[-6] - 1) * 100, 2) if len(closes) > 6 else None}

