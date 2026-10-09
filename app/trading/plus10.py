"""Potentiel +10 en direct (déclaration n° 9 du labo, 9 octobre 2026) : mesuré, ni GO ni note.

Les listes S+10 (gap ≥ +5 %, ATR ≥ 5 %) et Rebond (gap ≤ −5 %, ATR ≥ 5 %) sont celles du labo (app/lab/plus10.py,
même fonction `ranked`), sans filtre de prix, de liquidité ni de capitalisation (choix de Simão) :
1. liste d'attente, avec le scanner de 15 h 10 Paris (9 h 10 New York), au gap de pré-ouverture (dernier échange IEX).
   Elle n'est pas rejouable au labo (pas de pré-ouverture historique) : jamais présentée comme testée ;
2. liste confirmée à 9 h 46 New York, au gap d'ouverture, comme au labo, avec les niveaux des essais : cassure du range
   de 15 min (S+10 et Rebond) et règle de prix de Killmode (S+10), lus avec le code du labo (strategies.orb,
   killmode_check) sur les bougies de 5 min IEX en temps réel (le labo lit le SIP ; l'écart se mesure) ;
3. à 10 h 01 New York, les niveaux du range de 30 min pour Rebond.
Population du direct : 50 plus fortes hausses et 50 plus fortes baisses du screener d'Alpaca, 100 plus actives, liste
suivie et liste d'attente ; pas toute la base : le labo mesure la concordance de son top 5 avec celui-ci.
Chaque passage est noté dans data/plus10_scans.jsonl (lu par le labo pour la concordance).
"""
import asyncio
import json
import logging
import os
import re
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app import config
from app.lab import collector as CO
from app.lab import plus10 as LAB
from app.lab import potentiel as PO
from app.lab import strategies as ST
from app.sources import alpaca, finnhub
from app.trading import calendar as CAL
from app.trading import confirm as CF
from app.trading.service import _num

log = logging.getLogger(__name__)
NEW_YORK = ZoneInfo("America/New_York")
JOURNAL_PATH = os.getenv("JARVIS_PLUS10_SCANS_PATH", "data/plus10_scans.jsonl")
STATE_PATH = os.getenv("JARVIS_PLUS10_STATE_PATH", "data/plus10_state.json")
TICKER = re.compile(r"^[A-Z]{1,5}$")
DAILY_DAYS = 100
# Passages confirmés : heure de New York (une minute après la bougie qui ferme le range), fenêtre de rattrapage.
PASSES = {"confirmee": time(9, 46), "confirmee_30": time(10, 1)}
WINDOW = timedelta(minutes=9)
LABELS = {"s10": "S+10", "rebond": "Rebond"}


def _fr(x: float, digits: int = 1) -> str:
    return f"{x:+.{digits}f}".replace(".", ",")


def _read_state() -> dict:
    try:
        return json.loads(Path(STATE_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(state: dict) -> None:
    p = Path(STATE_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def journal(entry: dict) -> None:
    p = Path(JOURNAL_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


async def _symbols(extra: list[str]) -> list[str]:
    groups = await asyncio.gather(alpaca.most_actives(100), alpaca.top_gainers(50), alpaca.top_losers(50),
                                  return_exceptions=True)
    out = []
    for g in (*groups, config.WATCHLIST_STOCKS, extra):
        if isinstance(g, Exception):
            log.warning("Potentiel +10 : screener Alpaca : %s", g)
            continue
        out += [x.upper() for x in g]
    return list(dict.fromkeys(x for x in out if TICKER.match(x)))


def measure(rows: list[dict], snap: dict, now: datetime, at_open: bool) -> dict | None:
    """Gap (pré-ouverture : dernier échange ; ouverture : bougie du jour) et ATR de la veille, comme le labo :
    séances terminées, ATR de Wilder, au moins LAB.MIN_SESSIONS séances. None si une donnée manque."""
    today = now.astimezone(NEW_YORK).date()
    before = [r for r in rows if datetime.fromtimestamp(r["t"], NEW_YORK).date() < today]
    if len(before) < LAB.MIN_SESSIONS or before[-1]["c"] <= 0:
        return None
    prev = before[-1]["c"]
    if at_open:
        bar = next((r for r in rows if datetime.fromtimestamp(r["t"], NEW_YORK).date() == today), None)
        price = bar["o"] if bar else snap.get("open") if snap.get("open_day") == today.isoformat() else None
    else:
        price = snap.get("price")
    if not price:
        return None
    a = ST.atr(before)[-1]
    return {"gap_pct": (price / prev - 1) * 100, "atr_pct": a / prev * 100 if a else None, "prev_close": prev,
            "seances": len(before), "price": price}


async def lists(now: datetime, at_open: bool, extra: list[str] | None = None) -> tuple[dict, dict, dict]:
    """(listes complètes classées, mesures par symbole, journalier par symbole)."""
    symbols = await _symbols(extra or [])
    snaps, daily = await asyncio.gather(alpaca.snapshots(symbols), alpaca.bars(symbols, 1440, days=DAILY_DAYS, now=now))
    pop = {}
    for s in symbols:
        m = measure(daily.get(s) or [], snaps.get(s) or {}, now, at_open)
        if m:
            pop[s] = m
    return LAB.ranked(pop), pop, daily


async def _news(symbol: str) -> dict | None:
    try:
        items = await finnhub.company_news(symbol, 1, days=2)
    except Exception as exc:  # une actu manquante n'empêche pas la liste
        log.info("Potentiel +10 : actus de %s indisponibles : %s", symbol, exc)
        return None
    return items[0] if items else None


def lab_share(gap: float) -> float | None:
    """Part des séances du labo à ce gap qui ont touché +8 % depuis l'ouverture (potentiel.py), None si trop peu."""
    label = PO.gap_bucket(gap)
    cell = ((PO.current() or {}).get("par_gap") or {}).get(label) if label else None
    if not cell or cell.get("n", 0) < PO.MIN_N:
        return None
    return cell.get("zone_pct")


def _line(symbol: str, m: dict) -> str:
    share = lab_share(m["gap_pct"])
    atr = f"{m['atr_pct']:.1f}".replace(".", ",")
    return (f"**{symbol}** {_fr(m['gap_pct'])} % · ATR {atr} % · {_num(m['prev_close'])} $ la veille"
            + (f" · labo : {f'{share:g}'.replace('.', ',')} % des séances à ce gap touchent +8 %"
               if share is not None else ""))


async def waitlist(now: datetime | None = None) -> dict:
    """Liste d'attente de 9 h 10 New York (gap de pré-ouverture), notée au journal et gardée pour la confirmation."""
    now = now or datetime.now(timezone.utc)
    ranked, pop, _ = await lists(now, at_open=False)
    top = {k: v[:LAB.TOP] for k, v in ranked.items()}
    syms = [s for v in top.values() for s in v]
    news = dict(zip(syms, await asyncio.gather(*(_news(s) for s in syms))))
    day = now.astimezone(NEW_YORK).date().isoformat()
    entry = {"kind": "attente", "day": day, "at": now.isoformat(timespec="seconds"), "listes": top,
             "gaps": {s: round(pop[s]["gap_pct"], 2) for s in syms}, "examinees": len(pop)}
    journal(entry)
    _write_state({"day": day, "attente": top, "faits": []})
    return entry | {"mesures": {s: pop[s] for s in syms}, "actus": news}


def waitlist_text(w: dict) -> str:
    lines = ["🎯 **Potentiel +10 · liste d'attente** (gap de pré-ouverture, toutes valeurs, sans minimum de prix) · "
             "pas un GO : la liste confirmée part en MP à 15 h 46, au gap d'ouverture"]
    for liste, syms in w["listes"].items():
        lines.append(f"__{LAB.LISTS[liste]}__")
        if not syms:
            lines.append("rien ce matin")
        for s in syms:
            lines.append(_line(s, w["mesures"][s]))
            n = w["actus"].get(s)
            if n and n.get("title"):
                lines.append(f"📰 [{n['title'][:100]}](<{n.get('url', '')}>)")
    lines.append("-# En mesure au labo (déclaration n° 9) : cette liste de 9 h 10 n'est pas rejouable et n'est pas "
                 "testée ; seule la liste confirmée l'est.")
    return "\n".join(lines)


def _day_of(bars5: list[dict], daily: list[dict], now: datetime) -> ST.Day | None:
    """Le jour vu par le labo : bougies de 5 min closes de la séance régulière (IEX, temps réel) et journalier SIP
    de la veille."""
    today = now.astimezone(NEW_YORK).date()
    bounds = CO.session_ts(today)
    if not bounds:
        return None
    done = [b for b in alpaca.regular_hours(bars5) if b["t"] + ST.BAR <= now.timestamp()]
    hist = [b for b in done if b["t"] < bounds[0]][-ST.STOCK_WARMUP_BARS:]
    session = [b for b in done if bounds[0] <= b["t"] < bounds[1]]
    before = [r for r in daily if datetime.fromtimestamp(r["t"], NEW_YORK).date() < today]
    return ST.Day("stock", today, hist + session, before)


def orb_now(d: ST.Day, minutes: int) -> dict | None:
    """Le signal de strategies.orb dès la clôture du range (le labo, qui rejoue la séance entière, demande en plus une
    bougie après le range) : ordre stop d'achat 1 pas au-dessus, stop 1 pas sous le range, valable jusqu'à 12 h."""
    n = minutes // CO.BASE_TF
    if d.d1 - d.d0 < n or d.t[d.d0] != d.start_ts:
        return None
    hi, lo = max(d.h[d.d0:d.d0 + n]), min(d.l[d.d0:d.d0 + n])
    return ST._cand(d, d.d0 + n - 1, hi + d.tick(hi), lo - d.tick(lo), valid_until=d.orb_until_ts)


def _order(c: dict | None, risk_usd: float | None) -> str:
    if not c:
        return "pas de niveau (range incomplet sur IEX)"
    entry, stop = c["entry"], c["stop"]
    tps = " / ".join(_num(t) for t in ST._r_targets(entry, stop))
    until = datetime.fromtimestamp(c["entry_until"], NEW_YORK).strftime("%H h %M").replace(" 00", "")
    qty = CF.shares(entry, stop, risk_usd)
    return (f"ordre stop d'achat {_num(entry)} · stop {_num(stop)} ({_fr((stop / entry - 1) * 100)} %) · "
            f"TP {tps} (1/2/3 R) · valable jusqu'à {until} NY" + (f" · {qty} actions pour ce risque" if qty else ""))


async def confirmed(kind: str, now: datetime | None = None, risk_usd: float | None = None) -> dict:
    """Liste confirmée au gap d'ouverture et niveaux des essais (`kind` : « confirmee » à 9 h 46, « confirmee_30 » à
    10 h 01, Rebond seulement)."""
    now = now or datetime.now(timezone.utc)
    state = _read_state()
    day = now.astimezone(NEW_YORK).date().isoformat()
    waiting = [s for v in (state.get("attente") or {}).values() for s in v] if state.get("day") == day else []
    saved = state.get("confirmee") if state.get("day") == day else None
    ranked, pop, daily = await lists(now, at_open=True, extra=waiting + ((saved or {}).get("rebond") or []))
    top = {k: v[:LAB.TOP] for k, v in ranked.items()}
    if kind == "confirmee_30" and saved:  # la liste de 9 h 46, pas celle du screener de 10 h
        top = {"s10": [], "rebond": [s for s in saved.get("rebond", []) if s in pop]}
    wanted = top["rebond"] if kind == "confirmee_30" else [s for v in top.values() for s in v]
    bars = await alpaca.bars(wanted, CO.BASE_TF, days=4, now=now, feed="iex") if wanted else {}
    levels: dict = {}
    for liste, syms in top.items():
        for s in syms:
            if s not in wanted:
                continue
            d = _day_of(bars.get(s) or [], daily.get(s) or [], now)
            out = {}
            if d is not None:
                if kind == "confirmee_30":
                    out["orb30"] = orb_now(d, 30)
                else:
                    out["orb15"] = orb_now(d, 15)
                    if liste == "s10":
                        c, why = ST.killmode_check(d)
                        out["killmode"] = c
                        out["killmode_pourquoi"] = why
            levels[s] = out
    before = state.get("attente") if state.get("day") == day else None
    moves = None
    if before:
        moves = {k: {"entrees": [s for s in top[k] if s not in before.get(k, [])],
                     "sorties": [s for s in before.get(k, []) if s not in top[k]]} for k in top}
    entry = {"kind": kind, "day": day, "at": now.isoformat(timespec="seconds"), "listes": top,
             "gaps": {s: round(pop[s]["gap_pct"], 2) for v in top.values() for s in v},
             "gaps_attente": {s: g for s, g in ((s, _waited_gap(day, s)) for v in top.values() for s in v) if g is not None},
             "niveaux": {s: {k: ({"entry": c["entry"], "stop": c["stop"]} if isinstance(c, dict) else c)
                             for k, c in v.items()} for s, v in levels.items()},
             "mouvements": moves, "examinees": len(pop), "source_range": "iex"}
    journal(entry)
    if kind == "confirmee":
        state = _read_state() if state.get("day") == day else {"day": day, "attente": None, "faits": []}
        state["confirmee"] = top
        _write_state(state)
    return entry | {"mesures": {s: pop[s] for v in top.values() for s in v}, "niveaux_complets": levels,
                    "risk_usd": risk_usd}


def _waited_gap(day: str, symbol: str) -> float | None:
    """Gap de pré-ouverture noté par la liste d'attente du jour (écart pré-ouverture / ouverture)."""
    try:
        lines = Path(JOURNAL_PATH).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("kind") == "attente" and e.get("day") == day:
            return (e.get("gaps") or {}).get(symbol)
    return None


def confirmed_text(c: dict) -> str:
    late = c["kind"] == "confirmee_30"
    head = ("🎯 **Rebond · range de 30 min** (10 h 01 New York)" if late else
            "🎯 **Potentiel +10 · liste confirmée** (gap d'ouverture, 9 h 46 New York)")
    lines = [head + " · mesuré au labo (n° 9), ni GO ni note"]
    for liste, syms in c["listes"].items():
        if late and liste != "rebond":
            continue
        lines.append(f"__{LAB.LISTS[liste]}__")
        if not syms:
            lines.append("rien à l'ouverture")
        for s in syms:
            m, lv = c["mesures"][s], c["niveaux_complets"].get(s, {})
            pre = c["gaps_attente"].get(s)
            lines.append(_line(s, m) + (f" (pré-ouverture {_fr(pre)} %)" if pre is not None else " (absente à 9 h 10)"))
            if late:
                lines.append("range 30 min : " + _order(lv.get("orb30"), c["risk_usd"]))
                continue
            lines.append("range 15 min : " + _order(lv.get("orb15"), c["risk_usd"]))
            if liste == "s10":
                k = lv.get("killmode")
                why = "; ".join(lv.get("killmode_pourquoi") or [])
                lines.append("règle Killmode : " + ("✅ " + _order(k, c["risk_usd"]) if k else f"❌ {why or 'pas de signal'}"))
    if not late and c["listes"]["rebond"]:
        lines.append("-# Rebond, range de 30 min : à 16 h 01.")
    lines.append("-# Range lu sur IEX en temps réel (le labo rejoue le SIP) ; taille pour le risque de JARVIS_RISK_EUR.")
    return "\n".join(lines)


def due(now: datetime) -> str | None:
    """Passage confirmé à faire maintenant (une fois par séance), sinon None."""
    now_ny = now.astimezone(NEW_YORK)
    if not CAL.is_trading_day(now_ny.date()) or not alpaca.configured():
        return None
    state = _read_state()
    day = now_ny.date().isoformat()
    done = state.get("faits", []) if state.get("day") == day else []
    for kind, at in PASSES.items():
        planned = datetime.combine(now_ny.date(), at, NEW_YORK)
        if timedelta(0) <= now_ny - planned < WINDOW and kind not in done:
            return kind
    return None


def mark_done(kind: str, now: datetime) -> None:
    state = _read_state()
    day = now.astimezone(NEW_YORK).date().isoformat()
    if state.get("day") != day:
        state = {"day": day, "attente": None, "faits": []}
    state.setdefault("faits", []).append(kind)
    _write_state(state)
