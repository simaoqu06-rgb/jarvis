"""Calcul d'un plan de trade : entrée, stop, TP1/TP2/TP3 et délai estimé pour toucher chaque TP.

Tout est calculé sur des bougies {t, o, h, l, c, v} rangées de la plus ancienne à la plus récente.
Le délai n'est pas deviné : on rejoue le même plan (mêmes écarts en %) depuis chaque bougie
de l'historique de l'actif et on mesure combien de temps il a fallu pour toucher chaque TP
avant le stop. Une bougie qui touche à la fois le TP et le stop compte comme un stop (prudence).
"""
from dataclasses import dataclass
from statistics import median

DEFAULT_R_MULTIPLES = (1.0, 2.0, 3.0)
ATR_PERIOD = 14
SWING_BARS = 12


@dataclass
class Levels:
    side: str            # "long" ou "short"
    entry: float
    stop: float
    targets: list[float]

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop)

    def pct(self, price: float) -> float:
        """Écart en % depuis l'entrée, positif dans le sens du trade."""
        sign = 1 if self.side == "long" else -1
        return sign * (price - self.entry) / self.entry * 100


def atr(candles: list[dict], period: int = ATR_PERIOD) -> float:
    """Average True Range (moyenne simple des `period` dernières vraies amplitudes)."""
    ranges = []
    for prev, cur in zip(candles, candles[1:]):
        ranges.append(max(cur["h"] - cur["l"], abs(cur["h"] - prev["c"]), abs(cur["l"] - prev["c"])))
    recent = ranges[-period:]
    if not recent:
        raise ValueError("pas assez de bougies pour l'ATR")
    return sum(recent) / len(recent)


def ema(values: list[float], period: int) -> float:
    k = 2 / (period + 1)
    current = values[0]
    for v in values[1:]:
        current = v * k + current * (1 - k)
    return current


def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) <= period:
        return None
    gains = losses = 0.0
    for prev, cur in zip(closes[-period - 1:], closes[-period:]):
        change = cur - prev
        gains += max(change, 0)
        losses += max(-change, 0)
    if losses == 0:
        return 100.0
    return 100 - 100 / (1 + gains / losses)


def default_levels(
    candles: list[dict], side: str, entry: float | None = None, stop: float | None = None,
    targets: list[float] | None = None, r_multiples: tuple[float, ...] = DEFAULT_R_MULTIPLES,
) -> Levels:
    """Complète les niveaux non fournis.

    Stop : sous le dernier plus bas (au-dessus du dernier plus haut pour un short) des
    12 dernières bougies, à au moins 1 ATR et au plus 3 ATR de l'entrée.
    TP : 1 R, 2 R et 3 R, où R est la distance entrée-stop.
    """
    if side not in ("long", "short"):
        raise ValueError("side doit valoir long ou short")
    entry = entry or candles[-1]["c"]
    a = atr(candles)
    sign = 1 if side == "long" else -1
    if stop is None:
        recent = candles[-SWING_BARS:]
        swing = min(c["l"] for c in recent) if side == "long" else max(c["h"] for c in recent)
        distance = abs(entry - swing) + 0.2 * a
        distance = min(max(distance, 1.0 * a), 3.0 * a)
        stop = entry - sign * distance
    if (entry - stop) * sign <= 0:
        raise ValueError("le stop doit être sous l'entrée pour un long, au-dessus pour un short")
    risk = abs(entry - stop)
    if not targets:
        targets = [entry + sign * m * risk for m in r_multiples]
    targets = sorted(targets, reverse=(side == "short"))
    if any((t - entry) * sign <= 0 for t in targets):
        raise ValueError("les TP doivent être au-dessus de l'entrée pour un long, en dessous pour un short")
    return Levels(side, round(entry, 6), round(stop, 6), [round(t, 6) for t in targets])


def _quantile(sorted_values: list[float], q: float) -> float:
    index = min(int(q * len(sorted_values)), len(sorted_values) - 1)
    return sorted_values[index]


def historical_timing(candles: list[dict], levels: Levels, horizon_bars: int) -> dict:
    """Rejoue le plan (mêmes écarts en %) depuis chaque bougie passée.

    Renvoie, pour chaque TP : la part des départs où il a été touché avant le stop et dans
    l'horizon, et le nombre de bougies qu'il a fallu (médiane, 25 % et 75 % des cas).
    """
    sign = 1 if levels.side == "long" else -1
    stop_pct = abs(levels.stop - levels.entry) / levels.entry
    target_pcts = [abs(t - levels.entry) / levels.entry for t in levels.targets]
    starts = len(candles) - 1 - horizon_bars
    if starts < 30:
        raise ValueError("historique trop court pour estimer les délais")
    hits: list[list[int]] = [[] for _ in target_pcts]
    stops = 0
    for i in range(starts):
        base = candles[i]["c"]
        stop_px = base * (1 - sign * stop_pct)
        target_px = [base * (1 + sign * p) for p in target_pcts]
        reached = 0
        for j in range(1, horizon_bars + 1):
            bar = candles[i + j]
            stop_hit = bar["l"] <= stop_px if sign == 1 else bar["h"] >= stop_px
            if stop_hit:
                stops += 1
                break
            while reached < len(target_px) and (
                bar["h"] >= target_px[reached] if sign == 1 else bar["l"] <= target_px[reached]
            ):
                hits[reached].append(j)
                reached += 1
            if reached == len(target_px):
                break
    out = []
    for bars in hits:
        bars.sort()
        out.append({
            "hit_rate": round(len(bars) / starts, 3),
            "bars_median": median(bars) if bars else None,
            "bars_p25": _quantile(bars, 0.25) if bars else None,
            "bars_p75": _quantile(bars, 0.75) if bars else None,
            "samples": len(bars),
        })
    return {"starts": starts, "stop_first_rate": round(stops / starts, 3), "targets": out}


def context(candles: list[dict]) -> dict:
    """Tendance et élan sur les bougies fournies (EMA 20/50, RSI 14)."""
    closes = [c["c"] for c in candles]
    e20, e50 = ema(closes[-200:], 20), ema(closes[-200:], 50)
    last = closes[-1]
    if last > e20 > e50:
        trend = "haussière"
    elif last < e20 < e50:
        trend = "baissière"
    else:
        trend = "sans direction claire"
    return {"ema20": round(e20, 6), "ema50": round(e50, 6), "rsi14": _round(rsi(closes)), "trend": trend}


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def warnings(levels: Levels, timing: dict, ctx: dict) -> list[str]:
    """Points d'attention à afficher avec le plan."""
    out = []
    if levels.side == "long" and ctx["trend"] == "baissière":
        out.append("Achat contre la tendance (prix sous les EMA 20 et 50).")
    if levels.side == "short" and ctx["trend"] == "haussière":
        out.append("Vente à découvert contre la tendance (prix au-dessus des EMA 20 et 50).")
    rsi14 = ctx.get("rsi14")
    if rsi14 is not None and levels.side == "long" and rsi14 >= 75:
        out.append(f"RSI à {rsi14:.0f} : actif en surchauffe, risque de repli avant les TP.")
    if rsi14 is not None and levels.side == "short" and rsi14 <= 25:
        out.append(f"RSI à {rsi14:.0f} : actif survendu, risque de rebond avant les TP.")
    first = timing["targets"][0]["hit_rate"]
    if first < 0.4:
        out.append(f"Historiquement, le TP1 n'est touché avant le stop que dans {first:.0%} des cas.")
    return out
