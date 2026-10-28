"""API du nouveau Trading Cockpit : /api/cockpit/* (lecture ; PR 1 de la refonte, docs/CARTE-INTERFACE.md).

Tout est lu dans l'état que le processus web tient déjà (scheduler.book, scheduler.reco_book, scheduler.paper_book,
propositions, caches de bougies) ; le labo en lecture seule (lab.db.open_readonly). **Aucune route ne déclenche un
appel Twelve Data** : les actions sont servies par le cache des bougies que le suivi a déjà lues, sinon par Alpaca
(gratuit) ; la crypto par Kraken (gratuit, cache de 15 s). Aucune route ne passe d'ordre ni ne renvoie de secret.

Deux espaces : `trades` (« Mes trades » : trades IBKR déclarés à JARVIS, data/trades.json) et `paper` (« Papier » :
les recommandations envoyées sur Discord, suivies telles qu'envoyées, data/reco.json). Toutes les dates en ISO-8601
UTC ; prix en USD, risque et résultats de règle en EUR, résultats en R.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time as clock
from collections import deque
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from app import config, scheduler
from app.core import sources_health
from app.lab import db as lab_db
from app.lab import grade as lab_grade
from app.lab import heartbeat
from app.sources import alpaca, twelvedata
from app.trading import calendar as CAL
from app.trading import confirm as CONFIRM
from app.trading import paper
from app.trading import proposals as PR
from app.trading import reco as RECO
from app.trading import service as T
from app.trading.settings import settings

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/cockpit", include_in_schema=False)

NEW_YORK, PARIS = CAL.NEW_YORK, T.PARIS
SPACES = ("trades", "paper")
SYMBOL_RE = re.compile(r"^[A-Z0-9.\-]{1,12}$")
TIMEFRAMES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "1j": 1440}
STALE_S = {"crypto": 60, "stock": 240}       # péremption d'une cotation (action : en séance seulement)
LOOP_STALE_S = 180                           # planificateur sans passage
LAB_STALE_S = 1800                           # battement du labo (healthcheck de jarvis-lab)
TRACKING_LOOP = "suivi des trades et cockpit Discord"
STOP_SOURCE = {"trades": "IBKR, posé par toi · suivi par JARVIS", "paper": "message Discord · suivi par JARVIS"}
PREMARKET, AFTER_HOURS_END, EARLY_AFTER_END = time(4, 0), time(20, 0), time(17, 0)
MAIN_BARS, MINI_BARS = 130, 70


# --- Erreurs récentes (santé) : messages WARNING et plus, sans trace ni secret ------------------------------

SCRUB = re.compile(r"(?i)(api[_-]?key|apikey|token|secret|password|key)=([^&\s]+)")


class _RecentErrors(logging.Handler):
    def __init__(self, size: int = 50):
        super().__init__(logging.WARNING)
        self.items: deque = deque(maxlen=size)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = SCRUB.sub(r"\1=***", record.getMessage())[:300]
        except Exception:  # un message mal formé ne casse jamais la journalisation
            return
        self.items.append({"at": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="seconds"),
                           "niveau": record.levelname, "module": record.name, "message": msg})


recent_errors = _RecentErrors()
if not any(isinstance(h, _RecentErrors) for h in logging.getLogger().handlers):
    logging.getLogger().addHandler(recent_errors)


# --- Petits outils ---------------------------------------------------------------------------------------

def _space(space: str) -> str:
    if space not in SPACES:
        raise HTTPException(400, "space doit valoir trades ou paper")
    return space


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts is not None else None


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _symbol(symbol: str) -> str:
    s = symbol.strip().upper()
    if not SYMBOL_RE.match(s):
        raise HTTPException(400, "Symbole invalide (lettres, chiffres, point ou tiret, 12 au plus)")
    return s


def _tf(tf: str) -> int:
    if tf not in TIMEFRAMES:
        raise HTTPException(400, "tf doit valoir 1m, 5m, 15m, 1h ou 1j")
    return TIMEFRAMES[tf]


def _capital() -> float | None:
    return T.env_float("JARVIS_CAPITAL", 0.0) or None


def _eurusd(t: dict) -> float:
    from app.trading import killmode as K  # import tardif : killmode tire le labo

    s = t.get("sizing") or t.get("sizing_regle") or {}
    return s.get("eurusd") or K.EURUSD_DEFAULT


def _cached_bars(symbol: str, market: str, interval: int) -> tuple[list[dict], float | None]:
    """Bougies déjà en cache (aucun appel) et l'âge du cache en secondes."""
    hit = T._candle_cache.get((symbol, market, interval, ""))
    if not hit:
        return [], None
    return hit[1], clock.monotonic() - hit[0]


def freshness(market: str, as_of: float | None, now: datetime) -> dict:
    """Âge d'une cotation et péremption : crypto > 60 s ; action > 4 min en séance ; hors séance, « clôture »."""
    age = None if as_of is None else max(0, round(now.timestamp() - as_of))
    if market == "stock" and not CAL.session_open(now):
        return {"age_s": age, "stale": False, "etat": "cloture"}
    stale = age is None or age > STALE_S.get(market, 240)
    return {"age_s": age, "stale": stale, "etat": "perime" if stale else "frais"}


def last_quote(symbol: str, market: str, fallback_price: float | None = None,
               fallback_ts: float | None = None) -> dict:
    """Dernier prix connu SANS appel réseau : bougie d'1 min en cache (Kraken ou Twelve Data, lue par le suivi), sinon
    le dernier prix enregistré du trade. `as_of` = fin de la dernière bougie (début + 1 min)."""
    bars, _ = _cached_bars(symbol, market, 1)
    if bars:
        b = bars[-1]
        return {"price": b["c"], "as_of_ts": b["t"] + 60, "source": "Kraken" if market == "crypto" else "Twelve Data"}
    bars5, _ = _cached_bars(symbol, market, 5)
    if bars5:
        b = bars5[-1]
        return {"price": b["c"], "as_of_ts": b["t"] + 300, "source": "bougies 5 min du suivi"}
    return {"price": fallback_price, "as_of_ts": fallback_ts, "source": "dernier passage du suivi"}


# --- Bloc position (§9.5) ---------------------------------------------------------------------------

def _verdict(t: dict) -> str | None:
    if t.get("killmode"):
        return "KILL"
    if t.get("policy") == "go_confirme":
        return "GO"
    sc = t.get("score")
    if not sc:
        return None
    return "GO" if sc.get("go") else "NO GO"


def _targets(t: dict, now: datetime) -> list[dict]:
    single = t.get("sortie_unique")
    hits = {e["kind"]: e["at"] for e in t.get("events") or []}
    out = []
    for i, tp in enumerate(t.get("targets") or []):
        hit_at = hits.get(tp["name"].lower()) if i < t.get("targets_hit", 0) else None
        late_at = _parse(tp.get("eta_latest"))
        entry = t["entry"]
        out.append({"name": tp["name"], "price": tp["price"],
                    "pct": round((tp["price"] - entry) / entry * 100, 2) if entry else None,
                    "r_multiple": tp.get("r_multiple"),
                    "role": "exit" if single is None or i == single else "repere",
                    "hit_at": hit_at, "eta_text": tp.get("eta_text"), "eta_median": tp.get("eta_median"),
                    "eta_latest": tp.get("eta_latest"),
                    "late": bool(not hit_at and late_at and now >= late_at)})
    return out


def _ibkr_orders(t: dict, space: str, qty: int | None) -> list[dict]:
    """Lignes bracket en prix : celles du plan enregistré (Mes trades) ou du message (Papier)."""
    if space == "trades":
        b = t.get("ibkr") or {}
        return [{"quantity": r["quantity"], "entry": b.get("entry"), "stop": b.get("stop"), "name": r["name"],
                 "take_profit": r["take_profit"]} for r in b.get("brackets") or []]
    if not qty:
        return []
    from app.trading import killmode as K  # import tardif

    names = [tp["name"] for tp in t["targets"]]
    single = t.get("sortie_unique")
    if single is not None:
        return [{"quantity": qty, "entry": t.get("fill_ref", t["entry"]), "stop": t["stop"], "name": names[single],
                 "take_profit": t["targets"][single]["price"]}]
    parts = [{"prix": tp["price"], "part": 1 / len(names), "raison": i} for i, tp in enumerate(t["targets"])]
    return [{"quantity": n, "entry": t.get("fill_ref", t["entry"]), "stop": t["stop"], "name": names[e["raison"]],
             "take_profit": e["prix"]} for n, e in K._legs(qty, len(names), parts)]


def position_block(t: dict, space: str, now: datetime, qty: int | None = None, quote: dict | None = None) -> dict:
    """Bloc position commun (§9.5). Les SL et TP viennent du trade enregistré, jamais d'un calcul de l'interface."""
    sign = 1 if t["side"] == "long" else -1
    entry, stop = t["entry"], t["stop"]
    risk = abs(entry - stop) if entry is not None and stop is not None else None
    if qty is None and space == "trades":
        s = t.get("sizing") or t.get("sizing_regle") or {}
        q = s.get("quantity")
        qty = q if q else None
    quote = quote or last_quote(t["symbol"], t.get("market", "stock"), t.get("last_price"), t.get("last_check"))
    price, as_of = quote["price"], quote["as_of_ts"]
    fresh = freshness(t.get("market", "stock"), as_of, now)
    current = t.get("stop_current", stop)
    breakeven = current is not None and entry is not None and sign * (current - entry) >= 0
    move = sign * (price - entry) if price is not None and entry is not None else None
    r_now = round(move / risk, 2) if move is not None and risk else None
    unreal = None
    if move is not None and not fresh["stale"]:
        usd = round(qty * move, 2) if qty else None
        unreal = {"r": r_now, "usd": usd, "eur": round(usd / _eurusd(t), 2) if usd is not None else None}
    fees = None
    if space == "trades":
        b = t.get("ibkr") or {}
        f = (b.get("fees") or {}).get(b.get("plan") or "")
        fees = {"usd": f.get("usd"), "r": f.get("r"), "bareme": b.get("plan")} if f else None
    return {
        "id": t["id"], "symbol": t["symbol"], "market": t.get("market", "stock"), "side": t["side"], "qty": qty,
        "entry": entry, "created_at": t.get("created_at"), "verdict": _verdict(t),
        "against_jarvis": T.against_jarvis(t),
        "stop": {"price": current, "initial": stop, "kind": "breakeven" if breakeven else "fixed",
                 "source": STOP_SOURCE[space]} if stop is not None else None,
        "targets": _targets(t, now) if entry else [], "sortie_unique": t.get("sortie_unique"),
        "targets_hit": t.get("targets_hit", 0),
        "last_price": price, "as_of": _iso(as_of), "price_source": quote["source"], "freshness": fresh,
        "move_pct": round(move / entry * 100, 2) if move is not None and entry else None,
        "r_now": r_now, "unrealized": unreal, "fees_est": fees,
        "exit_signals": list(t.get("exit_signals") or []),
        "ibkr_orders": _ibkr_orders(t, space, qty),
        "conseil": (t.get("conseils") or [None])[-1],
        "events": list(t.get("events") or []),
    }


def open_positions(space: str, now: datetime) -> list[dict]:
    if space == "trades":
        return [position_block(t, "trades", now) for t in scheduler.book.open_trades()]
    out = []
    for r in scheduler.reco_book.recos:
        if r["status"] != "ouvert" or not r.get("trade"):
            continue
        block = position_block(r["trade"], "paper", now, qty=r["message"].get("qty"))
        out.append(block | {"reco_id": r["id"], "verdict": "KILL" if r["kind"] == "kill" else "GO",
                            "against_jarvis": False})
    return out


# --- Résultats de l'espace --------------------------------------------------------------------------

def _closed_rows(space: str) -> list[dict]:
    """(heure de sortie, R brut, R net, € net) des trades terminés de l'espace ; € inconnu (None) sans taille."""
    rows = []
    if space == "trades":
        for t in scheduler.book.trades:
            r = T.realized_r(t)
            if r is None:
                continue
            net = T.realized_r_net(t)
            risk_eur = (t.get("sizing") or t.get("sizing_regle") or {}).get("risk_amount_eur")
            at = t["events"][-1]["at"] if t.get("events") else t.get("created_at")
            rows.append({"at": at, "r": r, "r_net": net,
                         "eur": round(net * risk_eur, 2) if net is not None and risk_eur else None,
                         "fees_eur": round((r - net) * risk_eur, 2) if net is not None and risk_eur else None})
    else:
        for rc in scheduler.reco_book.recos:
            res = rc.get("result")
            if rc["status"] != "clos" or not res:
                continue
            fees = res.get("fees_usd")
            rate = rc["message"].get("eurusd")
            rows.append({"at": res.get("closed_at"), "r": res["r"], "r_net": res.get("r_net"), "eur": res.get("eur"),
                         "fees_eur": round(fees / rate, 2) if fees is not None and rate else None})
    return rows


def _sum(rows: list[dict], key: str) -> float | None:
    vals = [r[key] for r in rows if r.get(key) is not None]
    return round(sum(vals), 3) if vals else None


def _period(rows: list[dict], now: datetime) -> dict:
    today = now.astimezone(PARIS).date()
    week = today.isocalendar()[:2]

    def pick(pred) -> dict:
        sel = [r for r in rows if (d := _parse(r["at"])) and pred(d.astimezone(PARIS).date())]
        return {"n": len(sel), "r": _sum(sel, "r"), "r_net": _sum(sel, "r_net"), "eur": _sum(sel, "eur"),
                "fees_eur": _sum(sel, "fees_eur"), "eur_inconnus": sum(r.get("eur") is None for r in sel)}

    return {"jour": pick(lambda d: d == today), "semaine": pick(lambda d: d.isocalendar()[:2] == week)}


def portfolio(space: str, now: datetime, positions: list[dict]) -> dict:
    rows = _closed_rows(space)
    latent = [p["unrealized"] for p in positions if p["unrealized"]]
    out = {"positions_ouvertes": len(positions),
           "latent": {"r": round(sum(u["r"] for u in latent if u["r"] is not None), 2) if latent else None,
                      "eur": round(sum(u["eur"] for u in latent if u["eur"] is not None), 2)
                      if any(u["eur"] is not None for u in latent) else None,
                      "inconnus": len(positions) - len(latent)},
           **_period(rows, now)}
    if space == "trades":
        return out | {"capital": {"eur": _capital(), "etiquette": "déclaré (JARVIS_CAPITAL)"},
                      "note": "JARVIS ne connaît ni l'équité ni la trésorerie IBKR : seulement le capital déclaré."}
    b = RECO.bilan(scheduler.reco_book, _capital())
    today = now.astimezone(NEW_YORK).date().isoformat()
    day = [r for r in scheduler.reco_book.recos if r["day"] == today]
    return out | {"si_tout_suivi": b, "recos_du_jour": {s: sum(r["status"] == s for r in day) for s in RECO.STATUS_FR},
                  "go_en_attente_de_confirmation": sum(g["status"] in CONFIRM.PENDING
                                                       for g in (scheduler.confirm_state.get("gos") or {}).values())
                  if scheduler.confirm_state.get("day") == today else 0}


# --- Risque ---------------------------------------------------------------------------------------

def risk(now: datetime) -> dict:
    trades = scheduler.book.trades
    data = lab_grade.current()
    markets = {}
    for m in ("stock", "crypto"):
        try:
            g = lab_grade.risk_now(m, trades, now, data)
        except Exception as exc:  # grade illisible : 0 % (fail-closed), comme apply_grade
            g = lab_grade.unreadable(exc)
        markets[m] = g | {"risque_propose_eur": T.recommended_eur(g.get("risque_propose"))}
    return {"grades": markets, "grade_age_h": lab_grade.age_h(data, now) if data else None,
            "grade_max_age_h": lab_grade.MAX_AGE_H,
            "risque_ouvert_pct": lab_grade.open_risk(trades), "risque_ouvert_max_pct": lab_grade.RISQUE_OUVERT_MAX,
            "r_du_jour_net": lab_grade.day_r(trades, now), "arret_jour_r": lab_grade.ARRET_JOUR_R,
            "risque_max_par_trade_eur": T.rule_eur(), "capital_declare_eur": _capital(),
            "note": "risque au stop ≠ perte maximale : un gap au-delà du stop s'exécute plus loin"}


# --- Séances et agenda ----------------------------------------------------------------------------

def _at(day: date, t: time, tz=NEW_YORK) -> datetime:
    return datetime.combine(day, t, tz)


def _next_trading_day(day: date) -> date:
    d = day + timedelta(days=1)
    for _ in range(15):
        if CAL.is_trading_day(d):
            return d
        d += timedelta(days=1)
    return d


def market_sessions(now: datetime) -> dict:
    """État NYSE (pré-marché 4 h, séance, après-bourse jusqu'à 20 h, 17 h une demi-séance ; heure de New York) et
    crypto 24/7. Bornes en UTC ; l'interface les affiche à Paris (décalage d'heure d'été compris)."""
    ny = now.astimezone(NEW_YORK)
    day = ny.date()
    holiday = CAL.holidays(day.year).get(day)
    bounds = CAL.session_bounds(day)
    early = day in CAL.early_closes(day.year)

    def phases(d: date) -> dict | None:
        b = CAL.session_bounds(d)
        if not b:
            return None
        after_end = EARLY_AFTER_END if d in CAL.early_closes(d.year) else AFTER_HOURS_END
        return {"jour": d.isoformat(), "premarche": _at(d, PREMARKET).astimezone(timezone.utc).isoformat(),
                "ouverture": b[0].astimezone(timezone.utc).isoformat(), "cloture": b[1].astimezone(timezone.utc).isoformat(),
                "fin_apres_bourse": _at(d, after_end).astimezone(timezone.utc).isoformat(),
                "demi_seance": d in CAL.early_closes(d.year)}

    today = phases(day)
    if today and now < datetime.fromisoformat(today["premarche"]):
        etat, change = "ferme", today["premarche"]
    elif today and now < datetime.fromisoformat(today["ouverture"]):
        etat, change = "premarche", today["ouverture"]
    elif today and now < datetime.fromisoformat(today["cloture"]):
        etat, change = "seance", today["cloture"]
    elif today and now < datetime.fromisoformat(today["fin_apres_bourse"]):
        etat, change = "apres_bourse", today["fin_apres_bourse"]
    else:
        nxt = phases(_next_trading_day(day))
        etat, change = "ferme", nxt["premarche"] if nxt else None
    nxt_open = (today["ouverture"] if today and now < datetime.fromisoformat(today["ouverture"])
                else (phases(_next_trading_day(day)) or {}).get("ouverture"))
    gap = (now.astimezone(PARIS).utcoffset() - ny.utcoffset()).total_seconds() / 3600
    reason = ("week-end" if ny.weekday() >= 5 else f"jour férié : {holiday}" if holiday
              else CAL.SPECIAL_CLOSURES.get(day) or ("fermeture exceptionnelle" if not bounds else None))
    return {"nyse": {"etat": etat, "prochain_changement": change, "prochaine_ouverture": nxt_open,
                     "aujourd_hui": today, "raison_fermeture": reason, "demi_seance": early,
                     "ecart_paris_new_york_h": gap, "decalage_inhabituel": gap != 6},
            "crypto": {"etat": "ouvert", "label": "24/7"}}


def agenda_today(now: datetime) -> list[dict]:
    """Ce que JARVIS fait aujourd'hui, depuis ses propres horaires (aucune source externe ; calendrier économique et
    résultats : vue Agenda, PR 3)."""
    ny_day = now.astimezone(NEW_YORK).date()
    items: list[dict] = []

    def add(at: datetime, title: str, cat: str, source: str, importance: str = "normale") -> None:
        items.append({"at": at.astimezone(timezone.utc).isoformat(timespec="seconds"), "titre": title,
                      "categorie": cat, "source": source, "importance": importance,
                      "passe": at <= now})

    bounds = CAL.session_bounds(ny_day)
    if bounds:
        from app.trading import gemini as GEMINI
        from app.trading import plus10 as PLUS10

        hh, mm = (int(x) for x in str(settings["scan_time_ny"]).split(":"))
        if scheduler.enabled():
            add(_at(ny_day, time(9, 0)), "Point du matin sur Discord", "jarvis", "scheduler.tick_brief")
        if settings["scan_enabled"]:
            add(_at(ny_day, time(hh, mm)), "Scanner pré-ouverture (plans GO / NO GO)", "jarvis",
                "scheduler.tick_scan", "haute")
        add(bounds[0], "Ouverture NYSE", "marche", "calendar.py", "haute")
        add(bounds[0] + CONFIRM.WAIT, "Début des confirmations des GO (bougies 5 min)", "jarvis",
            "scheduler.tick_confirm", "haute")
        for kind, t in GEMINI.PASSES.items():
            add(_at(ny_day, t), f"Challenger Gemini · passage {kind}", "labo", "scheduler.tick_gemini")
        for kind, t in PLUS10.PASSES.items():
            add(_at(ny_day, t), "Potentiel +10 · liste confirmée" + (" (range 30 min)" if kind != "confirmee" else ""),
                "labo", "scheduler.tick_plus10")
        add(_at(ny_day, time(11, 0)), "Fin de validité des ordres KILL", "jarvis", "killmode (11 h New York)")
        add(_at(ny_day, CONFIRM.LAST_CHECK), "GO non confirmés expirés", "jarvis", "scheduler.tick_confirm")
        add(bounds[1] - timedelta(minutes=30), "Conseil avant la clôture (sortir, alléger, garder)", "jarvis",
            "scheduler.tick_closing")
        add(bounds[1], "Clôture NYSE" + (" (demi-séance)" if bounds[1].astimezone(NEW_YORK).hour == 13 else ""),
            "marche", "calendar.py", "haute")
        add(_at(ny_day, time(15, 12), PARIS), "Routine Claude « Recherche avant l'ouverture »", "routine",
            "routines/recherche-pre-ouverture.md")
        add(_at(ny_day, time(22, 52), PARIS), "Routine Claude « Équipe du soir du labo »", "routine",
            "labo/routine_soir.md")
    return sorted(items, key=lambda i: i["at"])


# --- Santé des boucles et du labo ------------------------------------------------------------------

def loops(now: datetime) -> dict:
    out = {}
    for name, run in scheduler.last_runs.items():
        age = now.timestamp() - _parse(run["at"]).timestamp()
        out[name] = run | {"age_s": round(age), "stale": age > max(LOOP_STALE_S, 3 * run.get("every_s", 60))}
    track = out.get(TRACKING_LOOP)
    return {"boucles": out, "suivi": {"at": track["at"] if track else None, "age_s": track["age_s"] if track else None,
                                      "stale": track["stale"] if track else True}}


def lab_heartbeat(now: datetime) -> dict:
    hb = heartbeat.read(now, Path(lab_db.db_path()).parent / "lab.heartbeat")
    if not hb:
        return {"etape": None, "age_s": None, "stale": True, "raison": "aucun battement (worker jarvis-lab arrêté ?)"}
    return {"etape": hb.get("etape"), "le": hb.get("le"), "age_s": hb["il_y_a_s"],
            "stale": hb["il_y_a_s"] > LAB_STALE_S}


def sources_short() -> dict:
    return {"lectures": sources_health.snapshot(), "twelve_data": twelvedata.credits_today(),
            "alpaca_configure": alpaca.configured(),
            "twelve_data_configure": bool(config.secret(twelvedata.KEY_NAME))}


def _pending_proposals() -> list[dict]:
    return [{k: p.get(k) for k in ("id", "kind", "key", "value", "current", "why", "created_at", "nom")}
            for p in PR.proposals.pending()]


def plans_today(now: datetime) -> dict:
    """Plans du jour : dernier passage du scanner (GO / NO GO) et état de la confirmation des GO."""
    from app.trading import research

    view = research.scan_view(research.last_scan(), now)
    scan = view.get("scan") or {}
    gos = scheduler.confirm_state.get("gos") or {} if scheduler.confirm_state.get("day") == view["jour"] else {}
    picks = []
    for p in scan.get("picks") or []:
        sc = p.get("score") or {}
        g = gos.get(p["symbol"]) or {}
        picks.append({"symbol": p["symbol"], "verdict": "GO" if sc.get("go") else "NO GO",
                      "decision": sc.get("decision"), "composite": sc.get("composite"),
                      "raisons": sc.get("reasons"), "entry": p.get("entry"), "stop": p.get("stop"),
                      "targets": p.get("targets"), "features": p.get("features"),
                      "confirmation": g.get("status"), "declencheur": g.get("trigger")})
    return {"du_jour": view["du_jour"], "scan_at": scan.get("at"), "picks": picks,
            "note_composite": "priorité, pas une probabilité"}


# --- Routes de lecture ----------------------------------------------------------------------------------

@router.get("/state")
async def state(space: str = "paper"):
    """État général, toutes les 5 s : sans appel réseau (caches et fichiers seulement)."""
    space = _space(space)
    now = datetime.now(timezone.utc)
    positions = open_positions(space, now)
    return {"generated_at": now.isoformat(timespec="seconds"), "space": space, "jarvis": loops(now),
            "lab": lab_heartbeat(now), "portfolio": portfolio(space, now, positions), "positions_open": positions,
            "risk": risk(now), "proposals_pending": _pending_proposals(), "market_sessions": market_sessions(now),
            "sources": sources_short(), "agenda_today": agenda_today(now), "plans_today": plans_today(now),
            "killmode": _killmode_state()}


def _killmode_state() -> dict:
    from app.trading import killmode as K  # import tardif : killmode tire le labo

    st = K.state()
    return {"actif": bool(st.get("actif")), "depuis": st.get("depuis")}


_snapshots: dict = {"at": 0.0, "symbols": (), "data": {}}


async def _stock_snapshots(symbols: list[str]) -> dict[str, dict]:
    """Snapshots Alpaca (IEX, gratuit) groupés, au plus un appel par minute ; {} sans clés ou en erreur."""
    if not symbols or not alpaca.configured():
        return {}
    key = tuple(sorted(symbols))
    if _snapshots["symbols"] == key and clock.monotonic() - _snapshots["at"] < 60:
        return _snapshots["data"]
    try:
        data = await alpaca.snapshots(list(key))
    except Exception as exc:
        log.warning("Cockpit : snapshots Alpaca indisponibles (%s)", exc)
        data = _snapshots["data"] if _snapshots["symbols"] == key else {}
    _snapshots.update(at=clock.monotonic(), symbols=key, data=data)
    return data


def _held_symbols(space: str | None = None) -> dict[str, str]:
    held = {t["symbol"]: t["market"] for t in scheduler.book.open_trades()} if space in (None, "trades") else {}
    if space in (None, "paper"):
        held |= {r["symbol"]: r["market"] for r in scheduler.reco_book.active()}
    return held


@router.get("/instruments")
async def instruments(space: str = "paper"):
    """Watchlist (JARVIS_WATCHLIST_STOCKS, JARVIS_WATCHLIST_CRYPTO) et symboles en position : prix, variation, source,
    âge, mini-série. Crypto : Kraken (cache 15 s) ; actions : bougies du suivi en cache, sinon snapshots Alpaca IEX."""
    space = _space(space)
    now = datetime.now(timezone.utc)
    held_space = _held_symbols(space)
    held = _held_symbols()
    crypto = list(dict.fromkeys(config.WATCHLIST_CRYPTO + [s for s, m in held.items() if m == "crypto"]))
    stocks = list(dict.fromkeys(config.WATCHLIST_STOCKS + [s for s, m in held.items() if m == "stock"]))
    rows = []
    for s in crypto:
        bars = await T._optional_candles(s, "crypto", 1) or []
        daily = await T._optional_candles(s, "crypto", 1440) or []
        price = bars[-1]["c"] if bars else None
        prev = daily[-2]["c"] if len(daily) >= 2 else None
        as_of = bars[-1]["t"] + 60 if bars else None
        rows.append({"symbol": s, "market": "crypto", "price": price, "source": "Kraken",
                     "change_pct": round((price / prev - 1) * 100, 2) if price and prev else None,
                     "as_of": _iso(as_of), "freshness": freshness("crypto", as_of, now),
                     "spark": [b["c"] for b in bars[-60:]], "en_position": s in held_space})
    cached = {s: _cached_bars(s, "stock", 1)[0] for s in stocks}
    missing = [s for s, b in cached.items() if not b]
    snaps = await _stock_snapshots(missing)
    for s in stocks:
        bars = cached[s]
        snap = snaps.get(s) or {}
        if bars:
            price, as_of, source = bars[-1]["c"], bars[-1]["t"] + 60, "Twelve Data (cache du suivi)"
        else:
            price, source = snap.get("price"), "Alpaca IEX" if snap else None
            as_of = _parse(snap.get("as_of")).timestamp() if _parse(snap.get("as_of")) else None
        prev = snap.get("prev_close")
        rows.append({"symbol": s, "market": "stock", "price": price, "source": source,
                     "change_pct": round((price / prev - 1) * 100, 2) if price and prev else None,
                     "as_of": _iso(as_of), "freshness": freshness("stock", as_of, now),
                     "spark": [b["c"] for b in bars[-60:]], "en_position": s in held_space})
    return {"generated_at": now.isoformat(timespec="seconds"), "space": space, "instruments": rows,
            "manque": None if alpaca.configured() or not missing else "clés Alpaca absentes : actions hors suivi sans prix"}


_alpaca_bars: dict[tuple, tuple[float, list[dict]]] = {}


async def bars_for(symbol: str, market: str, interval: int) -> tuple[list[dict], str | None, str | None]:
    """(bougies, source, manque) SANS appel Twelve Data : crypto Kraken ; actions cache du suivi, sinon Alpaca (cache
    de 60 s ; SIP 16 min de retard, IEX en secours)."""
    if market == "crypto":
        try:
            return await T.candles(symbol, "crypto", interval), "Kraken", None
        except T.PlanError as exc:
            return [], None, str(exc)
    bars, _ = _cached_bars(symbol, "stock", interval)
    if bars:
        return bars, "Twelve Data (cache du suivi)", None
    if not alpaca.configured():
        return [], None, "clés Alpaca absentes et aucune bougie en cache (la page ne déclenche jamais Twelve Data)"
    key = (symbol, interval)
    hit = _alpaca_bars.get(key)
    if hit and clock.monotonic() - hit[0] < 60:
        return hit[1], "Alpaca (16 min de retard)", None
    try:
        rows = await alpaca.candles(symbol, interval)
    except Exception as exc:
        return [], None, f"Alpaca : {exc}"
    if len(_alpaca_bars) > 64:
        _alpaca_bars.pop(next(iter(_alpaca_bars)))
    _alpaca_bars[key] = (clock.monotonic(), rows)
    return rows, "Alpaca (16 min de retard)", None


def _market_of(symbol: str, market: str | None) -> str:
    if market is not None:
        if market not in ("stock", "crypto"):
            raise HTTPException(400, "market doit valoir stock ou crypto")
        return market
    held = _held_symbols()
    if symbol in held:
        return held[symbol]
    from app.notify import bot

    return "crypto" if symbol in config.WATCHLIST_CRYPTO or symbol.removesuffix("USD") in bot.CRYPTO else "stock"


def _space_positions(space: str, symbol: str, now: datetime) -> list[dict]:
    return [p for p in open_positions(space, now) if p["symbol"] == symbol]


@router.get("/instrument/{symbol}")
async def instrument(symbol: str, space: str = "paper", tf: str = "15m", market: str | None = None):
    """Bougies OHLCV, trade(s) ouvert(s) de l'espace, plan du scanner du jour, âge des données (toutes les 10 s)."""
    space, interval, symbol = _space(space), _tf(tf), _symbol(symbol)
    market = _market_of(symbol, market)
    now = datetime.now(timezone.utc)
    bars, source, missing = await bars_for(symbol, market, interval)
    positions = _space_positions(space, symbol, now)
    window = MAIN_BARS if positions else 300
    last = bars[-1] if bars else None
    plan = next((p for p in plans_today(now)["picks"] if p["symbol"] == symbol), None)
    return {"symbol": symbol, "market": market, "tf": tf, "source": source, "manque": missing,
            "bars": [[b["t"], b["o"], b["h"], b["l"], b["c"], b.get("v", 0)] for b in bars[-window:]],
            "as_of": _iso(last["t"] + interval * 60) if last else None,
            "positions": positions, "plan_du_jour": plan, "space": space}


@router.get("/positions/charts")
async def positions_charts(space: str = "paper", tf: str = "15m"):
    """Chaque trade ouvert de l'espace avec ses 70 dernières bougies (mini-graphiques ; 10 s quand la vue est
    affichée)."""
    space, interval = _space(space), _tf(tf)
    now = datetime.now(timezone.utc)
    out = []
    for p in open_positions(space, now):
        bars, source, missing = await bars_for(p["symbol"], p["market"], interval)
        out.append({"position": p, "source": source, "manque": missing,
                    "bars": [[b["t"], b["o"], b["h"], b["l"], b["c"], b.get("v", 0)] for b in bars[-MINI_BARS:]]})
    return {"generated_at": now.isoformat(timespec="seconds"), "space": space, "tf": tf, "charts": out}


def reco_view(r: dict, trades: list[dict]) -> dict:
    t = r.get("trade") or {}
    linked = RECO.linked_trade(r, trades)
    return {"id": r["id"], "type": r["kind"], "type_fr": RECO.KINDS[r["kind"]], "symbol": r["symbol"],
            "market": r["market"], "side": r["side"], "day": r["day"], "sent_at": r["sent_at"], "message": r["message"],
            "status": r["status"], "status_fr": RECO.STATUS_FR[r["status"]],
            "fill": {"price": t.get("entry"), "at": t.get("filled_at")} if t else None,
            "targets_hit": t.get("targets_hit"), "stop_current": t.get("stop_current"),
            "last_price": t.get("last_price"), "events": t.get("events") or [],
            "result": r.get("result"), "unrealized": RECO.unrealized(r),
            "mes_trades": {"id": linked["id"], "entry": linked["entry"], "status": linked["status"],
                           "r_net": T.realized_r_net(linked)} if linked else None}


@router.get("/reco")
async def reco(days: int = Query(30, ge=1, le=3650)):
    """Journal des recommandations (espace Papier) et bilan « si j'avais tout suivi »."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    trades = scheduler.book.trades
    rows = [reco_view(r, trades) for r in reversed(scheduler.reco_book.recos)
            if (_parse(r["sent_at"]) or now) >= since]
    return {"generated_at": now.isoformat(timespec="seconds"), "jours": days, "recos": rows,
            "bilan": RECO.bilan(scheduler.reco_book, _capital()),
            "gestion": {"go_confirme": "ordre stop d'achat au déclencheur jusqu'à 11 h 30 New York, stop du plan, "
                                       "sortie unique sur TP2 (repères TP1, TP3), stop à l'entrée après TP2, pas de "
                                       "sortie au temps, taille du message, commissions IBKR déduites",
                        "kill": "ordre stop d'achat 1 min après l'envoi jusqu'à 11 h New York, un bracket par objectif "
                                "(1, 2, 3 R), stop à l'entrée dès TP1, vendu à la clôture, taille du journal de "
                                "Killmode, commissions IBKR déduites"}}


_lab_cache: dict = {"at": 0.0, "data": None}


def _lab_payload(now: datetime) -> dict:
    """Labo en lecture seule (lab.db) et fichiers du direct ; chaque partie à part (une erreur n'efface pas les autres)."""
    from app.lab import council as lab_council
    from app.lab import lessons as lab_lessons
    from app.lab import status as lab_status
    from app.lab import validator as lab_validator
    from app.trading import gemini as GEMINI
    from app.trading import killmode as K

    out: dict = {"generated_at": now.isoformat(timespec="seconds")}

    def part(name: str, fn) -> None:
        try:
            out[name] = fn()
        except Exception as exc:  # base absente, ancien schéma : la partie le dit, les autres restent
            out[name] = {"erreur": f"{type(exc).__name__}: {exc}"[:200]}

    def status():
        st = lab_status.status()
        return {k: st.get(k) for k in ("exists", "db_size_mb", "replay", "live", "trades", "method_version",
                                        "worker", "declaration", "labo2", "methode", "live_start", "error")}

    part("statut", status)
    conn = lab_db.open_readonly()
    try:
        if conn is None:
            out["base"] = {"erreur": "base du labo absente (le service jarvis-lab n'a pas encore démarré)"}
        else:
            part("declarations", lambda: lab_council.load(conn))
            part("regles", lambda: lab_validator.counts(conn))
            part("grade", lambda: lab_grade.payload(conn) | {"historique": lab_grade.history(conn, limit=10)})
            part("lecons", lambda: lab_lessons.counts(conn))
    finally:
        if conn is not None:
            conn.close()

    def gemini():
        book = GEMINI.plans()
        return {"plans": len(book), "j0": GEMINI.summary(book, "j0"), "j1": GEMINI.summary(book, "j1")}

    def killmode():
        calls = K.calls()
        kills = [c for c in calls if K._group_of(c) == "kill"]
        return {"actif": K.active(), "depuis": K.state().get("depuis"), "kill": len(kills),
                "temoins": len(calls) - len(kills),
                "derniers": [{k: c.get(k) for k in ("id", "symbol", "day", "entry", "stop", "targets")}
                             | {"r_20e": K.value(c)} for c in kills[-10:]]}

    def forward_test():
        st = paper.stats(scheduler.paper_book)
        return {k: v for k, v in st.items() if k != "text"} | {
            "note": "compte fictif Alpaca : GO et NO GO du scanner, 3 brackets, sans frais ; une mesure de critères, "
                    "pas des recommandations"}

    part("gemini", gemini)
    part("killmode", killmode)
    part("forward_test", forward_test)
    part("propositions", lambda: [p for p in reversed(PR.proposals.items)
                                  if p.get("kind") in ("labo", "grade", "promotion")][:30])
    out["veto"] = {"possible": True, "effet": "annule la dernière décision du council au prochain passage du labo "
                                              "(!veto)"}
    out["modules"] = {name: f"/api/lab/{name}" for name in (
        "strategies", "ouverture", "autopsie", "rates", "sec8k", "etude8k", "polyfed", "potentiel", "filtres",
        "lessons", "grade", "summary", "matin")}
    out["modules_note"] = ("détail lu par la vue Labo (PR 3) ; ces routes demandent la clé du labo, le cockpit les "
                           "lira par fonction, sans clé")
    return out


@router.get("/lab")
async def lab():
    """Labo en lecture seule (cache de 60 s : la base fait plusieurs Go)."""
    now = datetime.now(timezone.utc)
    if _lab_cache["data"] is None or clock.monotonic() - _lab_cache["at"] > 60:
        _lab_cache.update(data=await asyncio.to_thread(_lab_payload, now), at=clock.monotonic())
    return _lab_cache["data"]


def _trade_row(t: dict) -> dict:
    last = (t.get("events") or [{}])[-1]
    risk_eur = (t.get("sizing") or t.get("sizing_regle") or {}).get("risk_amount_eur")
    net = T.realized_r_net(t)
    return {"id": t["id"], "symbol": t["symbol"], "market": t.get("market"), "side": t["side"], "entry": t["entry"],
            "created_at": t.get("created_at"), "closed_at": last.get("at"), "exit_price": last.get("price"),
            "raison": last.get("reason") or last.get("kind"), "corrige": bool(last.get("corrige")),
            "r": T.realized_r(t), "r_net": net,
            "eur": round(net * risk_eur, 2) if net is not None and risk_eur else None,
            "verdict": _verdict(t), "against_jarvis": T.against_jarvis(t), "note": t.get("note")}


@router.get("/journal")
async def journal(space: str = "paper", limit: int = Query(100, ge=1, le=1000)):
    """Trades terminés de l'espace, désaccords, confirmations des GO, forward-test et décisions sur les propositions."""
    space = _space(space)
    now = datetime.now(timezone.utc)
    if space == "trades":
        closed = [_trade_row(t) for t in reversed(scheduler.book.trades) if t["status"] == "closed"][:limit]
    else:
        trades = scheduler.book.trades
        closed = [reco_view(r, trades) for r in reversed(scheduler.reco_book.recos)
                  if r["status"] in ("clos", "pas_declenche")][:limit]
    slopes = await asyncio.to_thread(T.note_slopes)
    rows = CONFIRM.read_log()
    st = paper.stats(scheduler.paper_book)
    return {"generated_at": now.isoformat(timespec="seconds"), "space": space, "clotures": closed,
            "desaccords": T.disagreements(scheduler.book, slopes),
            "confirmations": {"lignes": rows[-60:][::-1], "total": len(rows),
                              "aujourd_hui": scheduler.confirm_state.get("gos"),
                              "jour": scheduler.confirm_state.get("day")},
            "forward_test": {k: v for k, v in st.items() if k != "text"},
            "decisions": [p for p in reversed(PR.proposals.items) if p["status"] != "pending"][:50]}


def _disk() -> dict:
    folder = Path(os.getenv("JARVIS_DATA_DIR", "data"))
    target = folder if folder.exists() else Path(".")
    try:
        u = shutil.disk_usage(target)
    except OSError as exc:
        return {"erreur": str(exc)}
    return {"dossier": str(target), "total_go": round(u.total / 1e9, 1), "libre_go": round(u.free / 1e9, 1),
            "utilise_pct": round(u.used / u.total * 100, 1) if u.total else None}


@router.get("/health")
async def health():
    """Santé : sources (dernière lecture réussie), boucles du planificateur, battement du labo, file Discord, disque,
    crédits Twelve Data du jour, erreurs récentes."""
    now = datetime.now(timezone.utc)
    items = scheduler.outbox.items
    return {"generated_at": now.isoformat(timespec="seconds"), "api": {"status": "ok"},
            "sources": sources_short(), "planificateur": loops(now), "labo": lab_heartbeat(now),
            "discord": {"notifications": scheduler.enabled(), "file_en_attente": len(items),
                        "plus_ancien": min((i.get("created_at") or "" for i in items), default=None) or None},
            "disque": _disk(), "erreurs_recentes": list(recent_errors.items)[::-1]}


ENV_KEYS = ("TWELVEDATA_API_KEY", "FINNHUB_API_KEY", "ALPACA_API_KEY", "ALPACA_SECRET_KEY", "DISCORD_WEBHOOK_URL",
            "DISCORD_BOT_TOKEN", "JARVIS_LAB_API_KEY")


@router.get("/diagnostics")
async def diagnostics():
    """Diagnostic téléchargeable : AUCUN secret ni aucune valeur de variable d'environnement (seulement leurs noms et
    si les clés sont présentes)."""
    now = datetime.now(timezone.utc)
    body = {"generated_at": now.isoformat(timespec="seconds"), "version": "cockpit-api-1",
            "planificateur": loops(now), "labo": lab_heartbeat(now), "sources": sources_short(),
            "trades": {"ouverts": len(scheduler.book.open_trades()), "total": len(scheduler.book.trades)},
            "recommandations": {"total": len(scheduler.reco_book.recos), "depuis": scheduler.reco_book.since,
                                "actives": len(scheduler.reco_book.active())},
            "forward_test": {"entrees": len(scheduler.paper_book.entries)},
            "propositions_en_attente": len(PR.proposals.pending()),
            "cles_presentes": {k: bool(config.secret(k)) for k in ENV_KEYS},
            "variables_jarvis_definies": sorted(k for k in os.environ if k.startswith("JARVIS_")),
            "erreurs_recentes": list(recent_errors.items)[::-1][:20]}
    return JSONResponse(body, headers={"Content-Disposition":
                                       f'attachment; filename="jarvis-diagnostic-{now:%Y%m%d-%H%M}.json"'})
