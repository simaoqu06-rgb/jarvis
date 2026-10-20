"""Dérivés crypto (lot B, 8 octobre 2026) : funding et open interest des perpétuels USDT, pour mesurer si
les stratégies du labo v2 font mieux ou moins bien selon le positionnement des dérivés.

Source : Bybit (historique public sans clé ; OKX ne garde que ~3 mois de funding). Le labo trade le
comptant OKX ; le perpétuel Bybit du même actif ne sert que de thermomètre. Une paire OKX « XXX-USDT »
correspond au perpétuel « XXXUSDT », ou « 1000XXXUSDT » pour les petites unités ; sans perpétuel, rien.

Le worker remplit la table derivs par fenêtres (FUNDING_WINDOW, OI_WINDOW), du début de l'univers crypto
du labo jusqu'à maintenant, dans un budget d'appels par passage. Rien ici ne filtre ni ne note : le tableau
des stratégies (strat_stats) montre seulement l'espérance de chaque stratégie crypto par tranche de funding
et de variation d'open interest sur 24 h, comme les tranches de frais (aucun essai en plus). Une tranche
qui tiendrait deviendrait un challenger déclaré, à faire valider par Simão.

Connu au moment du signal, rien de plus :
- funding : le dernier taux réglé avant le signal (en %, par période de règlement, souvent 8 h ; le
  taux « neutre » de Bybit est 0,01 %) ;
- open interest : le dernier point de 4 h dont l'intervalle est clos avant le signal, comparé au point
  24 h plus tôt (variation en %).
"""
import logging
from bisect import bisect_right
from datetime import datetime

from app.core.http import SourceError
from app.lab import sources as SRC
from app.lab.db import LabDB

log = logging.getLogger(__name__)
JOB = "derivs"
OI_INTERVAL = 4 * 3600
FUNDING_WINDOW = 60 * 86400   # 200 réglements de 8 h ≈ 66 jours : une page
OI_WINDOW = 30 * 86400        # 200 points de 4 h ≈ 33 jours : une page
LOOKBACK = 2 * 86400          # avant le premier jour de l'univers : l'open interest de la veille
FUNDING_BUCKETS = ((-1e9, 0.0, "funding < 0"), (0.0, 0.0101, "funding 0 à 0,01 %"),
                   (0.0101, 0.03, "funding 0,01 à 0,03 %"), (0.03, 1e9, "funding ≥ 0,03 %"))
OI_BUCKETS = ((-1e9, -10.0, "OI 24 h ≤ −10 %"), (-10.0, -3.0, "OI 24 h −10 à −3 %"), (-3.0, 3.0, "OI 24 h ±3 %"),
              (3.0, 10.0, "OI 24 h +3 à +10 %"), (10.0, 1e9, "OI 24 h ≥ +10 %"))
# Déclaration n° 11 (piste B, vidéo kara) : open interest gagné sur 7 jours, régime de purge au-delà de +9 %
OI7_HOURS = 168
OI7_BUCKETS = ((-1e9, 0.0, "OI 7 j ≤ 0 %"), (0.0, 2.0, "OI 7 j 0 à 2 %"), (2.0, 4.0, "OI 7 j 2 à 4 %"),
               (4.0, 6.0, "OI 7 j 4 à 6 %"), (6.0, 9.0, "OI 7 j 6 à 9 %"), (9.0, 1e9, "OI 7 j ≥ 9 %"))


def candidates(okx_symbol: str) -> list[str]:
    base, _, quote = okx_symbol.partition("-")
    if quote != "USDT" or not base:
        return []
    return [f"{base}USDT", f"1000{base}USDT"]


def bucket(value: float | None, buckets) -> str:
    if value is None:
        return "inconnu"
    return next(label for lo, hi, label in buckets if lo <= value < hi)


# --- Collecte (worker) ----------------------------------------------------------------------

async def _map(db: LabDB, symbol: str, now: int, transport=None) -> tuple[str | None, int]:
    """Perpétuel Bybit de la paire OKX (mémorisé ; "" = aucun) et nombre d'appels faits."""
    known = db.get(JOB, f"map:{symbol}")
    if known is not None:
        return known or None, 0
    calls = 0
    for cand in candidates(symbol):
        calls += 1
        try:
            if await SRC.bybit_funding(cand, now - 30 * 86400, now, transport):
                db.set(JOB, f"map:{symbol}", cand)
                return cand, calls
        except SourceError as exc:
            if "10001" not in str(exc):  # 10001 : symbole inconnu de Bybit ; le reste est une panne
                raise
    db.set(JOB, f"map:{symbol}", "")
    return None, calls


def _store(db: LabDB, symbol: str, kind: str, points: list[tuple[int, float]]) -> int:
    with db.transaction():
        db.conn.executemany("INSERT OR REPLACE INTO derivs (symbol, kind, t, value) VALUES (?, ?, ?, ?)",
                            [(symbol, kind, t, v) for t, v in points])
    return len(points)


async def collect(db: LabDB, now: datetime, max_calls: int, transport=None) -> dict:
    """Remplit derivs pour les cryptos de l'univers du labo, au plus `max_calls` appels Bybit."""
    row = db.conn.execute("SELECT MIN(day) FROM universe WHERE market = 'crypto'").fetchone()
    if not row or not row[0] or max_calls <= 0:
        return {"appels": 0, "points": 0}
    first = int(datetime.fromisoformat(row[0] + "T00:00:00+00:00").timestamp()) - LOOKBACK
    end = int(now.timestamp()) // 3600 * 3600
    symbols = [r[0] for r in db.conn.execute("SELECT DISTINCT symbol FROM universe WHERE market = 'crypto' "
                                             "ORDER BY symbol")]
    calls = points = 0
    for symbol in symbols:
        if calls >= max_calls:
            break
        try:
            perp, used = await _map(db, symbol, end, transport)
            calls += used
            if not perp:
                continue
            for kind, window in (("funding", FUNDING_WINDOW), ("oi", OI_WINDOW)):
                while calls < max_calls:
                    cursor = db.get(JOB, f"{kind}:{symbol}", first)
                    if cursor >= end - (OI_INTERVAL if kind == "oi" else 3600):
                        break
                    stop = min(cursor + window, end)
                    fetch = SRC.bybit_funding if kind == "funding" else SRC.bybit_open_interest
                    got = await fetch(perp, cursor, stop, transport=transport)
                    calls += 1
                    points += _store(db, symbol, kind, got)
                    db.set(JOB, f"{kind}:{symbol}", stop)
        except SourceError as exc:
            log.warning("Labo : dérivés Bybit %s en échec (%s)", symbol, exc)
            calls += 1
    return {"appels": calls, "points": points}


# --- Lecture (statistiques) -----------------------------------------------------------------

def load(conn) -> dict:
    """{symbole: {"funding": ([t], [taux]), "oi": ([t], [oi])}} ; vide si la table manque."""
    out: dict = {}
    try:
        rows = conn.execute("SELECT symbol, kind, t, value FROM derivs ORDER BY symbol, kind, t")
        for symbol, kind, t, v in rows:
            ts, vals = out.setdefault(symbol, {}).setdefault(kind, ([], []))
            ts.append(t)
            vals.append(v)
    except Exception:
        return {}
    return out


def context(series: dict, symbol: str, ts: int | None) -> tuple[float | None, float | None]:
    """(funding en %, variation d'open interest sur 24 h en %) connus à `ts`."""
    s = series.get(symbol)
    if not s or ts is None:
        return None, None
    funding = oi_chg = None
    if "funding" in s:
        ts_f, vals = s["funding"]
        k = bisect_right(ts_f, ts)
        if k:
            funding = vals[k - 1] * 100
    if "oi" in s:
        oi_chg = oi_change(s, ts, 24)
    return funding, oi_chg


def oi_change(s: dict, ts: int, hours: int) -> float | None:
    """Variation d'open interest en % : dernier point de 4 h clos avant `ts` contre le point `hours` h plus tôt."""
    if "oi" not in s:
        return None
    ts_o, vals = s["oi"]
    k = bisect_right(ts_o, ts - OI_INTERVAL)  # intervalle de 4 h clos avant le signal
    if not k:
        return None
    back = ts_o[k - 1] - hours * 3600
    j = bisect_right(ts_o, back)
    if j and ts_o[j - 1] == back and vals[j - 1]:
        return (vals[k - 1] / vals[j - 1] - 1) * 100
    return None


def oi7(series: dict, symbol: str, ts: int | None) -> float | None:
    """Variation d'open interest sur 7 jours connue à `ts` (n° 11), None si inconnue."""
    s = series.get(symbol)
    return oi_change(s, ts, OI7_HOURS) if s and ts is not None else None
