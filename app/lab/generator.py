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
"""
import logging
from bisect import bisect_left
from datetime import date, datetime, timezone

from app.lab import collector as CO
from app.lab import simulator as SIM
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
              moment: int, s: dict) -> dict:
    now = datetime.fromtimestamp(moment, timezone.utc)
    return T.plan_from_bars(symbol, market, "long", bars, daily, h4, now, cat=None, capital=0, s=s,
                            timing_lookback=TIMING_LOOKBACK)


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

def stock_inputs(db: LabDB, symbol: str, day: date) -> dict | None:
    """Bougies connues à 9 h 10 et bougies de la séance à simuler."""
    bounds = CO.session_ts(day)
    if not bounds:
        return None
    moment = U.stock_decision_ts(day)
    start, end = CO.stock_window(day)
    bars5 = db.get_bars("stock", symbol, CO.BASE_TF, start, end)
    hist = CO.known_before(bars5, CO.BASE_TF, moment)
    session = [b for b in bars5[len(hist):] if bounds[0] <= b["t"] < bounds[1]]
    if len(hist) < 100 or not session:
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
    return {"moment": moment, "bars": bars, "h4": h4, "daily": daily, "session": session, "bounds": bounds}


def stock_trade(db: LabDB, symbol: str, day: date, s: dict, meta: dict | None = None,
                regime: dict | None = None) -> dict | None:
    inp = stock_inputs(db, symbol, day)
    if not inp:
        return None
    try:
        plan = make_plan(symbol, "stock", inp["bars"], inp["daily"], inp["h4"], inp["moment"], s)
    except T.PlanError as exc:
        log.debug("Labo : plan %s %s impossible (%s)", symbol, day, exc)
        return None
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
    }
    return _row("stock", symbol, day, inp["moment"], plan, context, result)


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
              errors: list | None = None) -> tuple[list[int], list[dict]]:
    """Plans et simulations de toutes les valeurs de l'univers pour la séance `day`."""
    if not CO.session_ts(day):
        return [], []
    cutoff = CO.ny_midnight(day)
    index_daily = db.get_bars("stock", INDEX["stock"], 1440, cutoff - 120 * 86400, cutoff)
    regime = U.regime(index_daily, cutoff, ANNUAL_DAYS["stock"])
    rows = [r for meta in universe
            if (r := _guarded(errors, meta["symbol"], stock_trade, db, meta["symbol"], day, s, meta, regime))]
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
            "h4": CO.aggregate(h1, 240, "crypto"), "daily": daily}


def crypto_inputs(data: dict, moment: int) -> dict | None:
    bars5, starts5 = data["bars5"], data["starts5"]
    k = bisect_left(starts5, moment - CO.BASE_TF * 60 + 1)  # bougies closes à `moment`
    if k < 100 or bars5[k - 1]["t"] + CO.BASE_TF * 60 < moment - 3600:
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
                 regime: dict | None = None) -> dict | None:
    inp = crypto_inputs(data, moment)
    if not inp or not inp["sim"]:
        return None
    try:
        plan = make_plan(symbol, "crypto", inp["bars"], inp["daily"], inp["h4"], moment, s)
    except T.PlanError as exc:
        log.debug("Labo : plan %s %s impossible (%s)", symbol, moment, exc)
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
    }
    return _row("crypto", symbol, day, moment, plan, context, result)


def crypto_day(db: LabDB, day: date, s: dict, universe: list[dict], now: int | None = None,
               errors: list | None = None) -> tuple[list[int], list[dict]]:
    """Plans et simulations des créneaux du jour UTC `day` dont l'horizon est passé (tous sans `now`)."""
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
        for moment in moments:
            row = _guarded(errors, meta["symbol"], crypto_trade, data, meta["symbol"], day, moment, s, meta, regime)
            if row:
                rows.append(row)
    return moments, rows
