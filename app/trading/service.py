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
from app.sources import alpaca, kraken, twelvedata
from app.trading import calendar as CAL
from app.trading import catalyst as C
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
#   action : bougies de 15 min sur ~190 séances, horizon 2 séances (26 h de cotation)
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
    et le levier découle de la taille (jamais l'inverse)."""
    if not capital:
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
                f"au plus tard {time_fr(eta_late, now)} dans 3 cas sur 4"
            )
        estimates.append(item)
    if capital is None:
        capital = env_float("JARVIS_CAPITAL", 0.0) or None
    plan = {
        "symbol": symbol, "market": market, "side": side,
        "entry": levels.entry, "stop": levels.stop, "stop_source": levels.stop_source,
        "stop_pct": round(-abs(levels.pct(levels.stop)), 2), "atr": P.round_price(atr_value),
        "targets": estimates,
        "stop_first_rate": timing["stop_first_rate"],
        "checklist": check,
        "sizing": sizing(levels.entry, levels.stop, capital, risk_pct or RISK_PCT),
        "method": (
            f"Délais mesurés en rejouant ce plan depuis chacune des {timing['starts']} bougies de "
            f"{step} min de l'historique ({cfg['label']}), horizon {cfg['horizon_bars'] * step // 60} h{clock_word}. "
            f"Heures de Paris."
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
    cat: dict | None = None,
) -> dict:
    """Plan complet : niveaux, délais rejoués, checklist, catalyseur et note composite.

    Lit les bougies (et le catalyseur pour une action) puis confie le calcul à plan_from_bars.
    `cat` : catalyseur déjà lu (le scanner l'a en main) pour ne pas rappeler Finnhub.
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
    apply_grade(plan, capital, risk_pct, now)
    return plan


def taken_trades() -> list[dict]:
    """Trades pris par Simão (remplacé par le scheduler au démarrage : son carnet)."""
    return []


def apply_grade(plan: dict, capital: float | None, risk_pct: float | None, now: datetime,
                trades: list[dict] | None = None) -> None:
    """Grade de JARVIS : risque proposé par trade (grade, risque déjà ouvert, arrêt de la journée).
    Un risque demandé explicitement est gardé tel quel. Jamais d'exception : plan inchangé en cas de souci."""
    from app.lab import grade as GRADE  # import tardif : le labo importe ce module

    try:
        g = GRADE.risk_now(plan["market"], taken_trades() if trades is None else trades, now)
    except Exception as exc:
        log.warning("Grade du labo non appliqué (%s: %s)", type(exc).__name__, exc)
        return
    plan["grade"] = g
    if risk_pct is None:
        cap = capital if capital is not None else (env_float("JARVIS_CAPITAL", 0.0) or None)
        plan["sizing"] = sizing(plan["entry"], plan["stop"], cap, g["risque_propose"]) if g["risque_propose"] > 0 else None

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
    for k, label in (("technique", "Technique"), ("catalyst", "Catalyseur"), ("risk", "Risque")):
        lines.append(f"-# {label} : {' ; '.join(parts[k]['reasons'])}")
    if sc.get("lecons"):
        les = sc["lecons"]
        lines.append(f"-# Leçons du labo ({les['ajustement']:+g} sur la note) : {' ; '.join(les['lignes'])}")
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
        lines.append(
            f"🎯 {t['name']} {_num(t['price'])} ({t['pct']:+.2f} %, {t['r_multiple']:g} R, {t['source']}) · "
            f"{t['eta_text']} · touché avant le stop {t['hit_rate']:.0%}, espérance {t['expectancy_r']:+.2f} R"
        )
    lines += confirmation_lines(p)
    marks = {True: "✅", False: "❌", None: "➖"}
    lines.append(" · ".join(f"{marks[i['ok']]} {i['label']}" for i in check["items"]))
    if p.get("sizing"):
        s = p["sizing"]
        lines.append(f"💰 Risque {s['risk_pct']:g} % = {s['risk_amount']:g} · taille {s['quantity']:g} "
                     f"({_num(s['notional'])}) · levier {s['leverage']:g}×")
    if p.get("grade"):
        from app.lab import grade as GRADE

        lines.append(GRADE.plan_line(p["grade"]))
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
    book.trades.remove(old)
    book.add(plan, f"rouvert : stop {_num(old['stop_current'])} non exécuté chez le courtier")
    return old


def _event(trade: dict, kind: str, price: float, at: int) -> None:
    trade["events"].append({"kind": kind, "price": price,
                            "at": datetime.fromtimestamp(at, timezone.utc).isoformat(timespec="seconds")})


def apply_bars(trade: dict, bars: list[dict], now: datetime) -> list[str]:
    """Rejoue les bougies d'1 min arrivées depuis le dernier passage. Renvoie les messages à envoyer.

    Le stop ne remonte pas au prix d'entrée dès le TP1 (EmperorBTC : « ne soyez pas pressé de
    passer votre SL au seuil de rentabilité ») : il y monte après le TP2.
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
            _event(trade, "stop", trade["stop_current"], bar["t"])
            if trade["targets_hit"]:
                messages.append(f"🛡️ **{trade['symbol']}** : stop suivi touché à {_num(trade['stop_current'])} après "
                                f"TP{trade['targets_hit']}. Trade terminé.")
            else:
                messages.append(f"🛑 **{trade['symbol']}** : stop touché à {_num(trade['stop_current'])} "
                                f"({trade['stop_pct']:+.2f} %). Point d'invalidation atteint, trade terminé.")
            break
        while trade["targets_hit"] < len(trade["targets"]):
            target = trade["targets"][trade["targets_hit"]]
            if not (bar["h"] >= target["price"] if sign == 1 else bar["l"] <= target["price"]):
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
                text += " Dernier objectif atteint, trade terminé. 🏁"
            else:
                nxt = trade["targets"][n]
                if n >= 2:
                    trade["stop_current"] = trade["entry"]
                    trade["stop_moved_at"] = bar["t"]
                    stop_text = f"Stop remonté à l'entrée ({_num(trade['entry'])})."
                else:
                    stop_text = f"Stop inchangé à {_num(trade['stop_current'])} (pas de break-even précipité)."
                text += f"\nProchain : {nxt['name']} {_num(nxt['price'])}, {nxt['eta_text']}. {stop_text} Garde le reste."
            messages.append(text)
    # Objectif en retard sur le délai à 75 % : l'élan faiblit peut-être.
    if trade["status"] == "open" and trade["targets_hit"] < len(trade["targets"]):
        nxt = trade["targets"][trade["targets_hit"]]
        if nxt["eta_latest"] and nxt["name"] not in trade["late_warned"] and now >= datetime.fromisoformat(nxt["eta_latest"]):
            trade["late_warned"].append(nxt["name"])
            messages.append(f"⏳ **{trade['symbol']}** : {nxt['name']} ({_num(nxt['price'])}) pas encore touché alors que "
                            f"3 cas passés sur 4 l'avaient atteint. Dernier prix {_num(trade['last_price'])}.")
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
                    "against_jarvis": against_jarvis(trade)}


# --- Désaccords : Simão contre JARVIS, mesuré ----------------------------------------------
# JARVIS ne dit pas oui parce que c'est Simão : un trade pris sur un plan NO GO est marqué
# « contre l'avis de JARVIS » et !desaccords compare ces trades aux autres pour voir qui avait raison.

def against_jarvis(p: dict) -> bool:
    """Vrai si le plan était noté NO GO (un plan sans note n'est ni pour ni contre)."""
    return bool(p.get("score")) and not p["score"]["go"]


def disagreement_text(p: dict) -> str:
    """Le désaccord dit franchement, avant le plan, quand Simão prend un trade noté NO GO."""
    reasons = p["score"]["reasons"] or ["note composite trop basse"]
    return (f"⛔ **Je ne suis pas d'accord** : ce plan est NO GO ({' ; '.join(reasons)}). "
            "Je le suis quand même, marqué « contre l'avis de JARVIS » ; `!desaccords` dira qui avait raison.")


def realized_r(trade: dict) -> float | None:
    """Gain d'un trade terminé en R, avec un tiers de la position par objectif (sa gestion habituelle)."""
    if trade["status"] != "closed" or not trade.get("targets"):
        return None
    sign = 1 if trade["side"] == "long" else -1
    risk = abs(trade["entry"] - trade["stop"])
    if not risk:
        return None
    r = lambda price: sign * (price - trade["entry"]) / risk  # noqa: E731
    legs = len(trade["targets"])
    hit = min(trade["targets_hit"], legs)
    total = sum(r(t["price"]) for t in trade["targets"][:hit]) / legs
    if hit < legs:
        exits = [e for e in trade["events"] if e["kind"] in ("stop", "close")]
        total += (legs - hit) / legs * r(exits[-1]["price"] if exits else trade["last_price"])
    return round(total, 2)


def disagreements(book: Book) -> dict:
    """Trades pris avec et contre l'avis de JARVIS : nombre, réussite et gain moyen des trades terminés."""
    groups = {"avec": [], "contre": []}
    for t in book.trades:
        if t.get("score"):
            groups["contre" if against_jarvis(t) else "avec"].append(t)
    out = {}
    for name, trades in groups.items():
        rs = [x for x in (realized_r(t) for t in trades) if x is not None]
        out[name] = {"total": len(trades), "closed": len(rs), "open": sum(t["status"] == "open" for t in trades),
                     "wins": sum(x > 0 for x in rs), "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
                     "sum_r": round(sum(rs), 2)}
    return out


def disagreements_text(st: dict) -> str:
    """Qui avait raison, en clair. Pas de conclusion tant que l'échantillon est trop petit."""
    lines = ["**Désaccords : toi contre JARVIS** (trades suivis, un tiers par objectif)"]
    for name, label in (("avec", "Plans GO (JARVIS d'accord)"), ("contre", "Plans NO GO pris quand même")):
        g = st[name]
        if not g["closed"]:
            lines.append(f"{label} : {g['total']} trade(s), aucun terminé.")
            continue
        lines.append(f"{label} : {g['closed']} terminé(s), {g['wins']} gagnant(s), "
                     f"{g['avg_r']:+.2f} R en moyenne, {g['sum_r']:+.2f} R au total"
                     + (f" ({g['open']} en cours)" if g["open"] else ""))
    a, c = st["avec"], st["contre"]
    if a["closed"] < 10 or c["closed"] < 10:
        lines.append("-# Pas encore de verdict : il faut au moins 10 trades terminés de chaque côté.")
    elif c["avg_r"] > a["avg_r"]:
        lines.append("➡️ Pour l'instant **tu as raison contre JARVIS** : sa note NO GO est trop sévère, "
                     "elle est à revoir.")
    else:
        lines.append("➡️ Pour l'instant **JARVIS a raison** : les plans NO GO pris quand même rapportent moins.")
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
