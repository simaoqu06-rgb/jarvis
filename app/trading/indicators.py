"""Indicateurs et figures de la formation EmperorBTC, calculés sur des bougies {t, o, h, l, c, v}.

Chaque fonction est pure (pas de réseau) : elle prend les bougies de la plus ancienne à la plus
récente et renvoie des nombres ou des booléens simples.
"""
from datetime import datetime, timezone
from statistics import mean
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")


def sma(values: list[float], period: int) -> float | None:
    return mean(values[-period:]) if len(values) >= period else None


def ema_series(values: list[float], period: int) -> list[float]:
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi_series(closes: list[float], period: int = 14) -> list[float | None]:
    """RSI de Wilder, None tant qu'il n'y a pas assez de bougies."""
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = [max(b - a, 0) for a, b in zip(closes, closes[1:])]
    losses = [max(a - b, 0) for a, b in zip(closes, closes[1:])]
    avg_g, avg_l = mean(gains[:period]), mean(losses[:period])
    for i in range(period, len(closes)):
        if i > period:
            avg_g = (avg_g * (period - 1) + gains[i - 1]) / period
            avg_l = (avg_l * (period - 1) + losses[i - 1]) / period
        out[i] = 100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
    return out


def stoch_rsi(closes: list[float], rsi_len: int = 14, stoch_len: int = 14, k: int = 3, d: int = 3) -> tuple[list, list]:
    """Stoch RSI (14, 14, 3, 3) : lignes K (rapide) et D (lente), de 0 à 100."""
    rsis = [r for r in rsi_series(closes, rsi_len) if r is not None]
    raw = []
    for i in range(len(rsis)):
        window = rsis[max(0, i - stoch_len + 1): i + 1]
        lo, hi = min(window), max(window)
        raw.append(50.0 if hi == lo else (rsis[i] - lo) / (hi - lo) * 100)
    k_line = [mean(raw[max(0, i - k + 1): i + 1]) for i in range(len(raw))]
    d_line = [mean(k_line[max(0, i - d + 1): i + 1]) for i in range(len(k_line))]
    return k_line, d_line


def session_key(t: int, market: str) -> str:
    """Séance de la bougie : jour de New York pour les actions, jour UTC pour la crypto."""
    tz = NEW_YORK if market == "stock" else timezone.utc
    return datetime.fromtimestamp(t, tz).date().isoformat()


def current_session(candles: list[dict], market: str, now: datetime | None = None) -> str:
    """Séance « du jour ».

    Crypto : jour UTC de la dernière bougie (inchangé). Actions : jour de New York de `now`
    (maintenant par défaut) s'il est plus récent que la dernière bougie. Avant l'ouverture, la
    dernière bougie est celle d'une séance terminée : la séance du jour n'a encore aucune bougie,
    la « veille » est bien la dernière séance complète et le VWAP du jour est vide.
    """
    last = session_key(candles[-1]["t"], market)
    if market != "stock":
        return last
    now = now or datetime.now(timezone.utc)
    return max(last, session_key(int(now.timestamp()), market))


def vwap(candles: list[dict], market: str, now: datetime | None = None) -> float | None:
    """VWAP de la séance en cours (prix typique pondéré par le volume) ; None si elle n'a pas commencé."""
    today = current_session(candles, market, now)
    num = den = 0.0
    for c in reversed(candles):
        if session_key(c["t"], market) != today:
            break
        typical = (c["h"] + c["l"] + c["c"]) / 3
        num += typical * c["v"]
        den += c["v"]
    return num / den if den else None


def previous_session(candles: list[dict], market: str, now: datetime | None = None) -> dict | None:
    """Plus haut, plus bas et clôture de la séance précédente (pour les points pivots)."""
    today = current_session(candles, market, now)
    prev_day, rows = None, []
    for c in reversed(candles):
        day = session_key(c["t"], market)
        if day == today:
            continue
        if prev_day is None:
            prev_day = day
        if day != prev_day:
            break
        rows.append(c)
    if not rows:
        return None
    return {"h": max(c["h"] for c in rows), "l": min(c["l"] for c in rows), "c": rows[0]["c"], "day": prev_day}


def pivots(prev: dict) -> dict:
    """Points pivots standard. Toutes ces lignes sont des supports ou résistances (EmperorBTC)."""
    p = (prev["h"] + prev["l"] + prev["c"]) / 3
    rng = prev["h"] - prev["l"]
    return {"P": p, "R1": 2 * p - prev["l"], "S1": 2 * p - prev["h"], "R2": p + rng, "S2": p - rng,
            "R3": prev["h"] + 2 * (p - prev["l"]), "S3": prev["l"] - 2 * (prev["h"] - p)}


def swings(candles: list[dict], side: int = 3) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Sommets et creux de swing : plus haut (plus bas) que les `side` bougies de chaque côté."""
    highs, lows = [], []
    for i in range(side, len(candles) - side):
        window = candles[i - side: i + side + 1]
        if candles[i]["h"] == max(c["h"] for c in window):
            highs.append((i, candles[i]["h"]))
        if candles[i]["l"] == min(c["l"] for c in window):
            lows.append((i, candles[i]["l"]))
    return highs, lows


def relative_volume(candles: list[dict], period: int = 14) -> float | None:
    """Volume de la dernière bougie close comparé à la moyenne des `period` précédentes (EmperorBTC : 14)."""
    vols = [c["v"] for c in candles]
    if len(vols) < period + 1:
        return None
    avg = mean(vols[-period - 1:-1])
    return vols[-1] / avg if avg else None


def _body(c: dict) -> float:
    return abs(c["c"] - c["o"])


def is_hammer(c: dict) -> bool:
    """Marteau : mèche basse > 2 fois le corps, mèche haute presque nulle (couleur indifférente)."""
    body = max(_body(c), 1e-12)
    lower = min(c["o"], c["c"]) - c["l"]
    upper = c["h"] - max(c["o"], c["c"])
    return lower > 2 * body and upper <= 0.3 * body + 0.1 * (c["h"] - c["l"])


def is_shooting_star(c: dict) -> bool:
    body = max(_body(c), 1e-12)
    upper = c["h"] - max(c["o"], c["c"])
    lower = min(c["o"], c["c"]) - c["l"]
    return upper > 2 * body and lower <= 0.3 * body + 0.1 * (c["h"] - c["l"])


def engulfing(prev: dict, cur: dict) -> str | None:
    """« haussier » si une bougie verte englobe le corps rouge précédent, « baissier » à l'inverse."""
    if prev["c"] < prev["o"] and cur["c"] > cur["o"] and cur["c"] >= prev["o"] and cur["o"] <= prev["c"]:
        return "haussier"
    if prev["c"] > prev["o"] and cur["c"] < cur["o"] and cur["c"] <= prev["o"] and cur["o"] >= prev["c"]:
        return "baissier"
    return None


def rsi_divergence(candles: list[dict], lookback: int = 60, want: str | None = None) -> str | None:
    """Divergence RSI forte sur les deux derniers creux (ou sommets) de clôture.

    `want` (« haussière » ou « baissière ») : ne cherche que ce sens ; sans lui, la haussière passe
    avant et masquerait une baissière présente en même temps.

    Haussière : le prix fait un plus bas plus bas, le RSI un plus bas plus haut.
    Baissière : le prix fait un plus haut plus haut, le RSI un plus haut plus bas.
    On regarde les clôtures (le RSI est calculé sur la clôture), comme dans la formation.
    """
    closes = [c["c"] for c in candles]
    rsis = rsi_series(closes)
    start = max(len(closes) - lookback, 15)
    lows, highs = [], []
    for i in range(start + 2, len(closes) - 2):
        window = closes[i - 2: i + 3]
        if closes[i] == min(window):
            lows.append(i)
        if closes[i] == max(window):
            highs.append(i)
    if len(lows) >= 2 and want != "baissière":
        a, b = lows[-2], lows[-1]
        if closes[b] < closes[a] and rsis[b] is not None and rsis[a] is not None and rsis[b] > rsis[a]:
            if b >= len(closes) - 10:
                return "haussière"
    if len(highs) >= 2 and want != "haussière":
        a, b = highs[-2], highs[-1]
        if closes[b] > closes[a] and rsis[b] is not None and rsis[a] is not None and rsis[b] < rsis[a]:
            if b >= len(closes) - 10:
                return "baissière"
    return None


def weis_anomaly(candles: list[dict], period: int = 14) -> bool:
    """Anomalie corps/volume (David Weis) en tendance haussière : volume qui bondit,
    petit corps et longue mèche haute. Signal de sortie pour un long."""
    if len(candles) < period + 2:
        return False
    c = candles[-1]
    rv = relative_volume(candles, period)
    rng = c["h"] - c["l"]
    if not rv or rng <= 0:
        return False
    upper = c["h"] - max(c["o"], c["c"])
    rising = candles[-1]["c"] > candles[-period]["c"]
    return rising and rv >= 2 and _body(c) <= 0.3 * rng and upper >= 0.5 * rng


def weis_anomaly_short(candles: list[dict], period: int = 14) -> bool:
    """Symétrique pour un short : volume qui bondit, petit corps, longue mèche basse en tendance baissière."""
    mirrored = [{"t": c["t"], "o": -c["o"], "h": -c["l"], "l": -c["h"], "c": -c["c"], "v": c["v"]} for c in candles]
    return weis_anomaly(mirrored, period)
