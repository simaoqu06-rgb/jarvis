"""Plans de trade et suivi des positions : bougies, délais estimés, TP/SL touchés, signaux de sortie.

JARVIS propose les plans, Simão passe les ordres lui-même : aucun ordre n'est envoyé à un courtier.
Les trades suivis sont gardés dans data/trades.json (JARVIS_TRADES_PATH).
"""
import json
import logging
import math
import os
import time as clock
import uuid
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import env_float
from app.core.http import SourceError
from app.lab import simulator as SIM
from app.sources import alpaca, kraken, twelvedata
from app.trading import calendar as CAL
from app.trading import catalyst as C
from app.trading import ibkr as IBKR
from app.trading import plan as P
from app.trading import score as SC
from app.trading import signals as S
from app.trading.settings import settings

log = logging.getLogger(__name__)
NEW_YORK = ZoneInfo("America/New_York")
PARIS = ZoneInfo("Europe/Paris")
OPEN, CLOSE, EARLY_CLOSE = time(9, 30), time(16, 0), time(13, 0)
TRADES_PATH = os.getenv("JARVIS_TRADES_PATH", "data/trades.json")
# Risque par défaut de plan_from_bars (labo, tests). En direct, build_plan propose le risque permis
# par le grade de JARVIS (app/lab/grade.py), sauf si un risque est demandé explicitement.
RISK_PCT = env_float("JARVIS_RISK_PCT", 3.0)  # EmperorBTC : 3 % du capital par trade, 5 % = agressif

# Bougies utilisées pour le plan et l'estimation des délais, par marché.
#   crypto : bougies d'1 h sur 30 jours (limite Kraken : 720), horizon 24 h
#   action : bougies de 15 min sur ~190 séances, horizon 52 bougies (13 h de cotation, 2 séances)
SETTINGS = {
    "crypto": {"interval": 60, "horizon_bars": 24, "label": "Kraken"},
    "stock": {"interval": 15, "horizon_bars": 52, "label": "Twelve Data"},
}
# Taille demandée à Twelve Data selon l'intervalle (1 crédit par appel, quelle que soit la taille).
STOCK_OUTPUTSIZE = {1: 60, 15: 5000, 240: 400, 1440: 300}
TWELVEDATA_MAX_OUTPUTSIZE = 5000
# 1 min : 15 s pour que le cockpit suive le prix en direct (Kraken est gratuit ; les actions restent
# limitées par STOCK_CHECK_SECONDS, la page ne déclenche jamais d'appel Twelve Data).
TTL = {1: 15, 15: 300, 60: 120, 240: 1800, 1440: 1800}
LIVE_BARS = 120  # mini-graphe du cockpit : les 2 dernières heures en bougies d'1 min
# Actions : 1 crédit Twelve Data par appel (800 par jour en gratuit). On ne relit les bougies d'1 min
# qu'une fois toutes les 2 minutes par action (quel que soit le nombre de trades dessus), et seulement
# pendant la séance ; les bougies du plan (signaux de sortie) une fois par bougie close.
STOCK_CHECK_SECONDS = env_float("JARVIS_STOCK_CHECK_SECONDS", 110)
_last_stock_check: dict[str, float] = {}       # action -> dernier appel (horloge monotone)
_last_plan_bar: dict[tuple, int] = {}          # (symbole, marché) -> dernière bougie du plan lue (fin, epoch)
_catchup_done: dict[str, str] = {}             # action -> séance déjà rattrapée après la clôture
# Cache des bougies : borné en âge et en taille pour ne pas grossir jour après jour.
CACHE_MAX_AGE = 24 * 3600
CACHE_MAX_ENTRIES = 256
_candle_cache: dict[tuple, tuple[float, list[dict]]] = {}


class PlanError(Exception):
    """Plan impossible (données absentes, niveaux incohérents)."""


def _market(market: str) -> str:
    market = market.lower()
    if market not in SETTINGS:
        raise PlanError("market doit valoir crypto ou stock")
    return market


def _prune_cache(now_mono: float) -> None:
    """Oublie les bougies trop anciennes, puis les plus vieilles au-delà de CACHE_MAX_ENTRIES."""
    for key in [k for k, (at, _) in _candle_cache.items() if now_mono - at > CACHE_MAX_AGE]:
        _candle_cache.pop(key, None)
    if len(_candle_cache) > CACHE_MAX_ENTRIES:
        oldest = sorted(_candle_cache, key=lambda k: _candle_cache[k][0])
        for key in oldest[: len(_candle_cache) - CACHE_MAX_ENTRIES]:
            _candle_cache.pop(key, None)


async def candles(symbol: str, market: str, interval: int, source: str | None = None,
                  max_age: float | None = None, outputsize: int | None = None) -> list[dict]:
    """Bougies de la plus ancienne à la plus récente ; la dernière peut être en cours.

    Actions : Twelve Data (temps réel, crédits comptés), Alpaca en secours ou sur demande
    (`source="alpaca"`, utilisé par le scanner : gratuit mais avec 16 min de retard).
    `max_age` : âge maximal accepté pour le cache (TTL de l'intervalle par défaut) ;
    `outputsize` : nombre de bougies demandées à Twelve Data (pour rattraper une coupure).
    """
    key = (symbol, market, interval, source or "")  # Alpaca (16 min de retard) ne sert pas un plan en direct
    now_mono = clock.monotonic()
    cached = _candle_cache.get(key)
    if cached and now_mono - cached[0] < (TTL.get(interval, 120) if max_age is None else max_age):
        return cached[1]
    size = outputsize or STOCK_OUTPUTSIZE.get(interval, 500)
    try:
        if market == "crypto":
            rows = await kraken.candles(symbol, interval)
        elif source == "alpaca":
            rows = await alpaca.candles(symbol, interval)
        else:
            try:
                rows = await twelvedata.candles(symbol, interval, outputsize=size)
            except SourceError:
                if not alpaca.configured():
                    raise
                days = math.ceil(size / 390) + 2 if interval == 1 and outputsize else None
                rows = await alpaca.candles(symbol, interval, days)
    except SourceError as exc:
        raise PlanError(f"bougies {symbol} indisponibles : {exc}") from exc
    _prune_cache(now_mono)
    _candle_cache[key] = (now_mono, rows)
    return rows


def with_open_bar(bars: list[dict], interval: int, now: datetime) -> list[dict]:
    """Le plan tient la dernière bougie pour en cours. Hors séance (scanner et labo à 09:10 NY,
    week-end) ou avec le retard d'Alpaca, la dernière bougie reçue est déjà close : on ajoute une
    bougie en cours vide (prix plat, volume nul), comme collector.in_progress, pour ne pas perdre la
    dernière bougie close (plus haut, plus bas et clôture de la veille, pivots, ATR)."""
    if not bars or bars[-1].get("pending") or bars[-1]["t"] + interval * 60 > now.timestamp():
        return bars
    c = bars[-1]["c"]
    return bars + [{"t": bars[-1]["t"] + interval * 60, "o": c, "h": c, "l": c, "c": c, "v": 0.0, "pending": True}]


async def _optional_candles(symbol: str, market: str, interval: int, source: str | None = None,
                            max_age: float | None = None) -> list[dict] | None:
    try:
        return await candles(symbol, market, interval, source, max_age=max_age)
    except PlanError as exc:
        log.warning("%s", exc)
        return None


# --- Heures de cotation ---------------------------------------------------------------

def add_market_minutes(start: datetime, minutes: float, closed: set[str] | None = None,
                       early: set[str] | None = None) -> datetime:
    """Avance de `minutes` minutes de cotation NYSE (9 h 30 - 16 h, heure de New York).

    Sans `closed` ni `early`, le calendrier calculé (app.trading.calendar) donne fériés et séances courtes.
    """
    if closed is None and early is None:
        try:
            return CAL.add_market_minutes(start, minutes)
        except ValueError as exc:
            raise PlanError(str(exc)) from exc
    closed, early = closed or set(), early or set()
    current = start.astimezone(NEW_YORK)
    remaining = timedelta(minutes=minutes)
    for _ in range(400):
        day = current.date()
        session_close = EARLY_CLOSE if day.isoformat() in early else CLOSE
        if day.weekday() < 5 and day.isoformat() not in closed:
            open_at = datetime.combine(day, OPEN, NEW_YORK)
            close_at = datetime.combine(day, session_close, NEW_YORK)
            if current < open_at:
                current = open_at
            if current < close_at:
                available = close_at - current
                if remaining <= available:
                    return current + remaining
                remaining -= available
        current = datetime.combine(day + timedelta(days=1), time(0, 0), NEW_YORK)
    raise PlanError("calendrier de cotation introuvable")


def duration_fr(minutes: float) -> str:
    minutes = int(round(minutes))
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest:02d}" if rest else f"{hours} h"


def time_fr(moment: datetime, now: datetime) -> str:
    local = moment.astimezone(PARIS)
    today = now.astimezone(PARIS).date()
    if local.date() == today:
        prefix = "aujourd'hui"
    elif local.date() == today + timedelta(days=1):
        prefix = "demain"
    else:
        prefix = local.strftime("%d/%m")
    return f"{prefix} {local.strftime('%H:%M')}"


def eta_at(market: str, start: datetime, minutes: float) -> datetime:
    """Heure estimée : minutes d'horloge pour la crypto, minutes de cotation pour les actions."""
    if market == "crypto":
        return start + timedelta(minutes=minutes)
    return add_market_minutes(start, minutes)


# --- Plan --------------------------------------------------------------------------------

def sizing(entry: float, stop: float, capital: float | None, risk_pct: float) -> dict | None:
    """Taille de position EmperorBTC : on risque `risk_pct` % du capital, la taille découle du stop,
    et le levier découle de la taille (jamais l'inverse). Sans change ni plafond : le labo ; en direct,
    apply_grade la remplace par la règle unique (euro_sizing)."""
    if not capital or risk_pct <= 0:
        return None
    risk_amount = capital * risk_pct / 100
    quantity = risk_amount / abs(entry - stop)
    notional = quantity * entry
    return {"capital": capital, "risk_pct": risk_pct, "risk_amount": round(risk_amount, 2),
            "quantity": round(quantity, 6), "notional": round(notional, 2),
            "leverage": round(max(notional / capital, 1.0), 2)}


def plan_levels(bars: list[dict], market: str, side: str, entry: float | None = None,
                stop: float | None = None, targets: list[float] | None = None, now: datetime | None = None,
                timing_lookback: int | None = None) -> tuple[P.Levels, dict, float]:
    """Niveaux, délais rejoués et ATR du plan ; PlanError si les bougies ne permettent pas de plan."""
    cfg = SETTINGS[market]
    if now is not None:
        bars = with_open_bar(bars, cfg["interval"], now)
    try:
        levels = P.default_levels(bars, side, entry, stop, targets, market=market, now=now)
        timing = P.historical_timing(bars, levels, cfg["horizon_bars"], lookback=timing_lookback)
        atr_value = levels.atr if levels.atr is not None else P.atr(bars)
    except ValueError as exc:
        raise PlanError(str(exc)) from exc
    return levels, timing, atr_value


def plan_from_bars(
    symbol: str, market: str, side: str, bars: list[dict], daily: list[dict] | None, h4: list[dict] | None,
    now: datetime, cat: dict | None = None, entry: float | None = None, stop: float | None = None,
    targets: list[float] | None = None, capital: float | None = None, risk_pct: float | None = None,
    s=None, timing_lookback: int | None = None, levels: tuple | None = None,
) -> dict:
    """Cœur du plan, sans réseau : niveaux, délais rejoués, checklist, catalyseur et note composite.

    Utilisé tel quel par build_plan (en direct) et par le labo (app/lab) : les bougies sont celles
    connues à `now`, la dernière étant tenue pour en cours. `s` : réglages de la note (ceux du
    moment par défaut) ; `levels` : résultat de plan_levels déjà calculé.
    """
    symbol, market, side = symbol.strip().upper(), _market(market), side.lower()
    s = settings if s is None else s
    cfg = SETTINGS[market]
    bars = with_open_bar(bars, cfg["interval"], now)
    levels, timing, atr_value = levels or plan_levels(bars, market, side, entry, stop, targets, now, timing_lookback)
    # Signaux sur les bougies closes : la dernière, en cours, fausserait le volume et les figures.
    check = S.checklist(bars[:-1], daily, h4, market, side, atr_value, now=now)
    step = cfg["interval"]
    clock_word = "" if market == "crypto" else " de cotation"
    estimates = []
    for i, (target, t) in enumerate(zip(levels.targets, timing["targets"]), start=1):
        r_multiple = round(abs(target.price - levels.entry) / levels.risk, 2)
        item = {
            "name": f"TP{i}", "price": target.price, "source": target.source,
            "pct": round(levels.pct(target.price), 2), "r_multiple": r_multiple,
            "hit_rate": t["hit_rate"], "stop_rate": t["stop_rate"], "samples": t["samples"],
            "hit_ci95": t["hit_ci95"], "reached_by_p75": t["reached_by_p75"],
            "still_after_p75": t["still_after_p75"], "reached_by_p75_given_prev": t["reached_by_p75_given_prev"],
            **P.expectancy(r_multiple, t["hit_rate"], t["stop_rate"]),
            "minutes_median": None, "minutes_p25": None, "minutes_p75": None,
            "eta_median": None, "eta_latest": None, "eta_text": "jamais atteint dans l'historique",
        }
        if t["bars_median"] is not None:
            item["minutes_median"] = t["bars_median"] * step
            item["minutes_p25"] = t["bars_p25"] * step
            item["minutes_p75"] = t["bars_p75"] * step
            eta_med = eta_at(market, now, item["minutes_median"])
            eta_late = eta_at(market, now, item["minutes_p75"])
            item["eta_median"] = eta_med.isoformat(timespec="minutes")
            item["eta_latest"] = eta_late.isoformat(timespec="minutes")
            item["eta_text"] = (
                f"~{duration_fr(item['minutes_median'])}{clock_word} (entre {duration_fr(item['minutes_p25'])} "
                f"et {duration_fr(item['minutes_p75'])}), vers {time_fr(eta_med, now)}, "
                f"au plus tard {time_fr(eta_late, now)} pour 3 sur 4 des cas qui l'ont touché "
                f"({t['reached_by_p75'] * 100:.0f} % de tous les cas)"
            )
        estimates.append(item)
    if capital is None:
        capital = env_float("JARVIS_CAPITAL", 0.0) or None
    plan = {
        "symbol": symbol, "market": market, "side": side,
        "entry": levels.entry, "stop": levels.stop, "stop_source": levels.stop_source,
        "stop_pct": round(-abs(levels.pct(levels.stop)), 2), "atr": P.round_price(atr_value),
        "targets": estimates,
        "stop_first_rate": timing["stop_first_rate"], "stop_first_ci95": timing["stop_first_ci95"],
        "checklist": check,
        "sizing": sizing(levels.entry, levels.stop, capital, RISK_PCT if risk_pct is None else risk_pct),
        "method": (
            f"Délais mesurés en rejouant ce plan sur {timing['starts']} départs qui se chevauchent "
            f"(≈ {timing['effective_starts']} fenêtres indépendantes au moins), bougies de {step} min de l'historique "
            f"({cfg['label']}), horizon {cfg['horizon_bars'] * step // 60} h{clock_word}. Heures de Paris."
        ),
        "created_at": now.isoformat(timespec="seconds"),
        "catalyst": cat,
    }
    plan["score"] = SC.evaluate(plan, daily, cat, s)
    return plan


# Plancher du stop en direct (choix de Simão, 2026-10-08) : un stop calculé sur bougies de 15 min peut
# tomber à quelques cents (FRSH : 9 cents pour 0,52 $ d'amplitude journalière), dans le bruit, et la
# taille qui en découle gonfle les frais. Le labo (plan_from_bars) garde la règle d'origine.
STOP_FLOOR_DAILY_ATR = 1 / 3


def _floor_stop(bars: list[dict], daily: list[dict] | None, market: str, side: str, entry: float | None,
                targets: list[float] | None, now: datetime, levels: tuple) -> tuple:
    """Éloigne le stop à au moins 1/3 de l'ATR journalier ; objectifs recalculés sur ce nouveau risque."""
    lv = levels[0]
    try:
        daily_atr = P.atr(daily[:-1]) if daily else None
    except ValueError:
        daily_atr = None
    floor = STOP_FLOOR_DAILY_ATR * daily_atr if daily_atr else None
    if not floor or lv.risk >= floor:
        return levels
    sign = 1 if side == "long" else -1
    new = plan_levels(bars, market, side, entry, lv.entry - sign * floor, targets, now)
    new[0].stop_source = (f"{lv.stop_source}, éloigné à 1/3 de l'ATR journalier "
                          f"({_num(P.round_price(daily_atr))}) contre le bruit, au lieu de {_num(lv.stop)}")
    return new


async def build_plan(
    symbol: str, market: str, side: str = "long", entry: float | None = None,
    stop: float | None = None, targets: list[float] | None = None, now: datetime | None = None,
    capital: float | None = None, risk_pct: float | None = None, source: str | None = None,
    cat: dict | None = None, methode: bool = True,
) -> dict:
    """Plan complet : niveaux, délais rejoués, checklist, catalyseur et note composite.

    Lit les bougies (et le catalyseur pour une action) puis confie le calcul à plan_from_bars.
    `cat` : catalyseur déjà lu (le scanner l'a en main) pour ne pas rappeler Finnhub.
    `methode` False : pas de filtre de la méthode championne (`!prends … go` : le GO confirmé a déjà été gardé par
    le champion au scanner, et le plan recalculé au déclenchement ne doit pas lui retirer la taille proposée).
    """
    symbol, market, side = symbol.strip().upper(), _market(market), side.lower()
    now = now or datetime.now(timezone.utc)
    cfg = SETTINGS[market]
    bars = await candles(symbol, market, cfg["interval"], source)
    # Niveaux d'abord : un plan impossible ne coûte ni bougies journalières ni appels Finnhub.
    levels = plan_levels(bars, market, side, entry, stop, targets, now)
    daily = await _optional_candles(symbol, market, 1440, source)
    if stop is None:
        levels = _floor_stop(bars, daily, market, side, entry, targets, now, levels)
    h4 = await _optional_candles(symbol, market, 240, source)
    if market == "stock" and cat is None:
        cat = await C.safe_check(symbol, now)
    plan = plan_from_bars(symbol, market, side, bars, daily, h4, now, cat, entry, stop, targets,
                          capital, risk_pct, levels=levels)
    # Leçons confirmées du labo : en direct seulement (le labo note ses plans sans elles).
    from app.lab import lessons as LESSONS  # import tardif : le labo importe ce module

    await LESSONS.apply(plan, daily, cat, now)
    # Filtre de la méthode championne du labo, comme au scanner : le grade est mesuré sur les trades qu'elle garde,
    # un plan qu'elle écarte ne reçoit pas son risque (apply_grade). None : aucune règle sur ce marché.
    from app.lab import live as LIVE  # import tardif : le labo importe ce module

    methode = await LIVE.plan_verdict(plan, daily, now) if methode else None
    apply_grade(plan, capital, risk_pct, now, usd_per_eur=await usd_per_eur(), methode=methode)
    IBKR.attach(plan)  # brackets prêts à saisir, à la taille du plan (règle unique en euros)
    return plan


def taken_trades() -> list[dict]:
    """Trades pris par Simão (remplacé par le scheduler au démarrage : son carnet)."""
    return []


def rule_eur() -> float:
    """La règle de Simão : JARVIS_RISK_EUR (20 € par défaut) de risque par trade, la même que Killmode."""
    from app.trading import killmode as K  # import tardif : killmode importe ce module

    return K.RISK_EUR


def recommended_eur(pct: float | None, capital: float | None = None) -> float | None:
    """Règle unique de taille : risque recommandé en €, min(capital × pct / 100, JARVIS_RISK_EUR), JARVIS_RISK_EUR
    si le capital n'est pas connu ; None si rien n'est proposé (pct ≤ 0). JARVIS_CAPITAL est en euros."""
    if not pct or pct <= 0:
        return None
    if capital is None:
        capital = env_float("JARVIS_CAPITAL", 0.0) or None
    return min(capital * pct / 100, rule_eur()) if capital else rule_eur()


def euro_sizing(entry: float, stop: float, market: str, risk_eur: float, usd_per_eur: float,
                capital: float | None = None) -> dict | None:
    """Taille pour `risk_eur` € de risque au change `usd_per_eur` (gardé dans la taille) : actions en nombre entier
    (0 quand une seule action dépasse ce risque : pas d'ordre), crypto en quantité fractionnaire arrondie vers le bas
    à 6 décimales (jamais plus de risque que prévu). La taille découle du stop, le levier de la taille."""
    unit = abs(entry - stop)
    if not unit or risk_eur <= 0:
        return None
    risk_usd = risk_eur * usd_per_eur
    quantity = (math.floor(risk_usd / unit + 1e-9) if market == "stock"
                else math.floor(risk_usd / unit * 1e6 + 1e-9) / 1e6)
    notional = quantity * entry
    return {"capital": capital, "risk_pct": round(risk_eur / capital * 100, 3) if capital else None,
            "risk_amount_eur": round(risk_eur, 2), "risk_amount": round(risk_usd, 2), "eurusd": usd_per_eur,
            "quantity": quantity, "notional": round(notional, 2), "unit_risk_eur": round(unit / usd_per_eur, 2),
            "leverage": round(max(notional / (capital * usd_per_eur), 1.0), 2) if capital else None}


async def usd_per_eur() -> float:
    """Change EUR→USD du jour (BCE, une heure de cache) pour la taille en euros ; 1,15 de Killmode si la source ne
    répond pas."""
    from app import needs  # import tardif : needs tire tout le serveur de données
    from app.trading import killmode as K  # import tardif : killmode importe ce module

    try:
        return (await needs.fx_rates("EUR", ["USD"])).data["rates"]["USD"] or K.EURUSD_DEFAULT
    except Exception as exc:
        log.warning("Taux EUR/USD indisponible pour la taille du plan (%s) : %g par défaut", exc, K.EURUSD_DEFAULT)
        return K.EURUSD_DEFAULT


def apply_grade(plan: dict, capital: float | None, risk_pct: float | None, now: datetime,
                trades: list[dict] | None = None, usd_per_eur: float | None = None, methode: dict | None = None) -> None:
    """Grade de JARVIS et taille du plan, une seule règle : risque en € = min(capital × % proposé, JARVIS_RISK_EUR)
    (JARVIS_RISK_EUR sans capital connu), au change `usd_per_eur` (1,15 par défaut). Le grade propose 0 % (F, E,
    non noté, périmé, illisible) : JARVIS ne propose pas de taille (`sizing` None) et la règle de Simão est à part
    (`sizing_regle`, hors grade). `methode` : verdict de la méthode championne (live.plan_verdict) ; un plan
    qu'elle écarte est hors de la population que mesure le grade : 0 % proposé, « écarté par la méthode vN ».
    Un risque demandé explicitement remplace le % du grade (même plafond en euros), marqué hors budget s'il engage
    plus d'euros que ce que propose JARVIS (arrêt de la journée compris). Jamais d'exception : un grade illisible
    vaut 0 % (fail-closed)."""
    from app.lab import grade as GRADE  # import tardif : le labo importe ce module
    from app.trading import killmode as K  # import tardif : killmode importe ce module

    try:
        g = GRADE.risk_now(plan["market"], taken_trades() if trades is None else trades, now)
    except Exception as exc:
        log.warning("Grade du labo illisible (%s: %s) : risque proposé 0 %%", type(exc).__name__, exc)
        g = GRADE.unreadable(exc)
    if methode is not None:
        plan["methode"] = methode
    if methode and not methode.get("garde", True):
        names = ", ".join(f"« {n} »" for n in methode.get("regles") or []) or "?"
        rule = "règles" if len(methode.get("regles") or []) > 1 else "règle"
        g = g | {"risque_propose": 0.0, "observation": True, "ecarte": True,
                 "raison": f"écarté par la méthode {methode.get('version') or '?'} ({rule} {names})"}
    plan["grade"] = g
    cap = capital if capital is not None else (env_float("JARVIS_CAPITAL", 0.0) or None)
    rate = usd_per_eur or K.EURUSD_DEFAULT
    proposed = recommended_eur(g["risque_propose"], cap)
    eur = proposed if risk_pct is None else recommended_eur(risk_pct, cap)
    if risk_pct is not None and (eur or 0.0) > (proposed or 0.0) + 1e-9:
        g |= {"hors_budget": True, "risque_demande": risk_pct, "risque_demande_eur": eur, "propose_eur": proposed}
    plan["sizing"] = euro_sizing(plan["entry"], plan["stop"], plan["market"], eur, rate, cap) if eur else None
    plan.pop("sizing_regle", None)
    if risk_pct is None and g["risque_propose"] <= 0:
        rule = euro_sizing(plan["entry"], plan["stop"], plan["market"], rule_eur(), rate, cap)
        if rule:
            plan["sizing_regle"] = rule | {"hors_grade": True}


def _num(x: float) -> str:
    return f"{x:,.6g}".replace(",", " ")


def score_lines(p: dict) -> list[str]:
    """Verdict, note composite et catalyseur, en quelques lignes."""
    sc = p["score"]
    parts = sc["parts"]
    icon = "✅" if sc["go"] else "⛔"
    sub = " · ".join(f"{label} {parts[k]['score']:g}" for k, label in
                     (("technique", "technique"), ("catalyst", "catalyseur"), ("risk", "risque"))
                     if parts[k]["score"] is not None)
    lines = [f"{icon} **{sc['decision']} · note {sc['composite']:g}/10** ({sub})"
             + (f" · {' ; '.join(sc['reasons'])}" if sc["reasons"] else "")]
    note = SC.slope_note(p.get("market", "stock"))
    if note:
        lines.append(f"-# {note[0].upper()}{note[1:]} : GO = règle de sélection figée, pas une prédiction")
    for k, label in (("technique", "Technique"), ("catalyst", "Catalyseur"), ("risk", "Risque")):
        lines.append(f"-# {label} : {' ; '.join(parts[k]['reasons'])}")
    if sc.get("lecons"):
        les = sc["lecons"]
        with_lessons = sc.get("composite_lecons")
        lines.append(f"-# Leçons du labo (affichées, sans effet sur le verdict) : {' ; '.join(les['lignes'])}"
                     + (f" · note avec leçons {with_lessons:g}" if with_lessons is not None else ""))
    cat = p.get("catalyst") or {}
    marks = {"positive": "🟢", "negative": "🔴", "dilution": "🟠", "neutre": "⚪"}
    for n in cat.get("news", [])[:3]:
        lines.append(f"{marks[n['tone']]} [{n['title'][:110]}](<{n['url']}>)")
    return lines


CONFIRM_WAIT_MIN = 15  # actions : pas d'entrée pendant le premier quart d'heure (range d'ouverture)


def confirmation_lines(p: dict, now: datetime | None = None) -> list[str]:
    """Ce qu'il faut voir avant d'entrer, et ce qui annule le plan : le prix du plan vieillit
    (pré-ouverture, bougies en retard), une condition observable sur le graphique reste valable.

    Repli vers l'entrée sans clôture au-delà du stop, puis reprise du VWAP sur une bougie 5 min :
    on entre par un ordre stop au-delà de cette bougie. Si le TP1 est touché avant, on ne court pas
    après. Lecture seule : ne change ni les niveaux ni la note.
    """
    if now is None:
        now = datetime.fromisoformat(p["created_at"]) if p.get("created_at") else datetime.now(timezone.utc)
    if p.get("entry") is None or p.get("stop") is None:
        return []
    long = p.get("side", "long") == "long"
    stock = p.get("market", "stock") == "stock"  # le scanner ne voit que des actions à l'achat
    entry, stop = _num(p["entry"]), _num(p["stop"])
    tp1 = _num(p["targets"][0]["price"]) if p.get("targets") else None
    bar = "5 min" if stock else "15 min"
    lines = []
    if stock:
        start = CAL.current_or_next_session(now)[0] + timedelta(minutes=CONFIRM_WAIT_MIN)
        if now < start:
            lines.append(f"⏱️ Pas d'entrée avant {time_fr(start, now)} (Paris), le temps que le range d'ouverture se forme.")
    if long:
        lines.append(f"👀 **Confirmation** : repli vers {entry} sans clôture {bar} sous {stop}, puis une bougie {bar} "
                     f"qui clôture au-dessus du VWAP → stop d'achat 1 tick au-dessus de son plus haut.")
        cancel = f"clôture {bar} sous {stop}"
    else:
        lines.append(f"👀 **Confirmation** : rebond vers {entry} sans clôture {bar} au-dessus de {stop}, puis une bougie "
                     f"{bar} qui clôture sous le VWAP → stop de vente 1 tick sous son plus bas.")
        cancel = f"clôture {bar} au-dessus de {stop}"
    if tp1:
        cancel += f", ou TP1 {tp1} touché avant l'entrée (ne pas courir après)"
    lines.append(f"❌ **Annulé** : {cancel}.")
    return lines


def _amount(x: float, digits: int = 2) -> str:
    """Montant à la française : « 20 », « 12,5 », « 23,0 » (digits fixe le nombre de décimales, 2 au plus sinon)."""
    if digits == 2:
        return f"{x:.2f}".rstrip("0").rstrip(".").replace(".", ",")
    return f"{x:.{digits}f}".replace(".", ",")


def crypto_qty(q: float) -> str:
    """Quantité crypto en clair, sans notation scientifique ni arrondi vers le haut : au plus 6 chiffres significatifs
    (tronqués), jamais moins que la partie entière, espaces comme les montants (« 37 096 774 », « 0.023 »)."""
    if q <= 0:
        return "0"
    digits = max(0, min(6, 5 - math.floor(math.log10(q))))  # décimales gardées
    text = f"{math.floor(q * 10 ** digits + 1e-9) / 10 ** digits:,.{digits}f}".replace(",", " ")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _quantity(p: dict, s: dict) -> str:
    """Taille en clair : actions entières (« pas d'ordre » quand une seule action dépasse le risque), crypto en
    quantité."""
    q = s["quantity"]
    if p.get("market") != "stock":
        return f"taille {crypto_qty(q)} pour {_num(s['notional'])} $" + (
            f" · levier {s['leverage']:g}×" if (s.get("leverage") or 1) > 1 else "")
    if q < 1:
        return (f"pas d'ordre, 1 action = {_amount(s['unit_risk_eur'])} € de risque, au-delà de "
                f"{_amount(s['risk_amount_eur'])} €")
    return f"{q} action{'s' if q > 1 else ''} pour {_num(s['notional'])} $"


def sizing_lines(p: dict) -> list[str]:
    """La ligne 💰 : la taille que propose JARVIS (règle unique en euros), ou, quand il n'en propose pas (grade à
    0 %), la règle de Simão à part, jamais confondue avec une recommandation."""
    s, rule = p.get("sizing"), p.get("sizing_regle")
    if s and "risk_amount_eur" in s:
        line = f"💰 Risque {_amount(s['risk_amount_eur'])} € ({_amount(s['risk_amount'], 1)} $) : {_quantity(p, s)}"
        if s.get("hors_grade"):
            line += " · taille du KILL, hors grade" + (
                f", au-delà de {_amount(rule_eur())} € (une action au moins)" if s.get("hors_budget") else "")
        elif (p.get("grade") or {}).get("hors_budget"):  # risk_pct explicite au-delà de ce que propose JARVIS
            line += " · risque demandé, hors budget"
        return [line]
    if s:  # taille sans change (plan_from_bars seul : labo, anciens trades)
        return [f"💰 Risque {s['risk_pct']:g} % = {s['risk_amount']:g} · taille {s['quantity']:g} "
                f"({_num(s['notional'])}) · levier {s['leverage']:g}×"]
    if rule:
        g = p.get("grade") or {}
        # La lettre seule quand c'est elle qui dit 0 % ; sinon la vraie raison (arrêt du jour, budget, grade périmé,
        # illisible, fenêtre discontinue, plan écarté par la méthode), qu'un « grade A : 0 % » cacherait.
        by_letter = (not (g.get("risque_grade") or 0) > 0 and not g.get("perime") and not g.get("ecarte")
                     and not str(g.get("raison", "")).startswith(("grade illisible", "fenêtre discontinue")))
        why = (f" (grade {g.get('grade', 'non noté')} : 0 %)" if by_letter or not g.get("raison")
               else f" : plan {g['raison']}" if g.get("ecarte") else f" (0 % : {g['raison']})")
        return [f"💰 JARVIS ne propose pas de taille{why}. "
                f"Ta règle de {_amount(rule['risk_amount_eur'])} € : {_quantity(p, rule)} (exception, hors avis de "
                "JARVIS)"]
    return []


def plan_text(p: dict) -> str:
    """Le plan en clair pour Discord."""
    arrow = "🟢 LONG" if p["side"] == "long" else "🔴 SHORT"
    check = p["checklist"]
    lines = [
        f"**{arrow} {p['symbol']}** · entrée {_num(p['entry'])} · stop {_num(p['stop'])} "
        f"({p['stop_pct']:+.2f} %, {p['stop_source']})",
        f"**{check['verdict']}** · checklist EmperorBTC {check['score']}/{check['max_score']}",
    ]
    if p.get("score"):
        lines += score_lines(p)
    for t in p["targets"]:
        ci = t.get("hit_ci95")
        ci_text = f" (IC 95 % : {ci[0] * 100:.0f}-{ci[1] * 100:.0f} %)" if ci else ""
        lines.append(
            f"🎯 {t['name']} {_num(t['price'])} ({t['pct']:+.2f} %, {t['r_multiple']:g} R, {t['source']}) · "
            f"{t['eta_text']} · touché avant le stop {t['hit_rate'] * 100:.0f} %{ci_text}, "
            f"espérance {t['expectancy_r']:+.2f} R"
        )
    lines += confirmation_lines(p)
    marks = {True: "✅", False: "❌", None: "➖"}
    lines.append(" · ".join(f"{marks[i['ok']]} {i['label']}" for i in check["items"]))
    lines += sizing_lines(p)
    if p.get("grade"):
        from app.lab import grade as GRADE

        lines.append(GRADE.plan_line(p["grade"]))
    lines += IBKR.lines(p)
    lines.append(f"-# {p['method']}")
    return "\n".join(lines)


# --- Trades suivis -----------------------------------------------------------------------

class Book:
    def __init__(self, path: str | None = TRADES_PATH):
        self.path = Path(path) if path else None
        self.trades: list[dict] = []
        if self.path and self.path.exists():
            try:
                self.trades = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                log.warning("Fichier des trades illisible, on repart de zéro")

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.trades, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, trade_id: str) -> dict | None:
        return next((t for t in self.trades if t["id"] == trade_id), None)

    def add(self, p: dict, note: str | None = None) -> dict:
        trade = p | {
            "id": uuid.uuid4().hex[:8], "status": "open", "note": note,
            "stop_current": p["stop"], "targets_hit": 0, "events": [],
            "last_price": p["entry"], "last_check": int(datetime.fromisoformat(p["created_at"]).timestamp()),
            "late_warned": [], "exit_signals": [],
        }
        self.trades.append(trade)
        self.save()
        return trade

    def close(self, trade_id: str, price: float | None, reason: str = "clôturé à la main") -> dict | None:
        trade = self.get(trade_id)
        if trade and trade["status"] == "open":
            trade["status"] = "closed"
            trade["events"].append({"kind": "close", "price": trade["last_price"] if price is None else price, "reason": reason,
                                    "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
            _settle(trade)
            self.save()
        return trade

    def remove(self, trade_id: str) -> bool:
        before = len(self.trades)
        self.trades = [t for t in self.trades if t["id"] != trade_id]
        self.save()
        return len(self.trades) < before

    def open_trades(self) -> list[dict]:
        return [t for t in self.trades if t["status"] == "open"]


def reopen(book: Book, symbol: str, plan: dict) -> dict | None:
    """Remplace le dernier trade de `symbol` terminé par son stop (stop non exécuté chez le courtier,
    position toujours tenue) par un trade ouvert sur `plan`. Renvoie l'ancien trade, ou None s'il n'y en a pas."""
    old = next((t for t in reversed(book.trades) if t["symbol"] == symbol.upper() and t["status"] == "closed"
                and t["events"] and t["events"][-1]["kind"] == "stop"), None)
    if old is None:
        return None
    # Même position : un KILL pris garde sa gestion et sa taille (`!prends … kill`), un GO confirmé son camp (!desaccords).
    plan |= {k: old[k] for k in ("killmode", "policy") if old.get(k)}
    # La note, le grade et la taille de l'entrée font foi (même trade, même quantité tenue) : la note recalculée au
    # niveau du stop ne fait pas passer un trade « contre JARVIS », et la taille garde les frais du trade réel.
    for k in ("score", "grade", "sizing", "sizing_regle"):
        if k in old:
            plan[k] = old[k]
        else:
            plan.pop(k, None)
    if plan.get("killmode"):
        from app.trading import killmode as K  # import tardif : killmode importe ce module

        plan["sizing"] = K.taken_sizing(plan)
        plan.pop("sizing_regle", None)
    book.trades.remove(old)
    book.add(plan, f"rouvert : stop {_num(old['stop_current'])} non exécuté chez le courtier")
    return old


def _event(trade: dict, kind: str, price: float, at: int) -> None:
    trade["events"].append({"kind": kind, "price": price,
                            "at": datetime.fromtimestamp(at, timezone.utc).isoformat(timespec="seconds")})


def apply_bars(trade: dict, bars: list[dict], now: datetime) -> list[str]:
    """Rejoue les bougies d'1 min arrivées depuis le dernier passage. Renvoie les messages à envoyer.

    Le stop ne remonte pas au prix d'entrée dès le TP1 (EmperorBTC : « ne soyez pas pressé de
    passer votre SL au seuil de rentabilité ») : il y monte après le TP2. Un KILL pris (`!prends … kill`) suit la
    gestion que mesure le journal de Killmode : stop à l'entrée dès le TP1.
    """
    messages = []
    sign = 1 if trade["side"] == "long" else -1
    created = datetime.fromisoformat(trade["created_at"])
    for bar in bars:
        if bar["t"] < trade["last_check"] or trade["status"] != "open":
            continue
        trade["last_price"] = bar["c"]
        # La dernière bougie peut être en cours : elle sera relue au prochain passage (sans doublon).
        trade["last_check"] = bar["t"]
        stop_hit = bar["l"] <= trade["stop_current"] if sign == 1 else bar["h"] >= trade["stop_current"]
        # La bougie du TP2 est relue au passage suivant : son plus bas date d'avant le TP2, donc d'avant
        # la remontée du stop à l'entrée ; elle ne peut pas toucher ce nouveau stop.
        if stop_hit and bar["t"] <= trade.get("stop_moved_at", -1):
            stop_hit = False
        if stop_hit:
            trade["status"] = "closed"
            # Ouverture au-delà du stop (gap) : l'ordre stop part au marché, au pire du stop et de l'ouverture
            # (règle du labo, simulator.py).
            stop = trade["stop_current"]
            fill = min(stop, bar["o"]) if sign == 1 else max(stop, bar["o"])
            _event(trade, "stop", fill, bar["t"])
            _settle(trade)
            where = (f"exécuté vers {_num(fill)} (gap {'sous le' if sign == 1 else 'au-dessus du'} stop {_num(stop)})"
                     if fill != stop else f"touché à {_num(stop)}")
            if trade["targets_hit"]:
                messages.append(f"🛡️ **{trade['symbol']}** : stop suivi {where} après "
                                f"TP{trade['targets_hit']}. Trade terminé.")
            else:
                tail = (f" ({trade['stop_pct']:+.2f} %)" if fill == stop
                        else f", soit {-abs(fill - trade['entry']) / trade['entry'] * 100:+.2f} %")
                messages.append(f"🛑 **{trade['symbol']}** : stop {where}{tail}. "
                                "Point d'invalidation atteint, trade terminé.")
            break
        while trade["targets_hit"] < len(trade["targets"]):
            target = trade["targets"][trade["targets_hit"]]
            # Un objectif compte franchi d'au moins un pas de cotation, comme au labo (un contact ne remplit
            # pas forcément l'ordre limite) ; comparaison en pas, avec une tolérance pour les flottants.
            tick = SIM.tick_size(trade.get("market", "stock"), target["price"])
            if ((bar["h"] if sign == 1 else bar["l"]) - target["price"]) * sign < (1 - 1e-6) * tick:
                break
            trade["targets_hit"] += 1
            n = trade["targets_hit"]
            _event(trade, target["name"].lower(), target["price"], bar["t"])
            elapsed = (bar["t"] - created.timestamp()) / 60
            text = (f"🎯 **{trade['symbol']} {target['name']} touché** à {_num(target['price'])} "
                    f"({target['pct']:+.2f} %) après {duration_fr(max(elapsed, 1))}")
            if target["minutes_median"]:
                text += f" (prévu ~{duration_fr(target['minutes_median'])})"
            text += "."
            if n == len(trade["targets"]):
                trade["status"] = "closed"
                _settle(trade)
                text += " Dernier objectif atteint, trade terminé. 🏁"
            else:
                nxt = trade["targets"][n]
                if trade.get("killmode"):  # la gestion du KILL, celle que son journal mesure : l'entrée dès TP1
                    trade["stop_current"] = trade["entry"]
                    trade["stop_moved_at"] = bar["t"]
                    bounds = CAL.session_bounds(datetime.fromtimestamp(bar["t"], NEW_YORK).date())
                    last = (bounds[1] - timedelta(minutes=1)).astimezone(NEW_YORK) if bounds else None
                    stop_text = (f"Vérifie que le stop ajustable des brackets restants est passé à l'entrée "
                                 f"({_num(trade['entry'])}), sinon remonte-les : c'est la gestion du KILL que le "
                                 "journal mesure. Le reste se vend au marché "
                                 + (f"vers {last.hour} h {last.minute:02d} New York." if last
                                    else "juste avant la clôture."))
                elif n >= 2:
                    trade["stop_current"] = trade["entry"]
                    trade["stop_moved_at"] = bar["t"]
                    stop_text = f"Stop remonté à l'entrée ({_num(trade['entry'])})."
                else:
                    stop_text = f"Stop inchangé à {_num(trade['stop_current'])} (pas de break-even précipité)."
                text += f"\nProchain : {nxt['name']} {_num(nxt['price'])}, {nxt['eta_text']}. {stop_text} Garde le reste."
            messages.append(text)
    # Objectif en retard sur le délai à 75 % des cas qui l'ont touché : l'élan faiblit peut-être. Avant le TP
    # précédent, la population est celle de tous les cas passés ; après, celle des cas qui l'ont touché.
    if trade["status"] == "open" and trade["targets_hit"] < len(trade["targets"]):
        k = trade["targets_hit"]
        nxt = trade["targets"][k]
        if nxt["eta_latest"] and nxt["name"] not in trade["late_warned"] and now >= datetime.fromisoformat(nxt["eta_latest"]):
            trade["late_warned"].append(nxt["name"])
            given = nxt.get("reached_by_p75_given_prev") if k else None
            share, still = given if given is not None else nxt.get("reached_by_p75"), nxt.get("still_after_p75")
            if share is None:  # trade suivi avant ces mesures : seul le p75 des cas qui l'ont touché
                why = "3 sur 4 des cas passés qui l'ont touché l'avaient déjà fait à cette heure"
            else:
                who = "des cas passés" + (f" qui avaient touché {trade['targets'][k - 1]['name']}" if given is not None
                                          else "")
                why = f"à cette heure, {share * 100:.0f} % {who} l'avaient atteint"
                if still is not None:
                    # still_after_p75 porte sur tous les départs, même quand `share` ne compte que ceux du TP précédent
                    why += (f" ; parmi tous les cas passés encore en vie à cette heure (ni objectif ni stop), "
                            f"{still * 100:.0f} % l'ont touché ensuite")
            messages.append(f"⏳ **{trade['symbol']}** : {nxt['name']} ({_num(nxt['price'])}) pas encore touché ; "
                            f"{why}. Dernier prix {_num(trade['last_price'])}.")
    return messages


def apply_exit_signals(trade: dict, bars: list[dict]) -> list[str]:
    """Signes de retournement pendant un trade gagnant : une alerte par type de signal et par trade."""
    if trade["status"] != "open":
        return []
    sign = 1 if trade["side"] == "long" else -1
    in_profit = (trade["last_price"] - trade["entry"]) * sign > 0
    if not in_profit and not trade["targets_hit"]:
        return []
    messages = []
    for kind, text in S.exit_signals(bars, trade["side"]):
        if kind in trade["exit_signals"]:
            continue
        trade["exit_signals"].append(kind)
        messages.append(f"⚠️ **{trade['symbol']}** : {text}. Signe de sortie anticipée (EmperorBTC : on prend "
                        f"ses bénéfices sur un retournement, on ne se retourne pas). Dernier prix {_num(trade['last_price'])}.")
    return messages


def _watch_exit(trade: dict) -> bool:
    """Les signaux de sortie ne servent que sur un trade gagnant (cf. apply_exit_signals)."""
    sign = 1 if trade["side"] == "long" else -1
    return trade["status"] == "open" and ((trade["last_price"] - trade["entry"]) * sign > 0 or trade["targets_hit"])


def minute_outputsize(trades: list[dict], now: datetime) -> int:
    """Bougies d'1 min à demander pour couvrir tout ce qui s'est passé depuis le dernier passage
    (coupure, redémarrage) : minutes de cotation écoulées + marge, plafonné à la limite de Twelve Data."""
    oldest = min(t["last_check"] for t in trades)
    minutes = CAL.market_minutes_between(datetime.fromtimestamp(oldest, timezone.utc), now)
    return max(STOCK_OUTPUTSIZE[1], min(math.ceil(minutes) + 5, TWELVEDATA_MAX_OUTPUTSIZE))


def _plan_bar_boundary(market: str, interval: int, now: datetime) -> int | None:
    """Fin de la dernière bougie du plan close à `now` (epoch), None si aucune bougie de séance n'a clos."""
    step = interval * 60
    boundary = int(now.timestamp()) // step * step
    if market == "stock":
        moment = datetime.fromtimestamp(boundary, timezone.utc)
        bounds = CAL.session_bounds(moment.astimezone(NEW_YORK).date())
        if not bounds or not bounds[0] < moment <= bounds[1]:
            return None
    return boundary


async def _check_symbol(symbol: str, market: str, trades: list[dict], now: datetime, out: list[str]) -> None:
    """Un seul appel de bougies d'1 min par symbole, appliqué à tous ses trades ouverts."""
    outputsize = None
    if market == "stock":
        if CAL.session_open(now):
            ready = clock.monotonic() - _last_stock_check.get(symbol, float("-inf")) >= STOCK_CHECK_SECONDS
        else:
            # Après la clôture : un seul rattrapage si la fin de séance n'a pas été lue (coupure).
            prev = CAL.previous_session(now)
            day = prev[1].date().isoformat() if prev else None
            ready = bool(prev) and _catchup_done.get(symbol) != day and \
                any(t["last_check"] < prev[1].timestamp() - 60 for t in trades)
            if ready:
                _catchup_done[symbol] = day
        if not ready:
            for trade in trades:
                out += apply_bars(trade, [], now)  # seulement l'alerte de retard éventuelle
            return
        _last_stock_check[symbol] = clock.monotonic()
        outputsize = minute_outputsize(trades, now)
    try:
        bars = await candles(symbol, market, 1, max_age=0 if market == "stock" else None, outputsize=outputsize)
    except PlanError as exc:
        log.warning("Suivi %s impossible : %s", symbol, exc)
        return
    for trade in trades:
        out += apply_bars(trade, bars, now)
    # Signaux de sortie : une seule lecture par bougie du plan close, et seulement sur un trade gagnant.
    interval = SETTINGS[market]["interval"]
    key = (symbol, market)
    boundary = _plan_bar_boundary(market, interval, now)
    watched = [t for t in trades if _watch_exit(t)]
    if boundary is None or not watched or _last_plan_bar.get(key, 0) >= boundary:
        return
    plan_bars = await _optional_candles(symbol, market, interval, max_age=now.timestamp() - boundary)
    if not plan_bars:
        return
    _last_plan_bar[key] = boundary
    closed = [b for b in plan_bars if b["t"] + interval * 60 <= now.timestamp()]
    for trade in watched:
        out += apply_exit_signals(trade, closed)


async def check_trades(book: Book, now: datetime | None = None, save: bool = True) -> list[str]:
    """Vérifie les trades ouverts : TP/SL sur les bougies d'1 min, signaux de sortie sur les bougies du plan.

    `save=False` : l'appelant enregistre le carnet lui-même (après avoir mis les messages en file d'envoi).
    """
    now = now or datetime.now(timezone.utc)
    messages: list[str] = []
    groups: dict[tuple[str, str], list[dict]] = {}
    for trade in book.open_trades():
        groups.setdefault((trade["symbol"], trade["market"]), []).append(trade)
    for (symbol, market), trades in groups.items():
        try:
            await _check_symbol(symbol, market, trades, now, messages)
        except Exception:  # une réponse inattendue d'une source ne doit pas arrêter le suivi des autres
            log.exception("Suivi %s en échec", symbol)
    # Les états par action ne gardent que les symboles encore suivis.
    symbols = {s for s, _ in groups}
    for d in (_last_stock_check, _catchup_done):
        for k in [k for k in d if k not in symbols]:
            d.pop(k, None)
    for k in [k for k in _last_plan_bar if k not in groups]:
        _last_plan_bar.pop(k, None)
    if save:
        book.save()
    return messages


def summary(trade: dict) -> dict:
    """Ce que le cockpit affiche pour un trade."""
    sign = 1 if trade["side"] == "long" else -1
    move = sign * (trade["last_price"] - trade["entry"]) / trade["entry"] * 100
    risk = abs(trade["entry"] - trade["stop"])
    r_now = sign * (trade["last_price"] - trade["entry"]) / risk if risk else 0
    return trade | {"move_pct": round(move, 2), "r_now": round(r_now, 2),
                    "against_jarvis": against_jarvis(trade), "realized_r": realized_r(trade),
                    "realized_r_net": realized_r_net(trade)}


# --- Désaccords : Simão contre JARVIS, mesuré ----------------------------------------------
# JARVIS ne dit pas oui parce que c'est Simão : un trade pris sur un plan NO GO est marqué
# « contre l'avis de JARVIS » et !desaccords compare ces trades aux autres pour voir qui avait raison.

def against_jarvis(p: dict) -> bool:
    """Vrai si le plan était noté NO GO (un plan sans note n'est ni pour ni contre). Un KILL de Killmode pris
    (`!prends … kill`) n'est ni pour ni contre : sa mesure est le journal de Killmode. Un GO confirmé pris
    (`!prends … go`) est avec JARVIS : son GO est celui du scanner, confirmé après l'ouverture, même si la note
    recalculée à ton entrée a baissé."""
    return (bool(p.get("score")) and not p["score"]["go"] and not p.get("killmode")
            and p.get("policy") != "go_confirme")


def at_rule(t: dict) -> bool:
    """Trade pris à ta taille quand JARVIS proposait 0 % : ta règle de 20 € sous un grade à 0 % (pas de taille de
    JARVIS, `sizing_regle` à part) ou un risque demandé alors qu'il ne proposait rien (grade.hors_budget avec un
    risque proposé nul). C'est le pari « 20 € quand JARVIS propose 0 % », mesuré par `!desaccords` à part de la
    note : un GO pris sous un grade F est d'accord avec la note, pas avec la taille. Un risque demandé au-delà d'un
    risque proposé > 0 n'en est pas : JARVIS proposait de prendre le trade, seulement moins gros."""
    g = t.get("grade") or {}
    return bool((t.get("sizing") is None and t.get("sizing_regle"))
                or (g.get("hors_budget") and not (g.get("risque_propose") or 0) > 0))


def disagreement_text(p: dict) -> str:
    """Le désaccord dit franchement, avant le plan, quand Simão prend un trade noté NO GO."""
    reasons = p["score"]["reasons"] or ["note composite trop basse"]
    return (f"⛔ **Je ne suis pas d'accord** : ce plan est NO GO ({' ; '.join(reasons)}). "
            "Je le suis quand même, marqué « contre l'avis de JARVIS » ; `!desaccords` dira qui avait raison.")


EXIT_KINDS = ("stop", "close")


def suivi_politique() -> str:
    """Ce que mesurent `!desaccords` et les trades réels de `!grade` : ton suivi, pas le labo (entrée, break-even,
    sortie au temps, taille)."""
    from app.trading import killmode as K  # import tardif : killmode importe ce module

    return ("ton suivi `!prends` : entrée à ton prix, stop d'origine jusqu'au TP2 puis à l'entrée, aucune sortie au "
            f"temps, un tiers par objectif ; taille du trade (sinon {K.RISK_EUR:g} € de risque), frais IBKR estimés à "
            "cette taille (crypto : frais du labo)")


def realized_r(trade: dict) -> float | None:
    """Gain d'un trade terminé en R, avec un tiers de la position par objectif (sa gestion habituelle), avant frais.
    Chaque tiers sort au prix noté par le suivi (objectif, stop exécuté, clôture) ou corrigé avec `!sortie`."""
    if trade["status"] != "closed" or not trade.get("targets"):
        return None
    sign = 1 if trade["side"] == "long" else -1
    risk = abs(trade["entry"] - trade["stop"])
    if not risk:
        return None
    r = lambda price: sign * (price - trade["entry"]) / risk  # noqa: E731
    legs = len(trade["targets"])
    hit = min(trade["targets_hit"], legs)
    events = trade.get("events") or []
    prices = {e["kind"]: e["price"] for e in events}
    total = sum(r(prices.get(f"tp{i}", t["price"])) for i, t in enumerate(trade["targets"][:hit], start=1)) / legs
    if hit < legs:
        exits = [e for e in events if e["kind"] in EXIT_KINDS]
        total += (legs - hit) / legs * r(exits[-1]["price"] if exits else trade["last_price"])
    return round(total, 2)


def realized_r_net(trade: dict) -> float | None:
    """realized_r moins les frais estimés (app/trading/fees.py) : le chiffre de l'arrêt de la journée, de
    `!desaccords` et des trades réels de `!grade`."""
    from app.trading import fees as FEES  # import tardif : fees lit killmode, qui importe ce module

    r = realized_r(trade)
    if r is None:
        return None
    try:
        fees = FEES.fees_r(trade) or 0.0
    except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:  # trade mal formé : le brut, jamais d'arrêt
        log.warning("Frais du trade %s non estimés (%s: %s)", trade.get("id"), type(exc).__name__, exc)
        fees = 0.0
    return round(r - fees, 3)


def _settle(trade: dict) -> None:
    """Note le R net de frais estimés du trade qui vient de se terminer (ou dont la sortie est corrigée)."""
    trade["realized_r_net"] = realized_r_net(trade)


def record_exit(book: Book, symbol: str, price: float) -> dict | None:
    """`!sortie SYMBOLE prix` : ta sortie réelle. Ferme à ce prix le dernier trade ouvert de `symbol` ; sans trade
    ouvert, corrige le prix de la dernière sortie (stop, objectif ou clôture) du dernier trade terminé, marquée
    `corrige` (le prix du suivi reste dans `prix_suivi`). None si aucun trade de `symbol` n'est suivi."""
    symbol = symbol.upper()
    mine = [t for t in book.trades if t["symbol"] == symbol]
    opened = [t for t in mine if t["status"] == "open"]
    if opened:
        trade = book.close(opened[-1]["id"], price, "sortie déclarée sur Discord")
        return {"trade": trade, "corrige": False, "autres": len(opened) - 1}
    done = [t for t in mine if t["status"] == "closed" and t.get("events")
            and (t["events"][-1]["kind"] in EXIT_KINDS or t["events"][-1]["kind"].startswith("tp"))]
    if not done:
        return None
    trade = max(done, key=lambda t: t["events"][-1]["at"])
    ev = trade["events"][-1]
    before = {"price": ev["price"], "r": realized_r(trade), "r_net": realized_r_net(trade)}
    ev.setdefault("prix_suivi", ev["price"])
    ev.update(price=price, corrige=True)
    _settle(trade)
    book.save()
    return {"trade": trade, "corrige": True, "avant": before, "sortie": ev}


def exit_text(res: dict, now: datetime | None = None) -> str:
    """Réponse à `!sortie` : le trade fermé ou corrigé, son R net de frais estimés et brut."""
    now = now or datetime.now(timezone.utc)
    t = res["trade"]
    net, gross = realized_r_net(t), realized_r(t)
    result = (f"{net:+.2f} R nets de frais estimés ({gross:+.2f} R brut)" if net is not None
              else "résultat en R inconnu (trade sans objectifs)")
    if not res["corrige"]:
        text = f"🏁 **{t['symbol']}** clôturé à {_num(t['events'][-1]['price'])} (entrée {_num(t['entry'])}) : {result}."
        if res["autres"]:
            text += (f"\n-# {res['autres']} autre(s) trade(s) {t['symbol']} encore suivi(s) : `!sortie {t['symbol']} "
                     "prix` à nouveau pour le suivant.")
        return text
    ev, before = res["sortie"], res["avant"]
    kind = {"stop": "stop", "close": "clôture"}.get(ev["kind"], ev["kind"].upper())
    return (f"✏️ **{t['symbol']}** : sortie corrigée à {_num(ev['price'])} au lieu de {_num(before['price'])} "
            f"({kind} noté par le suivi {time_fr(datetime.fromisoformat(ev['at']), now)}, entrée {_num(t['entry'])}). "
            f"Résultat : {result}" + (f", contre {before['r_net']:+.2f} R nets avant correction" if before["r_net"]
                                      is not None else "") + ".")


def note_slopes() -> dict:
    """Pente de la note au labo par marché (R par point de note, cache des statistiques du worker), la même que
    sous les plans (score.lab_slopes) ; {} sans labo. Ne lève jamais d'exception."""
    try:
        return SC.lab_slopes()
    except Exception as exc:
        log.warning("Pente de la note du labo illisible (%s: %s)", type(exc).__name__, exc)
        return {}


def _exit_day(t: dict) -> str:
    """Jour de la sortie (UTC ; pendant la séance américaine, c'est aussi le jour de New York)."""
    return t["events"][-1]["at"][:10] if t.get("events") else t["created_at"][:10]


def disagreements(book: Book, slopes: dict | None = None) -> dict:
    """Trades pris avec et contre l'avis de JARVIS : nombre, réussite et gain moyen des trades terminés, en R nets
    de frais estimés (et bruts), avec erreur type et IC à 95 % en grappes par jour de sortie (stats.mean_ci).
    Deux désaccords séparés : la note (« avec » : GO, « contre » : NO GO pris) et la taille (« regle » : à ta règle
    ou à ton risque quand JARVIS proposait 0 %, at_rule ; un trade peut être dans « avec » et « regle »,
    `avec.regle` les compte). `slopes` : pente de la note au labo par marché (note_slopes), qui conditionne
    « JARVIS a raison »."""
    from app.lab import stats as LS  # import tardif : le labo importe ce module

    groups = {"avec": [], "contre": [], "regle": []}
    for t in book.trades:
        if t.get("killmode"):
            continue  # un KILL pris : mesuré par le journal de Killmode
        if t.get("score"):
            groups["contre" if against_jarvis(t) else "avec"].append(t)
        if at_rule(t):
            groups["regle"].append(t)
    out: dict = {}
    for name, trades in groups.items():
        done = [(t, r, realized_r_net(t)) for t, r in ((t, realized_r(t)) for t in trades) if r is not None]
        nets = [net for _, _, net in done]
        day_sums: dict = {}
        for t, _, net in done:
            d = day_sums.setdefault(_exit_day(t), [0.0, 0])
            d[0] += net
            d[1] += 1
        m, se, ci = LS.mean_ci(day_sums, sum(nets), len(nets))
        out[name] = {"total": len(trades), "closed": len(done), "open": sum(t["status"] == "open" for t in trades),
                     "wins": sum(x > 0 for x in nets), "avg_r": round(m, 4) if m is not None else None,
                     "sum_r": round(sum(nets), 2), "se": se, "ci": ci, "days": len(day_sums),
                     "avg_r_brut": round(sum(r for _, r, _ in done) / len(done), 4) if done else None,
                     "sum_r_brut": round(sum(r for _, r, _ in done), 2),
                     "marches": sorted({t.get("market", "stock") for t, _, _ in done})}
        if name in ("avec", "contre"):
            out[name]["regle"] = sum(at_rule(t) for t in trades)  # dont pris à ta taille, hors avis de JARVIS
    out["pente_note"] = dict(slopes or {})
    return out


def _ci_text(ci: list | None) -> str:
    return f"IC 95 % [{ci[0]:+.2f} ; {ci[1]:+.2f}]" if ci else "IC 95 % inconnu (un seul jour)"


def disagreements_text(st: dict) -> str:
    """Qui avait raison, en clair, sur deux questions séparées : la note sépare-t-elle les bons trades (écart
    NO GO pris − GO), et les NO GO pris rapportent-ils (NO GO trop sévère ?). Verdict seulement avec au moins
    30 trades sur 20 jours de chaque côté et un IC à 95 % qui exclut 0 ; « JARVIS a raison » seulement si la note
    est prédictive au labo (pente > 0 sur chaque marché des trades)."""
    from app.lab import stats as LS  # import tardif : le labo importe ce module

    market_fr = {"stock": "actions", "crypto": "crypto"}
    lines = ["**Désaccords : toi contre JARVIS** (R nets de frais estimés, bruts entre parenthèses ; sorties au prix "
             "suivi, ou au tien avec `!sortie`)",
             f"-# Politique mesurée : {suivi_politique()}."]
    groups = (("avec", "Plans GO (JARVIS d'accord sur la note)"), ("contre", "Plans NO GO pris quand même"),
              ("regle", "Taille à ta règle, hors avis de JARVIS (il proposait 0 %)"))
    for name, label in groups:
        g = st.get(name)
        if g is None:  # tableau d'avant le groupe « regle »
            continue
        rule = (f" ; dont {g['regle']} à ta règle de taille" if g.get("regle") else "")
        if not g["closed"]:
            lines.append(f"{label} : {g['total']} trade(s), aucun terminé{rule}.")
            continue
        lines.append(f"{label} : {g['closed']} terminé(s) sur {g['days']} jour(s), {g['wins']} gagnant(s), "
                     f"{g['avg_r']:+.2f} R en moyenne ({g['avg_r_brut']:+.2f} brut), {g['sum_r']:+.2f} R au total"
                     + (f" ({g['open']} en cours)" if g["open"] else "") + rule)
    a, c = st["avec"], st["contre"]
    slopes = st.get("pente_note") or {}
    markets = sorted(set(a.get("marches") or []) | set(c.get("marches") or []))
    flat = [m for m in markets if slopes.get(m) is not None and slopes[m] <= 0]
    unknown = [m for m in markets if slopes.get(m) is None]
    slope_ok = bool(markets) and not flat and not unknown
    for m in flat:  # pente du labo ≤ 0 : même mention que sous les plans
        value = f"{slopes[m]:+.3f}".replace(".", ",").replace("-", "−")
        lines.append(f"⚠️ Note non prédictive au labo (pente {value} R/pt, {market_fr.get(m, m)})")
    if unknown:
        lines.append(f"-# Pente de la note au labo inconnue ({', '.join(market_fr.get(m, m) for m in unknown)}) : "
                     "aucun verdict en faveur de JARVIS tant qu'elle n'est pas mesurée positive.")

    def enough(g: dict) -> bool:
        return g["closed"] >= LS.MIN_TRADES and g["days"] >= LS.DEGRADE_MIN_DAYS

    q1 = "**1. La note sépare-t-elle les bons trades ?** "
    if not a["closed"] or not c["closed"]:
        lines.append(q1 + "Pas de verdict : il faut des trades terminés des deux côtés.")
    else:
        delta = c["avg_r"] - a["avg_r"]
        se = math.sqrt(a["se"] ** 2 + c["se"] ** 2) if a["se"] is not None and c["se"] is not None else None
        q = LS.t_quantile(0.975, min(a["days"], c["days"]) - 1) if min(a["days"], c["days"]) > 1 else None
        ci = [delta - q * se, delta + q * se] if se is not None and q is not None else None  # t de Student, comme mean_ci
        sure = enough(a) and enough(c) and ci is not None and (ci[0] > 0 or ci[1] < 0)
        gap = f"écart {delta:+.2f} R, {_ci_text(ci)}"
        if sure and ci[0] > 0:
            lines.append(q1 + f"➡️ **tu as raison contre JARVIS** : les NO GO pris font mieux que les GO ({gap}). "
                         "La note classe à l'envers, elle est à revoir.")
        elif sure and slope_ok:
            lines.append(q1 + f"➡️ **JARVIS a raison** : les GO font mieux que les NO GO pris ({gap}).")
        elif sure:
            lines.append(q1 + f"Pas de verdict pour JARVIS : les GO font mieux que les NO GO pris ({gap}), mais la "
                         "note n'est pas prédictive au labo.")
        else:
            lines.append(q1 + f"Pas de verdict : {gap} ({a['closed']}/{c['closed']} trades, {a['days']}/{c['days']} "
                         "jours)" + ("" if enough(a) and enough(c) else
                                     f" ; il faut {LS.MIN_TRADES} trades sur {LS.DEGRADE_MIN_DAYS} jours de chaque côté")
                         + ".")
    q2 = "**2. NO GO trop sévère ?** "
    if not c["closed"]:
        lines.append(q2 + "Pas de réponse : aucun NO GO pris n'est terminé.")
    else:
        mean = f"{c['avg_r']:+.2f} R par trade, {_ci_text(c['ci'])}"
        sure = enough(c) and c["ci"] is not None
        if sure and c["ci"][0] > 0:
            lines.append(q2 + f"➡️ **Oui pour l'instant** : les NO GO que tu as pris rapportent ({mean}).")
        elif sure and c["ci"][1] < 0 and slope_ok:
            lines.append(q2 + f"➡️ **Non, JARVIS a raison de dire NO GO** : les NO GO pris perdent ({mean}).")
        elif sure and c["ci"][1] < 0:
            lines.append(q2 + f"Non : les NO GO pris perdent ({mean}), mais la note n'est pas prédictive au labo : "
                         "ce n'est pas un point pour JARVIS.")
        else:
            lines.append(q2 + f"Pas de réponse : {mean} ({c['closed']} trades, {c['days']} jours"
                         + ("" if enough(c) else f" ; il en faut {LS.MIN_TRADES} sur {LS.DEGRADE_MIN_DAYS}") + ").")
    r = st.get("regle")
    q3 = "**3. Ta taille quand JARVIS n'en propose pas ?** "
    if r is not None and not r["closed"]:
        lines.append(q3 + "Pas de réponse : aucun trade à ta règle terminé" + (f" ({r['total']} en cours)."
                                                                               if r["total"] else "."))
    elif r is not None:
        mean = f"{r['avg_r']:+.2f} R par trade, {_ci_text(r['ci'])}"
        sure = enough(r) and r["ci"] is not None
        if sure and r["ci"][0] > 0:
            lines.append(q3 + f"➡️ **Tu as raison pour l'instant** : tes trades pris à ta règle rapportent ({mean}).")
        elif sure and r["ci"][1] < 0:
            lines.append(q3 + f"➡️ **JARVIS avait raison de ne proposer aucun risque** : tes trades pris à ta règle "
                         f"perdent ({mean}).")
        else:
            lines.append(q3 + f"Pas de réponse : {mean} ({r['closed']} trades, {r['days']} jours"
                         + ("" if enough(r) else f" ; il en faut {LS.MIN_TRADES} sur {LS.DEGRADE_MIN_DAYS}") + ").")
    lines.append("-# Les frais touchent les deux groupes presque autant : ils baissent les moyennes mais changent peu "
                 "l'écart.")
    return "\n".join(lines)


async def live(book: Book) -> list[dict]:
    """Dernier prix et mini-graphe de chaque trade ouvert, pour le cockpit.

    Crypto : bougies d'1 min Kraken (gratuit, cache 15 s). Actions : uniquement les bougies que le
    suivi a déjà lues, pour ne dépenser aucun crédit Twelve Data de plus.
    """
    out = []
    for t in book.open_trades():
        if t["market"] == "crypto":
            bars = await _optional_candles(t["symbol"], "crypto", 1) or []
        else:
            cached = _candle_cache.get((t["symbol"], "stock", 1, ""))
            bars = cached[1] if cached else []
        bars = bars[-LIVE_BARS:]
        price = bars[-1]["c"] if bars else t["last_price"]
        out.append({"id": t["id"], "price": price, "chart": [[b["t"], b["c"]] for b in bars],
                    "as_of": datetime.fromtimestamp(bars[-1]["t"], timezone.utc).isoformat() if bars else None})
    return out


def cockpit_text(book: Book, now: datetime | None = None) -> str:
    """Tableau de bord des trades ouverts pour le message Discord épinglé."""
    now = now or datetime.now(timezone.utc)
    lines = [f"🎯 **Trading Cockpit JARVIS** · mis à jour {time_fr(now, now)}"]
    trades = book.open_trades()
    if not trades:
        lines.append("Aucun trade suivi en ce moment.")
    for t in trades:
        s = summary(t)
        arrow = "🟢" if t["side"] == "long" else "🔴"
        lines.append(f"\n{arrow} **{t['symbol']}** {_num(t['last_price'])} ({s['move_pct']:+.2f} %, {s['r_now']:+.2f} R) · "
                     f"entrée {_num(t['entry'])} · stop {_num(t['stop_current'])}")
        for i, tp in enumerate(t["targets"]):
            done = i < t["targets_hit"]
            when = next((e["at"] for e in t["events"] if e["kind"] == tp["name"].lower()), None)
            status = (f"✅ touché {time_fr(datetime.fromisoformat(when), now)}" if done and when else
                      f"⏱️ {tp['eta_text']}")
            lines.append(f"   {tp['name']} {_num(tp['price'])} ({tp['pct']:+.2f} %) · {status}")
        if t["exit_signals"]:
            lines.append(f"   ⚠️ signes de sortie : {', '.join(t['exit_signals'])}")
    return "\n".join(lines)
