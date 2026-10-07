"""Collecte des bougies du labo et agrégation à la volée.

On ne stocke que des bougies de 5 min (tf = 5) et des bougies journalières de la source (tf = 1440 :
Alpaca 1Day pour les actions, OKX 1Dutc pour la crypto). Les bougies de 15 min, 1 h et 4 h se
calculent ici, au format attendu par app/trading/plan.py : {t, o, h, l, c, v}, t = début de la
bougie en secondes UTC, de la plus ancienne à la plus récente.

Actions : séance régulière seulement (9 h 30 - 16 h ou 13 h les jours courts, calendrier NYSE),
comme Twelve Data en direct ; bougies de 15 min / 1 h / 4 h alignées sur l'ouverture (4 h :
9 h 30 - 13 h 30 puis 13 h 30 - clôture), journalières datées de minuit à New York.
Crypto : 24 h/24, bougies alignées sur l'heure UTC, journée UTC.

Historique gardé avant chaque jour rejoué : 120 séances de 5 min pour les actions (délais rejoués,
EMA 200 en 4 h), 40 jours pour la crypto (les 720 bougies d'1 h que Kraken donne en direct),
et ~450 jours de bougies journalières (MM 200 jours, univers).
"""
import logging
import re
from bisect import bisect_left
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache

from app.core.http import SourceError
from app.lab import heartbeat
from app.lab import sources as SRC
from app.lab.db import LabDB
from app.trading import calendar as CAL

log = logging.getLogger(__name__)
NEW_YORK = CAL.NEW_YORK
BASE_TF = 5
STOCK_HISTORY_SESSIONS = 120
CRYPTO_HISTORY_DAYS = 40
DAILY_HISTORY_DAYS = 450
STOCK_EXCHANGES = {"NYSE", "NASDAQ", "AMEX", "ARCA", "BATS"}
STOCK_TICKER = re.compile(r"^[A-Z]{1,5}$")
STOCK_BATCH = 100         # symboles par requête Alpaca (5 min, une séance : ~7 800 bougies, une page)
DAILY_BATCH = 100         # symboles par requête Alpaca (journalier)
OVERLAP_DAYS = 7          # jours déjà stockés relus en prolongeant le journalier (opérations sur titre)
SPLIT_TOLERANCE = 0.01    # écart de clôture au-delà duquel l'historique stocké est rechargé
ASSETS_MAX_AGE_DAYS = 7


# --- Calendrier en epoch ------------------------------------------------------------------

@lru_cache(maxsize=8192)
def session_ts(day: date) -> tuple[int, int] | None:
    """Ouverture et clôture de la séance (epoch s), None si la Bourse est fermée."""
    bounds = CAL.session_bounds(day)
    return (int(bounds[0].timestamp()), int(bounds[1].timestamp())) if bounds else None


def ny_midnight(day: date) -> int:
    return int(datetime.combine(day, time(0, 0), NEW_YORK).timestamp())


def utc_midnight(day: date) -> int:
    return int(datetime.combine(day, time(0, 0), timezone.utc).timestamp())


def ny_day(ts: int) -> date:
    return datetime.fromtimestamp(ts, NEW_YORK).date()


def utc_day(ts: int) -> date:
    return datetime.fromtimestamp(ts, timezone.utc).date()


def sessions_back(day: date, n: int) -> date:
    """n-ième séance avant `day` (non comprise)."""
    d = day
    for _ in range(n):
        d -= timedelta(days=1)
        while not CAL.is_trading_day(d):
            d -= timedelta(days=1)
    return d


def previous_trading_day(day: date) -> date:
    return sessions_back(day, 1)


def trading_days(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if CAL.is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


# --- Agrégation ---------------------------------------------------------------------------

def _new(bar: dict, t: int) -> dict:
    return {"t": t, "o": bar["o"], "h": bar["h"], "l": bar["l"], "c": bar["c"], "v": bar["v"]}


def _merge(cur: dict, bar: dict) -> None:
    if bar["h"] > cur["h"]:
        cur["h"] = bar["h"]
    if bar["l"] < cur["l"]:
        cur["l"] = bar["l"]
    cur["c"] = bar["c"]
    cur["v"] += bar["v"]


def aggregate(bars: list[dict], tf: int, market: str) -> list[dict]:
    """Regroupe des bougies de 5 min (triées) en bougies de `tf` minutes (1440 = journalière).

    Actions : les bougies hors séance régulière sont ignorées ; chaque bougie agrégée commence à
    l'ouverture + k × tf (la dernière de la séance peut être plus courte).
    """
    out: list[dict] = []
    cur, key = None, None
    if market == "crypto":
        step = 86400 if tf >= 1440 else tf * 60
        for b in bars:
            k = b["t"] - b["t"] % step
            if k != key:
                cur, key = _new(b, k), k
                out.append(cur)
            else:
                _merge(cur, b)
        return out
    step = tf * 60
    open_ts = close_ts = midnight = None
    for b in bars:
        t = b["t"]
        if close_ts is None or t >= close_ts:
            day = ny_day(t)
            bounds = session_ts(day)
            if not bounds:
                continue
            open_ts, close_ts = bounds
            midnight = ny_midnight(day)
        if not open_ts <= t < close_ts:
            continue
        k = midnight if tf >= 1440 else open_ts + (t - open_ts) // step * step
        if k != key:
            cur, key = _new(b, k), k
            out.append(cur)
        else:
            _merge(cur, b)
    return out


def regular_hours(bars: list[dict]) -> list[dict]:
    """Bougies de 5 min de la séance régulière NYSE (jours courts compris)."""
    out = []
    for b in bars:
        bounds = session_ts(ny_day(b["t"]))
        if bounds and bounds[0] <= b["t"] and b["t"] + BASE_TF * 60 <= bounds[1]:
            out.append(b)
    return out


def known_before(bars: list[dict], tf: int, moment: int) -> list[dict]:
    """Bougies closes à `moment` (fin de bougie ≤ moment) ; les bougies sont triées."""
    starts = [b["t"] for b in bars]
    return bars[:bisect_left(starts, moment - tf * 60 + 1)]


def in_progress(bars5: list[dict], start: int, moment: int, last_close: float) -> dict:
    """Bougie en cours à `moment` (commencée à `start`) faite des bougies de 5 min déjà closes ;
    vide (prix plat, volume nul) si rien n'a encore clos, comme une bougie Kraken qui vient de s'ouvrir."""
    part = [b for b in bars5 if start <= b["t"] and b["t"] + BASE_TF * 60 <= moment]
    if not part:
        return {"t": start, "o": last_close, "h": last_close, "l": last_close, "c": last_close, "v": 0.0}
    cur = _new(part[0], start)
    for b in part[1:]:
        _merge(cur, b)
    return cur


# --- Univers de symboles ------------------------------------------------------------------

def stock_symbols(assets: list[dict]) -> list[str]:
    """Actions des grandes places US, tickers simples ; aucune exclusion par taille ni liquidité.
    Les ETF ne sont écartés que si Alpaca les signale (attribut « etf »)."""
    out = set()
    for a in assets:
        if a.get("class", "us_equity") != "us_equity" or a.get("exchange") not in STOCK_EXCHANGES:
            continue
        if a.get("status", "active") == "active" and not a.get("tradable", True):
            continue
        if any(str(x).lower() == "etf" for x in a.get("attributes") or []):
            continue
        if STOCK_TICKER.match(a.get("symbol", "")):
            out.add(a["symbol"])
    return sorted(out)


STABLES = {"USDT", "USDC", "DAI", "TUSD", "FDUSD", "USDE", "PYUSD", "USDD", "USDP", "BUSD", "USDG", "RLUSD",
           "EURT", "EURC", "USDS", "GUSD", "LUSD", "FRAX", "SUSD", "CUSD", "USDK", "UST"}
WRAPPED = {"WBTC", "WETH", "STETH", "WSTETH", "WBETH", "CBBTC", "CBETH", "BETH", "WEETH", "RETH"}
LEVERAGED = re.compile(r"\d+[LS]$")


def crypto_pairs(instruments: list[dict]) -> list[str]:
    """Paires USDT au comptant, sans stablecoin contre stablecoin, jeton enveloppé ou à levier."""
    out = []
    for i in instruments:
        base = (i.get("base") or "").upper()
        if i.get("quote") != "USDT" or i.get("state", "live") != "live":
            continue
        if base in STABLES or base in WRAPPED or LEVERAGED.search(base):
            continue
        out.append(i["inst_id"])
    return sorted(out)


# --- Collecte (réseau) ----------------------------------------------------------------------

async def stock_universe_symbols(db: LabDB, today: date, transport=None) -> list[str]:
    cached = db.get("assets", "stock")
    if cached and (today - date.fromisoformat(cached["day"])).days < ASSETS_MAX_AGE_DAYS:
        return cached["symbols"]
    symbols = stock_symbols(await SRC.alpaca_assets(transport=transport))
    db.set("assets", "stock", {"day": today.isoformat(), "symbols": symbols})
    return symbols


async def crypto_universe_symbols(db: LabDB, today: date, transport=None) -> list[str]:
    cached = db.get("assets", "crypto")
    if cached and (today - date.fromisoformat(cached["day"])).days < ASSETS_MAX_AGE_DAYS:
        return cached["symbols"]
    pairs = crypto_pairs(await SRC.okx_instruments(transport=transport))
    db.set("assets", "crypto", {"day": today.isoformat(), "symbols": pairs})
    return pairs


def _missing(cov: list | None, start: int, end: int) -> list[tuple[int, int]]:
    if end <= start:
        return []
    if not cov:
        return [(start, end)]
    out = []
    if start < cov[0]:
        out.append((start, cov[0]))
    if end > cov[1]:
        out.append((cov[1], end))
    return out


def _extend(cov: list | None, start: int, end: int) -> list[int]:
    return [min(start, cov[0]), max(end, cov[1])] if cov else [start, end]


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def split_detected(stored: list[dict], fetched: list[dict]) -> bool:
    """Opération sur titre (division, regroupement…) : une même bougie déjà stockée et relue
    aujourd'hui diffère de plus de SPLIT_TOLERANCE. Les prix « ajustés des divisions » le sont à la
    date de lecture : sans rechargement, l'historique et les bougies neuves ne se raccordent plus."""
    fresh = {b["t"]: b["c"] for b in fetched}
    for b in stored:
        c = fresh.get(b["t"])
        if c and b["c"] and abs(c / b["c"] - 1) > SPLIT_TOLERANCE:
            return True
    return False


async def _fetch_daily(market: str, symbols: list[str], a: int, b: int, transport=None) -> dict[str, list[dict]]:
    if market == "stock":
        return await SRC.alpaca_bars(symbols, 1440, datetime.fromtimestamp(a, timezone.utc),
                                     datetime.fromtimestamp(b, timezone.utc), transport=transport)
    return {symbols[0]: await SRC.okx_candles(symbols[0], 1440, a, b, transport=transport)}


async def ensure_daily(db: LabDB, market: str, symbols: list[str], first: date, last: date,
                       now: datetime | None = None, transport=None, _reload: bool = False) -> int:
    """Bougies journalières de chaque symbole entre deux dates, couverture suivie symbole par symbole :
    un symbole en échec ou nouveau dans la liste est complété au passage suivant.

    En prolongeant un historique, on relit aussi les OVERLAP_DAYS jours déjà stockés : s'ils ont changé
    (division d'actions…), tout l'historique du symbole (journalier et 5 min) est effacé et rechargé.
    """
    now = now or datetime.now(timezone.utc)
    midnight = ny_midnight if market == "stock" else utc_midnight
    start, end = midnight(first), min(midnight(last + timedelta(days=1)), int(now.timestamp()))
    job = f"covD:{market}"
    groups: dict[tuple[int, int, bool], list[str]] = {}
    for symbol in symbols:
        cov = db.get(job, symbol)
        for a, b in _missing(cov, start, end):
            groups.setdefault((a, b, bool(cov) and a == cov[1]), []).append(symbol)
    stored, reload = 0, []
    for (a, b, forward), group in groups.items():
        fetch_from = a - OVERLAP_DAYS * 86400 if forward else a
        for batch in _chunks(group, DAILY_BATCH if market == "stock" else 1):
            try:
                rows = await _fetch_daily(market, batch, fetch_from, b, transport)
            except SourceError as exc:
                log.warning("Labo : journalier %s indisponible (%s), repris au prochain passage",
                            ", ".join(batch[:3]) + ("…" if len(batch) > 3 else ""), exc)
                continue
            with db.transaction():
                for symbol in batch:
                    bars = rows.get(symbol) or []
                    if forward and split_detected(db.get_bars(market, symbol, 1440, fetch_from, a), bars):
                        reload.append(symbol)
                        continue
                    stored += db.put_bars(market, symbol, 1440, [r for r in bars if a <= r["t"] < b])
                    db.set(job, symbol, _extend(db.get(job, symbol), a, b))
            heartbeat.beat()
    if reload and not _reload:
        log.warning("Labo : opération sur titre détectée (%s) : historique rechargé", ", ".join(reload))
        for symbol in reload:
            db.delete_symbol(market, symbol)
        stored += await ensure_daily(db, market, reload, first, last, now, transport, _reload=True)
    return stored


def session_windows(a: int, b: int) -> list[tuple[int, int]]:
    """Séances régulières (ouverture, clôture) qui recoupent [a, b[, bornées à [a, b[."""
    out = []
    for day in trading_days(ny_day(a), ny_day(max(a, b - 1))):
        o, c = session_ts(day)
        lo, hi = max(o, a), min(c, b)
        if lo < hi:
            out.append((lo, hi))
    return out


async def ensure_intraday(db: LabDB, market: str, symbols: list[str], start: int, end: int,
                          now: datetime | None = None, transport=None) -> int:
    """Bougies de 5 min de chaque symbole sur [start, end[ ; ne redemande que ce qui manque.

    Actions : une requête par séance régulière et par lot de STOCK_BATCH symboles (jamais la
    pré-ouverture ni l'après-séance, et pas plus d'une séance de données en mémoire par lot).
    Un symbole dont une requête échoue garde sa couverture : il sera complété au passage suivant.
    """
    now = now or datetime.now(timezone.utc)
    cap = int((now - SRC.alpaca.SIP_DELAY).timestamp()) if market == "stock" else int(now.timestamp())
    end = min(end, cap - cap % (BASE_TF * 60))
    job = f"cov5:{market}"
    # Symbole de retour dans l'univers après une absence : on repart de `start` au lieu de combler
    # tout le trou depuis sa dernière couverture (des mois de bougies inutiles) ; les anciennes
    # bougies, d'avant `start`, sont effacées pour ne pas rester hors couverture.
    base = {}
    for s in symbols:
        cov = db.get(job, s)
        if cov and cov[1] < start:
            with db.transaction():
                db.conn.execute("DELETE FROM bars WHERE market = ? AND symbol = ? AND tf = ? AND ts < ?",
                                (market, s, BASE_TF, start))
                db.conn.execute("DELETE FROM progress WHERE job = ? AND key = ?", (job, s))
            cov = None
        base[s] = cov
    wanted = {s: _missing(base[s], start, end) for s in symbols}
    wanted = {s: r for s, r in wanted.items() if r}
    stored, failed = 0, set()
    if market == "stock":
        by_session: dict[tuple[int, int], list[str]] = {}
        for symbol, ranges in wanted.items():
            for a, b in ranges:
                for win in session_windows(a, b):
                    by_session.setdefault(win, []).append(symbol)
        for (a, b), group in sorted(by_session.items()):
            for batch in _chunks(group, STOCK_BATCH):
                try:
                    rows = await SRC.alpaca_bars(batch, BASE_TF, datetime.fromtimestamp(a, timezone.utc),
                                                 datetime.fromtimestamp(b, timezone.utc), transport=transport)
                except SourceError as exc:
                    log.warning("Labo : 5 min %s indisponibles (%s), repris au prochain passage",
                                datetime.fromtimestamp(a, NEW_YORK).date(), exc)
                    failed.update(batch)
                    continue
                with db.transaction():
                    for symbol in batch:
                        bars = regular_hours([r for r in rows.get(symbol, []) if a <= r["t"] < b])
                        stored += db.put_bars("stock", symbol, BASE_TF, bars)
                heartbeat.beat()
    else:
        for symbol, ranges in wanted.items():
            for a, b in ranges:
                try:
                    bars = await SRC.okx_candles(symbol, BASE_TF, a, b, transport=transport)
                except SourceError as exc:
                    log.warning("Labo : bougies %s indisponibles (%s), reprises au prochain passage", symbol, exc)
                    failed.add(symbol)
                    continue
                stored += db.put_bars("crypto", symbol, BASE_TF, bars)
                heartbeat.beat()
    with db.transaction():
        for symbol, ranges in wanted.items():
            if symbol in failed:
                continue
            cov = base[symbol]
            for a, b in ranges:
                cov = _extend(cov, a, b)
            db.set(job, symbol, cov)
    return stored


def prune_intraday(db: LabDB, market: str, cutoff: int) -> int:
    """Efface les bougies de 5 min commencées avant `cutoff` et rogne la couverture d'autant.
    Sans cela, le rejeu de 2 ans gardait toutes ses bougies (~50 Mo par jour d'actions rejoué) et
    remplissait le disque du serveur. Renvoie le nombre de bougies effacées."""
    job = f"cov5:{market}"
    removed = 0
    with db.transaction():
        for symbol, cov in db.keys(job).items():
            if not cov or cov[0] >= cutoff:
                continue
            removed += db.conn.execute("DELETE FROM bars WHERE market = ? AND symbol = ? AND tf = ? AND ts < ?",
                                       (market, symbol, BASE_TF, cutoff)).rowcount
            if cov[1] <= cutoff:
                db.conn.execute("DELETE FROM progress WHERE job = ? AND key = ?", (job, symbol))
            else:
                db.set(job, symbol, [cutoff, cov[1]])
    return removed


def stock_window(day: date) -> tuple[int, int]:
    """Bougies de 5 min utiles pour la séance `day` : 120 séances avant, et la séance elle-même."""
    first = sessions_back(day, STOCK_HISTORY_SESSIONS)
    return session_ts(first)[0], session_ts(day)[1]


def crypto_window(day: date) -> tuple[int, int]:
    """40 jours avant le jour UTC `day`, et jusqu'à l'horizon du dernier créneau (20 h + 24 h)."""
    start = utc_midnight(day)
    return start - CRYPTO_HISTORY_DAYS * 86400, start + 2 * 86400
