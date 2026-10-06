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
from datetime import date, datetime, time

from app.lab import collector as CO
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
    if market == "stock":
        cutoff = CO.ny_midnight(day)
        daily = db.daily_cross_section("stock", cutoff - 40 * 86400, cutoff)
        rows = stock_universe(daily, day)
    else:
        cutoff = CO.utc_midnight(day)
        daily = db.daily_cross_section("crypto", cutoff - 40 * 86400, cutoff)
        rows = crypto_universe(daily, day)
    if save:
        db.replace_universe(market, day.isoformat(), rows)
    return rows


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

