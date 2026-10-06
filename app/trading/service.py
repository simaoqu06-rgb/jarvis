"""Plans de trade et suivi des positions : bougies, délais estimés, TP/SL touchés.

JARVIS propose les plans, Simão passe les ordres lui-même : aucun ordre n'est envoyé à un courtier.
Les trades suivis sont gardés dans data/trades.json (JARVIS_TRADES_PATH).
"""
import json
import logging
import os
import time as clock
import uuid
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app import needs
from app.core.http import SourceError
from app.sources import kraken, twelvedata
from app.trading import plan as P

log = logging.getLogger(__name__)
NEW_YORK = ZoneInfo("America/New_York")
PARIS = ZoneInfo("Europe/Paris")
OPEN, CLOSE, EARLY_CLOSE = time(9, 30), time(16, 0), time(13, 0)
TRADES_PATH = os.getenv("JARVIS_TRADES_PATH", "data/trades.json")

# Bougies utilisées pour le plan et l'estimation des délais, par marché.
#   crypto : bougies d'1 h sur 30 jours (limite Kraken : 720), horizon 24 h
#   action : bougies de 15 min sur ~190 séances, horizon 2 séances (26 h de cotation)
SETTINGS = {
    "crypto": {"interval": 60, "horizon_bars": 24, "label": "Kraken"},
    "stock": {"interval": 15, "horizon_bars": 52, "label": "Twelve Data"},
}
CANDLE_TTL = 120
_candle_cache: dict[tuple, tuple[float, list[dict]]] = {}


class PlanError(Exception):
    """Plan impossible (données absentes, niveaux incohérents)."""


def _market(market: str) -> str:
    market = market.lower()
    if market not in SETTINGS:
        raise PlanError("market doit valoir crypto ou stock")
    return market


async def candles(symbol: str, market: str, interval: int) -> list[dict]:
    key = (symbol, market, interval)
    cached = _candle_cache.get(key)
    ttl = 50 if interval == 1 else CANDLE_TTL
    if cached and clock.monotonic() - cached[0] < ttl:
        return cached[1]
    try:
        if market == "crypto":
            rows = await kraken.candles(symbol, interval)
            rows = rows[:-1] if interval > 1 else rows  # la dernière bougie horaire n'est pas close
        else:
            rows = await twelvedata.candles(symbol, interval, outputsize=5000 if interval > 1 else 60)
    except SourceError as exc:
        raise PlanError(f"bougies {symbol} indisponibles : {exc}") from exc
    _candle_cache[key] = (clock.monotonic(), rows)
    return rows


# --- Heures de cotation ---------------------------------------------------------------

async def _nyse_calendar(year: int) -> tuple[set[str], set[str]]:
    result = await needs.us_market_holidays(year)
    closed = {h["date"] for h in (result.data or []) if h.get("closed")}
    early = {h["date"] for h in (result.data or []) if h.get("early_close")}
    return closed, early


def add_market_minutes(start: datetime, minutes: float, closed: set[str], early: set[str]) -> datetime:
    """Avance de `minutes` minutes de cotation NYSE (9 h 30 - 16 h, heure de New York)."""
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


def _duration_fr(minutes: float) -> str:
    minutes = int(round(minutes))
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest:02d}" if rest else f"{hours} h"


def _time_fr(moment: datetime, now: datetime) -> str:
    local = moment.astimezone(PARIS)
    today = now.astimezone(PARIS).date()
    if local.date() == today:
        prefix = "aujourd'hui"
    elif local.date() == today + timedelta(days=1):
        prefix = "demain"
    else:
        prefix = local.strftime("%d/%m")
    return f"{prefix} {local.strftime('%H:%M')}"


async def _eta(market: str, start: datetime, minutes: float) -> datetime:
    if market == "crypto":
        return start + timedelta(minutes=minutes)
    local = start.astimezone(NEW_YORK)
    closed, early = await _nyse_calendar(local.year)
    if local.month == 12:  # l'estimation peut déborder sur janvier
        next_closed, next_early = await _nyse_calendar(local.year + 1)
        closed, early = closed | next_closed, early | next_early
    return add_market_minutes(start, minutes, closed, early)


# --- Plan --------------------------------------------------------------------------------

async def build_plan(
    symbol: str, market: str, side: str = "long", entry: float | None = None,
    stop: float | None = None, targets: list[float] | None = None, now: datetime | None = None,
) -> dict:
    symbol, market, side = symbol.strip().upper(), _market(market), side.lower()
    now = now or datetime.now(timezone.utc)
    cfg = SETTINGS[market]
    bars = await candles(symbol, market, cfg["interval"])
    try:
        levels = P.default_levels(bars, side, entry, stop, targets)
        timing = P.historical_timing(bars, levels, cfg["horizon_bars"])
    except ValueError as exc:
        raise PlanError(str(exc)) from exc
    ctx = P.context(bars)
    step = cfg["interval"]
    estimates = []
    for i, (price, t) in enumerate(zip(levels.targets, timing["targets"]), start=1):
        item = {
            "name": f"TP{i}", "price": price, "pct": round(levels.pct(price), 2),
            "r_multiple": round(abs(price - levels.entry) / levels.risk, 2),
            "hit_rate": t["hit_rate"], "samples": t["samples"],
            "minutes_median": None, "minutes_p25": None, "minutes_p75": None,
            "eta_median": None, "eta_latest": None, "eta_text": "jamais atteint dans l'historique",
        }
        if t["bars_median"] is not None:
            item["minutes_median"] = t["bars_median"] * step
            item["minutes_p25"] = t["bars_p25"] * step
            item["minutes_p75"] = t["bars_p75"] * step
            eta_med = await _eta(market, now, item["minutes_median"])
            eta_late = await _eta(market, now, item["minutes_p75"])
            item["eta_median"] = eta_med.isoformat(timespec="minutes")
            item["eta_latest"] = eta_late.isoformat(timespec="minutes")
            clock_word = "" if market == "crypto" else " de cotation"
            item["eta_text"] = (
                f"~{_duration_fr(item['minutes_median'])}{clock_word} (entre {_duration_fr(item['minutes_p25'])} "
                f"et {_duration_fr(item['minutes_p75'])}), vers {_time_fr(eta_med, now)} (Paris), "
                f"au plus tard {_time_fr(eta_late, now)} dans 3 cas sur 4"
            )
        estimates.append(item)
    return {
        "symbol": symbol, "market": market, "side": side,
        "entry": levels.entry, "stop": levels.stop, "stop_pct": round(-abs(levels.pct(levels.stop)), 2),
        "targets": estimates,
        "stop_first_rate": timing["stop_first_rate"],
        "context": ctx,
        "warnings": P.warnings(levels, timing, ctx),
        "method": (
            f"Délais mesurés en rejouant ce plan depuis chacune des {timing['starts']} bougies de "
            f"{step} min de l'historique ({cfg['label']}), horizon {cfg['horizon_bars'] * step // 60} h"
            f"{'' if market == 'crypto' else ' de cotation'}. Statistique passée, pas une promesse."
        ),
        "created_at": now.isoformat(timespec="seconds"),
    }


def plan_text(p: dict) -> str:
    """Le plan en clair pour Discord."""
    arrow = "🟢 LONG" if p["side"] == "long" else "🔴 SHORT"
    lines = [
        f"**{arrow} {p['symbol']}** · entrée {p['entry']:g} · stop {p['stop']:g} ({p['stop_pct']:+.2f} %)",
        f"Tendance {p['context']['trend']}, RSI {p['context']['rsi14']}",
    ]
    for t in p["targets"]:
        lines.append(
            f"🎯 {t['name']} {t['price']:g} ({t['pct']:+.2f} %, {t['r_multiple']:g} R) · "
            f"touché avant le stop {t['hit_rate']:.0%} des fois · {t['eta_text']}"
        )
    for w in p["warnings"]:
        lines.append(f"⚠️ {w}")
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
            "late_warned": [],
        }
        self.trades.append(trade)
        self.save()
        return trade

    def close(self, trade_id: str, price: float | None, reason: str = "clôturé à la main") -> dict | None:
        trade = self.get(trade_id)
        if trade and trade["status"] == "open":
            trade["status"] = "closed"
            trade["events"].append({"kind": "close", "price": price or trade["last_price"], "reason": reason,
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


def _event(trade: dict, kind: str, price: float, at: int) -> None:
    trade["events"].append({"kind": kind, "price": price,
                            "at": datetime.fromtimestamp(at, timezone.utc).isoformat(timespec="seconds")})


def apply_bars(trade: dict, bars: list[dict], now: datetime) -> list[str]:
    """Rejoue les bougies d'1 min arrivées depuis le dernier passage. Renvoie les messages à envoyer.

    Après le TP1, le stop suivi remonte au prix d'entrée ; après le TP2, au TP1.
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
        if stop_hit:
            trade["status"] = "closed"
            _event(trade, "stop", trade["stop_current"], bar["t"])
            if trade["targets_hit"]:
                messages.append(f"🛡️ **{trade['symbol']}** : stop suivi touché à {trade['stop_current']:g} après "
                                f"TP{trade['targets_hit']}, gain sécurisé. Trade terminé.")
            else:
                messages.append(f"🛑 **{trade['symbol']}** : stop touché à {trade['stop_current']:g} "
                                f"({trade['stop_pct']:+.2f} %). Trade terminé, on ne le rachète pas sur le coup.")
            break
        while trade["targets_hit"] < len(trade["targets"]):
            target = trade["targets"][trade["targets_hit"]]
            if not (bar["h"] >= target["price"] if sign == 1 else bar["l"] <= target["price"]):
                break
            trade["targets_hit"] += 1
            n = trade["targets_hit"]
            _event(trade, target["name"].lower(), target["price"], bar["t"])
            elapsed = (bar["t"] - created.timestamp()) / 60
            text = (f"🎯 **{trade['symbol']} {target['name']} touché** à {target['price']:g} "
                    f"({target['pct']:+.2f} %) après {_duration_fr(max(elapsed, 1))}")
            if target["minutes_median"]:
                text += f" (prévu ~{_duration_fr(target['minutes_median'])})"
            text += "."
            if n == len(trade["targets"]):
                trade["status"] = "closed"
                text += " Dernier objectif atteint, trade terminé. 🏁"
            else:
                nxt = trade["targets"][n]
                trade["stop_current"] = trade["entry"] if n == 1 else trade["targets"][n - 2]["price"]
                text += (f"\nProchain : {nxt['name']} {nxt['price']:g}, {nxt['eta_text']}. "
                         f"Stop remonté à {trade['stop_current']:g} : garde le reste.")
            messages.append(text)
    # Objectif en retard sur le délai à 75 % : l'élan faiblit peut-être.
    if trade["status"] == "open" and trade["targets_hit"] < len(trade["targets"]):
        nxt = trade["targets"][trade["targets_hit"]]
        if nxt["eta_latest"] and nxt["name"] not in trade["late_warned"] and now >= datetime.fromisoformat(nxt["eta_latest"]):
            trade["late_warned"].append(nxt["name"])
            messages.append(f"⏳ **{trade['symbol']}** : {nxt['name']} ({nxt['price']:g}) pas encore touché alors que "
                            f"75 % des cas passés l'avaient atteint. Dernier prix {trade['last_price']:g} : "
                            f"envisage de sécuriser si l'élan faiblit.")
    return messages


async def check_trades(book: Book, now: datetime | None = None) -> list[str]:
    """Vérifie les trades ouverts sur les bougies d'1 min depuis le dernier passage."""
    now = now or datetime.now(timezone.utc)
    messages = []
    for trade in book.open_trades():
        try:
            bars = await candles(trade["symbol"], trade["market"], 1)
        except PlanError as exc:
            log.warning("Suivi %s impossible : %s", trade["symbol"], exc)
            continue
        messages += apply_bars(trade, bars, now)
    book.save()
    return messages


def summary(trade: dict) -> dict:
    """Ce que le cockpit affiche pour un trade."""
    sign = 1 if trade["side"] == "long" else -1
    move = sign * (trade["last_price"] - trade["entry"]) / trade["entry"] * 100
    return trade | {"move_pct": round(move, 2)}
