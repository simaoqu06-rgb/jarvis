"""Régime de marché de chaque jour, sans regard vers l'avenir (table regimes).

Indice de référence : SPY pour les actions, BTC-USDT pour la crypto (bougies journalières de la base).
Le régime du jour D ne lit que les bougies closes avant D (clôture de D-1) :
- tendance : « haussier » si la clôture de D-1 est au-dessus de sa moyenne mobile 50 jours, sinon
  « baissier » ;
- volatilité : volatilité réalisée sur 20 jours à la clôture de D-1, classée en tiers (« calme »,
  « normal », « agite ») par rapport aux 252 dernières valeurs connues à cette date (jamais aux
  valeurs futures). Moins de 60 valeurs connues : tiers inconnu.
Étiquette : « haussier_calme », « baissier_agite »… ou « inconnu » faute d'historique.
"""
import json
import math
from bisect import bisect_left
from datetime import date

from app.lab import collector as CO
from app.lab import generator as G
from app.lab.db import LabDB

MA_DAYS = 50
VOL_DAYS = 20
TERCILE_WINDOW = 252
TERCILE_MIN = 60
TERCILES = ("calme", "normal", "agite")


def _cutoff(market: str, day: date) -> int:
    return CO.ny_midnight(day) if market == "stock" else CO.utc_midnight(day)


def _vol(closes: list[float], i: int) -> float | None:
    """Volatilité réalisée (écart type des rendements log, en %) des 20 jours finissant à i."""
    if i < VOL_DAYS:
        return None
    rets = [math.log(closes[k] / closes[k - 1]) for k in range(i - VOL_DAYS + 1, i + 1)
            if closes[k] > 0 and closes[k - 1] > 0]
    if len(rets) < 2:
        return None
    m = sum(rets) / len(rets)
    return math.sqrt(sum((x - m) ** 2 for x in rets) / (len(rets) - 1)) * 100


def series(daily: list[dict], market: str, days: list[date]) -> dict[str, dict]:
    """Régime de chaque jour de `days` (fonction pure) à partir des bougies journalières de l'indice."""
    daily = sorted(daily, key=lambda b: b["t"])
    starts = [b["t"] for b in daily]
    closes = [b["c"] for b in daily]
    vols = [_vol(closes, i) for i in range(len(closes))]
    out = {}
    for day in days:
        j = bisect_left(starts, _cutoff(market, day)) - 1  # dernière bougie close avant D
        if j < MA_DAYS - 1:
            out[day.isoformat()] = {"trend": None, "vol_tercile": None, "label": "inconnu", "metrics": {}}
            continue
        ma = sum(closes[j - MA_DAYS + 1:j + 1]) / MA_DAYS
        trend = "haussier" if closes[j] > ma else "baissier"
        known = [v for v in vols[max(0, j - TERCILE_WINDOW + 1):j + 1] if v is not None]
        vol = vols[j]
        tercile = None
        if vol is not None and len(known) >= TERCILE_MIN:
            ranked = sorted(known)
            lo, hi = ranked[len(ranked) // 3], ranked[2 * len(ranked) // 3]
            tercile = 0 if vol < lo else (1 if vol < hi else 2)
        label = f"{trend}_{TERCILES[tercile]}" if tercile is not None else f"{trend}_inconnu"
        out[day.isoformat()] = {"trend": trend, "vol_tercile": tercile, "label": label,
                                "metrics": {"dist_ma50_pct": round((closes[j] / ma - 1) * 100, 2),
                                            "vol20_pct": round(vol, 3) if vol is not None else None,
                                            "as_of": starts[j]}}
    return out


def refresh(db: LabDB, market: str, limit: int = 2000) -> int:
    """Calcule le régime des jours qui ont des trades et pas encore de régime connu (ou « inconnu »)."""
    days = [date.fromisoformat(d) for (d,) in db.conn.execute(
        "SELECT DISTINCT t.day FROM trades t LEFT JOIN regimes r ON r.market = t.market AND r.day = t.day "
        "WHERE t.market = ? AND (r.day IS NULL OR r.label = 'inconnu') ORDER BY t.day LIMIT ?", (market, limit))]
    if not days:
        return 0
    first = _cutoff(market, days[0]) - 500 * 86400
    daily = db.get_bars(market, G.INDEX[market], 1440, first, _cutoff(market, days[-1]))
    rows = series(daily, market, days)
    with db.transaction():
        db.conn.executemany("INSERT OR REPLACE INTO regimes VALUES (?, ?, ?, ?, ?, ?)",
                            [(market, d, r["trend"], r["vol_tercile"], r["label"], json.dumps(r["metrics"]))
                             for d, r in rows.items()])
    return len(rows)
