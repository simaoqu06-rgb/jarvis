"""Déclaration n° 9 (9 octobre 2026, docs/labo-strategies.md) : scanner « potentiel +10 ».

Le scanner de 15 h 10 cherche des valeurs calmes en tendance ; le labo mesure que l'amplitude depuis l'ouverture vient
du gap (potentiel.py). Deux listes figées, sur toutes les actions de la base (journalier Alpaca ajusté des divisions,
actifs inactifs compris), sans filtre de prix, de liquidité ni de capitalisation (règle de Simão), au gap d'ouverture
contre la clôture de la veille, avec un ATR de Wilder 14 j sur les séances terminées et 15 séances au moins :
- S+10 : gap ≥ +5 % et ATR ≥ 5 %, classées par gap décroissant ;
- Rebond : gap ≤ −5 % et ATR ≥ 5 %, classées par gap croissant (les plus fortes baisses d'abord).
Les 5 premières de chaque liste sont « la liste » ; les rangs 6 et plus servent de comparaison.

Quatre essais, chacun une entrée déjà codée au labo (strategies.strategy_rows, mêmes coûts, sorties et témoin) :
plus10_orb15 et plus10_kill (règle de prix de Killmode) sur S+10, rebond_orb15 et rebond_orb30 sur Rebond. Jugés sur le
top 5 seulement, comme les stratégies du labo v2 (strat_stats._item : IC en t, écart au témoin, robustesse, stress de
3 c, deux moitiés).

Exécution sur les prix réellement négociés : bougies de 5 min SIP **non ajustées** (adjustment=raw), journalier remis
à l'échelle de ces prix (facteur = clôture brute de la veille / clôture ajustée). Quand le gap brut et le gap ajusté
ne concordent pas (écart de plus de OP_RATIO), une opération sur titre a eu lieu entre la veille et le jour même : la
valeur est écartée des trades et comptée à part.

Mesures sans essai, gardées par séance dans progress (job JOB) :
- amplitude depuis l'ouverture (journalier : +8 %, +10 %, −8 %) pour la base, les listes (top 5 et rangs 6+) et les
  filtres actuels de 15 h 10 rejoués au gap d'ouverture (scanner.features et filter_reasons, mêmes réglages) ;
- par tranche de prix de la veille (ce que le minimum de 5 $ retire) ;
- ce qui reste à prendre après la décision (9 h 45, ou 10 h pour orb30), depuis la clôture de la dernière bougie
  connue, et l'heure du plus haut de la séance ;
- données : valeurs éligibles sans bougies de 5 min (par liste, rang et tranche), opérations sur titre, gaps de plus de
  AUDIT_GAP % en valeur absolue, historiques courts, valeurs devenues inactives chez Alpaca ;
- classement : top 5 contre rangs 6+ et contre DRAWS tirages pré-déclarés de 5 valeurs parmi les rangs 6+ du même jour
  (graines fixes, moyenne et dispersion, jamais le meilleur) ;
- concordance du direct : top 5 de la liste confirmée du direct (data/plus10_scans.jsonl) contre celui du labo.
Le bloc scellé n'est ni lu ni calculé.
"""
import json
import logging
import os
import random
import sqlite3
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app.lab import collector as CO
from app.lab import heartbeat
from app.lab import seal as SEAL
from app.lab import sources as SRC
from app.lab import strat_stats as SS
from app.lab import strategies as ST
from app.lab.db import LabDB, db_path
from app.trading import scanner as SC
from app.trading.settings import settings

log = logging.getLogger(__name__)
JOB = "plus10:n9"
INACTIVE_KEY = "plus10:inactifs"
NUMERO = 9
GAP_MIN = 5.0          # en %, gap à l'ouverture (S+10 : ≥ +5 ; Rebond : ≤ −5)
ATR_MIN = 5.0          # ATR de Wilder 14 j de la veille, en % de sa clôture
TOP = 5
MIN_SESSIONS = ST.ATR_LEN + 1
SHORT_HISTORY = 30     # moins de 30 séances : dans les listes, compté à part
DRAWS = 20             # tirages du témoin de classement
AUDIT_GAP = 30.0       # gaps listés pour l'audit des opérations sur titre
OP_RATIO = 0.25        # écart entre gap brut et gap ajusté (en rapport) au-delà duquel c'est une opération sur titre
ZONE, PLUS10, DOWN = 8.0, 10.0, -8.0
DAILY_LOOKBACK_DAYS = 110  # ~75 séances : MM50 et RSI des filtres de 15 h 10
STOCK_BATCH = 100
LISTS = {"s10": "S+10 (gap ≥ +5 %, ATR ≥ 5 %)", "rebond": "Rebond (gap ≤ −5 %, ATR ≥ 5 %)"}
# Essai → (liste, stratégie du labo, heure de décision en minutes après l'ouverture).
TRIALS = {"plus10_orb15": ("s10", "orb15", 15), "plus10_kill": ("s10", "killmode", 15),
          "rebond_orb15": ("rebond", "orb15", 15), "rebond_orb30": ("rebond", "orb30", 30)}
LABELS = {"plus10_orb15": "S+10, cassure du range d'ouverture 15 min",
          "plus10_kill": "S+10, règle de prix de Killmode",
          "rebond_orb15": "Rebond, cassure du range d'ouverture 15 min",
          "rebond_orb30": "Rebond, cassure du range d'ouverture 30 min"}
PRICE_BUCKETS = ((0.0, 1.0, "< 1 $"), (1.0, 5.0, "1-5 $"), (5.0, 20.0, "5-20 $"), (20.0, 1e18, "≥ 20 $"))
HOUR_BUCKETS = ((0, 15, "avant 9 h 45"), (15, 30, "9 h 45 - 10 h"), (30, 90, "10 h - 11 h"), (90, 10**6, "après 11 h"))
TRADE_KEYS = ("seances", "gap_pct", "atr_pct", "entry", "stop", "signal_at", "triggered", "exit_reason", "tp1_hit",
              "mfe_r", "mae_r", "temoin_r", "temoin_triggered", *SS.COLUMNS)
COLUMNS = ("day", "symbol", "essai", "liste", "rang", *TRADE_KEYS)
SCANS_FILE = "plus10_scans.jsonl"


def scans_path(db_file: str | None = None) -> Path:
    """Journal du direct (app/trading/plus10.py), à côté de la base du labo (même dossier data/)."""
    override = os.getenv("JARVIS_PLUS10_SCANS_PATH")
    return Path(override) if override else Path(db_file or db_path()).parent / SCANS_FILE


def price_bucket(price: float | None) -> str:
    if not price or price <= 0:
        return "prix inconnu"
    return next(label for lo, hi, label in PRICE_BUCKETS if lo <= price < hi)


def ranked(pop: dict[str, dict]) -> dict[str, list[str]]:
    """Les deux listes complètes (tous rangs), dans l'ordre figé par la déclaration ; égalités par symbole."""
    ok = {s: p for s, p in pop.items() if p["atr_pct"] is not None and p["atr_pct"] >= ATR_MIN}
    return {"s10": sorted((s for s, p in ok.items() if p["gap_pct"] >= GAP_MIN), key=lambda s: (-ok[s]["gap_pct"], s)),
            "rebond": sorted((s for s, p in ok.items() if p["gap_pct"] <= -GAP_MIN), key=lambda s: (ok[s]["gap_pct"], s))}


def _scan_filter(before: list[dict], price: float, today: str) -> bool | None:
    """Les filtres actuels du scanner de 15 h 10 (scanner.features et filter_reasons, réglages du moment), au prix de
    l'ouverture ; None si l'historique ne suffit pas (le scanner l'écarte aussi, « historique »)."""
    f = SC.features(before, price, None, today)
    return None if f is None else not SC.filter_reasons(f, settings)


def population(conn: sqlite3.Connection, day: date) -> dict[str, dict] | None:
    """Lecture seule. Chaque action de la base qui a coté le jour `day` avec au moins MIN_SESSIONS séances avant :
    gap d'ouverture, ATR de la veille, clôture de la veille, amplitude du jour depuis l'ouverture et filtres de 15 h 10
    (journalier ajusté des divisions). None si le journalier de la séance n'est pas en base."""
    cutoff = CO.ny_midnight(day)
    series: dict[str, list[dict]] = defaultdict(list)
    for symbol, ts, o, h, lo, c, v in conn.execute(
            "SELECT symbol, ts, o, h, l, c, v FROM bars WHERE market = 'stock' AND tf = 1440 AND ts >= ? AND ts < ? "
            "ORDER BY symbol, ts", (cutoff - DAILY_LOOKBACK_DAYS * 86400, cutoff + 86400)):
        series[symbol].append({"t": ts, "o": o, "h": h, "l": lo, "c": c, "v": v or 0})
    out: dict[str, dict] = {}
    seen_day = False
    key = day.isoformat()
    for symbol, rows in series.items():
        if CO.ny_day(rows[-1]["t"]) != day:
            continue
        seen_day = True
        before, today = rows[:-1], rows[-1]
        if len(before) < MIN_SESSIONS or before[-1]["c"] <= 0 or not today["o"] or today["o"] <= 0:
            continue
        prev = before[-1]["c"]
        a = ST.atr(before)[-1]
        out[symbol] = {"gap_pct": (today["o"] / prev - 1) * 100, "atr_pct": a / prev * 100 if a else None,
                       "prev_close": prev, "seances": len(before), "open": today["o"],
                       "up_pct": (today["h"] / today["o"] - 1) * 100, "down_pct": (today["l"] / today["o"] - 1) * 100,
                       "scanner": _scan_filter(before, today["o"], key)}
    return out if seen_day else None


def _cell(cells: dict, key: str, p: dict) -> None:
    c = cells.setdefault(key, [0, 0, 0, 0])
    c[0] += 1
    c[1] += p["up_pct"] >= ZONE
    c[2] += p["up_pct"] >= PLUS10
    c[3] += p["down_pct"] <= DOWN


def amplitude(pop: dict[str, dict], lists: dict[str, list[str]]) -> dict:
    """Comptes [n, ≥ +8 %, ≥ +10 %, ≤ −8 %] depuis l'ouverture, par groupe et par tranche de prix de la veille."""
    groups: dict = {}
    prices: dict = {}
    for s, p in pop.items():
        _cell(groups, "base", p)
        _cell(prices.setdefault("base", {}), price_bucket(p["prev_close"]), p)
        if p["scanner"]:
            _cell(groups, "filtres_1510", p)
    for name, syms in lists.items():
        for i, s in enumerate(syms):
            _cell(groups, f"{name}_{'top5' if i < TOP else 'rang6'}", pop[s])
            _cell(prices.setdefault(name, {}), price_bucket(pop[s]["prev_close"]), pop[s])
    return {"groupes": groups, "par_prix": prices}


def remaining(session: list[dict], start_ts: int, minutes: int) -> tuple[float | None, float | None]:
    """Ce qui reste à prendre après la décision (ouverture + `minutes`) : plus haut et plus bas des bougies d'après,
    en % de la clôture de la dernière bougie close à la décision."""
    decision = start_ts + minutes * 60
    known = [b for b in session if b["t"] + ST.BAR <= decision]
    after = [b for b in session if b["t"] >= decision]
    if not known or not after or known[-1]["c"] <= 0:
        return None, None
    ref = known[-1]["c"]
    return (max(b["h"] for b in after) / ref - 1) * 100, (min(b["l"] for b in after) / ref - 1) * 100


def high_bucket(session: list[dict], start_ts: int, low: bool = False) -> str | None:
    """Tranche horaire du plus haut de la séance (du plus bas avec `low`)."""
    if not session:
        return None
    top = min(session, key=lambda b: b["l"]) if low else max(session, key=lambda b: b["h"])
    m = (top["t"] - start_ts) // 60
    return next(label for lo, hi, label in HOUR_BUCKETS if lo <= m < hi)


def first_touch(session: list[dict]) -> str:
    """Lequel de +8 % et de −8 % depuis l'ouverture la séance touche en premier (bougies de 5 min)."""
    if not session or session[0]["o"] <= 0:
        return "aucun"
    up, down = session[0]["o"] * (1 + ZONE / 100), session[0]["o"] * (1 + DOWN / 100)
    for b in session:
        hit_up, hit_down = b["h"] >= up, b["l"] <= down
        if hit_up and hit_down:
            return "même bougie"
        if hit_up:
            return "+8 % d'abord"
        if hit_down:
            return "−8 % d'abord"
    return "aucun"


def scale(daily: list[dict], factor: float) -> list[dict]:
    """Journalier ajusté remis à l'échelle des prix négociés le jour rejoué."""
    return [{"t": r["t"], "o": r["o"] * factor, "h": r["h"] * factor, "l": r["l"] * factor, "c": r["c"] * factor,
             "v": (r.get("v") or 0) / factor} for r in daily]


def symbol_rows(day: date, symbol: str, liste: str, rang: int, p: dict, hist: list[dict], session: list[dict],
                daily: list[dict]) -> tuple[list[dict], str | None]:
    """Lignes des essais de la liste pour une valeur, et l'anomalie qui l'écarte (« operation_titre ») sinon None."""
    raw_prev = hist[-1]["c"] if hist else None
    if not raw_prev or raw_prev <= 0 or not daily or daily[-1]["c"] <= 0:
        return [], "donnees"
    ratio = (session[0]["o"] / raw_prev) / (1 + p["gap_pct"] / 100)
    if abs(ratio - 1) > OP_RATIO:
        return [], "operation_titre"
    bars = CO.regular_hours(hist)[-ST.STOCK_WARMUP_BARS:] + session
    d = ST.Day("stock", day, bars, scale(daily, raw_prev / daily[-1]["c"]))
    gap = ST.kill_context(d)["gap_pct"]
    out = []
    for essai, (lst, strat, _) in TRIALS.items():
        if lst != liste:
            continue
        for r in ST.strategy_rows(d, symbol, None, round(gap, 3) if gap is not None else None, names=[strat]):
            row = {k: r.get(k) for k in TRADE_KEYS} | {"seances": p["seances"], "gap_pct": r.get("gap_open_pct"),
                                                       "triggered": int(bool(r["triggered"]))}
            out.append(row | {"day": day.isoformat(), "symbol": symbol, "essai": essai, "liste": liste, "rang": rang})
    return out, None


def last_daily(db: LabDB) -> str | None:
    last = db.conn.execute("SELECT MAX(ts) FROM bars WHERE market = 'stock' AND tf = 1440").fetchone()[0]
    return CO.ny_day(last).isoformat() if last is not None else None


def ready_days(db: LabDB, limit: int = 30) -> list[date]:
    """Séances de l'univers du labo, hors bloc scellé, pas encore calculées, dont le journalier est en base, la plus
    ancienne d'abord."""
    done = db.keys(JOB)
    known = last_daily(db)
    if known is None:
        return []
    return [date.fromisoformat(d) for (d,) in db.conn.execute(
        "SELECT DISTINCT day FROM universe WHERE market = 'stock' AND day <= ? ORDER BY day", (known,))
        if d not in done and not SEAL.sealed("replay", d)][:limit]


async def inactive_symbols(db: LabDB, today: date, transport=None) -> set[str]:
    """Actions inactives chez Alpaca (lues une fois par jour) : la part des valeurs des listes devenues inactives."""
    cached = db.get("assets", INACTIVE_KEY)
    if cached and cached.get("day") == today.isoformat():
        return set(cached["symbols"])
    try:
        rows = await SRC.with_retry(SRC.alpaca.assets, "inactive", limiter=SRC.ALPACA_LIMIT, transport=transport)
    except Exception as exc:  # noqa: BLE001 (mesure annexe : la séance se calcule quand même)
        log.warning("Labo : actifs inactifs Alpaca indisponibles pour la n° 9 (%s)", exc)
        return set((cached or {}).get("symbols") or [])
    symbols = sorted({a["symbol"] for a in rows if a.get("symbol")})
    db.set("assets", INACTIVE_KEY, {"day": today.isoformat(), "symbols": symbols})
    return set(symbols)


async def day_rows(db: LabDB, day: date, inactive: set[str], transport=None) -> tuple[list[dict], dict] | None:
    """Lignes et mesures d'une séance ; None si le journalier de la séance manque."""
    pop = population(db.conn, day)
    if pop is None:
        return None
    lists = ranked(pop)
    stats = {"population": len(pop), "listes": {k: v for k, v in lists.items()}, **amplitude(pop, lists),
             "reste": {}, "heure_haut": {}, "heure_bas": {}, "premier": {}, "manquantes": {}, "operations": [], "audit": [],
             "courts": {k: sum(pop[s]["seances"] < SHORT_HISTORY for s in v) for k, v in lists.items()},
             "inactives": {k: {"top5": sum(s in inactive for s in v[:TOP]), "rang6": sum(s in inactive for s in v[TOP:])}
                           for k, v in lists.items()}}
    stats["audit"] = sorted(([s, round(p["gap_pct"], 1)] for s, p in pop.items() if abs(p["gap_pct"]) >= AUDIT_GAP),
                            key=lambda x: -abs(x[1]))[:30]
    names = sorted({s for v in lists.values() for s in v})
    bounds = CO.session_ts(day)
    if not bounds or not names:
        return [], stats
    first = CO.session_ts(CO.sessions_back(day, 2))[0]
    cutoff = CO.ny_midnight(day)
    rows: list[dict] = []
    position = {(liste, s): i + 1 for liste, v in lists.items() for i, s in enumerate(v)}
    for i in range(0, len(names), STOCK_BATCH):
        batch = names[i:i + STOCK_BATCH]
        bars = await SRC.alpaca_bars(batch, CO.BASE_TF, datetime.fromtimestamp(first, timezone.utc),
                                     datetime.fromtimestamp(bounds[1], timezone.utc), transport=transport,
                                     adjustment="raw")
        heartbeat.beat()
        for symbol in batch:
            b5 = CO.regular_hours(bars.get(symbol) or [])
            session = [b for b in b5 if bounds[0] <= b["t"] < bounds[1]]
            hist = [b for b in b5 if b["t"] < bounds[0]]
            daily = db.get_bars("stock", symbol, 1440, cutoff - DAILY_LOOKBACK_DAYS * 86400, cutoff)
            for liste in LISTS:
                rang = position.get((liste, symbol))
                if rang is None:
                    continue
                part = "top5" if rang <= TOP else "rang6"
                tranche = price_bucket(pop[symbol]["prev_close"])
                if not session or session[0]["t"] != bounds[0]:
                    cell = stats["manquantes"].setdefault(liste, {}).setdefault(part, {})
                    cell[tranche] = cell.get(tranche, 0) + 1
                    continue
                for minutes in sorted({m for lst, _, m in TRIALS.values() if lst == liste}):
                    up, down = remaining(session, bounds[0], minutes)
                    if up is not None:
                        c = stats["reste"].setdefault(f"{liste}_{part}_{minutes}", [0, 0, 0, 0])
                        c[0] += 1
                        c[1] += up >= ZONE
                        c[2] += up >= PLUS10
                        c[3] += down <= DOWN
                for key, bucket in (("heure_haut", high_bucket(session, bounds[0])),
                                    ("heure_bas", high_bucket(session, bounds[0], low=True)),
                                    ("premier", first_touch(session))):
                    h = stats[key].setdefault(f"{liste}_{part}", {})
                    h[bucket] = h.get(bucket, 0) + 1
                try:
                    got, anomaly = symbol_rows(day, symbol, liste, rang, pop[symbol], hist, session, daily)
                except Exception as exc:  # noqa: BLE001 (comme le rattrapage : une action ne bloque pas la séance)
                    log.warning("Labo : n° 9 %s %s ignoré (%s: %s)", symbol, day, type(exc).__name__, exc)
                    continue
                if anomaly == "operation_titre":
                    stats["operations"].append(symbol)
                elif anomaly:
                    cell = stats["manquantes"].setdefault(liste, {}).setdefault(part, {})
                    cell[tranche] = cell.get(tranche, 0) + 1
                rows += got
    return rows, stats


async def step(db: LabDB, now: datetime, max_seconds: float, transport=None) -> int:
    """Worker : calcule des séances dans le temps donné ; renvoie le nombre de séances faites."""
    if max_seconds <= 0:
        return 0
    days = ready_days(db)
    if not days:
        return 0
    inactive = await inactive_symbols(db, now.date(), transport)
    started, done = time.monotonic(), 0
    for day in days:
        if done and time.monotonic() - started >= max_seconds:
            break
        heartbeat.beat(f"labo : potentiel +10 (n° 9) {day}")
        got = await day_rows(db, day, inactive, transport)
        if got is None:  # journalier de la séance absent : noté vide s'il ne viendra plus (séance déjà dépassée)
            if day.isoformat() < (last_daily(db) or ""):
                db.set(JOB, day.isoformat(), {"absente": True})
            continue
        rows, stats = got
        with db.transaction():
            db.conn.execute("DELETE FROM plus10_trades WHERE day = ?", (day.isoformat(),))
            db.conn.executemany(f"INSERT INTO plus10_trades ({', '.join(COLUMNS)}) VALUES "
                                f"({', '.join('?' for _ in COLUMNS)})", [tuple(r.get(c) for c in COLUMNS) for r in rows])
            db.set(JOB, day.isoformat(), stats)
        log.info("Labo : n° 9 %s, %d S+10, %d Rebond, %d lignes", day, len(stats["listes"]["s10"]),
                 len(stats["listes"]["rebond"]), len(rows))
        done += 1
    return done


# --- Tableau (lecture seule) -----------------------------------------------------------------------

def _pct(cell: list[int] | None) -> dict | None:
    if not cell or not cell[0]:
        return None
    n = cell[0]
    return {"n": n, "plus8_pct": round(100 * cell[1] / n, 1), "plus10_pct": round(100 * cell[2] / n, 1),
            "moins8_pct": round(100 * cell[3] / n, 1)}


def _add(total: dict, part: dict) -> None:
    for k, c in part.items():
        t = total.setdefault(k, [0, 0, 0, 0])
        for i, x in enumerate(c):
            t[i] += x


def _ref(values: dict, entry, stop) -> float | None:
    """Référence des essais : r_net moins la commission IBKR, comme les stratégies d'actions du labo v2."""
    if values["r_net"] is None:
        return None
    return values["r_net"] - (SS.ibkr_commission_r(entry, stop) or 0.0)


def _feed_row(a: dict, day: str, values: dict, entry, stop, tp1, wit, wit_trig) -> None:
    comm = SS.ibkr_commission_r(entry, stop) or 0.0
    ref = _ref(values, entry, stop)
    SS._feed(a, {"market": "stock", "day": day, "tp1": tp1, "rank": None, "values": values, "label": "n° 9",
                 "ref": ref, "refc": "r_net", "comm": comm,
                 "frais": SS._cost_bucket(SS.expected_cost_r("stock", entry, stop, None)),
                 "ecarts": {}, "vwap": None, "bandes": {}, "temoin": wit,
                 "ecart_temoin": values["r_net"] - wit if wit_trig and wit is not None and values["r_net"] is not None
                 else None})


def draws(day: str, liste: str, pool: list[str], k: int) -> list[str]:
    """Tirage n° k de 5 valeurs parmi les rangs 6+ d'une séance (graine fixe : séance, liste, tirage)."""
    rng = ST._seed("plus10", day, liste, k)
    return rng.sample(pool, min(TOP, len(pool))) if pool else []


def _ranking(trades: dict, days: dict[str, dict], essai: str) -> dict:
    """Top 5 contre rangs 6+ (par trade) et contre DRAWS tirages de 5 valeurs des rangs 6+ (par trade et par jour,
    écart apparié par jour)."""
    liste = TRIALS[essai][0]
    top, rest = SS.Mean(), SS.Mean()
    per_draw = []
    for k in range(DRAWS):
        trade_mean, gap_day = SS.Mean(), SS.Mean()
        for day, stats in days.items():
            ranked_syms = stats["listes"][liste]
            tops = [trades.get((day, s, essai)) for s in ranked_syms[:TOP]]
            picked = [trades.get((day, s, essai)) for s in draws(day, liste, ranked_syms[TOP:], k)]
            tops = [x for x in tops if x is not None]
            picked = [x for x in picked if x is not None]
            for x in picked:
                trade_mean.add(day, x)
            if tops and picked:
                gap_day.add(day, sum(tops) - sum(picked))
        per_draw.append((trade_mean.result()["esperance_r"], gap_day.result()))
    for (day, symbol, e), x in trades.items():
        if e != essai:
            continue
        rang = days[day]["listes"][liste].index(symbol) + 1 if symbol in days[day]["listes"][liste] else None
        (top if rang and rang <= TOP else rest).add(day, x)
    means = [m for m, _ in per_draw if m is not None]
    gaps = [g["esperance_r"] for _, g in per_draw if g["esperance_r"] is not None]
    return {"top5": top.result(), "rang6": rest.result(),
            "tirages": {"n": DRAWS, "esperance_moyenne_r": round(sum(means) / len(means), 4) if means else None,
                        "esperance_min_r": min(means) if means else None, "esperance_max_r": max(means) if means else None,
                        "ecart_jour_moyen_r": round(sum(gaps) / len(gaps), 4) if gaps else None,
                        "ecart_jour_min_r": min(gaps) if gaps else None, "ecart_jour_max_r": max(gaps) if gaps else None,
                        "jours_compares": max((g["jours"] for _, g in per_draw), default=0)}}


def _concordance(days: dict[str, dict], path: Path | None = None) -> dict:
    """Top 5 de la liste confirmée du direct (gap d'ouverture, population du screener) contre celui du labo."""
    path = path or scans_path()
    hits = total = seen = 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    except OSError:
        lines = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("kind") != "confirmee" or entry.get("day") not in days:
            continue
        seen += 1
        for liste in LISTS:
            live = set((entry.get("listes") or {}).get(liste) or [])
            lab = set(days[entry["day"]]["listes"][liste][:TOP])
            if lab:
                total += len(lab)
                hits += len(lab & live)
    return {"seances": seen, "valeurs_du_labo": total, "retrouvees": hits,
            "concordance_pct": round(100 * hits / total, 1) if total else None}


def report(conn: sqlite3.Connection, trials: int, scans: Path | None = None) -> dict | None:
    """Mesures de la déclaration n° 9 ; None avant la première séance calculée."""
    try:
        days = {k: v for k, v in ((k, json.loads(v)) for k, v in conn.execute(
            "SELECT key, value FROM progress WHERE job = ? ORDER BY key", (JOB,))) if not v.get("absente")}
        rows = conn.execute(f"SELECT {', '.join(COLUMNS)} FROM plus10_trades ORDER BY day").fetchall()
    except sqlite3.Error:
        return None
    if not days:
        return None
    acc = {e: SS._acc() for e in TRIALS}
    short = {e: SS.Mean() for e in TRIALS}
    excursions = {e: {"mfe_r": SS.Mean(), "mae_r": SS.Mean()} for e in TRIALS}
    prices = {e: defaultdict(lambda: {c: SS.Mean() for c in ("r_net", "r_3c", "r_20e")}) for e in TRIALS}
    trades: dict = {}
    for row in rows:
        r = dict(zip(COLUMNS, row))
        day, essai = r["day"], r["essai"]
        if essai not in TRIALS or day not in days:
            continue
        values = {c: r[c] for c in SS.COLUMNS}
        top = r["rang"] <= TOP
        if top:
            acc[essai]["signaux"] += 1
        if not r["triggered"] or values["r_net"] is None:
            continue
        ref = _ref(values, r["entry"], r["stop"])
        trades[(day, r["symbol"], essai)] = ref
        if not top:
            continue
        _feed_row(acc[essai], day, values, r["entry"], r["stop"], r["tp1_hit"], r["temoin_r"], r["temoin_triggered"])
        for c, m in excursions[essai].items():
            m.add(day, r[c])
        if r["seances"] is not None and r["seances"] < SHORT_HISTORY:
            short[essai].add(day, ref)
        cell = prices[essai][price_bucket(r["entry"])]
        for c in cell:
            cell[c].add(day, values[c])
    essais = {}
    for e, a in acc.items():
        item = SS._item("stock", e, a, trials)
        item["nom"] = LABELS[e]
        essais[e] = item | {"classement": _ranking(trades, days, e), "historique_court": short[e].result(),
                            "excursions": {c: m.result() for c, m in excursions[e].items()},
                            "par_prix": {k: {c: m.result() for c, m in v.items()}
                                         for k, v in sorted(prices[e].items())}}
    groups, by_price, reste = {}, defaultdict(dict), {}
    hours = {k: defaultdict(lambda: defaultdict(int)) for k in ("heure_haut", "heure_bas", "premier")}
    missing: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    ops = courts = 0
    inactive = defaultdict(lambda: defaultdict(int))
    audit = []
    for day, s in days.items():
        _add(groups, s["groupes"])
        for k, v in s["par_prix"].items():
            _add(by_price[k], v)
        _add(reste, s["reste"])
        for family, acc_h in hours.items():
            for k, v in s.get(family, {}).items():
                for b, n in v.items():
                    acc_h[k][b] += n
        for liste, parts in s["manquantes"].items():
            for part, cells in parts.items():
                for tranche, n in cells.items():
                    missing[liste][part][tranche] += n
        ops += len(s["operations"])
        courts += sum(s["courts"].values())
        for liste, parts in s["inactives"].items():
            for part, n in parts.items():
                inactive[liste][part] += n
        audit += [[day, *x] for x in s["audit"]]
    keys = sorted(days)
    return {
        "numero": NUMERO, "seances": len(days), "du": keys[0], "au": keys[-1], "essais": essais,
        "amplitude": {k: _pct(v) for k, v in groups.items()},
        "amplitude_par_prix": {k: {t: _pct(c) for t, c in v.items()} for k, v in by_price.items()},
        "reste_apres_decision": {k: _pct(v) for k, v in sorted(reste.items())},
        "heure_du_plus_haut": {k: dict(v) for k, v in hours["heure_haut"].items()},
        "heure_du_plus_bas": {k: dict(v) for k, v in hours["heure_bas"].items()},
        "premier_touche": {k: dict(v) for k, v in hours["premier"].items()},
        "donnees": {"manquantes": {k: {p: dict(c) for p, c in v.items()} for k, v in missing.items()},
                    "operations_sur_titre": ops, "historiques_courts": courts,
                    "devenues_inactives": {k: dict(v) for k, v in inactive.items()},
                    "audit_gaps": sorted(audit, key=lambda x: -abs(x[2]))[:20]},
        "concordance_direct": _concordance(days, scans),
    }


def _amp(x: dict | None) -> str:
    return "n/d" if not x else f"{x['plus8_pct']:g} %".replace(".", ",")


def text(rep: dict | None) -> str | None:
    """Lignes de !strategies."""
    if not rep:
        return None
    lines = [f"🎯 Potentiel +10 (n° {rep['numero']}, {rep['seances']} séances {rep['du']} → {rep['au']}) :"]
    for e, it in rep["essais"].items():
        flag = "✅ " if it["candidate"] else ""
        ci = f" [{SS._r(it['ic95_ibkr'][0])} ; {SS._r(it['ic95_ibkr'][1])}]" if it.get("ic95_ibkr") else ""
        tir = it["classement"]["tirages"]
        lines.append(f"- {flag}{it['nom']} : {SS._r(it['esperance_ref_r'])} R{ci} · {it['declenches']} trades · "
                     f"témoin z {SS._z(it['temoin']['z'])} (seuil {SS._z(it['seuil_t'])}) · stress 3 c "
                     f"{SS._r(it['robuste'].get('r_3c'))} R · rangs 6+ {SS._r(it['classement']['rang6']['esperance_r'])} R"
                     f" · 5 au hasard {SS._r(tir['esperance_moyenne_r'])} R")
    a = rep["amplitude"]
    lines.append(f"- touchent +8 % depuis l'ouverture : base {_amp(a.get('base'))}, filtres de 15 h 10 "
                 f"{_amp(a.get('filtres_1510'))}, S+10 top 5 {_amp(a.get('s10_top5'))}, Rebond top 5 "
                 f"{_amp(a.get('rebond_top5'))}")
    r = rep["reste_apres_decision"]
    lines.append(f"- encore +8 % après la décision : S+10 top 5 {_amp(r.get('s10_top5_15'))}, Rebond top 5 "
                 f"{_amp(r.get('rebond_top5_15'))} (9 h 45) / {_amp(r.get('rebond_top5_30'))} (10 h)")
    c = rep["concordance_direct"]
    if c["seances"]:
        lines.append(f"- direct : {c['concordance_pct']:g} % du top 5 du labo retrouvé ({c['seances']} séances)"
                     .replace(".", ","))
    return "\n".join(lines)
