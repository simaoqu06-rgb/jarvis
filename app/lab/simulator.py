"""Exécution simulée d'un plan sur les bougies de 5 min qui suivent la décision (fonction pure).

Règles (choix de Simão pour la gestion de position) :
- Entrée : ordre à cours limité au prix d'entrée du plan.
  * Ordre « au marché » (entrée au niveau du dernier prix ou au-delà, pour un long) : exécuté à
    l'ouverture de la bougie suivante si elle n'est pas au-dessus de la limite ; sinon l'ordre reste
    posé (un ordre limité n'est jamais exécuté au-dessus de sa limite, même après un gap).
  * Ordre posé : exécuté seulement si le prix traverse la limite d'au moins un pas de cotation
    (bas ≤ entrée - 1 pas pour un long), au prix min(ouverture, entrée).
  * Fenêtre : jusqu'à la clôture de la séance (actions), 24 h (crypto). Sinon « non_declenche ».
- Sorties : 1/3 au TP1, 1/3 au TP2, 1/3 au TP3 ; après le TP1, le stop remonte à l'entrée
  (break-even) et y reste après le TP2 (aucun stop suiveur inventé).
  * Stop prioritaire si une bougie touche le stop et un TP (prudence) ; une bougie qui ouvre
    au-delà du stop sort à l'ouverture (gap).
  * TP : ordre limité, exécuté si le prix le traverse d'un pas (ouverture au-delà : à l'ouverture).
  * Sur la bougie d'une entrée en cours de bougie, seul le stop compte (on ignore si le plus haut
    est venu avant l'exécution).
  * Actions : ce qui reste est soldé à la clôture de la séance d'entrée (intraday). Crypto : à
    l'horizon du plan (24 h après la décision), à la clôture de la bougie.
- Coûts (estimations que le labo affinera, toutes ici) : demi-écart + glissement par côté en points
  de base selon le prix (actions) ou le rang de volume (crypto), aucune commission sur les actions.
  Frais OKX : « maker » 0,08 % pour un ordre limité qui attendait dans le carnet (entrée posée, TP),
  « taker » 0,10 % pour ce qui prend la liquidité (entrée au marché, stop, sortie à la clôture ou à
  l'horizon).
- Résultats en R du risque prévu (|entrée - stop|) : brut et net de coûts, délai de chaque TP
  (minutes de cotation pour les actions, compté au milieu de la bougie qui le touche, comme
  plan.historical_timing), excursions maximales favorable (MFE) et défavorable (MAE, négative).
"""
from datetime import datetime, timezone

from app.trading import calendar as CAL

BAR_SECONDS = 300
# Demi-écart + glissement par côté (points de base), actions : (prix plancher, bps).
STOCK_SLIPPAGE_BPS = ((20.0, 3.0), (5.0, 6.0), (1.0, 15.0), (0.0, 40.0))
STOCK_COMMISSION = 0.0
# Crypto : selon le rang de volume du jour (univers) ; frais OKX au comptant.
CRYPTO_SLIPPAGE_BPS = {"top10": 3.0, "other": 8.0}
OKX_MAKER_FEE = 0.0008
OKX_TAKER_FEE = 0.0010
EXIT_REASONS = ("stop", "stop_be", "tp3", "cloture", "horizon", "non_declenche")


def stock_slippage_bps(price: float) -> float:
    for floor, bps in STOCK_SLIPPAGE_BPS:
        if price >= floor:
            return bps
    return STOCK_SLIPPAGE_BPS[-1][1]


def crypto_slippage_bps(volume_rank: int | None) -> float:
    return CRYPTO_SLIPPAGE_BPS["top10" if volume_rank is not None and volume_rank <= 10 else "other"]


def costs(market: str, price: float, volume_rank: int | None = None) -> dict:
    """Coût par côté : glissement (fraction) et frais (fraction du montant)."""
    if market == "stock":
        return {"slippage": stock_slippage_bps(price) / 10000, "maker": STOCK_COMMISSION, "taker": STOCK_COMMISSION}
    return {"slippage": crypto_slippage_bps(volume_rank) / 10000, "maker": OKX_MAKER_FEE, "taker": OKX_TAKER_FEE}


def tick_size(market: str, price: float) -> float:
    if market == "stock":
        return 0.01 if price >= 1 else 0.0001
    return price * 1e-4


def round_stock(price: float) -> float:
    return round(price, 2) if price >= 1 else round(price, 4)


def _minutes(market: str, start: int, end: int) -> float:
    if market == "stock":
        return round(CAL.market_minutes_between(datetime.fromtimestamp(start, timezone.utc),
                                                datetime.fromtimestamp(end, timezone.utc)), 1)
    return round((end - start) / 60, 1)


def simulate(plan: dict, bars: list[dict], *, market: str, decided_at: int, entry_until: int, exit_at: int,
             last_price: float, cost: dict, exit_reason_end: str | None = None) -> dict:
    """Rejoue le plan sur `bars` (5 min, toutes commencées à `decided_at` ou après).

    `entry_until` : fin de la fenêtre d'entrée ; `exit_at` : tout est soldé à la clôture de la
    dernière bougie finissant au plus tard à cet instant ; `last_price` : prix à la décision.
    """
    if any(b["t"] < decided_at for b in bars):
        raise ValueError("bougie antérieure à la décision : le simulateur ne voit que la suite")
    side = plan["side"]
    sign = 1 if side == "long" else -1
    entry, stop = plan["entry"], plan["stop"]
    targets = [t["price"] for t in plan["targets"]][:3]
    if market == "stock":
        entry, stop, targets = round_stock(entry), round_stock(stop), [round_stock(t) for t in targets]
    risk = abs(entry - stop) or abs(plan["entry"] - plan["stop"])
    tick = tick_size(market, entry)
    end_reason = exit_reason_end or ("cloture" if market == "stock" else "horizon")
    window = [b for b in bars if b["t"] + BAR_SECONDS <= exit_at]
    result = {"triggered": False, "filled_at": None, "fill_price": None, "entry_mode": None,
              "exit_reason": "non_declenche", "exits": [], "r_gross": None, "r_net": None,
              "tp_hits": [False] * len(targets), "minutes_to_tp": [None] * len(targets),
              "mfe_r": None, "mae_r": None, "closed_at": None, "minutes_to_fill": None,
              "slippage_bps": round(cost["slippage"] * 10000, 2), "fees": {"maker": cost["maker"], "taker": cost["taker"]},
              "entry_liquidity": None,
              "order_prices": {"entry": entry, "stop": stop, "targets": targets},
              "data_incomplete": not window or window[-1]["t"] + BAR_SECONDS < exit_at}
    if not window:
        return result

    # --- Entrée ---
    marketable = (entry - last_price) * sign >= 0
    fill_i = fill_price = mode = None
    liquidity = "maker"  # ordre posé dans le carnet, sauf exécution immédiate au marché
    for i, b in enumerate(window):
        if b["t"] >= entry_until:
            break
        if marketable and i == 0 and (b["o"] - entry) * sign <= 0:
            fill_i, fill_price, mode, liquidity = i, b["o"], "ouverture", "taker"
            break
        through = b["l"] <= entry - tick if sign == 1 else b["h"] >= entry + tick
        if through:
            opened_beyond = (b["o"] - entry) * sign < 0
            fill_i, fill_price, mode = i, (b["o"] if opened_beyond else entry), \
                ("ouverture" if opened_beyond else "limite")
            break
    if fill_i is None:
        return result
    result.update(triggered=True, filled_at=window[fill_i]["t"], fill_price=fill_price, entry_mode=mode,
                  entry_liquidity=liquidity,
                  minutes_to_fill=_minutes(market, decided_at, window[fill_i]["t"]))

    # --- Sorties ---
    remaining = 1.0
    part = 1.0 / len(targets) if targets else 1.0
    exits: list[dict] = []
    stop_now, hit = stop, 0
    best = worst = fill_price
    reason = None

    def excursion(*prices: float) -> None:
        nonlocal best, worst
        for p in prices:
            if (p - best) * sign > 0:
                best = p
            if (p - worst) * sign < 0:
                worst = p

    def leave(price: float, at: int, why: str, share: float) -> None:
        nonlocal remaining
        maker = why.startswith("tp")  # TP : ordre limité posé ; stop, clôture, horizon : au marché
        exits.append({"price": price, "at": at, "reason": why, "part": round(share, 6),
                      "fee": cost["maker"] if maker else cost["taker"]})
        remaining -= share

    last = window[fill_i]
    for i in range(fill_i, len(window)):
        b = window[i]
        last = b
        at_open = i > fill_i or mode == "ouverture"
        stop_label = "stop_be" if hit else "stop"
        # Stop d'abord : gap à l'ouverture, puis contact dans la bougie.
        if i > fill_i and (b["o"] - stop_now) * sign <= 0:
            excursion(b["o"])
            leave(b["o"], b["t"], stop_label, remaining)
            reason = stop_label
            break
        touched = b["l"] <= stop_now if sign == 1 else b["h"] >= stop_now
        if touched:
            price = stop_now if i > fill_i or (fill_price - stop_now) * sign > 0 else fill_price
            excursion(price)  # au-delà du stop, la position est déjà soldée
            leave(price, b["t"], stop_label, remaining)
            reason = stop_label
            break
        excursion(b["l"] if sign == 1 else b["h"])
        if not at_open:
            continue  # entrée en cours de bougie : on ignore son plus haut
        excursion(b["h"] if sign == 1 else b["l"])
        hit_now = False
        while hit < len(targets):
            tp = targets[hit]
            if not ((b["h"] >= tp + tick) if sign == 1 else (b["l"] <= tp - tick)):
                break
            price = b["o"] if (b["o"] - tp) * sign >= tick and i > fill_i else tp
            hit += 1
            hit_now = True
            result["tp_hits"][hit - 1] = True
            result["minutes_to_tp"][hit - 1] = _minutes(market, decided_at, b["t"] + BAR_SECONDS // 2)
            leave(price, b["t"], f"tp{hit}", part if hit < len(targets) else remaining)
            stop_now = entry  # break-even dès le TP1 (choix de Simão)
        if hit == len(targets):
            reason = f"tp{hit}"
            break
        if hit_now and ((b["l"] <= stop_now) if sign == 1 else (b["h"] >= stop_now)):
            # TP et retour à l'entrée dans la même bougie : ordre inconnu, on suppose le pire.
            leave(stop_now, b["t"], "stop_be", remaining)
            reason = "stop_be"
            break
    if reason is None:
        leave(last["c"], last["t"], end_reason, remaining)
        excursion(last["c"])
        reason = end_reason

    slip = cost["slippage"]
    fill_eff = fill_price * (1 + sign * (slip + cost[liquidity]))
    gross = sum(e["part"] * (e["price"] - fill_price) * sign for e in exits) / risk
    net = sum(e["part"] * (e["price"] * (1 - sign * (slip + e["fee"])) - fill_eff) * sign for e in exits) / risk
    result.update(exit_reason=reason, exits=exits, r_gross=round(gross, 4), r_net=round(net, 4),
                  mfe_r=round((best - fill_price) * sign / risk, 4), mae_r=round((worst - fill_price) * sign / risk, 4),
                  closed_at=exits[-1]["at"] + BAR_SECONDS)
    return result


def day_range_pct(bars: list[dict]) -> float | None:
    """Amplitude (plus haut - plus bas) / plus bas, en %, sur les bougies données."""
    if not bars:
        return None
    hi, lo = max(b["h"] for b in bars), min(b["l"] for b in bars)
    return round((hi - lo) / lo * 100, 3) if lo > 0 else None
