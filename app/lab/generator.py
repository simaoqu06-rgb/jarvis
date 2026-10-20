"""Génération des plans du labo avec le code du direct (app/trading/service.plan_from_bars).

À l'heure de décision t, le plan ne voit que les bougies closes à t (fin ≤ t) ; la crypto reçoit
en plus la bougie « en cours » faite des bougies de 5 min déjà closes, comme Kraken en direct.

- Actions : un plan long par valeur de l'univers et par séance, à 9 h 10 (New York), sur des
  bougies de 15 min (SETTINGS["stock"]) de la séance régulière ; avant l'ouverture la dernière
  bougie connue est celle de 15 h 45 la veille, que le plan tient pour en cours, exactement comme
  en direct. Entrée : celle du plan (dernier cours connu), pas de prix de pré-ouverture.
- Crypto : un plan long par paire de l'univers toutes les 4 h (0 h, 4 h… 20 h UTC), bougies d'1 h
  (SETTINGS["crypto"]) : les 720 dernières, comme Kraken.
- Catalyseur non rejoué (pas d'historique des actus avant le Labo 2) : cat=None, la note se calcule
  sans lui et context.catalyst_replayed = false.
- Délais rejoués limités aux 1 500 derniers départs (historical_timing(lookback=…)) : plus d'un an
  de bougies d'1 h ou ~58 séances de 15 min, pour tenir des centaines de plans par jour.
- Déclaration n° 7 (actions) : à côté du plan v1 (règle d'origine), le plan tel que le direct le fait depuis
  la PR #36 (stock_plans : stop éloigné à au moins 1/3 de l'ATR journalier par service._floor_stop, objectifs
  et note recalculés). Il ne change rien à v1 ; il est gardé dans trades.plan (« plancher ») et rejoué par
  strategies.go_rows.
- Données manquantes (avis du 10 octobre, I4) : une valeur de l'univers sans trade est notée avec son motif
  (`absent` : « historique<100 », introduction récente ; « séance vide », suspension ou trou du fournisseur ;
  « plan impossible ») ; chaque trade garde la couverture de ses bougies (context : barres_seance,
  ouverture_presente et couverture, la part des créneaux de 5 min remplis rapportée à celle que le symbole
  remplit d'habitude, pour ne pas classer « incomplètes » toutes les actions peu liquides).
"""
import logging
from bisect import bisect_left
from datetime import date, datetime, timezone
from statistics import median

from app.lab import collector as CO
from app.lab import simulator as SIM
from app.lab import strategies as ST
from app.lab import universe as U
from app.lab.db import LabDB
from app.trading import service as T

log = logging.getLogger(__name__)
TIMING_LOOKBACK = 1500
PLAN_BARS = {"stock": TIMING_LOOKBACK + T.SETTINGS["stock"]["horizon_bars"] + 1, "crypto": 720}
H4_BARS = {"stock": 400, "crypto": 720}
DAILY_BARS = 300
CRYPTO_HOURS_UTC = (0, 4, 8, 12, 16, 20)
INDEX = {"stock": "SPY", "crypto": "BTC-USDT"}
ANNUAL_DAYS = {"stock": 252, "crypto": 365}


class LookAhead(AssertionError):
    """Une donnée postérieure à la décision a atteint le plan : refus net."""


def check_known(bars: list[dict], tf: int, moment: int, label: str) -> None:
    if bars and bars[-1]["t"] + tf * 60 > moment:
        raise LookAhead(f"{label} : bougie {bars[-1]['t']} non close à {moment}")


def compact_plan(p: dict) -> dict:
    """Ce que le journal garde du plan (le reste se recalcule)."""
    sc, check = p["score"], p["checklist"]
    return {
        "entry": p["entry"], "stop": p["stop"], "stop_source": p["stop_source"], "stop_pct": p["stop_pct"],
        "atr": p["atr"], "stop_first_rate": p["stop_first_rate"], "created_at": p["created_at"],
        "targets": [{k: t[k] for k in ("name", "price", "source", "pct", "r_multiple", "hit_rate", "stop_rate",
                                       "expectancy_r", "minutes_median", "minutes_p25", "minutes_p75")}
                    for t in p["targets"]],
        "score": {"composite": sc["composite"], "decision": sc["decision"], "go": sc["go"], "reasons": sc["reasons"],
                  "parts": {k: {"score": v["score"], "reasons": v["reasons"]} for k, v in sc["parts"].items()}},
        "checklist": {"score": check["score"], "max_score": check["max_score"], "trigger": check["trigger"],
                      "verdict": check["verdict"], "items": {i["key"]: i["ok"] for i in check["items"]}},
        "method": p["method"],
    }


def setup_type(p: dict) -> str:
    """Type de configuration tiré de la checklist (grossier, de quoi regrouper les trades)."""
    items = {i["key"]: i for i in p["checklist"]["items"]}
    if p["checklist"]["trigger"]:
        return "declencheur_complet"
    pivot = items.get("pivot") or {}
    if pivot.get("ok"):
        return "cassure_pivot" if "cassé" in pivot.get("detail", "") else "rebond_pivot"
    if items.get("divergence", {}).get("ok"):
        return "divergence"
    if items.get("trend_daily", {}).get("ok") is False:
        return "contre_tendance"
    return "tendance_sans_declencheur"


def make_plan(symbol: str, market: str, bars: list[dict], daily: list[dict] | None, h4: list[dict] | None,
              moment: int, s: dict, levels: tuple | None = None) -> dict:
    now = datetime.fromtimestamp(moment, timezone.utc)
    return T.plan_from_bars(symbol, market, "long", bars, daily, h4, now, cat=None, capital=0, s=s,
                            timing_lookback=TIMING_LOOKBACK, levels=levels)


def stock_plans(symbol: str, inp: dict, s: dict) -> tuple[dict, dict | None]:
    """Plan v1 (règle d'origine) et plan du direct depuis la PR #36 (déclaration n° 7) : comme build_plan, stop
    éloigné à au moins 1/3 de l'ATR journalier, objectifs recalculés sur ce risque et plan noté avec (donc son
    propre GO) ; le même plan quand le stop est déjà assez loin. Plan du direct impossible : None."""
    now = datetime.fromtimestamp(inp["moment"], timezone.utc)
    levels = T.plan_levels(inp["bars"], "stock", "long", now=now, timing_lookback=TIMING_LOOKBACK)
    plan = make_plan(symbol, "stock", inp["bars"], inp["daily"], inp["h4"], inp["moment"], s, levels)
    try:
        floored = T._floor_stop(inp["bars"], inp["daily"], "stock", "long", None, None, now, levels)
        if floored is levels:
            return plan, plan
        return plan, make_plan(symbol, "stock", inp["bars"], inp["daily"], inp["h4"], inp["moment"], s, floored)
    except T.PlanError as exc:
        log.debug("Labo : plan du direct %s impossible (%s)", symbol, exc)
        return plan, None


def floor_summary(plan: dict, floored: dict | None) -> dict | None:
    """Ce que trades.plan garde du plan du direct (n° 7) ; None s'il n'a pas pu être fait."""
    if floored is None:
        return None
    sc = floored["score"]
    return {"eloigne": floored is not plan, "entry": floored["entry"], "stop": floored["stop"],
            "stop_source": floored["stop_source"], "targets": [t["price"] for t in floored["targets"]],
            "go": sc["go"], "decision": sc["decision"], "composite": sc["composite"]}


def median_fill(bars5: list[dict], market: str, before: int) -> float | None:
    """Part médiane des créneaux de 5 min remplis par jour complet d'historique avant `before` (actions : séances
    régulières, jours courts compris ; crypto : jours UTC) ; None sans historique."""
    starts = [b["t"] for b in bars5 if b["t"] < before]
    if not starts:
        return None
    fills = []
    if market == "stock":
        for d in CO.trading_days(CO.ny_day(starts[0]), CO.ny_day(before - 1)):
            o, c = CO.session_ts(d)
            if o >= starts[0] and c <= before:
                fills.append((bisect_left(starts, c) - bisect_left(starts, o)) / ((c - o) // (CO.BASE_TF * 60)))
    else:
        counts: dict[int, int] = {}
        for t in starts:
            counts[t // 86400] = counts.get(t // 86400, 0) + 1
        first = starts[0] // 86400 + (starts[0] % 86400 > 0)
        fills = [counts.get(d, 0) / (86400 // (CO.BASE_TF * 60)) for d in range(first, before // 86400)]
    return median(fills) if fills else None


def coverage(bars: list[dict], start: int, end: int, fill: float | None) -> dict:
    """Couverture des bougies simulées sur [start, end[ (séance ou 24 h) : nombre, première bougie au départ, et part
    des créneaux remplis rapportée à la médiane du symbole (None sans historique)."""
    slots = max(1, (end - start) // (CO.BASE_TF * 60))
    return {"barres_seance": len(bars), "ouverture_presente": bool(bars) and bars[0]["t"] == start,
            "couverture": round(len(bars) / slots / fill, 3) if fill else None}


def _row(market: str, symbol: str, day: date, moment: int, plan: dict, context: dict, result: dict) -> dict:
    hits, minutes = result["tp_hits"] + [False] * 3, result["minutes_to_tp"] + [None] * 3
    flat = {
        "triggered": int(result["triggered"]), "filled_at": result["filled_at"], "exit_reason": result["exit_reason"],
        "r_net": result["r_net"], "r_gross": result["r_gross"],
        "tp1_hit": int(hits[0]), "tp2_hit": int(hits[1]), "tp3_hit": int(hits[2]),
        "minutes_to_tp1": minutes[0], "minutes_to_tp2": minutes[1], "minutes_to_tp3": minutes[2],
        "mfe_r": result["mfe_r"], "mae_r": result["mae_r"], "day_range_pct": result.get("day_range_pct"),
        "decision": plan["score"]["decision"], "composite": plan["score"]["composite"],
    }
    return {"symbol": symbol, "side": plan["side"], "day": day.isoformat(), "planned_at": moment,
            "plan": compact_plan(plan), "context": context, "result": result, "flat": flat}


# --- Actions ------------------------------------------------------------------------------

def stock_inputs(db: LabDB, symbol: str, day: date, why: list | None = None) -> dict | None:
    """Bougies connues à 9 h 10 et bougies de la séance à simuler (`why` reçoit le motif d'un None)."""
    bounds = CO.session_ts(day)
    if not bounds:
        if why is not None:
            why.append("séance vide")
        return None
    moment = U.stock_decision_ts(day)
    start, end = CO.stock_window(day)
    bars5 = db.get_bars("stock", symbol, CO.BASE_TF, start, end)
    hist = CO.known_before(bars5, CO.BASE_TF, moment)
    session = [b for b in bars5[len(hist):] if bounds[0] <= b["t"] < bounds[1]]
    if len(hist) < 100 or not session:
        if why is not None:
            why.append("historique<100" if len(hist) < 100 else "séance vide")
        return None
    m15 = CO.aggregate(hist, 15, "stock")
    bars = m15[-PLAN_BARS["stock"]:]
    h4 = CO.aggregate(m15, 240, "stock")[-H4_BARS["stock"]:]  # 15 min alignées sur 9 h 30 : mêmes bougies de 4 h
    cutoff = CO.ny_midnight(day)
    daily = db.get_bars("stock", symbol, 1440, cutoff - CO.DAILY_HISTORY_DAYS * 86400, cutoff)[-DAILY_BARS:] \
        or CO.aggregate(hist, 1440, "stock")[-DAILY_BARS:]
    check_known(bars, 15, moment, "15 min")
    check_known(h4, 0, cutoff - 1, "4 h")        # séances closes seulement (jamais le jour même)
    check_known(daily, 0, cutoff - 1, "journalier")
    return {"moment": moment, "bars": bars, "h4": h4, "daily": daily, "session": session, "bounds": bounds,
            "hist5": hist}


def _strategies(fn, *args) -> list[dict]:
    """Labo v2 : une erreur dans une stratégie ne coûte jamais le trade v1 du même actif."""
    try:
        return fn(*args)
    except LookAhead:
        raise
    except Exception as exc:
        log.warning("Labo v2 : stratégies %s ignorées (%s: %s)", args[1], type(exc).__name__, exc)
        return []


def stock_trade(db: LabDB, symbol: str, day: date, s: dict, meta: dict | None = None,
                regime: dict | None = None, strat_out: list | None = None,
                index5: list[dict] | None = None, absent: list | None = None) -> dict | None:
    """Trade v1 de l'action ; avec `strat_out`, y ajoute aussi les lignes du labo v2 (strategies.py),
    calculées sur les mêmes bougies, chargées une seule fois (`index5` : séance de SPY en 5 min). Pas de trade :
    `absent` reçoit le symbole et le motif."""
    why: list[str] = []
    inp = stock_inputs(db, symbol, day, why)
    if not inp:
        if absent is not None:
            absent.append({"symbol": symbol, "motif": why[0] if why else "séance vide"})
        return None
    row = v1 = None
    try:
        row, v1 = _stock_v1(inp, symbol, day, s, meta, regime)
    finally:
        if strat_out is not None:
            strat_out.extend(_strategies(ST.stock_day, day, symbol, inp["hist5"], inp["session"], inp["daily"],
                                         (meta or {}).get("rank"), v1, index5))
    if row is None and absent is not None:
        absent.append({"symbol": symbol, "motif": "plan impossible"})
    return row


def _stock_v1(inp: dict, symbol: str, day: date, s: dict, meta: dict | None, regime: dict | None):
    try:
        plan, floored = stock_plans(symbol, inp, s)
    except T.PlanError as exc:
        log.debug("Labo : plan %s %s impossible (%s)", symbol, day, exc)
        return None, None
    last_price = inp["bars"][-1]["c"]
    cost = SIM.costs("stock", plan["entry"])
    result = SIM.simulate(plan, inp["session"], market="stock", decided_at=inp["moment"],
                          entry_until=inp["bounds"][1], exit_at=inp["bounds"][1], last_price=last_price, cost=cost)
    result["day_range_pct"] = SIM.day_range_pct(inp["session"])
    m = (meta or {}).get("metrics") or {}
    prev_close = inp["daily"][-1]["c"] if inp["daily"] else last_price
    context = {
        "setup": setup_type(plan), "target_sources": [t["source"] for t in plan["targets"]],
        "universe_reason": (meta or {}).get("reason"), "universe_rank": (meta or {}).get("rank"),
        "regime": regime, "rvol": m.get("rvol"), "day_change_pct": m.get("change_pct"),
        "dollar_volume": m.get("dollar_volume"),
        # Connu à l'ouverture (9 h 30), après la décision mais avant toute exécution.
        "gap_open_pct": round((inp["session"][0]["o"] / prev_close - 1) * 100, 3) if prev_close else None,
        "sector": None, "catalyst_replayed": False,
        **coverage(inp["session"], *inp["bounds"], median_fill(inp["hist5"], "stock", inp["bounds"][0])),
    }
    v1 = {"plan": plan, "decided_at": inp["moment"], "entry_until": inp["bounds"][1], "last_price": last_price,
          "cost": cost, "result": result, "go": plan["score"]["go"],
          "plancher": {"plan": floored, "go": floored["score"]["go"]} if floored else None}
    row = _row("stock", symbol, day, inp["moment"], plan, context, result)
    row["plan"]["plancher"] = floor_summary(plan, floored)
    return row, v1


def _guarded(errors: list | None, symbol: str, fn, *args):
    """Un symbole en erreur (donnée aberrante, bug) ne bloque jamais le reste du jour : une ligne de
    journal, l'erreur comptée, et on continue."""
    try:
        return fn(*args)
    except LookAhead:
        raise
    except Exception as exc:
        log.warning("Labo : %s ignoré (%s: %s)", symbol, type(exc).__name__, exc)
        if errors is not None:
            errors.append({"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"[:200]})
        return None


def stock_day(db: LabDB, day: date, s: dict, universe: list[dict],
              errors: list | None = None, strat_out: list | None = None,
              absent: list | None = None) -> tuple[list[int], list[dict]]:
    """Plans et simulations de toutes les valeurs de l'univers pour la séance `day` (et, avec
    `strat_out`, les lignes du labo v2 ; `absent` : les valeurs sans trade et pourquoi)."""
    if not CO.session_ts(day):
        return [], []
    cutoff = CO.ny_midnight(day)
    index_daily = db.get_bars("stock", INDEX["stock"], 1440, cutoff - 120 * 86400, cutoff)
    regime = U.regime(index_daily, cutoff, ANNUAL_DAYS["stock"])
    bounds = CO.session_ts(day)
    index5 = db.get_bars("stock", INDEX["stock"], CO.BASE_TF, *bounds) if strat_out is not None else None
    rows = [r for meta in universe
            if (r := _guarded(errors, meta["symbol"], stock_trade, db, meta["symbol"], day, s, meta, regime, strat_out,
                              index5, absent))]
    return [U.stock_decision_ts(day)], rows


# --- Crypto -------------------------------------------------------------------------------

def crypto_moments(day: date) -> list[int]:
    start = CO.utc_midnight(day)
    return [start + h * 3600 for h in CRYPTO_HOURS_UTC]


def crypto_symbol_day(db: LabDB, symbol: str, day: date) -> dict:
    """Bougies d'un jour UTC (fenêtre de 40 jours avant, 2 jours après) et agrégats, chargés une fois."""
    start, end = CO.crypto_window(day)
    bars5 = db.get_bars("crypto", symbol, CO.BASE_TF, start, end)
    cutoff = CO.utc_midnight(day)
    daily = db.get_bars("crypto", symbol, 1440, cutoff - CO.DAILY_HISTORY_DAYS * 86400, cutoff + 86400)
    h1 = CO.aggregate(bars5, 60, "crypto")
    return {"bars5": bars5, "starts5": [b["t"] for b in bars5], "h1": h1,
            "h4": CO.aggregate(h1, 240, "crypto"), "daily": daily, "remplissage": median_fill(bars5, "crypto", cutoff)}


def crypto_inputs(data: dict, moment: int, why: list | None = None) -> dict | None:
    bars5, starts5 = data["bars5"], data["starts5"]
    k = bisect_left(starts5, moment - CO.BASE_TF * 60 + 1)  # bougies closes à `moment`
    if k < 100 or bars5[k - 1]["t"] + CO.BASE_TF * 60 < moment - 3600:
        if why is not None:
            why.append("historique<100" if k < 100 else "séance vide")
        return None  # pas assez d'historique ou paire muette depuis plus d'une heure
    hist5 = bars5[:k]
    last = hist5[-1]["c"]
    h1 = CO.known_before(data["h1"], 60, moment)
    h4 = CO.known_before(data["h4"], 240, moment)
    day_start = moment - moment % 86400
    daily = [b for b in data["daily"] if b["t"] + 86400 <= day_start]
    check_known(h1, 60, moment, "1 h")
    check_known(h4, 240, moment, "4 h")
    check_known(daily, 1440, moment, "journalier")
    tail5 = hist5[-12 * 24:]
    bars = h1[-(PLAN_BARS["crypto"] - 1):] + [CO.in_progress(tail5, moment - moment % 3600, moment, last)]
    h4 = h4[-(H4_BARS["crypto"] - 1):] + [CO.in_progress(tail5, moment - moment % 14400, moment, last)]
    daily = daily[-(DAILY_BARS - 1):] + [CO.in_progress(tail5, day_start, moment, last)]
    future = bars5[k:]
    horizon = moment + T.SETTINGS["crypto"]["horizon_bars"] * T.SETTINGS["crypto"]["interval"] * 60
    sim = future[:bisect_left([b["t"] for b in future], horizon)]
    return {"bars": bars, "h4": h4, "daily": daily, "sim": sim, "horizon": horizon, "last": last}


def crypto_trade(data: dict, symbol: str, day: date, moment: int, s: dict, meta: dict | None = None,
                 regime: dict | None = None, v1_out: list | None = None, why: list | None = None) -> dict | None:
    """Trade v1 d'un créneau ; pas de trade : `why` reçoit le motif."""
    why = [] if why is None else why
    inp = crypto_inputs(data, moment, why)
    if not inp or not inp["sim"]:
        if inp:
            why.append("séance vide")
        return None
    try:
        plan = make_plan(symbol, "crypto", inp["bars"], inp["daily"], inp["h4"], moment, s)
    except T.PlanError as exc:
        log.debug("Labo : plan %s %s impossible (%s)", symbol, moment, exc)
        why.append("plan impossible")
        return None
    rank = (meta or {}).get("rank")
    cost = SIM.costs("crypto", plan["entry"], rank)
    result = SIM.simulate(plan, inp["sim"], market="crypto", decided_at=moment, entry_until=inp["horizon"],
                          exit_at=inp["horizon"], last_price=inp["last"], cost=cost)
    result["day_range_pct"] = SIM.day_range_pct(inp["sim"])
    m = (meta or {}).get("metrics") or {}
    context = {
        "setup": setup_type(plan), "target_sources": [t["source"] for t in plan["targets"]],
        "universe_reason": (meta or {}).get("reason"), "universe_rank": rank, "regime": regime,
        "rvol": m.get("rvol"), "day_change_pct": m.get("change_pct"), "dollar_volume": m.get("dollar_volume"),
        "window_utc": datetime.fromtimestamp(moment, timezone.utc).strftime("%H:%M"),
        "gap_open_pct": None, "sector": None, "catalyst_replayed": False,
        **coverage(inp["sim"], moment, inp["horizon"], data.get("remplissage")),
    }
    if v1_out is not None:
        v1_out.append({"plan": plan, "decided_at": moment, "entry_until": inp["horizon"], "last_price": inp["last"],
                       "cost": cost, "result": result})
    return _row("crypto", symbol, day, moment, plan, context, result)


def crypto_day(db: LabDB, day: date, s: dict, universe: list[dict], now: int | None = None,
               errors: list | None = None, strat_out: list | None = None,
               absent: list | None = None) -> tuple[list[int], list[dict]]:
    """Plans et simulations des créneaux du jour UTC `day` dont l'horizon est passé (tous sans `now`).
    Avec `strat_out`, et seulement quand toutes les sorties du jour sont connues
    (strategies.crypto_complete), les lignes du labo v2. Une paire sans aucun trade du jour va dans `absent`,
    avec le motif de son premier créneau manqué."""
    moments = [m for m in crypto_moments(day) if now is None or m + 86400 <= now]
    if not moments:
        return [], []
    cutoff = CO.utc_midnight(day)
    regime = U.regime(db.get_bars("crypto", INDEX["crypto"], 1440, cutoff - 120 * 86400, cutoff),
                      cutoff, ANNUAL_DAYS["crypto"])
    rows = []
    for meta in universe:
        data = _guarded(errors, meta["symbol"], crypto_symbol_day, db, meta["symbol"], day)
        if data is None:
            continue
        v1s: list[dict] = []
        why: list[str] = []
        before = len(rows)
        for moment in moments:
            row = _guarded(errors, meta["symbol"], crypto_trade, data, meta["symbol"], day, moment, s, meta, regime, v1s,
                           why)
            if row:
                rows.append(row)
        if len(rows) == before and why and absent is not None:
            absent.append({"symbol": meta["symbol"], "motif": why[0]})
        if strat_out is not None and ST.crypto_complete(day, now):
            strat_out.extend(_strategies(ST.crypto_day, day, meta["symbol"], data["bars5"], data["daily"],
                                         meta.get("rank"), v1s))
    return moments, rows
