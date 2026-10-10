"""Rattrapage du labo v2 : les jours traités avant son arrivée n'ont pas de lignes de stratégies.

Le rejeu efface les bougies de 5 min d'un jour une fois traité (collector.prune_intraday) : pour ces
jours, on redemande les bougies à la source (Alpaca, OKX), on les garde EN MÉMOIRE seulement (rien
n'est stocké, le disque ne bouge pas), et on calcule les mêmes lignes qu'en direct :
- les stratégies (strategies.STRATEGIES), sur les mêmes fenêtres ;
- v1 rejouée avec les sorties et tests du labo v2, à partir du plan gardé dans trades (ou
  trades_sealed) : entrée, stop, objectifs, heure de décision. Le prix « dernier cours » de v1 est
  son prix d'entrée (v1 entre toujours au dernier cours connu) ;
- n° 7 : les GO de ce plan (score.go) et, pour les jours faits par le générateur depuis la n° 7, ceux du
  plan du direct (« plancher ») : brut contre confirmé (strategies.go_rows). Avant, le plan du direct
  n'existe pas : il faudrait ~60 séances de bougies de 15 min par action pour le refaire.

Un curseur par marché et par origine (progress strategies.progress_job(marché), clé
rattrapage:<origine>) avance jour par jour jusqu'au premier jour calculé par le rejeu lui-même
(inline_from:<origine>, posé par runner.run_day). Reprenable, idempotent (replace_strat), borné en temps
par passage du worker. La clé dépend de la déclaration en vigueur : une nouvelle déclaration (stratégie
ajoutée) repart du début, et chaque jour est recalculé avec toutes les stratégies.

n° 10 : les actions se mesurent aux prix négociés (bougies de 5 min non ajustées, journalier et plans v1 remis à leur
échelle, comme la n° 9) ; le générateur n'écrit plus leurs lignes, le rattrapage va donc jusqu'au dernier jour traité
(last_day du rejeu ou du direct) dont le journalier est en base. Une valeur dont le gap brut et le gap ajusté
divergent (opération sur titre entre la veille et la séance) est écartée. En crypto, sans frontière posée (rejeu déjà
fini quand la déclaration change), le rattrapage va jusqu'au dernier jour traité.
"""
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone

from app.lab import collector as CO
from app.lab import generator as G
from app.lab import heartbeat
from app.lab import plus10 as P10
from app.lab import simulator as SIM
from app.lab import sources as SRC
from app.lab import strategies as ST
from app.lab.db import LabDB

log = logging.getLogger(__name__)
ORIGINS = ("replay", "live")
STOCK_BATCH = 100
DAILY_LOOKBACK_DAYS = 90  # ≥ strategies.ZONE_DAILY_BARS séances et la MM 20 jours
SPLIT_TOL = 0.05  # n° 10 : en deçà, prix négocié et prix ajusté sont les mêmes (clôture officielle / dernière bougie)


def _start(db: LabDB, market: str, origin: str) -> date | None:
    if origin == "replay":
        start = db.get(f"replay:{market}", "start")
    else:
        start = db.get("lab", "live_start")
    return date.fromisoformat(start) if start else None


def due_days(db: LabDB, market: str, origin: str) -> list[date]:
    """Jours à rattraper, dans l'ordre : du curseur (exclu) au premier jour calculé en ligne (exclu) ; n° 10 : en
    actions, et en crypto sans jour calculé en ligne, jusqu'au dernier jour traité (inclus). En actions, pas au-delà
    du dernier journalier en base : sans la bougie du jour, une opération sur titre du jour même ne se voit pas
    (operation), et le direct ne l'a qu'au passage de la séance suivante."""
    job = ST.progress_job(market)
    inline = None if market == "stock" else db.get(job, f"inline_from:{origin}")
    done = db.get(f"{origin}:{market}", "last_day")
    start = _start(db, market, origin)
    if not (inline or done) or not start:
        return []  # le labo n'a encore traité aucun jour : il n'y a pas de frontière
    cursor = db.get(job, f"rattrapage:{origin}")
    first = date.fromisoformat(cursor) + timedelta(days=1) if cursor else start
    last = date.fromisoformat(inline) - timedelta(days=1) if inline else date.fromisoformat(done)
    if market == "stock":
        known = P10.last_daily(db)
        if known is None:
            return []
        last = min(last, date.fromisoformat(known))
    if first > last:
        return []
    from app.lab.runner import market_days  # import tardif : runner importe generator

    return market_days(market, first, last)


def v1_trades(db: LabDB, origin: str, market: str, day: str) -> dict[str, list[dict]]:
    """Plans v1 du jour par symbole (tables visible et scellée)."""
    out: dict[str, list[dict]] = {}
    for table in ("trades", "trades_sealed"):
        for symbol, planned_at, plan in db.conn.execute(
                f"SELECT symbol, planned_at, plan FROM {table} WHERE origin = ? AND market = ? AND day = ? "
                "ORDER BY planned_at", (origin, market, day)):
            out.setdefault(symbol, []).append({"planned_at": planned_at, "plan": json.loads(plan or "{}")})
    return out


def _v1(market: str, trade: dict, entry_until: int, rank, factor: float = 1.0) -> dict | None:
    """Plan v1 gardé, remis à l'échelle des prix négociés par `factor` (n° 10, actions)."""
    p = trade["plan"]
    if not p.get("entry") or not p.get("stop") or not p.get("targets"):
        return None
    floor = p.get("plancher")  # n° 7 : plan du direct, gardé par le générateur depuis la déclaration n° 7
    entry = p["entry"] * factor
    return {"plan": {"entry": entry, "stop": p["stop"] * factor,
                     "targets": [{"price": t["price"] * factor} for t in p["targets"]]},
            "decided_at": trade["planned_at"], "entry_until": entry_until, "last_price": entry,
            "cost": SIM.costs(market, entry, rank), "go": bool((p.get("score") or {}).get("go")),
            "plancher": {"plan": {"entry": floor["entry"] * factor, "stop": floor["stop"] * factor,
                                  "targets": [{"price": x * factor} for x in floor["targets"]]}, "go": floor["go"]}
            if floor else None}


def _factor(raw: float | None, adjusted: float | None) -> float:
    """Rapport prix négocié / prix ajusté d'un même cours ; 1 sans division survenue depuis (écart ≤ SPLIT_TOL)."""
    if not raw or not adjusted or raw <= 0 or adjusted <= 0 or abs(raw / adjusted - 1) <= SPLIT_TOL:
        return 1.0
    return raw / adjusted


def operation(hist: list[dict], session: list[dict], daily: list[dict], today: list[dict]) -> bool:
    """Opération sur titre entre la veille et la séance : gap brut (bougies non ajustées) et gap ajusté (journalier de
    la base) divergent de plus de plus10.OP_RATIO. Sans l'un des deux, rien n'est écarté."""
    if not hist or not session or not daily or not today or min(hist[-1]["c"], daily[-1]["c"], today[0]["o"]) <= 0:
        return False
    return abs((session[0]["o"] / hist[-1]["c"]) / (today[0]["o"] / daily[-1]["c"]) - 1) > P10.OP_RATIO


def _daily(db: LabDB, market: str, symbol: str, cutoff: int) -> list[dict]:
    return db.get_bars(market, symbol, 1440, cutoff - DAILY_LOOKBACK_DAYS * 86400, cutoff)


async def stock_rows(db: LabDB, origin: str, day: date, transport=None) -> list[dict] | None:
    bounds = CO.session_ts(day)
    universe = db.universe("stock", day.isoformat())
    if not bounds or not universe:
        return None
    v1 = v1_trades(db, origin, "stock", day.isoformat())
    first = CO.session_ts(CO.sessions_back(day, 2))[0]
    cutoff = CO.ny_midnight(day)
    rows: list[dict] = []
    names = [u["symbol"] for u in universe]
    ranks = {u["symbol"]: u.get("rank") for u in universe}
    # Déclaration n° 6 : séance de SPY en 5 min (sortie sur avalement baissier de l'indice).
    index = (await SRC.alpaca_bars([G.INDEX["stock"]], CO.BASE_TF, datetime.fromtimestamp(bounds[0], timezone.utc),
                                   datetime.fromtimestamp(bounds[1], timezone.utc),
                                   transport=transport, adjustment="raw")).get(G.INDEX["stock"])
    for i in range(0, len(names), STOCK_BATCH):
        batch = names[i:i + STOCK_BATCH]
        bars = await SRC.alpaca_bars(batch, CO.BASE_TF, datetime.fromtimestamp(first, timezone.utc),
                                     datetime.fromtimestamp(bounds[1], timezone.utc), transport=transport,
                                     adjustment="raw")
        heartbeat.beat()
        for symbol in batch:
            b5 = CO.regular_hours(bars.get(symbol) or [])
            hist = [b for b in b5 if b["t"] < bounds[0]]
            session = [b for b in b5 if bounds[0] <= b["t"] < bounds[1]]
            if not session:
                continue
            daily = _daily(db, "stock", symbol, cutoff)
            if operation(hist, session, daily, db.get_bars("stock", symbol, 1440, cutoff, cutoff + 86400)):
                log.info("Labo v2 : rattrapage %s %s écarté (opération sur titre)", symbol, day)
                continue
            raw_prev = hist[-1]["c"] if hist else None
            scale = _factor(raw_prev, daily[-1]["c"]) if daily else 1.0
            if scale != 1.0:
                daily = P10.scale(daily, scale)
            trades = v1.get(symbol, [])
            factor = _factor(raw_prev, trades[0]["plan"].get("entry")) if trades else 1.0
            v = next((_v1("stock", t, bounds[1], ranks[symbol], factor) for t in trades), None)
            try:
                rows += ST.stock_day(day, symbol, hist, session, daily, ranks[symbol], v, index, traded=True)
            except Exception as exc:
                log.warning("Labo v2 : rattrapage %s %s ignoré (%s: %s)", symbol, day, type(exc).__name__, exc)
    return rows


async def crypto_rows(db: LabDB, origin: str, day: date, transport=None) -> list[dict] | None:
    universe = db.universe("crypto", day.isoformat())
    if not universe:
        return None
    v1 = v1_trades(db, origin, "crypto", day.isoformat())
    start = CO.utc_midnight(day)
    rows: list[dict] = []
    for u in universe:
        symbol, rank = u["symbol"], u.get("rank")
        bars = await SRC.okx_candles(symbol, CO.BASE_TF, start - 86400, start + 2 * 86400, transport=transport)
        heartbeat.beat()
        if not bars:
            continue
        v1s = [x for t in v1.get(symbol, []) if (x := _v1("crypto", t, t["planned_at"] + ST.CRYPTO_HORIZON, rank))]
        try:
            rows += ST.crypto_day(day, symbol, bars, _daily(db, "crypto", symbol, start), rank, v1s)
        except Exception as exc:
            log.warning("Labo v2 : rattrapage %s %s ignoré (%s: %s)", symbol, day, type(exc).__name__, exc)
    return rows


async def step(db: LabDB, market: str, max_seconds: float, transport=None) -> int:
    """Rattrape des jours (rejeu puis direct) dans le temps donné ; renvoie le nombre de jours faits."""
    started, done = time.monotonic(), 0
    job = ST.progress_job(market)
    for origin in ORIGINS:
        for day in due_days(db, market, origin):
            if done and time.monotonic() - started >= max_seconds:
                return done
            heartbeat.beat(f"labo v2 : rattrapage {market} {day}")
            rows = await (stock_rows if market == "stock" else crypto_rows)(db, origin, day, transport)
            with db.transaction():
                if rows is not None:
                    db.replace_strat(origin, market, day.isoformat(), rows)
                db.set(job, f"rattrapage:{origin}", day.isoformat())
            log.info("Labo v2 : rattrapage %s %s %s, %d lignes", origin, market, day, len(rows or []))
            done += 1
    return done


def remaining(conn, market: str) -> dict:
    """Jours restant à rattraper par origine (lecture seule, pour l'état du labo)."""
    db = LabDB.__new__(LabDB)
    db.conn = conn
    return {o: len(due_days(db, market, o)) for o in ORIGINS}
