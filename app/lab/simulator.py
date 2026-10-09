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
  de base selon le prix (actions, jamais moins d'un demi-pas de cotation depuis la n° 8) ou le rang de
  volume (crypto), aucune commission sur les actions.
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
EXIT_REASONS = ("stop", "stop_be", "stop_suiveur", "tp1", "tp2", "tp3", "cloture", "horizon", "non_declenche")
ENTRY_ORDERS = ("limit", "stop", "market")
EXIT_MODES = ("v1", "sans_be", "suiveur")
# Ordre dans une bougie ambiguë : « v1 » (le labo, inchangé), « prudente » (le pire ordre possible partout),
# « favorable » (le meilleur, bougie par bougie : une sensibilité, pas un maximum). Journal de Killmode, 8 octobre.
RESOLUTIONS = ("v1", "prudente", "favorable")
# Bougie faite d'une seule transaction (journal de Killmode, méthodes n° 3 et 4) : ces champs passent à la sortie ou
# au remplissage qu'elle déclenche (horodatage en ns, pire bid et pire ask de l'ordre au marché qui en part, instant
# de la cotation suivante, taille de l'ask retenu, rang de la transaction dans sa minute).
TICK_FIELDS = ("ns", "bid", "ask", "q_ns", "ask_size", "seq")


def stock_slippage_bps(price: float) -> float:
    for floor, bps in STOCK_SLIPPAGE_BPS:
        if price >= floor:
            return bps
    return STOCK_SLIPPAGE_BPS[-1][1]


def crypto_slippage_bps(volume_rank: int | None) -> float:
    return CRYPTO_SLIPPAGE_BPS["top10" if volume_rank is not None and volume_rank <= 10 else "other"]


def stock_slippage(price: float) -> float:
    """Demi-écart + glissement par côté (fraction du prix), jamais moins d'un demi-pas de cotation
    (déclaration n° 8, D3 : entre 1 et 3,33 $ et entre 5 et 8,33 $, le palier en points de base donnait moins)."""
    if price <= 0:
        return stock_slippage_bps(price) / 10000
    return max(stock_slippage_bps(price) / 10000, tick_size("stock", price) / 2 / price)


def costs(market: str, price: float, volume_rank: int | None = None) -> dict:
    """Coût par côté : glissement (fraction) et frais (fraction du montant)."""
    if market == "stock":
        return {"slippage": stock_slippage(price), "maker": STOCK_COMMISSION, "taker": STOCK_COMMISSION}
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
             last_price: float, cost: dict, exit_reason_end: str | None = None, entry_order: str = "limit",
             exit_mode: str = "v1", trail_r: float = 1.0, bar_seconds: int = BAR_SECONDS,
             resolution: str = "v1", flip: frozenset = frozenset(), tp_ticks: int = 1) -> dict:
    """Rejoue le plan sur `bars` (5 min, toutes commencées à `decided_at` ou après).

    `entry_until` : fin de la fenêtre d'entrée ; `exit_at` : tout est soldé à la clôture de la
    dernière bougie finissant au plus tard à cet instant ; `last_price` : prix à la décision.

    Labo v2 (strategies.py), les valeurs par défaut gardent exactement le comportement de v1 :
    - `entry_order` : « limit » (v1) ; « stop » = ordre stop d'achat posé au prix d'entrée, exécuté dès
      qu'une bougie l'atteint, au prix d'entrée ou à l'ouverture si elle ouvre au-delà (prise de
      liquidité, frais « taker ») ; « market » = au marché, à l'ouverture de la première bougie.
    - `exit_mode` : « v1 » (tiers aux TP, stop à l'entrée après le TP1) ; « sans_be » (même chose sans
      remonter le stop) ; « suiveur » (aucun objectif : le stop suit le plus haut atteint à `trail_r`
      fois le risque, recalculé à la clôture de chaque bougie, solde à la clôture ou à l'horizon).

    Journal de Killmode (8 octobre, avis de GPT et Gemini), les valeurs par défaut gardent le labo tel quel :
    - `bar_seconds` : durée des bougies (60 pour un solde en bougies d'1 min) ;
    - `resolution` : « v1 » ; « prudente » = le pire ordre dans chaque bougie ambiguë : stop avant objectif,
      plus bas de la bougie d'entrée après l'exécution, un seul objectif avant un retour à l'entrée, et un
      objectif rempli à son prix (en séance continue, un ordre limité posé s'exécute à son prix), ou au prix
      d'entrée s'il est posé alors que le prix l'a déjà dépassé (entrée remplie à l'ouverture au-delà) ;
      avec l'ordre stop d'achat, un objectif traversé dans la bougie d'entrée est certain (elle ouvre sous
      l'entrée, son plus haut vient après l'exécution) et compte. « favorable » = le meilleur ordre, bougie par
      bougie. Les deux donnent `ambigus` : les bougies où l'ordre a été choisi, avec leur cas.

    Troisième avis de GPT et Gemini (8 octobre), hors v1 seulement :
    - `flip` : bougies (leur `t`) jouées dans l'autre ordre que `resolution`, et `("be", t)` : objectifs traversés
      puis retour à l'entrée dans la bougie ; le journal rejoue toutes les combinaisons, parce que le pire ordre
      bougie par bougie n'est pas le pire résultat (l'autre ordre peut garder la position ouverte jusqu'à un gap) ;
    - une bougie qui ouvre au-delà d'un objectif l'exécute avant tout le reste : il n'est plus ambigu ;
    - un objectif doit être dépassé de `tp_ticks` pas, le pas de son propre prix, comparé en pas entiers (en
      flottants, 16,01 + 0,01 dépasse 16,02 : un plus haut à 16,02 ne comptait pas) ;
    - une clôture à l'entrée ou au-delà après un objectif rend le retour à l'entrée certain, même dans l'ordre
      favorable ;
    - une bougie faite d'une seule transaction (`ns`) transmet `TICK_FIELDS` au remplissage (`fill_tick`) et aux
      sorties qu'elle déclenche.

    Quatrième avis de GPT (9 octobre), hors v1 : sur la transaction qui remplit l'achat, un objectif que le prix a
    dépassé (ou que le meilleur bid atteint) ne part sur-le-champ que si le bid l'atteint (`bid`, le pire des deux
    cotations ; l'ask pour une vente) ; sinon il reste posé et s'exécute quand le prix le dépasse ensuite d'un pas.
    Quand une seule des deux cotations l'atteint (`bid_hi`, la meilleure), ou sans cotation lue, les deux sont
    possibles : la minute est ambiguë (« objectif_pose »), posé par défaut, exécuté si sa minute est dans `flip`.
    """
    if entry_order not in ENTRY_ORDERS or exit_mode not in EXIT_MODES or resolution not in RESOLUTIONS:
        raise ValueError(f"ordre {entry_order}, sortie {exit_mode} ou résolution {resolution} inconnus")
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
    window = [b for b in bars if b["t"] + bar_seconds <= exit_at]
    result = {"triggered": False, "filled_at": None, "fill_price": None, "entry_mode": None,
              "exit_reason": "non_declenche", "exits": [], "r_gross": None, "r_net": None,
              "tp_hits": [False] * len(targets), "minutes_to_tp": [None] * len(targets),
              "mfe_r": None, "mae_r": None, "closed_at": None, "minutes_to_fill": None,
              "slippage_bps": round(cost["slippage"] * 10000, 2), "fees": {"maker": cost["maker"], "taker": cost["taker"]},
              "entry_liquidity": None,
              "order_prices": {"entry": entry, "stop": stop, "targets": targets},
              "data_incomplete": not window or window[-1]["t"] + bar_seconds < exit_at}
    if resolution != "v1":
        result["ambigus"] = []
    if not window:
        return result

    # --- Entrée ---
    marketable = (entry - last_price) * sign >= 0
    fill_i = fill_price = mode = None
    liquidity = "maker"  # ordre posé dans le carnet, sauf exécution immédiate au marché
    for i, b in enumerate(window):
        if b["t"] >= entry_until:
            break
        if entry_order == "market":
            fill_i, fill_price, mode, liquidity = i, b["o"], "ouverture", "taker"
            break
        if entry_order == "stop":
            if (b["h"] >= entry) if sign == 1 else (b["l"] <= entry):
                opened_beyond = (b["o"] - entry) * sign >= 0
                fill_i, fill_price, mode, liquidity = i, (b["o"] if opened_beyond else entry), \
                    ("ouverture" if opened_beyond else "stop"), "taker"
                break
            continue
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
    seen = {k: window[fill_i][k] for k in TICK_FIELDS if k in window[fill_i]}
    if seen:
        result["fill_tick"] = seen

    # --- Sorties ---
    if exit_mode == "suiveur":
        targets = []
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

    def leave(price: float, b: dict, why: str, share: float) -> None:
        nonlocal remaining
        maker = why.startswith("tp")  # TP : ordre limité posé ; stop, clôture, horizon : au marché
        exits.append({"price": price, "at": b["t"], "reason": why, "part": round(share, 6),
                      "fee": cost["maker"] if maker else cost["taker"]}
                     | {k: b[k] for k in TICK_FIELDS if k in b})
        remaining -= share

    v1 = resolution == "v1"
    # Ordre stop d'achat exécuté en cours de bougie (elle ouvre avant l'entrée) : son extrême favorable vient
    # après l'exécution, son extrême défavorable a pu venir avant.
    stop_fill = entry_order == "stop" and mode == "stop"

    def adverse(b: dict, level: float) -> bool:
        return b["l"] <= level if sign == 1 else b["h"] >= level

    def crossed(b: dict, tp: float) -> bool:
        if v1:
            return (b["h"] >= tp + tick) if sign == 1 else (b["l"] <= tp - tick)
        step = tick_size(market, tp)
        return ((b["h"] if sign == 1 else b["l"]) - tp) * sign >= (tp_ticks - 1e-6) * step

    def note(b: dict, case: str) -> None:
        if not v1 and not any(a["t"] == b["t"] for a in result["ambigus"]):
            result["ambigus"].append({"t": b["t"], "cas": case})

    # Objectif dépassé par la transaction qui remplit l'achat, ou que le meilleur bid atteint (bougies d'une
    # transaction, hors v1) : les ordres liés deviennent actifs avec l'achat, sous l'une des deux cotations. Exécuté
    # sur-le-champ si le pire bid l'atteint, posé si aucun ne l'atteint, l'un ou l'autre (minute ambiguë) si seul le
    # meilleur l'atteint ou si aucune cotation n'est lue.
    fill_bar = window[fill_i]
    single = not v1 and "ns" in fill_bar
    release = fill_bar.get("bid" if sign == 1 else "ask") if single else None
    release_best = fill_bar.get("bid_hi" if sign == 1 else "ask_lo", release) if single else None

    def reached(i: int, tp: float) -> bool:
        return i == fill_i and release_best is not None and (release_best - tp) * sign >= 0

    def resting(i: int, tp: float) -> bool:
        if i != fill_i or not single or (release is not None and (release - tp) * sign >= 0):
            return False
        if release is not None and (release_best - tp) * sign < 0:
            return True
        note(fill_bar, "objectif_pose")
        return fill_bar["t"] not in flip

    last = window[fill_i]
    for i in range(fill_i, len(window)):
        b = window[i]
        last = b
        be = not v1 and ("be", b["t"]) in flip  # objectifs, puis retour à l'entrée dans la bougie
        fav = not v1 and ((resolution == "favorable") != (b["t"] in flip) or be)  # ordre choisi dans cette bougie
        at_open = i > fill_i or mode == "ouverture"
        targets_ok = at_open or (not v1 and stop_fill)  # objectifs comptés dans cette bougie
        stop_label = ("stop_be" if hit and exit_mode == "v1"
                      else "stop_suiveur" if (stop_now - stop) * sign > 0 else "stop")
        # Stop d'abord : gap à l'ouverture, puis contact dans la bougie.
        if i > fill_i and (b["o"] - stop_now) * sign <= 0:
            excursion(b["o"])
            leave(b["o"], b, stop_label, remaining)
            reason = stop_label
            break
        if not v1 and at_open:  # objectifs déjà dépassés à l'ouverture : exécutés avant tout le reste de la bougie
            opened = False
            while hit < len(targets) and ((b["o"] - targets[hit]) * sign
                                          >= (tp_ticks - 1e-6) * tick_size(market, targets[hit])
                                          or reached(i, targets[hit])) and not resting(i, targets[hit]):
                hit += 1
                opened = True
                result["tp_hits"][hit - 1] = True
                result["minutes_to_tp"][hit - 1] = _minutes(market, decided_at, b["t"] + bar_seconds // 2)
                # Posé alors que le prix l'a dépassé (bougie d'entrée) : il part au prix d'entrée, jamais sous le sien.
                tp = targets[hit - 1]
                leave(fill_price if i == fill_i and (fill_price - tp) * sign >= 0 else tp, b, f"tp{hit}",
                      part if hit < len(targets) else remaining)
                if exit_mode == "v1":
                    stop_now = entry
            if opened:
                excursion(b["o"])
                if hit == len(targets):
                    reason = f"tp{hit}"
                    break
                if exit_mode == "v1":
                    stop_label = "stop_be"
        crosses = targets_ok and hit < len(targets) and crossed(b, targets[hit])
        adverse_before = targets_first = False  # ordre choisi par « favorable » dans une bougie ambiguë
        if adverse(b, stop_now):
            case = None
            if i == fill_i and stop_fill and (b["c"] - stop_now) * sign > 0:
                case = "entree_et_stop"  # le plus bas a pu précéder l'exécution
            elif crosses:
                case = "stop_et_objectif"
            if case:
                note(b, case)
            if fav and case == "entree_et_stop" and not be:
                adverse_before = True  # le plus bas avant l'exécution
            elif fav and (case == "stop_et_objectif" or (case == "entree_et_stop" and crosses)):
                targets_first = True  # objectifs d'abord, puis le plus bas : retour à l'entrée
            else:
                price = stop_now if i > fill_i or (fill_price - stop_now) * sign > 0 else fill_price
                excursion(price)  # au-delà du stop, la position est déjà soldée
                leave(price, b, stop_label, remaining)
                reason = stop_label
                break
        if not adverse_before:
            excursion(b["l"] if sign == 1 else b["h"])
        if not targets_ok:
            continue  # entrée en cours de bougie : on ignore son plus haut
        excursion(b["h"] if sign == 1 else b["l"])
        hit_now = False
        while hit < len(targets):
            tp = targets[hit]
            if not (crossed(b, tp) or reached(i, tp)) or resting(i, tp):
                break
            if v1:
                price = b["o"] if (b["o"] - tp) * sign >= tick and i > fill_i else tp
            else:  # posé, il s'exécute à son prix ; posé alors que le prix l'a dépassé, il part au prix d'entrée
                price = fill_price if i == fill_i and (fill_price - tp) * sign >= 0 else tp
            hit += 1
            hit_now = True
            result["tp_hits"][hit - 1] = True
            result["minutes_to_tp"][hit - 1] = _minutes(market, decided_at, b["t"] + bar_seconds // 2)
            leave(price, b, f"tp{hit}", part if hit < len(targets) else remaining)
            if exit_mode == "v1":
                stop_now = entry  # break-even dès le TP1 (choix de Simão)
                if not v1 and not fav and hit < len(targets) and adverse(b, stop_now):
                    break  # retour à l'entrée dans la même bougie : au pire, un seul objectif avant
        if targets and hit == len(targets):
            reason = f"tp{hit}"
            break
        back = hit_now and exit_mode == "v1" and adverse(b, stop_now)
        if back and not targets_first:
            note(b, "objectif_puis_entree")
        certain = (b["c"] - stop_now) * sign <= 0  # clôture à l'entrée ou au-delà : le retour suit l'objectif
        if back and (targets_first or certain or be or (not adverse_before and not fav)):
            # TP et retour à l'entrée dans la même bougie : ordre inconnu, on suppose le pire (sauf « favorable »).
            leave(stop_now, b, "stop_be", remaining)
            reason = "stop_be"
            break
        if exit_mode == "suiveur":
            trail = best - sign * trail_r * risk  # vaut pour la bougie suivante
            if (trail - stop_now) * sign > 0:
                stop_now = trail
    if reason is None:
        leave(last["c"], last, end_reason, remaining)
        excursion(last["c"])
        reason = end_reason

    slip = cost["slippage"]
    fill_eff = fill_price * (1 + sign * (slip + cost[liquidity]))
    gross = sum(e["part"] * (e["price"] - fill_price) * sign for e in exits) / risk
    net = sum(e["part"] * (e["price"] * (1 - sign * (slip + e["fee"])) - fill_eff) * sign for e in exits) / risk
    result.update(exit_reason=reason, exits=exits, r_gross=round(gross, 4), r_net=round(net, 4),
                  mfe_r=round((best - fill_price) * sign / risk, 4), mae_r=round((worst - fill_price) * sign / risk, 4),
                  closed_at=exits[-1]["at"] + bar_seconds)
    return result


def net_r(result: dict, cost: dict, side: str = "long", slip_factor: float = 1.0) -> float | None:
    """R net d'un résultat de simulate avec un glissement multiplié par `slip_factor` (test de
    robustesse : ×2, ×3) ; même formule que simulate, None si l'ordre n'a pas été exécuté."""
    if not result["triggered"] or not result["exits"]:
        return None
    sign = 1 if side == "long" else -1
    o = result["order_prices"]
    risk = abs(o["entry"] - o["stop"])
    if not risk:
        return None
    slip = cost["slippage"] * slip_factor
    fill_eff = result["fill_price"] * (1 + sign * (slip + cost[result["entry_liquidity"]]))
    return round(sum(e["part"] * (e["price"] * (1 - sign * (slip + e["fee"])) - fill_eff) * sign
                     for e in result["exits"]) / risk, 4)


def day_range_pct(bars: list[dict]) -> float | None:
    """Amplitude (plus haut - plus bas) / plus bas, en %, sur les bougies données."""
    if not bars:
        return None
    hi, lo = max(b["h"] for b in bars), min(b["l"] for b in bars)
    return round((hi - lo) / lo * 100, 3) if lo > 0 else None
