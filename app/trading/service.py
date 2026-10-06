"""Plans de trade et suivi des positions : bougies, délais estimés, TP/SL touchés, signaux de sortie.

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
from app.config import env_float
from app.core.http import SourceError
from app.sources import kraken, twelvedata
from app.trading import plan as P
from app.trading import signals as S

log = logging.getLogger(__name__)
NEW_YORK = ZoneInfo("America/New_York")
PARIS = ZoneInfo("Europe/Paris")
OPEN, CLOSE, EARLY_CLOSE = time(9, 30), time(16, 0), time(13, 0)
TRADES_PATH = os.getenv("JARVIS_TRADES_PATH", "data/trades.json")
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
TTL = {1: 50, 15: 120, 60: 120, 240: 1800, 1440: 1800}
_candle_cache: dict[tuple, tuple[float, list[dict]]] = {}


class PlanError(Exception):
    """Plan impossible (données absentes, niveaux incohérents)."""


def _market(market: str) -> str:
    market = market.lower()
    if market not in SETTINGS:
        raise PlanError("market doit valoir crypto ou stock")
    return market


async def candles(symbol: str, market: str, interval: int) -> list[dict]:
    """Bougies de la plus ancienne à la plus récente ; la dernière peut être en cours."""
    key = (symbol, market, interval)
    cached = _candle_cache.get(key)
    if cached and clock.monotonic() - cached[0] < TTL.get(interval, 120):
        return cached[1]
    try:
        if market == "crypto":
            rows = await kraken.candles(symbol, interval)
        else:
            rows = await twelvedata.candles(symbol, interval, outputsize=STOCK_OUTPUTSIZE.get(interval, 500))
    except SourceError as exc:
        raise PlanError(f"bougies {symbol} indisponibles : {exc}") from exc
    _candle_cache[key] = (clock.monotonic(), rows)
    return rows


async def _optional_candles(symbol: str, market: str, interval: int) -> list[dict] | None:
    try:
        return await candles(symbol, market, interval)
    except PlanError as exc:
        log.warning("%s", exc)
        return None


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


async def build_plan(
    symbol: str, market: str, side: str = "long", entry: float | None = None,
    stop: float | None = None, targets: list[float] | None = None, now: datetime | None = None,
    capital: float | None = None, risk_pct: float | None = None,
) -> dict:
    symbol, market, side = symbol.strip().upper(), _market(market), side.lower()
    now = now or datetime.now(timezone.utc)
    cfg = SETTINGS[market]
    bars = await candles(symbol, market, cfg["interval"])
    try:
        levels = P.default_levels(bars, side, entry, stop, targets, market=market)
        timing = P.historical_timing(bars, levels, cfg["horizon_bars"])
        atr_value = P.atr(bars)
    except ValueError as exc:
        raise PlanError(str(exc)) from exc
    daily = await _optional_candles(symbol, market, 1440)
    h4 = await _optional_candles(symbol, market, 240)
    # Signaux sur les bougies closes : la dernière, en cours, fausserait le volume et les figures.
    check = S.checklist(bars[:-1], daily, h4, market, side, atr_value)
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
            eta_med = await _eta(market, now, item["minutes_median"])
            eta_late = await _eta(market, now, item["minutes_p75"])
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
    return {
        "symbol": symbol, "market": market, "side": side,
        "entry": levels.entry, "stop": levels.stop, "stop_source": levels.stop_source,
        "stop_pct": round(-abs(levels.pct(levels.stop)), 2), "atr": round(atr_value, 6),
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
    }


def _num(x: float) -> str:
    return f"{x:,.6g}".replace(",", " ")


def plan_text(p: dict) -> str:
    """Le plan en clair pour Discord."""
    arrow = "🟢 LONG" if p["side"] == "long" else "🔴 SHORT"
    check = p["checklist"]
    lines = [
        f"**{arrow} {p['symbol']}** · entrée {_num(p['entry'])} · stop {_num(p['stop'])} "
        f"({p['stop_pct']:+.2f} %, {p['stop_source']})",
        f"**{check['verdict']}** · checklist EmperorBTC {check['score']}/{check['max_score']}",
    ]
    for t in p["targets"]:
        lines.append(
            f"🎯 {t['name']} {_num(t['price'])} ({t['pct']:+.2f} %, {t['r_multiple']:g} R, {t['source']}) · "
            f"{t['eta_text']} · touché avant le stop {t['hit_rate']:.0%}, espérance {t['expectancy_r']:+.2f} R"
        )
    marks = {True: "✅", False: "❌", None: "➖"}
    lines.append(" · ".join(f"{marks[i['ok']]} {i['label']}" for i in check["items"]))
    if p.get("sizing"):
        s = p["sizing"]
        lines.append(f"💰 Risque {s['risk_pct']:g} % = {s['risk_amount']:g} · taille {s['quantity']:g} "
                     f"({_num(s['notional'])}) · levier {s['leverage']:g}×")
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


async def check_trades(book: Book, now: datetime | None = None) -> list[str]:
    """Vérifie les trades ouverts : TP/SL sur les bougies d'1 min, signaux de sortie sur les bougies du plan."""
    now = now or datetime.now(timezone.utc)
    messages = []
    for trade in book.open_trades():
        try:
            bars = await candles(trade["symbol"], trade["market"], 1)
        except PlanError as exc:
            log.warning("Suivi %s impossible : %s", trade["symbol"], exc)
            continue
        messages += apply_bars(trade, bars, now)
        plan_bars = await _optional_candles(trade["symbol"], trade["market"], SETTINGS[trade["market"]]["interval"])
        if plan_bars:
            messages += apply_exit_signals(trade, plan_bars[:-1])
    book.save()
    return messages


def summary(trade: dict) -> dict:
    """Ce que le cockpit affiche pour un trade."""
    sign = 1 if trade["side"] == "long" else -1
    move = sign * (trade["last_price"] - trade["entry"]) / trade["entry"] * 100
    risk = abs(trade["entry"] - trade["stop"])
    r_now = sign * (trade["last_price"] - trade["entry"]) / risk if risk else 0
    return trade | {"move_pct": round(move, 2), "r_now": round(r_now, 2)}


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
