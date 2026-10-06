"""Calcul d'un plan de trade selon la formation EmperorBTC (« Trident de trading ») :
déclencheur d'entrée, niveau d'invalidation (stop) et objectifs sur les prochains supports/résistances.

Tout est calculé sur des bougies {t, o, h, l, c, v} rangées de la plus ancienne à la plus récente.
Le délai de chaque TP n'est pas deviné : on rejoue le même plan (mêmes écarts en %) depuis chaque
bougie de l'historique de l'actif et on mesure combien de temps il a fallu pour toucher chaque TP
avant le stop. Une bougie qui touche à la fois le TP et le stop compte comme un stop (prudence).
"""
from dataclasses import dataclass, field
from statistics import median

from app.trading import indicators as I

ATR_PERIOD = 14
SWING_LOOKBACK = 24      # bougies où chercher le creux (ou sommet) d'invalidation
TARGET_LOOKBACK = 300    # bougies où chercher les résistances (ou supports) des objectifs
STOP_BUFFER_ATR = 0.25   # stop un peu au-delà de l'invalidation, contre la chasse aux stops
TARGET_BUFFER_ATR = 0.1  # TP un peu avant le niveau, là où le prix risque de se retourner
MIN_TARGET_R = 0.5       # objectifs espacés d'au moins 0,5 R (de l'entrée, puis entre eux)
MERGE_ATR = 0.3          # niveaux plus proches que ça = une seule zone


@dataclass
class Target:
    price: float
    source: str  # d'où vient le niveau : "pivot R1", "sommet de swing", "2 R"…


@dataclass
class Levels:
    side: str            # "long" ou "short"
    entry: float
    stop: float
    targets: list[Target]
    stop_source: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def sign(self) -> int:
        return 1 if self.side == "long" else -1

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop)

    def pct(self, price: float) -> float:
        """Écart en % depuis l'entrée, positif dans le sens du trade."""
        return self.sign * (price - self.entry) / self.entry * 100


def atr(candles: list[dict], period: int = ATR_PERIOD) -> float:
    """Average True Range (moyenne simple des `period` dernières vraies amplitudes)."""
    ranges = []
    for prev, cur in zip(candles, candles[1:]):
        ranges.append(max(cur["h"] - cur["l"], abs(cur["h"] - prev["c"]), abs(cur["l"] - prev["c"])))
    recent = ranges[-period:]
    if not recent:
        raise ValueError("pas assez de bougies pour l'ATR")
    return sum(recent) / len(recent)


def _invalidation(candles: list[dict], side: str, entry: float, a: float) -> tuple[float, str]:
    """Stop = niveau d'invalidation : sous le dernier creux de swing (au-dessus du dernier sommet
    pour un short), un peu au-delà pour éviter la chasse aux stops. Entre 1 et 3 ATR de l'entrée."""
    sign = 1 if side == "long" else -1
    recent = candles[-SWING_LOOKBACK:]
    highs, lows = I.swings(recent, side=2)
    if side == "long":
        below = [p for _, p in lows if p < entry]
        level, source = (below[-1], "sous le dernier creux de swing") if below else (min(c["l"] for c in recent), "sous le plus bas récent")
    else:
        above = [p for _, p in highs if p > entry]
        level, source = (above[-1], "au-dessus du dernier sommet de swing") if above else (max(c["h"] for c in recent), "au-dessus du plus haut récent")
    distance = abs(entry - level) + STOP_BUFFER_ATR * a
    clamped = min(max(distance, 1.0 * a), 3.0 * a)
    if clamped != distance:
        source += f" (ramené à {clamped / a:.1f} ATR)"
    return entry - sign * clamped, source


def _candidate_levels(candles: list[dict], market: str, side: str, entry: float) -> list[Target]:
    """Résistances au-dessus de l'entrée (supports en dessous pour un short) : sommets de swing,
    points pivots de la séance, plus haut et plus bas de la séance précédente."""
    sign = 1 if side == "long" else -1
    out = []
    highs, lows = I.swings(candles[-TARGET_LOOKBACK:], side=3)
    for _, price in (highs if side == "long" else lows):
        out.append(Target(price, "sommet de swing" if side == "long" else "creux de swing"))
    prev = I.previous_session(candles, market)
    if prev:
        for name, price in I.pivots(prev).items():
            out.append(Target(price, f"pivot {name}"))
        out.append(Target(prev["h"], "plus haut de la veille"))
        out.append(Target(prev["l"], "plus bas de la veille"))
    return sorted((t for t in out if (t.price - entry) * sign > 0), key=lambda t: abs(t.price - entry))


def _merge(levels: list[Target], a: float) -> list[Target]:
    """Regroupe les niveaux proches en zones ; on garde le plus proche de l'entrée et cite les sources."""
    zones: list[Target] = []
    for t in levels:
        if zones and abs(t.price - zones[-1].price) < MERGE_ATR * a:
            if t.source not in zones[-1].source:
                zones[-1].source += f" + {t.source}"
            continue
        zones.append(Target(t.price, t.source))
    return zones


def default_levels(
    candles: list[dict], side: str, entry: float | None = None, stop: float | None = None,
    targets: list[float] | None = None, market: str = "crypto", count: int = 3,
) -> Levels:
    """Complète les niveaux non fournis selon la méthode EmperorBTC.

    Stop : niveau d'invalidation (dernier creux/sommet de swing) avec une marge, entre 1 et 3 ATR.
    TP : les prochaines résistances (supports pour un short) à au moins 0,5 R, juste avant le niveau ;
    s'il en manque, on complète par des multiples de R au-delà du dernier objectif.
    """
    if side not in ("long", "short"):
        raise ValueError("side doit valoir long ou short")
    entry = entry or candles[-1]["c"]
    a = atr(candles)
    sign = 1 if side == "long" else -1
    stop_source = "donné"
    if stop is None:
        stop, stop_source = _invalidation(candles, side, entry, a)
    if (entry - stop) * sign <= 0:
        raise ValueError("le stop doit être sous l'entrée pour un long, au-dessus pour un short")
    risk = abs(entry - stop)
    if targets:
        chosen = [Target(t, "donné") for t in targets]
        if any((t.price - entry) * sign <= 0 for t in chosen):
            raise ValueError("les TP doivent être au-dessus de l'entrée pour un long, en dessous pour un short")
    else:
        zones = _merge(_candidate_levels(candles, market, side, entry), a)
        chosen = []
        for z in zones:
            price = z.price - sign * TARGET_BUFFER_ATR * a
            far_enough = abs(price - entry) >= MIN_TARGET_R * risk
            spaced = not chosen or (price - chosen[-1].price) * sign >= MIN_TARGET_R * risk
            if far_enough and spaced:
                chosen.append(Target(price, z.source))
            if len(chosen) == count:
                break
        while len(chosen) < count:
            base = chosen[-1].price if chosen else entry
            r_from_entry = abs(base - entry) / risk
            nxt = entry + sign * risk * (int(r_from_entry) + 1)
            chosen.append(Target(nxt, f"{abs(nxt - entry) / risk:.0f} R (pas de niveau plus loin)"))
    chosen.sort(key=lambda t: t.price, reverse=(side == "short"))
    return Levels(side, round(entry, 6), round(stop, 6), [Target(round(t.price, 6), t.source) for t in chosen], stop_source)


def _quantile(sorted_values: list[float], q: float) -> float:
    index = min(int(q * len(sorted_values)), len(sorted_values) - 1)
    return sorted_values[index]


def historical_timing(candles: list[dict], levels: Levels, horizon_bars: int) -> dict:
    """Rejoue le plan (mêmes écarts en %) depuis chaque bougie passée.

    Pour chaque TP : part des départs où il a été touché avant le stop et dans l'horizon,
    part des départs où le stop est venu avant lui, et nombre de bougies qu'il a fallu
    (médiane, 25 % et 75 % des cas).
    """
    sign = levels.sign
    stop_pct = abs(levels.stop - levels.entry) / levels.entry
    target_pcts = [abs(t.price - levels.entry) / levels.entry for t in levels.targets]
    starts = len(candles) - 1 - horizon_bars
    if starts < 30:
        raise ValueError("historique trop court pour estimer les délais")
    hits: list[list[int]] = [[] for _ in target_pcts]
    stopped_before = [0] * len(target_pcts)
    stops = 0
    for i in range(starts):
        base = candles[i]["c"]
        stop_px = base * (1 - sign * stop_pct)
        target_px = [base * (1 + sign * p) for p in target_pcts]
        reached = 0
        for j in range(1, horizon_bars + 1):
            bar = candles[i + j]
            if bar["l"] <= stop_px if sign == 1 else bar["h"] >= stop_px:
                stops += 1
                for k in range(reached, len(target_px)):
                    stopped_before[k] += 1
                break
            while reached < len(target_px) and (
                bar["h"] >= target_px[reached] if sign == 1 else bar["l"] <= target_px[reached]
            ):
                hits[reached].append(j)
                reached += 1
            if reached == len(target_px):
                break
    out = []
    for bars, stopped in zip(hits, stopped_before):
        bars.sort()
        out.append({
            "hit_rate": round(len(bars) / starts, 3),
            "stop_rate": round(stopped / starts, 3),
            "bars_median": median(bars) if bars else None,
            "bars_p25": _quantile(bars, 0.25) if bars else None,
            "bars_p75": _quantile(bars, 0.75) if bars else None,
            "samples": len(bars),
        })
    return {"starts": starts, "stop_first_rate": round(stops / starts, 3), "targets": out}


def expectancy(r_multiple: float, hit_rate: float, stop_rate: float) -> dict:
    """Espérance en R si tout est vendu à ce TP (les cas ni TP ni stop comptent pour 0).

    EmperorBTC : « choisissez un R:R qui correspond à votre taux de gain ». Le R:R minimal
    pour être rentable avec ce taux de réussite est stop_rate / hit_rate.
    """
    decided = hit_rate + stop_rate
    win_rate = hit_rate / decided if decided else 0.0
    breakeven = (stop_rate / hit_rate) if hit_rate else None
    return {
        "win_rate": round(win_rate, 3),
        "breakeven_r": round(breakeven, 2) if breakeven is not None else None,
        "expectancy_r": round(hit_rate * r_multiple - stop_rate, 2),
    }
