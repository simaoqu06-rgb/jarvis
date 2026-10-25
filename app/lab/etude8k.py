"""Étude des 8-K sur plusieurs jours : COLLECTE seulement. Aucun chiffre de l'étude n'est calculé ici.

Protocole figé : /mnt/project-files/labo/protocole-etude-8k-multijours.md (v2.1 du 25 octobre 2026, accepté par GPT,
Gemini et JARVIS). Ordre imposé : 1. collecte ; 2. table de couverture et contrôle manuel d'un échantillon, publiés
avant tout chiffre de performance ; 3. exploration 2021-2024 ; validation lue une seule fois, plus tard. Feu vert de
Simão pour la collecte le 25 octobre 2026 (fil « Mesurer la dérive après les 8-K »).

Ce que la collecte rassemble, dans des tables à part (e8k_*, copie figée de l'étude, jamais lue par le reste du labo) :
1. Index complet d'EDGAR (index « master » trimestriel depuis le 4e trimestre 2020) : tous les 8-K, 8-K/A et avis de
   radiation (25, 25-NSE, 15), de tous les déposants, sociétés radiées comprises. On ne part jamais des tickers
   actuels ni des fiches de gros mouvements.
2. Pour chaque CIK qui a déposé un 8-K depuis FILINGS_FROM : ses 8-K et 8-K/A (codes d'item, heure JSON, document
   principal) depuis FILINGS_FROM, son nom, ses tickers actuels, ses places de cotation et son code SIC (sensibilité
   « secteur » du protocole, SIC à 2 chiffres).
3. Heure d'acceptation exacte (`ACCEPTANCE-DATETIME` de l'en-tête EDGAR) pour chaque 8-K d'item 2.02 ou 1.01 dont
   l'heure JSON est ambiguë : le JSON est parfois en retard de 4 h (5 h l'hiver), jamais en avance (edgar.py), et
   l'autre heure possible changerait le classement « hors séance » (16 h 00 la veille → 9 h 30) ou « pendant la
   séance ». D'abord CALIBRATION_N dépôts de contrôle de chaque sorte (retard certain, heure juste certaine), pour
   vérifier que l'en-tête donne bien l'heure attendue.
4. Ticker à la date : tickers actuels de la SEC (l'historique Alpaca d'une société renommée suit son symbole actuel) ;
   pour un CIK sans ticker actuel (radiée, rachetée…), le symbole déclaré sur la page de garde de son dernier 8-K
   d'item 2.02 ou 1.01 (dei:TradingSymbol), daté de ce 8-K (`asof`), jusqu'à COVER_TRIES pages de garde.
5. Bougies journalières Alpaca (SIP, ajustées des divisions) de chaque série (symbole, ou symbole@asof) depuis
   BARS_FROM, lues en une fois : une série est cohérente avec elle-même (un seul ajustement), puis figée. SPY sert à
   l'excès de rendement.

Réseau : hors séance US seulement, budget e8k_calls (appels SEC, limite partagée avec la mesure 8-K) et e8k_seconds
(Alpaca) du worker. Sans SEC_USER_AGENT, rien n'est appelé à la SEC ; sans clés Alpaca, pas de bougies.
L'état de la collecte (`!etude8k`, GET /api/lab/etude8k) ne compte que des données : dépôts, CIK, heures, tickers,
séries. Aucun rendement.
"""
import json
import logging
import re
import sqlite3
import time as clock
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone

from app.config import secret
from app.core.http import SourceError
from app.lab import collector as CO
from app.lab import heartbeat
from app.lab import sec8k as SEC8K
from app.lab import sources as SRC
from app.lab.db import LabDB, now_iso
from app.sources import alpaca, edgar

log = logging.getLogger(__name__)
STUDY_FROM = date(2021, 1, 1)      # premier J0 de l'exploration (protocole § 7)
FILINGS_FROM = date(2020, 12, 1)   # 8-K lus depuis : témoins sans 8-K de J−5 à J0
BARS_FROM = date(2020, 9, 1)       # bougies depuis : adv20 et ATR de Wilder déjà stabilisés en janvier 2021
INDEX_FROM = (2020, 4)             # premier trimestre de l'index lu
INDEX_REFRESH_DAYS = 7             # un trimestre non clos est relu au plus une fois par semaine
INDEX_FINAL_DAYS = 7               # un trimestre est clos 7 jours après sa fin
EVENT_ITEMS = frozenset({"2.02", "1.01"})
CALIBRATION_N = 20
CALIBRATION_MIN = 0.95
MATCH_S = 120
ATTEMPTS = 3
COVER_TRIES = 3
BARS_BATCH = 100
INDEX_SYMBOL = "SPY"
TICKER_RE = re.compile(r"^[A-Z]{1,5}(\.[A-QSTVX-Z])?$")  # classe d'actions (BRK.B), pas les unités, droits, bons (U, R, W)
SUBS_SHARE, EXACT_SHARE = 5 / 8, 7 / 8   # parts cumulées du budget SEC : CIK, puis heures, puis pages de garde


# --- Fonctions pures ------------------------------------------------------------------------

def quarters(today: date, first: tuple[int, int] = INDEX_FROM) -> list[tuple[int, int]]:
    y, q = first
    out = []
    while (y, q) <= (today.year, (today.month - 1) // 3 + 1):
        out.append((y, q))
        y, q = (y + 1, 1) if q == 4 else (y, q + 1)
    return out


def quarter_end(year: int, quarter: int) -> date:
    return (date(year + 1, 1, 1) if quarter == 4 else date(year, 3 * quarter + 1, 1)) - timedelta(days=1)


def session_class(ts: int) -> tuple[str, str] | None:
    """(« hors », J0) si `ts` tombe entre la clôture de la séance précédente et l'ouverture de J0, (« seance », J) pendant
    la séance J (calendrier NYSE, clôtures anticipées comprises), heure de New York."""
    d = CO.ny_day(ts)
    for _ in range(15):
        bounds = CO.session_ts(d)
        if bounds:
            if ts < bounds[0]:
                return "hors", d.isoformat()
            if ts < bounds[1]:
                return "seance", d.isoformat()
        d += timedelta(days=1)
    return None


def ambiguous(ts: int | None) -> bool:
    """L'autre heure possible (JSON en retard) change le classement hors séance / pendant la séance."""
    alt = edgar.shifted_back(ts)
    return alt is not None and session_class(ts) != session_class(alt)


def alpaca_symbol(sec_ticker: str | None) -> str | None:
    """Ticker de la SEC (BRK-B) → symbole Alpaca (BRK.B) ; None s'il n'est pas une action ordinaire ou une classe
    d'actions (bons ABC-WT, préférentielles XYZ-PA…, unités, droits)."""
    s = (sec_ticker or "").strip().upper().replace("-", ".")
    return s if TICKER_RE.match(s) else None


def serie_key(symbol: str, asof: str | None) -> str:
    return f"{symbol}@{asof}" if asof else symbol


def _items(value) -> set[str]:
    return set(edgar.items(value))


# --- Appels ---------------------------------------------------------------------------------

async def _sec(fn, *args):
    return await SRC.with_retry(fn, *args, limiter=SEC8K.SEC_LIMIT, tries=3)


class _Budget:
    def __init__(self, total: int):
        self.total, self.used = total, 0

    def left(self, share: float = 1.0) -> bool:
        return self.used < int(self.total * share)


# --- 1. Index EDGAR -------------------------------------------------------------------------

async def collect_index(db: LabDB, now: datetime, budget: _Budget, report: dict) -> None:
    today = now.astimezone(CO.NEW_YORK).date()
    for y, q in quarters(today):
        if not budget.left():
            return
        key = f"{y}Q{q}"
        st = db.get("e8k_index", key)
        if st and (st.get("final") or now.timestamp() - st.get("ts", 0) < INDEX_REFRESH_DAYS * 86400):
            continue
        budget.used += 1
        try:
            rows = await _sec(edgar.master_index, y, q)
        except SourceError as exc:
            report["erreurs"] += 1
            log.info("Labo : index EDGAR %s indisponible (%s)", key, exc)
            continue
        with db.transaction():
            db.conn.executemany(
                "INSERT OR REPLACE INTO e8k_index (accession, cik, form, date_filed, company) VALUES (?, ?, ?, ?, ?)",
                [(r["accession"], r["cik"], r["form"], r["date_filed"], r["company"]) for r in rows])
            db.set("e8k_index", key, {"ts": int(now.timestamp()), "n": len(rows),
                                      "final": today > quarter_end(y, q) + timedelta(days=INDEX_FINAL_DAYS)})
        report["trimestres"] += 1
        heartbeat.beat()


# --- 2. Dépôts de chaque CIK ----------------------------------------------------------------

def due_ciks(conn: sqlite3.Connection, now: datetime) -> list[int]:
    """CIK à (re)lire : jamais lus, en erreur (moins de ATTEMPTS essais), couverts trop tard, ou avec un 8-K à l'index
    déposé depuis leur dernière lecture (lue il y a plus d'un jour). Les jamais lus d'abord, puis les plus récents."""
    latest = dict(conn.execute("SELECT cik, MAX(date_filed) FROM e8k_index WHERE form IN ('8-K', '8-K/A') "
                               "AND date_filed >= ? GROUP BY cik", (FILINGS_FROM.isoformat(),)))
    eight_k = {r[0] for r in conn.execute("SELECT DISTINCT cik FROM e8k_index WHERE form = '8-K' AND date_filed >= ?",
                                          (FILINGS_FROM.isoformat(),))}
    known = {r[0]: r[1:] for r in conn.execute(
        "SELECT cik, status, covered_from, fetched_ts, fetched_day, attempts FROM e8k_ciks")}
    todo = []
    for cik in eight_k:
        row = known.get(cik)
        if row is None:
            todo.append((0, latest[cik], cik))
            continue
        status, covered, fetched_ts, fetched_day, attempts = row
        if status == "erreur":
            if (attempts or 0) < ATTEMPTS:
                todo.append((1, latest[cik], cik))
        elif (covered and covered > FILINGS_FROM.isoformat()) or (
                latest[cik] >= (fetched_day or "") and now.timestamp() - (fetched_ts or 0) > 86400):
            todo.append((1, latest[cik], cik))
    todo.sort(key=lambda t: t[1], reverse=True)
    todo.sort(key=lambda t: t[0])
    return [c for _, _, c in todo]


def _store_cik(db: LabDB, cik: int, sub: dict, filings: list[dict], covered: str, now: datetime) -> None:
    db.conn.executemany(
        "INSERT OR REPLACE INTO e8k_filings (accession, cik, form, items, accepted_ts, filing_date, suspect, "
        "primary_doc) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(f["accession"], cik, f["form"], ",".join(f["items"]), f["accepted_ts"], f["filing_date"], int(f["suspect"]),
          f.get("primary_doc")) for f in filings if f["form"] in ("8-K", "8-K/A")])
    db.conn.execute(
        "INSERT INTO e8k_ciks (cik, status, covered_from, fetched_ts, fetched_day, attempts, name, tickers, exchanges, "
        "sic, note) VALUES (?, 'ok', ?, ?, ?, 0, ?, ?, ?, ?, NULL) ON CONFLICT (cik) DO UPDATE SET status = 'ok', "
        "covered_from = excluded.covered_from, fetched_ts = excluded.fetched_ts, fetched_day = excluded.fetched_day, "
        "attempts = 0, name = excluded.name, tickers = excluded.tickers, exchanges = excluded.exchanges, "
        "sic = excluded.sic, note = NULL",
        (cik, covered, int(now.timestamp()), now.astimezone(CO.NEW_YORK).date().isoformat(), sub.get("name"),
         json.dumps(sub.get("tickers") or []), json.dumps(sub.get("exchanges") or []),
         str(sub["sic"]) if sub.get("sic") else None))
    db.conn.execute("DELETE FROM e8k_tickers WHERE cik = ? AND source = 'sec_actuel'", (cik,))
    for t in sub.get("tickers") or []:
        sym = alpaca_symbol(t)
        if sym:
            db.conn.execute("INSERT OR IGNORE INTO e8k_tickers (cik, symbol, source, asof, accession) "
                            "VALUES (?, ?, 'sec_actuel', '', NULL)", (cik, sym))


async def collect_ciks(db: LabDB, now: datetime, budget: _Budget, report: dict) -> None:
    for cik in due_ciks(db.conn, now):
        if not budget.left(SUBS_SHARE):
            return
        try:
            budget.used += 1
            sub = await _sec(edgar.submissions, cik)
            filings, covered = list(sub["recent"]), sub["oldest"]
            if covered and covered > FILINGS_FROM.isoformat():
                names = edgar.pages_needed(sub["files"], FILINGS_FROM)
                for name in names:
                    budget.used += 1
                    filings += await _sec(edgar.page, name)
                if len(names) == len(sub["files"]):  # toutes les pages utiles lues : l'historique est complet
                    covered = FILINGS_FROM.isoformat()
                else:
                    covered = min([covered] + [f["filing_date"] for f in filings if f["filing_date"]])
            if not covered:
                covered = FILINGS_FROM.isoformat()
        except (SourceError, ValueError, TypeError, KeyError) as exc:
            prev = db.conn.execute("SELECT attempts FROM e8k_ciks WHERE cik = ?", (cik,)).fetchone()
            with db.transaction():
                db.conn.execute(
                    "INSERT INTO e8k_ciks (cik, status, attempts, fetched_ts, note) VALUES (?, 'erreur', ?, ?, ?) "
                    "ON CONFLICT (cik) DO UPDATE SET status = 'erreur', attempts = excluded.attempts, "
                    "fetched_ts = excluded.fetched_ts, note = excluded.note",
                    (cik, (prev[0] if prev else 0) + 1, int(now.timestamp()), f"SEC : {exc}"[:200]))
            report["erreurs"] += 1
            log.info("Labo : dépôts SEC du CIK %s indisponibles (%s)", cik, exc)
            continue
        with db.transaction():
            _store_cik(db, cik, sub, filings, covered, now)
        report["cik"] += 1
        heartbeat.beat()


# --- 3. Heures exactes (en-tête EDGAR) ------------------------------------------------------

def exact_queue(conn: sqlite3.Connection) -> list[tuple[str, int, str]]:
    """8-K dont l'en-tête reste à lire, en (numéro, CIK, sorte) : d'abord les contrôles qui manquent, puis les 8-K
    d'item 2.02 ou 1.01 à heure ambiguë, les plus récents d'abord. Rien si le contrôle est complet et raté."""
    done = {r[0]: r[1:] for r in conn.execute("SELECT accession, kind, status, attempts FROM e8k_exact")}
    have = Counter(kind for kind, status, _ in done.values() if status != "erreur")
    if min(have["controle_retard"], have["controle_juste"]) >= CALIBRATION_N and not exact_check(conn)["fiable"]:
        return []
    since = int(datetime.combine(FILINGS_FROM, time(0), CO.NEW_YORK).timestamp())
    controls: dict[str, list] = {"controle_retard": [], "controle_juste": []}
    todo = []
    for acc, cik, ts, items, suspect in conn.execute(
            "SELECT accession, cik, accepted_ts, items, suspect FROM e8k_filings WHERE form = '8-K' "
            "AND accepted_ts >= ? ORDER BY accepted_ts DESC", (since,)):
        prev = done.get(acc)
        if prev and (prev[1] != "erreur" or prev[2] >= ATTEMPTS):
            continue
        at = datetime.fromtimestamp(ts, CO.NEW_YORK).time()
        alt = edgar.shifted_back(ts)
        if suspect or not edgar.EDGAR_OPEN <= at < edgar.EDGAR_CLOSE:
            if alt is None:
                continue
            kind = "controle_retard"
        elif alt is None:
            kind = "controle_juste"
        else:
            if _items(items) & EVENT_ITEMS and ambiguous(ts):
                todo.append((acc, cik, "ambigu"))
            continue
        if len(controls[kind]) < CALIBRATION_N - have[kind]:
            controls[kind].append((acc, cik, kind))
    return controls["controle_retard"] + controls["controle_juste"] + todo


def exact_check(conn: sqlite3.Connection) -> dict:
    """Contrôle de l'en-tête sur des 8-K dont l'heure juste est connue (retard certain : JSON + décalage de New York ;
    heure juste certaine : JSON) : fiable si chaque sorte a au moins CALIBRATION_N // 2 dépôts et CALIBRATION_MIN de
    confirmés."""
    check = {"controle_retard": [0, 0], "controle_juste": [0, 0]}
    status, ambigus = Counter(), Counter()
    for kind, true, st, ts in conn.execute(
            "SELECT e.kind, e.accepted_ts, e.status, f.accepted_ts FROM e8k_exact e "
            "JOIN e8k_filings f ON f.accession = e.accession"):
        status[st] += 1
        if kind == "ambigu":
            ambigus[st] += 1
        if st != "ok" or true is None or ts is None or kind not in check:
            continue
        check[kind][0] += 1
        check[kind][1] += abs(true - (SEC8K._late(ts) if kind == "controle_retard" else ts)) <= MATCH_S
    reliable = all(n >= CALIBRATION_N // 2 and ok >= CALIBRATION_MIN * n for n, ok in check.values())
    return {"controles": {k: {"n": n, "confirmes": ok} for k, (n, ok) in check.items()}, "fiable": reliable,
            "lues": status["ok"], "illisibles": status["illisible"], "erreurs": status["erreur"],
            "ambigus_lus": ambigus["ok"]}


async def collect_exact(db: LabDB, now: datetime, budget: _Budget, report: dict) -> None:
    queue = exact_queue(db.conn) if budget.left(EXACT_SHARE) else []
    for acc, cik, kind in queue:
        if not budget.left(EXACT_SHARE):
            return
        budget.used += 1
        prev = db.conn.execute("SELECT attempts FROM e8k_exact WHERE accession = ?", (acc,)).fetchone()
        try:
            exact = await _sec(edgar.accepted_header, cik, acc)
            status, attempts = ("ok" if exact else "illisible"), 0
        except SourceError as exc:
            exact, status, attempts = None, "erreur", (prev[0] if prev else 0) + 1
            log.info("Labo : en-tête SEC de %s indisponible (%s)", acc, exc)
        with db.transaction():
            db.conn.execute(
                "INSERT INTO e8k_exact (accession, kind, accepted_ts, status, fetched_ts, attempts) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (accession) DO UPDATE SET kind = excluded.kind, "
                "accepted_ts = excluded.accepted_ts, status = excluded.status, fetched_ts = excluded.fetched_ts, "
                "attempts = excluded.attempts", (acc, kind, exact, status, int(now.timestamp()), attempts))
        report["entetes"] += 1
        heartbeat.beat()


# --- 4. Ticker à la date (page de garde) ----------------------------------------------------

def cover_queue(db: LabDB) -> list[tuple[int, str, str, str]]:
    """(CIK, numéro, document, date) de la page de garde à lire pour chaque CIK lu sans ticker actuel utilisable et
    avec au moins un 8-K d'item 2.02 ou 1.01 depuis STUDY_FROM : son 8-K le plus récent pas encore essayé, jusqu'à
    COVER_TRIES essais par CIK. Les CIK au 8-K le plus récent d'abord."""
    with_ticker = {r[0] for r in db.conn.execute("SELECT DISTINCT cik FROM e8k_tickers")}
    state = db.keys("e8k_couverture")
    candidates: dict[int, list] = defaultdict(list)
    for acc, cik, items, fdate, doc in db.conn.execute(
            "SELECT f.accession, f.cik, f.items, f.filing_date, f.primary_doc FROM e8k_filings f "
            "JOIN e8k_ciks c ON c.cik = f.cik AND c.status = 'ok' WHERE f.form = '8-K' AND f.filing_date >= ? "
            "AND f.primary_doc IS NOT NULL ORDER BY f.filing_date DESC", (STUDY_FROM.isoformat(),)):
        if cik not in with_ticker and _items(items) & EVENT_ITEMS:
            candidates[cik].append((acc, doc, fdate))
    out = []
    for cik, docs in candidates.items():
        st = state.get(str(cik)) or {}
        tried = st.get("essais") or []
        if st.get("status") == "ok" or len(tried) >= COVER_TRIES or st.get("erreurs", 0) >= ATTEMPTS:
            continue
        nxt = next(((a, d, f) for a, d, f in docs if a not in tried), None)
        if nxt:
            out.append((cik, *nxt))
    out.sort(key=lambda r: r[3], reverse=True)
    return out


async def collect_covers(db: LabDB, budget: _Budget, report: dict) -> None:
    for cik, acc, doc, fdate in (cover_queue(db) if budget.left() else []):
        if not budget.left():
            return
        budget.used += 1
        st = db.get("e8k_couverture", str(cik)) or {}
        try:
            symbols = [s for s in (alpaca_symbol(t) for t in await _sec(edgar.trading_symbols, cik, acc, doc)) if s]
        except SourceError as exc:
            with db.transaction():
                db.set("e8k_couverture", str(cik), st | {"status": "erreur", "erreurs": st.get("erreurs", 0) + 1,
                                                         "note": str(exc)[:200]})
            report["erreurs"] += 1
            continue
        with db.transaction():
            for sym in symbols:
                db.conn.execute("INSERT OR IGNORE INTO e8k_tickers (cik, symbol, source, asof, accession) "
                                "VALUES (?, ?, 'couverture', ?, ?)", (cik, sym, fdate, acc))
            db.set("e8k_couverture", str(cik), {"status": "ok" if symbols else "sans_symbole",
                                                "essais": (st.get("essais") or []) + [acc],
                                                "erreurs": st.get("erreurs", 0)})
        report["pages_de_garde"] += 1
        heartbeat.beat()


# --- 5. Bougies journalières ----------------------------------------------------------------

def sync_series(db: LabDB) -> int:
    """Une série par ticker à la date, plus SPY ; une série déjà lue n'est jamais relue (copie figée)."""
    rows = [(serie_key(s, a or None), s, a or None) for s, a in
            db.conn.execute("SELECT DISTINCT symbol, asof FROM e8k_tickers")]
    rows.append((INDEX_SYMBOL, INDEX_SYMBOL, None))
    with db.transaction():
        before = db.conn.total_changes
        db.conn.executemany("INSERT OR IGNORE INTO e8k_series (serie, symbol, asof) VALUES (?, ?, ?)", rows)
        return db.conn.total_changes - before


def pending_series(conn: sqlite3.Connection) -> dict[str | None, list[tuple[str, str]]]:
    """Séries à lire, groupées par `asof` : jamais lues, ou en erreur (moins de ATTEMPTS essais)."""
    out: dict = defaultdict(list)
    for serie, symbol, asof in conn.execute(
            "SELECT serie, symbol, asof FROM e8k_series WHERE status IS NULL OR (status = 'erreur' AND attempts < ?) "
            "ORDER BY serie", (ATTEMPTS,)):
        out[asof].append((serie, symbol))
    return out


async def collect_bars(db: LabDB, now: datetime, seconds: float, report: dict) -> None:
    sync_series(db)
    deadline = clock.monotonic() + seconds
    start = datetime.combine(BARS_FROM, time(0), CO.NEW_YORK)
    end = now - alpaca.SIP_DELAY
    for asof, series in pending_series(db.conn).items():
        for i in range(0, len(series), BARS_BATCH):
            if clock.monotonic() >= deadline:
                return
            batch = series[i:i + BARS_BATCH]
            symbols = [s for _, s in batch]
            try:
                rows = await alpaca.bars_range(symbols, 1440, start, end, call=SRC._alpaca_call, asof=asof)
            except SourceError as exc:
                with db.transaction():
                    db.conn.executemany(
                        "UPDATE e8k_series SET status = 'erreur', attempts = attempts + 1, fetched_ts = ?, note = ? "
                        "WHERE serie = ?", [(int(now.timestamp()), f"Alpaca : {exc}"[:200], k) for k, _ in batch])
                report["erreurs"] += 1
                log.info("Labo : bougies de l'étude 8-K indisponibles (%s…) : %s", symbols[0], exc)
                continue
            with db.transaction():
                for key, symbol in batch:
                    bars = rows.get(symbol) or []
                    db.conn.execute("DELETE FROM e8k_bars WHERE serie = ?", (key,))
                    db.conn.executemany(
                        "INSERT OR REPLACE INTO e8k_bars (serie, d, o, h, l, c, v) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [(key, CO.ny_day(int(b["t"])).toordinal(), b["o"], b["h"], b["l"], b["c"], b.get("v") or 0.0)
                         for b in bars])
                    days = sorted(CO.ny_day(int(b["t"])) for b in bars)
                    db.conn.execute(
                        "UPDATE e8k_series SET status = ?, n = ?, first_day = ?, last_day = ?, fetched_ts = ?, "
                        "note = NULL WHERE serie = ?",
                        ("ok" if bars else "vide", len(bars), days[0].isoformat() if days else None,
                         days[-1].isoformat() if days else None, int(now.timestamp()), key))
            report["series"] += len(batch)
            heartbeat.beat()


# --- Passage du worker ----------------------------------------------------------------------

async def collect(db: LabDB, now: datetime, sec_calls: int, seconds: float) -> dict:
    """Étape « etude8k_collecte » : index, CIK, en-têtes et pages de garde dans `sec_calls` appels SEC, puis bougies
    pendant `seconds` secondes. Met à jour l'état de la collecte (progress e8k/etat)."""
    report = {"appels_sec": 0, "trimestres": 0, "cik": 0, "entetes": 0, "pages_de_garde": 0, "series": 0,
              "erreurs": 0}
    if sec_calls > 0 and secret(edgar.KEY_NAME):
        budget = _Budget(sec_calls)
        await collect_index(db, now, budget, report)
        await collect_ciks(db, now, budget, report)
        await collect_exact(db, now, budget, report)
        await collect_covers(db, budget, report)
        report["appels_sec"] = budget.used
    elif sec_calls > 0:
        report["note"] = f"{edgar.KEY_NAME} absent : rien n'est appelé à la SEC"
    if seconds > 0 and alpaca.configured():
        await collect_bars(db, now, seconds, report)
    with db.transaction():
        db.set("e8k", "etat", state(db))
        db.set("e8k", "at", now_iso())
    return report


# --- État de la collecte (données seulement, aucun rendement) -------------------------------

def state(db: LabDB) -> dict:
    conn = db.conn
    today = datetime.now(timezone.utc).astimezone(CO.NEW_YORK).date()
    qs = quarters(today)
    read = db.keys("e8k_index")
    forms = dict(conn.execute("SELECT form, COUNT(*) FROM e8k_index GROUP BY form"))
    by_year = dict(conn.execute("SELECT substr(date_filed, 1, 4), COUNT(*) FROM e8k_index WHERE form = '8-K' "
                                "GROUP BY 1"))
    eight_k_ciks = conn.execute("SELECT COUNT(DISTINCT cik) FROM e8k_index WHERE form = '8-K' AND date_filed >= ?",
                                (FILINGS_FROM.isoformat(),)).fetchone()[0]
    ciks = dict(conn.execute("SELECT status, COUNT(*) FROM e8k_ciks GROUP BY status"))
    eligible = defaultdict(lambda: [0, set()])
    for cik, items, fdate in conn.execute("SELECT cik, items, filing_date FROM e8k_filings WHERE form = '8-K' "
                                          "AND filing_date >= ?", (STUDY_FROM.isoformat(),)):
        if _items(items) & EVENT_ITEMS:
            cell = eligible[fdate[:4]]
            cell[0] += 1
            cell[1].add(cik)
    tickers = dict(conn.execute("SELECT source, COUNT(DISTINCT cik) FROM e8k_tickers GROUP BY source"))
    eligible_ciks = set().union(*(c[1] for c in eligible.values())) if eligible else set()
    with_ticker = {r[0] for r in conn.execute("SELECT DISTINCT cik FROM e8k_tickers")}
    covers = Counter((v or {}).get("status") for v in db.keys("e8k_couverture").values())
    series = dict(conn.execute("SELECT COALESCE(status, 'a_lire'), COUNT(*) FROM e8k_series GROUP BY 1"))
    bars, first, last = conn.execute("SELECT COALESCE(SUM(n), 0), MIN(first_day), MAX(last_day) FROM e8k_series "
                                     "WHERE status = 'ok'").fetchone()
    spy = conn.execute("SELECT status, first_day, last_day FROM e8k_series WHERE serie = ?",
                       (INDEX_SYMBOL,)).fetchone()
    remaining = {"trimestres": sum(1 for y, q in qs if f"{y}Q{q}" not in read),
                 "cik": len(due_ciks(conn, datetime.now(timezone.utc))),
                 "entetes": len(exact_queue(conn)), "pages_de_garde": len(cover_queue(db)),
                 "series": series.get("a_lire", 0)}
    return {"protocole": "v2.1 (25/10/2026)", "depuis": STUDY_FROM.isoformat(),
            "index": {"trimestres_lus": sum(1 for y, q in qs if f"{y}Q{q}" in read), "trimestres": len(qs),
                      "formulaires": forms, "8k_par_annee": by_year, "cik_8k": eight_k_ciks},
            "cik": ciks, "eligibles_par_annee": {y: {"depots": c[0], "cik": len(c[1])}
                                                 for y, c in sorted(eligible.items())},
            "tickers": {"sec_actuel": tickers.get("sec_actuel", 0), "couverture": tickers.get("couverture", 0),
                        "cik_eligibles": len(eligible_ciks),
                        "cik_eligibles_avec_ticker": len(eligible_ciks & with_ticker)},
            "pages_de_garde": dict(covers), "entetes": exact_check(conn),
            "series": series, "bougies": bars, "premiere": first, "derniere": last,
            "spy": {"status": spy[0], "premiere": spy[1], "derniere": spy[2]} if spy else None,
            "reste": remaining, "complete": not any(remaining.values())}


def cached(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT value FROM progress WHERE job = 'e8k' AND key = 'etat'").fetchone()
    return json.loads(row[0]) if row else None


def _n(x) -> str:
    return "n/d" if x is None else f"{x:,}".replace(",", " ")


def text(data: dict | None) -> str:
    """`!etude8k` : avancement de la collecte de l'étude des 8-K sur plusieurs jours (aucun rendement)."""
    if not data:
        return ("🗂️ Collecte de l'étude des 8-K pas encore commencée (le worker du labo la lance hors séance US, après "
                "le déploiement ; SEC_USER_AGENT et les clés Alpaca sont nécessaires).")
    ix, c, t, h, r = data["index"], data["cik"], data["tickers"], data["entetes"], data["reste"]
    ctl = h["controles"]
    elig = " · ".join(f"{y} {_n(v['depots'])} ({_n(v['cik'])} CIK)" for y, v in data["eligibles_par_annee"].items())
    share = (f"{100 * t['cik_eligibles_avec_ticker'] / t['cik_eligibles']:.0f} %".replace(".", ",")
             if t["cik_eligibles"] else "n/d")
    lines = [f"🗂️ **Étude des 8-K sur plusieurs jours : collecte** (protocole {data['protocole']}, sans aucun calcul)",
             f"-# Index EDGAR : {ix['trimestres_lus']}/{ix['trimestres']} trimestres · {_n(ix['formulaires'].get('8-K'))}"
             f" 8-K · {_n(ix['cik_8k'])} CIK avec un 8-K depuis décembre 2020",
             f"-# CIK lus : {_n(c.get('ok', 0))}, en erreur {_n(c.get('erreur', 0))}, reste {_n(r['cik'])}",
             f"-# 8-K 2.02 ou 1.01 depuis {data['depuis'][:4]} : {elig or 'aucun pour l’instant'}",
             f"-# Ticker à la date : {_n(t['cik_eligibles_avec_ticker'])}/{_n(t['cik_eligibles'])} CIK éligibles "
             f"({share}) · pages de garde lues {_n(sum(data['pages_de_garde'].values()))}, reste {_n(r['pages_de_garde'])}",
             f"-# En-têtes EDGAR : {_n(h['ambigus_lus'])} heures ambiguës lues, reste {_n(r['entetes'])} · contrôles "
             f"{ctl['controle_retard']['confirmes']}/{ctl['controle_retard']['n']} retards, "
             f"{ctl['controle_juste']['confirmes']}/{ctl['controle_juste']['n']} heures justes "
             f"({'fiable' if h['fiable'] else 'pas encore fiable'})",
             f"-# Bougies : {_n(data['series'].get('ok', 0))} séries lues, {_n(data['series'].get('vide', 0))} vides, "
             f"{_n(data['series'].get('erreur', 0))} en erreur, reste {_n(r['series'])} · {_n(data['bougies'])} "
             f"bougies, du {data['premiere'] or 'n/d'} au {data['derniere'] or 'n/d'}",
             "✅ Collecte complète : prochaine étape, la table de couverture et le contrôle manuel, avant tout chiffre."
             if data["complete"] else "⏳ Collecte en cours. Aucun rendement n'est calculé avant la table de couverture."]
    return "\n".join(lines)
