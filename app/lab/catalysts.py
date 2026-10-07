"""Fiches catalyseurs : chaque gros mouvement de l'univers du jour, avec les actus autour.

1. Mouvements du jour (table day_moves, sans réseau) : pour chaque valeur de l'univers du jour,
   ouverture, plus haut, plus bas, clôture, clôture de la veille, hausse et baisse depuis l'ouverture,
   gap, heure du plus haut et du plus bas, volume relatif des 30 premières minutes.
   Actions : bougies de 5 min de la séance régulière ; crypto : journée UTC (ouverture à 0 h UTC).
2. Fiche « mouvement » (table catalysts) dès qu'une valeur fait environ ±8 % ou plus : depuis
   l'ouverture (plus haut ou plus bas de séance) ou depuis la clôture de la veille (gap compris).
   On garde l'amplitude réelle, le gap, l'heure de l'extrême et le « départ » : la première bougie de
   5 min où le prix franchit le tiers de l'amplitude finale (depuis l'ouverture, ou depuis la clôture de
   la veille quand le mouvement vient surtout du gap). Signaux d'avant le mouvement : gap, volume
   relatif des 30 premières minutes (le labo ne garde pas la pré-ouverture), secteur (profil Finnhub,
   mis en cache), mouvement du BTC pour la crypto.
3. Actus (actions seulement) : Finnhub company-news du jour J-2 au jour J+1, au plus 8 titres, chacun
   avec sa source, son heure exacte et sa position : avant l'ouverture, avant le départ, pendant
   (jusqu'à l'extrême), après. La formule gratuite remonte ~12 mois : au-delà, « hors_profondeur ».
   Peu d'appels (JARVIS_LAB_FINNHUB_PER_MINUTE, 20 par défaut, et un budget par passage), jamais
   pendant la séance US, comme la pause Alpaca. Crypto : pas encore de source d'actus, fiche sans
   actus et note explicite.
4. Contre-exemples : un jour sur COUNTER_EVERY (tirage fixe par la date), au plus COUNTER_PER_DAY
   actions de l'univers qui n'ont PAS bougé (|mouvement| < 3 %), avec leurs actus : sans eux, les
   statistiques « actu → mouvement » seraient biaisées.
Le type de catalyseur reste vide (« à classer ») : l'équipe du soir le classe au Labo 3
(set_catalyst_type).
"""
import hashlib
import json
from bisect import bisect_right
import logging
import os
from datetime import date, datetime, timedelta, timezone

from app.config import secret
from app.core.http import SourceError
from app.lab import collector as CO
from app.lab import generator as G
from app.lab import heartbeat
from app.lab import sources as SRC
from app.lab.db import LabDB, now_iso
from app.sources import finnhub

log = logging.getLogger(__name__)
MOVE_PCT = 8.0
QUIET_PCT = 3.0
DEPART_FRACTION = 1 / 3
RVOL_SESSIONS = 20
FIRST_MINUTES = 30
COUNTER_EVERY = 5
COUNTER_PER_DAY = 3
NEWS_MAX = 8
NEWS_DEPTH_DAYS = 365
NEWS_ATTEMPTS = 3
CRYPTO_NOTE = "pas encore de source d'actus crypto : fiche sans actus"
# Types de catalyseurs (classés le soir par la routine). Les exemples de Simão y sont tous :
# changement de PDG, contrat signé ou perdu, résultats, FDA, dilution, partenariat, listing, hack.
TYPES = ("resultats", "previsions", "direction", "contrat", "partenariat", "fusion_acquisition", "fda",
         "reglementation", "produit", "analyste", "dilution", "financement", "listing", "hack", "justice",
         "macro", "secteur", "rumeur", "aucun", "autre")
POSITIONS = {"avant_ouverture": "avant l'ouverture", "avant_depart": "avant le départ", "pendant": "pendant",
             "apres": "après", "seance": "pendant la séance"}


def _per_minute() -> int:
    try:
        return max(1, int(os.getenv("JARVIS_LAB_FINNHUB_PER_MINUTE", "20")))
    except ValueError:
        return 20


FINNHUB_LIMIT = SRC.RateLimiter(_per_minute(), 60.0)


# --- Fonctions pures ------------------------------------------------------------------------

def day_stats(bars: list[dict], prev_close: float | None) -> dict | None:
    """Résumé d'une journée de bougies de 5 min triées."""
    if not bars or bars[0]["o"] <= 0:
        return None
    o = bars[0]["o"]
    hi = max(bars, key=lambda b: b["h"])
    lo = min(bars, key=lambda b: b["l"])
    return {"open": o, "high": hi["h"], "low": lo["l"], "close": bars[-1]["c"], "prev_close": prev_close,
            "up_pct": round((hi["h"] / o - 1) * 100, 3), "down_pct": round((lo["l"] / o - 1) * 100, 3),
            "gap_pct": round((o / prev_close - 1) * 100, 3) if prev_close else None,
            "t_high": hi["t"], "t_low": lo["t"]}


def classify(st: dict, threshold: float = MOVE_PCT) -> dict | None:
    """Mouvement d'au moins `threshold` % (depuis l'ouverture ou depuis la veille), sinon None.

    Renvoie la direction, l'amplitude depuis l'ouverture et depuis la veille, le prix de référence
    du départ (ouverture, ou clôture de la veille si le mouvement vient surtout du gap)."""
    up, down = st["up_pct"], st["down_pct"]
    pc = st.get("prev_close")
    up_prev = (st["high"] / pc - 1) * 100 if pc else None
    down_prev = (st["low"] / pc - 1) * 100 if pc else None
    best = max(up, -down, up_prev or 0.0, -(down_prev or 0.0))
    if best < threshold:
        return None
    direction = "hausse" if max(up, up_prev or 0.0) >= max(-down, -(down_prev or 0.0)) else "baisse"
    intraday = up if direction == "hausse" else down
    vs_prev = up_prev if direction == "hausse" else down_prev
    ref = st["open"] if abs(intraday) >= threshold or vs_prev is None else pc
    return {"direction": direction, "amplitude_pct": round(intraday, 3),
            "amplitude_veille_pct": round(vs_prev, 3) if vs_prev is not None else None,
            "ref": ref, "extreme": st["high"] if direction == "hausse" else st["low"],
            "extreme_ts": st["t_high"] if direction == "hausse" else st["t_low"]}


def departure(bars: list[dict], ref: float, extreme: float, fraction: float = DEPART_FRACTION) -> int | None:
    """Première bougie de 5 min où le prix franchit `fraction` de l'amplitude ref → extrême."""
    level = ref + (extreme - ref) * fraction
    up = extreme >= ref
    for b in bars:
        if (b["h"] >= level) if up else (b["l"] <= level):
            return b["t"]
    return None


def quiet(st: dict, threshold: float = QUIET_PCT) -> bool:
    """Valeur qui n'a pas bougé : moins de `threshold` % dans tous les sens (ouverture et veille)."""
    moves = [st["up_pct"], -st["down_pct"]]
    if st.get("prev_close"):
        moves += [abs(st["high"] / st["prev_close"] - 1) * 100, abs(st["low"] / st["prev_close"] - 1) * 100]
    return max(moves) < threshold


def first_volume(bars: list[dict] | list[tuple], open_ts: int, minutes: int = FIRST_MINUTES) -> float:
    end = open_ts + minutes * 60
    return sum((b["v"] if isinstance(b, dict) else b[1]) for b in bars
               if open_ts <= (b["t"] if isinstance(b, dict) else b[0]) < end)


def rvol_first(today: float, history: list[float]) -> float | None:
    """Volume des 30 premières minutes / moyenne des mêmes minutes des séances précédentes."""
    history = [h for h in history if h > 0]
    if len(history) < 5 or today <= 0:
        return None
    return round(today / (sum(history) / len(history)), 3)


def news_position(ts: int, open_ts: int, start_ts: int | None, extreme_ts: int | None, close_ts: int) -> str:
    """Position d'une actu par rapport au mouvement (ou à la séance pour un contre-exemple)."""
    if ts < open_ts:
        return "avant_ouverture"
    if start_ts is None:
        return "seance" if ts < close_ts else "apres"
    if ts < start_ts:
        return "avant_depart"
    if ts <= (extreme_ts or start_ts) + 300:
        return "pendant"
    return "apres"


def counter_day(day: str, every: int = COUNTER_EVERY) -> bool:
    """Jour tiré pour les contre-exemples (tirage fixe : le même jour donne toujours la même réponse)."""
    return int(hashlib.sha1(f"jour:{day}".encode()).hexdigest(), 16) % every == 0


def counter_pick(day: str, symbols: list[str], k: int = COUNTER_PER_DAY) -> list[str]:
    return sorted(symbols, key=lambda s: hashlib.sha1(f"{day}:{s}".encode()).hexdigest())[:k]


def select_news(items: list[dict], open_ts: int, start_ts: int | None, extreme_ts: int | None, close_ts: int,
                limit: int = NEWS_MAX) -> list[dict]:
    """Les `limit` actus les plus proches du départ (ou de l'ouverture), rangées par heure."""
    anchor = start_ts or open_ts
    seen, uniq = set(), []
    for n in items:
        key = (n.get("title") or "").strip().lower()
        if not key or key in seen or not n.get("published_ts"):
            continue
        seen.add(key)
        uniq.append(n)
    chosen = sorted(uniq, key=lambda n: abs(n["published_ts"] - anchor))[:limit]
    return [{"titre": (n.get("title") or "")[:200], "source": n.get("source"),
             "publie_a": datetime.fromtimestamp(n["published_ts"], timezone.utc).isoformat(timespec="minutes"),
             "ts": n["published_ts"],
             "position": news_position(n["published_ts"], open_ts, start_ts, extreme_ts, close_ts),
             "url": n.get("url")}
            for n in sorted(chosen, key=lambda n: n["published_ts"])]


# --- Mouvements du jour (base seule) --------------------------------------------------------

def _bounds(market: str, day: date) -> tuple[int, int] | None:
    if market == "stock":
        return CO.session_ts(day)
    start = CO.utc_midnight(day)
    return start, start + 86400


def _prev_close(db: LabDB, market: str, symbol: str, open_ts: int) -> float | None:
    row = db.conn.execute("SELECT c FROM bars WHERE market = ? AND symbol = ? AND tf = ? AND ts < ? "
                          "ORDER BY ts DESC LIMIT 1", (market, symbol, CO.BASE_TF, open_ts)).fetchone()
    return row[0] if row else None


def _history_opens(market: str, day: date) -> list[int]:
    if market == "stock":
        first = CO.sessions_back(day, RVOL_SESSIONS)
        return [CO.session_ts(d)[0] for d in CO.trading_days(first, day - timedelta(days=1))]
    start = CO.utc_midnight(day)
    return [start - k * 86400 for k in range(RVOL_SESSIONS, 0, -1)]


def symbol_move(db: LabDB, market: str, symbol: str, day: date) -> dict | None:
    bounds = _bounds(market, day)
    if not bounds:
        return None
    bars = db.get_bars(market, symbol, CO.BASE_TF, bounds[0], bounds[1])
    st = day_stats(bars, _prev_close(db, market, symbol, bounds[0]))
    if not st:
        return None
    opens = _history_opens(market, day)
    hist = db.conn.execute("SELECT ts, v FROM bars WHERE market = ? AND symbol = ? AND tf = ? AND ts >= ? AND ts < ?",
                           (market, symbol, CO.BASE_TF, opens[0], bounds[0])).fetchall() if opens else []
    per_open = {o: 0.0 for o in opens}
    span = FIRST_MINUTES * 60
    for ts, v in hist:
        i = bisect_right(opens, ts) - 1  # ouverture de la séance de cette bougie
        if i >= 0 and ts < opens[i] + span:
            per_open[opens[i]] += v or 0.0
    st["rvol30"] = rvol_first(first_volume(bars, bounds[0]), list(per_open.values()))
    st["bars"] = bars
    st["bounds"] = bounds
    return st


def _btc_context(db: LabDB, day: date, start_ts: int | None) -> dict:
    bounds = _bounds("crypto", day)
    bars = db.get_bars("crypto", G.INDEX["crypto"], CO.BASE_TF, bounds[0], bounds[1])
    if not bars:
        return {}
    o = bars[0]["o"]
    out = {"btc_jour_pct": round((bars[-1]["c"] / o - 1) * 100, 3)}
    if start_ts:
        before = [b for b in bars if b["t"] < start_ts]
        if before:
            out["btc_avant_depart_pct"] = round((before[-1]["c"] / o - 1) * 100, 3)
    return out


def _sector(db: LabDB, symbol: str) -> str | None:
    value = db.get("sector", symbol)
    return value or None


def _record(market: str, day: str, symbol: str, kind: str, st: dict, mv: dict | None, signals: dict) -> tuple:
    start = None
    if mv:
        start = departure(st["bars"], mv["ref"], mv["extreme"])
    status = "sans_source" if market == "crypto" else "a_chercher"
    note = CRYPTO_NOTE if market == "crypto" else None
    signals = {**signals, "gap_pct": st["gap_pct"], "rvol30": st.get("rvol30"),
               "hausse_ouverture_pct": st["up_pct"], "baisse_ouverture_pct": st["down_pct"],
               "cloture_pct": round((st["close"] / st["open"] - 1) * 100, 3),
               "heure_haut": st["t_high"], "heure_bas": st["t_low"],
               "ouverture_ts": st["bounds"][0], "cloture_ts": st["bounds"][1]}
    if mv:
        signals["amplitude_veille_pct"] = mv["amplitude_veille_pct"]
    # Le volume des 30 premières minutes n'est un signal « d'avant le mouvement » que si le départ
    # vient après ces 30 minutes ; sinon il contient le mouvement lui-même.
    signals["rvol30_avant_depart"] = start is None or start >= st["bounds"][0] + FIRST_MINUTES * 60
    return (market, day, symbol, kind, mv["direction"] if mv else None, mv["amplitude_pct"] if mv else None,
            st["gap_pct"], start, mv["extreme_ts"] if mv else None, signals, status, note)


def compute_day(db: LabDB, market: str, day: date) -> dict:
    """Mouvements de toutes les valeurs de l'univers du jour, fiches des gros mouvements et
    contre-exemples ; tout dans une transaction. Ne touche jamais aux actus déjà récupérées."""
    key = day.isoformat()
    symbols = [u["symbol"] for u in db.universe(market, key)]
    moves, records, quiet_syms, stats = [], [], [], {}
    for symbol in symbols:
        st = symbol_move(db, market, symbol, day)
        if not st:
            continue
        stats[symbol] = st
        moves.append((market, key, symbol, st["open"], st["prev_close"], st["high"], st["low"], st["close"],
                      st["up_pct"], st["down_pct"], st["gap_pct"], st["t_high"], st["t_low"], st["rvol30"]))
        mv = classify(st)
        if mv:
            signals = {"secteur": _sector(db, symbol)} if market == "stock" else {}
            rec = _record(market, key, symbol, "mouvement", st, mv, signals)
            if market == "crypto":
                rec[9].update(_btc_context(db, day, rec[7]))
            records.append(rec)
        elif market == "stock" and quiet(st):
            quiet_syms.append(symbol)
    if market == "stock" and counter_day(key):
        for symbol in counter_pick(key, quiet_syms):
            records.append(_record(market, key, symbol, "contre_exemple", stats[symbol], None,
                                   {"secteur": _sector(db, symbol)}))
    created = now_iso()
    with db.transaction():
        db.conn.execute("DELETE FROM day_moves WHERE market = ? AND day = ?", (market, key))
        db.conn.executemany("INSERT INTO day_moves VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", moves)
        for r in records:
            # Une fiche existante garde ses actus et son type ; seuls les chiffres du mouvement sont mis à jour.
            db.conn.execute(
                "INSERT INTO catalysts (market, day, symbol, kind, direction, amplitude_pct, gap_pct, start_ts, "
                "extreme_ts, signals, news, news_status, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "'[]', ?, ?, ?) ON CONFLICT (market, day, symbol) DO UPDATE SET kind = excluded.kind, "
                "direction = excluded.direction, amplitude_pct = excluded.amplitude_pct, gap_pct = excluded.gap_pct, "
                "start_ts = excluded.start_ts, extreme_ts = excluded.extreme_ts, signals = excluded.signals",
                (*r[:9], json.dumps(r[9], ensure_ascii=False), r[10], r[11], created))
        db.set(f"moves:{market}", key, {"valeurs": len(moves), "fiches": len(records)})
    return {"valeurs": len(moves), "fiches": len(records)}


def days_pending(db: LabDB, market: str, now: datetime, limit: int) -> list[date]:
    """Jours dont l'univers est connu, la journée complète et les mouvements pas encore calculés
    (les plus récents d'abord : le résumé du soir en a besoin)."""
    from app.lab.runner import day_ready  # import tardif (le runner importe beaucoup)

    rows = db.conn.execute(
        "SELECT DISTINCT u.day FROM universe u WHERE u.market = ? AND NOT EXISTS "
        "(SELECT 1 FROM progress p WHERE p.job = ? AND p.key = u.day) ORDER BY u.day DESC LIMIT ?",
        (market, f"moves:{market}", limit * 3)).fetchall()
    out = []
    for (d,) in rows:
        day = date.fromisoformat(d)
        if day_ready(market, day, now):
            out.append(day)
        if len(out) >= limit:
            break
    return out


def backfill_moves(db: LabDB, now: datetime, max_days: int) -> int:
    done = 0
    for market in ("stock", "crypto"):
        for day in days_pending(db, market, now, max_days):
            compute_day(db, market, day)
            done += 1
            heartbeat.beat()
    return done


# --- Actus (réseau) -------------------------------------------------------------------------

async def _call(fn, *args):
    return await SRC.with_retry(fn, *args, limiter=FINNHUB_LIMIT, tries=3)


async def fetch_news(db: LabDB, now: datetime, budget: int) -> dict:
    """Actus des fiches d'actions en attente, les plus récentes d'abord, `budget` appels au plus
    (profil de secteur compris, mis en cache par symbole)."""
    report = {"appels": 0, "fiches": 0, "erreurs": 0, "hors_profondeur": 0}
    if budget <= 0 or not secret(finnhub.KEY_NAME):
        return report
    oldest = (now.date() - timedelta(days=NEWS_DEPTH_DAYS)).isoformat()
    with db.transaction():
        n = db.conn.execute("UPDATE catalysts SET news_status = 'hors_profondeur', "
                            "note = 'au-delà de la profondeur de Finnhub (~12 mois)' "
                            "WHERE news_status = 'a_chercher' AND day < ?", (oldest,)).rowcount
    report["hors_profondeur"] = n
    pending = db.conn.execute(
        "SELECT id, day, symbol, start_ts, extreme_ts, signals FROM catalysts WHERE news_status = 'a_chercher' "
        "AND market = 'stock' ORDER BY day DESC, id LIMIT ?", (budget,)).fetchall()
    for cid, day, symbol, start_ts, extreme_ts, signals in pending:
        if report["appels"] >= budget:
            break
        sig = json.loads(signals or "{}")
        try:
            if db.get("sector", symbol) is None and report["appels"] + 2 <= budget:
                report["appels"] += 1
                try:
                    prof = await _call(finnhub.profile, symbol)
                    db.set("sector", symbol, prof.get("industry") or "")
                except SourceError:
                    db.set("sector", symbol, "")
            d = date.fromisoformat(day)
            report["appels"] += 1
            items = await _call(finnhub.company_news_between, symbol, d - timedelta(days=2), d + timedelta(days=1))
            news = select_news(items, sig.get("ouverture_ts") or 0, start_ts, extreme_ts, sig.get("cloture_ts") or 0)
        except (SourceError, ValueError, TypeError, AttributeError, KeyError) as exc:
            # Réponse inattendue comprise (objet d'erreur au lieu d'une liste) : la fiche compte ses
            # essais et finit en « erreur » au lieu de bloquer la file à chaque passage.
            report["erreurs"] += 1
            attempts = (db.get("news_attempts", str(cid), 0) or 0) + 1
            db.set("news_attempts", str(cid), attempts)
            if attempts >= NEWS_ATTEMPTS:
                db.conn.execute("UPDATE catalysts SET news_status = 'erreur', note = ? WHERE id = ?",
                                (f"Finnhub : {exc}"[:200], cid))
            log.info("Labo : actus %s %s indisponibles (%s)", symbol, day, exc)
            continue
        sig["secteur"] = db.get("sector", symbol) or sig.get("secteur")
        db.conn.execute("UPDATE catalysts SET news = ?, news_status = ?, signals = ? WHERE id = ?",
                        (json.dumps(news, ensure_ascii=False), "ok" if news else "aucune",
                         json.dumps(sig, ensure_ascii=False), cid))
        report["fiches"] += 1
        heartbeat.beat()
    return report


# --- Classement (Labo 3) et lecture ----------------------------------------------------------

def set_catalyst_type(db: LabDB, catalyst_id: int, kind: str | None) -> bool:
    """Classe une fiche (None remet « à classer »). Types permis : TYPES."""
    if kind is not None and kind not in TYPES:
        raise ValueError(f"type inconnu : {kind} (permis : {', '.join(TYPES)})")
    cur = db.conn.execute("UPDATE catalysts SET catalyst_type = ?, classified_at = ? WHERE id = ?",
                          (kind, now_iso() if kind else None, catalyst_id))
    return cur.rowcount == 1


def row_dict(row) -> dict:
    (cid, market, day, symbol, kind, direction, amp, gap, start, extreme, signals, news, status, note, ctype,
     classified) = row
    return {"id": cid, "marche": market, "jour": day, "symbole": symbol, "genre": kind, "direction": direction,
            "amplitude_pct": amp, "gap_pct": gap,
            "depart": datetime.fromtimestamp(start, timezone.utc).isoformat(timespec="minutes") if start else None,
            "extreme": datetime.fromtimestamp(extreme, timezone.utc).isoformat(timespec="minutes") if extreme else None,
            "signaux": json.loads(signals or "{}"), "actus": json.loads(news or "[]"), "actus_etat": status,
            "note": note, "type": ctype or "à classer", "classe_le": classified}


COLUMNS = ("id, market, day, symbol, kind, direction, amplitude_pct, gap_pct, start_ts, extreme_ts, signals, news, "
           "news_status, note, catalyst_type, classified_at")


def query(conn, day: str | None = None, symbol: str | None = None, market: str | None = None,
          kind: str | None = None, since: str | None = None, until: str | None = None,
          unclassified: bool = False, limit: int = 100) -> list[dict]:
    clauses, params = [], []
    for col, value in (("day", day), ("symbol", symbol), ("market", market), ("kind", kind)):
        if value:
            clauses.append(f"{col} = ?")
            params.append(value)
    if since:
        clauses.append("day >= ?")
        params.append(since)
    if until:
        clauses.append("day <= ?")
        params.append(until)
    if unclassified:
        clauses.append("catalyst_type IS NULL")
    sql = f"SELECT {COLUMNS} FROM catalysts" + (" WHERE " + " AND ".join(clauses) if clauses else "")
    sql += " ORDER BY day DESC, ABS(COALESCE(amplitude_pct, 0)) DESC LIMIT ?"
    return [row_dict(r) for r in conn.execute(sql, (*params, limit))]
