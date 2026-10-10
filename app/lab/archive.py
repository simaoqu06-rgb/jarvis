"""Archive des actus des gappers (déclaration n° 8, D1 : rejouer le pilier catalyseur de Killmode).

Finnhub gratuit ne garde qu'environ un an d'actus : chaque jour, la séance la plus ancienne sort de sa
profondeur. Ce module archive, sans rien en lire, ce qu'il faut pour rejouer plus tard le pilier catalyseur
de Killmode (catalyst.check puis killmode.catalyst_codes) tel que le direct l'aurait vu à 9 h 45 New York,
l'heure où le labo pose l'ordre de Killmode (fin du range de 15 min) :

1. Population (base seule) : pour chaque séance d'actions non scellée encore dans la profondeur de Finnhub,
   toutes les actions de la base (journalier Alpaca, pas seulement l'univers du labo, qui rate l'action qui
   gappe sur une actu de la nuit) ouvertes en gap ≥ KILL_GAP_PCT − GAP_MARGIN avec un ATR de Wilder 14 j
   ≥ KILL_ATR_PCT (universe.gappers, le calcul de la couverture n° 6). La marge de gap couvre l'écart entre
   l'ouverture journalière et celle de la première bougie de 5 min, que la règle du labo lit.
2. Actus : company-news de Finnhub des NEWS_DAYS jours qui précèdent la séance (fenêtre de catalyst.check),
   gardées seulement si publiées au plus tard à 9 h 45 New York, de la plus récente à la plus ancienne. La
   fenêtre est coupée à la profondeur de Finnhub (news_from la note) ; `received` compte ce que Finnhub a
   renvoyé avant la coupure à 9 h 45 (une réponse tronquée se verrait à un plafond).
3. Calendrier des résultats : toutes les sociétés, une date par appel, de D − 10 à D + 30 pour chaque séance
   D archivée (fenêtre de catalyst.check), une fois la date passée de EARNINGS_SETTLE_DAYS jours.
4. Profil : nom de chaque société (catalyst._about reconnaît une actu par le ticker ou par le nom).
Ordre : calendrier (peu d'appels), puis profils des séances dont toutes les actus sont archivées (Killmode
complet peut alors les rejouer sans attendre la fin de l'archive), puis actus de la séance la plus ancienne
d'abord (c'est elle qui sort la première de la profondeur), puis les autres profils. Jamais pendant la séance US ni dans l'heure d'avant (le scanner de
9 h 10 a besoin de Finnhub), avec son propre débit (JARVIS_LAB_ARCHIVE_PER_MINUTE, 30 par défaut, le reste
des 60 par minute de Finnhub gratuit allant au direct) et une durée par passage du worker.

Rien n'est lu ici : ni tonalité, ni résultat de trade. Aucune route ne montre ces tables, seulement leurs
comptes (`counts`, dans /api/lab/status). La déclaration n° 8 écrira le critère avant toute lecture.
Limites connues, à reprendre dans la déclaration : le calendrier (dates, BPA publié) et les profils sont ceux
que Finnhub donne aujourd'hui, remplis après coup ; une société retirée de la cote peut ne plus avoir de
profil ; Finnhub renvoie une liste vide aussi bien sans actu que pour un symbole qu'il ne connaît pas ; la
fidélité des heures de publication reste à vérifier sur un échantillon.
"""
import json
import logging
import os
import sqlite3
import time as clock
from datetime import date, datetime, time, timedelta

from app.config import secret
from app.core.http import SourceError
from app.lab import collector as CO
from app.lab import heartbeat
from app.lab import seal as SEAL
from app.lab import sources as SRC
from app.lab import strategies as ST
from app.lab import universe as U
from app.lab.catalysts import NEWS_DEPTH_DAYS
from app.lab.db import LabDB, now_iso
from app.sources import finnhub

log = logging.getLogger(__name__)
DECISION = time(9, 45)            # fin du range de 15 min : l'ordre de Killmode du labo est posé là
NEWS_DAYS = 7                     # catalyst.check : actus des 7 derniers jours
EARNINGS_BEFORE, EARNINGS_AFTER = 10, 30  # catalyst.check : résultats de J − 10 à J + 30
EARNINGS_SETTLE_DAYS = 2          # une date de résultats n'est archivée que passée (BPA publié rempli)
GAP_MARGIN = 0.5                  # point de gap en moins que la règle (ouverture journalière ≠ bougie de 5 min)
ATTEMPTS = 3                      # essais d'un appel en échec, sur plusieurs passages, avant « erreur »
STOP_AFTER_ERRORS = 5             # échecs de suite : Finnhub en panne ou quota, on arrête le passage
POPULATION_DAYS = 20              # séances dont on calcule les gappers par passage (base seule)
QUIET_FROM, QUIET_TO = time(8, 30), time(16, 30)  # New York, jours de Bourse : Finnhub laissé au direct
POPULATION_JOB = "archive:population"
PROFILE_JOB = "archive:profil"
PROFILE_ATTEMPTS_JOB = "archive:profil_essais"
KILL_JOB = "killcomplet:n8"       # séances rejouées par kill_complet.py (déclaration n° 8), comptées ici


def _per_minute() -> int:
    try:
        return max(1, int(os.getenv("JARVIS_LAB_ARCHIVE_PER_MINUTE", "30")))
    except ValueError:
        return 30


LIMIT = SRC.RateLimiter(_per_minute(), 60.0)


async def _call(fn, *args):
    return await SRC.with_retry(fn, *args, limiter=LIMIT, tries=3)


# --- Calendrier (sans réseau) ---------------------------------------------------------------

def today_ny(now: datetime) -> date:
    return now.astimezone(CO.NEW_YORK).date()


def oldest(now: datetime) -> date:
    """Plus ancienne date encore dans la profondeur des actus de Finnhub gratuit (~12 mois)."""
    return today_ny(now) - timedelta(days=NEWS_DEPTH_DAYS)


def quiet(now: datetime) -> bool:
    """Séance US ou l'heure d'avant (scanner de 9 h 10) : l'archive laisse Finnhub au direct."""
    local = now.astimezone(CO.NEW_YORK)
    return CO.CAL.is_trading_day(local.date()) and QUIET_FROM <= local.time() < QUIET_TO


def seconds_before_quiet(now: datetime) -> float:
    """Secondes avant la prochaine fenêtre calme du jour (l'archive s'arrête avant), l'infini sinon."""
    local = now.astimezone(CO.NEW_YORK)
    start = datetime.combine(local.date(), QUIET_FROM, CO.NEW_YORK)
    if not CO.CAL.is_trading_day(local.date()) or local >= start:
        return float("inf")
    return (start - local).total_seconds()


def cutoff_ts(day: date) -> int:
    return int(datetime.combine(day, DECISION, CO.NEW_YORK).timestamp())


def news_window(day: date, now: datetime) -> tuple[date, date]:
    """Dates demandées à Finnhub pour la séance `day` : NEWS_DAYS jours avant, coupés à la profondeur."""
    return max(day - timedelta(days=NEWS_DAYS), oldest(now)), day


def keep_news(items: list[dict], cutoff: int) -> list[dict]:
    """Actus publiées au plus tard à l'heure de décision, de la plus récente à la plus ancienne."""
    return sorted((i for i in items if i.get("published_ts") and i["published_ts"] <= cutoff),
                  key=lambda i: i["published_ts"], reverse=True)


def days_to_populate(db: LabDB, now: datetime, limit: int) -> list[date]:
    """Séances d'actions non scellées, dans la profondeur de Finnhub, dont le journalier est en base et dont
    les gappers ne sont pas encore listés, la plus ancienne d'abord."""
    last = db.conn.execute("SELECT MAX(ts) FROM bars WHERE market = 'stock' AND tf = 1440").fetchone()[0]
    if last is None:
        return []
    done = db.keys(POPULATION_JOB)
    return [d for d in CO.trading_days(oldest(now), CO.ny_day(last))
            if d.isoformat() not in done and not SEAL.sealed("replay", d)][:limit]


def populate(db: LabDB, now: datetime, limit: int = POPULATION_DAYS) -> int:
    """Liste les gappers des séances suivantes (statut « a_chercher »). Renvoie le nombre de séances listées."""
    days = days_to_populate(db, now, limit)
    for day in days:
        found = U.gappers(db.conn, day, ST.KILL_GAP_PCT - GAP_MARGIN, ST.KILL_ATR_PCT)
        cut = cutoff_ts(day)
        with db.transaction():
            db.conn.executemany(
                "INSERT OR IGNORE INTO gap_news (day, symbol, gap_pct, atr_pct, prev_close, open, cutoff_ts, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'a_chercher')",
                [(day.isoformat(), g["symbol"], g["gap_pct"], g["atr_pct"], g["prev_close"], g["open"], cut)
                 for g in found or []])
            # None : séance absente du journalier en base (trou de collecte), comptée à part dans `counts`.
            db.set(POPULATION_JOB, day.isoformat(), None if found is None else len(found))
    return len(days)


def earnings_dates(db: LabDB, now: datetime) -> list[date]:
    """Dates du calendrier des résultats encore à archiver : D − 10 à D + 30 de chaque séance listée, passées
    de EARNINGS_SETTLE_DAYS jours au moins, la plus ancienne d'abord."""
    last = today_ny(now) - timedelta(days=EARNINGS_SETTLE_DAYS)
    need: set[date] = set()
    for key, n in db.keys(POPULATION_JOB).items():
        if not n:
            continue
        d = date.fromisoformat(key)
        need.update(d + timedelta(days=k) for k in range(-EARNINGS_BEFORE, EARNINGS_AFTER + 1))
    done = {r[0] for r in db.conn.execute("SELECT day FROM earnings_days WHERE status IN ('ok', 'erreur')")}
    return sorted(x for x in need if x <= last and x.isoformat() not in done)


# --- Réseau -----------------------------------------------------------------------------------

class _Budget:
    """Durée du passage et arrêt après STOP_AFTER_ERRORS échecs de suite."""

    def __init__(self, seconds: float):
        self.deadline = clock.monotonic() + seconds
        self.errors_in_row = 0

    def left(self) -> bool:
        return clock.monotonic() < self.deadline and self.errors_in_row < STOP_AFTER_ERRORS

    def result(self, ok: bool) -> None:
        self.errors_in_row = 0 if ok else self.errors_in_row + 1


UNEXPECTED = (SourceError, ValueError, TypeError, AttributeError, KeyError)


async def fetch_earnings(db: LabDB, now: datetime, budget: _Budget) -> dict:
    report = {"appels": 0, "dates": 0, "erreurs": 0}
    for x in earnings_dates(db, now):
        if not budget.left():
            break
        report["appels"] += 1
        try:
            items = await _call(finnhub.earnings_calendar, x, x)
        except UNEXPECTED as exc:
            budget.result(False)
            report["erreurs"] += 1
            row = db.conn.execute("SELECT attempts FROM earnings_days WHERE day = ?", (x.isoformat(),)).fetchone()
            attempts = (row[0] if row else 0) + 1
            db.conn.execute("INSERT OR REPLACE INTO earnings_days (day, status, attempts, note) VALUES (?, ?, ?, ?)",
                            (x.isoformat(), "erreur" if attempts >= ATTEMPTS else "a_chercher", attempts,
                             f"Finnhub : {exc}"[:200]))
            log.info("Labo : calendrier des résultats du %s indisponible (%s)", x, exc)
            continue
        budget.result(True)
        db.conn.execute("INSERT OR REPLACE INTO earnings_days (day, status, attempts, n, items, fetched_at) "
                        "VALUES (?, 'ok', 0, ?, ?, ?)",
                        (x.isoformat(), len(items), json.dumps(items, ensure_ascii=False), now_iso()))
        report["dates"] += 1
        heartbeat.beat()
    return report


async def fetch_news(db: LabDB, now: datetime, budget: _Budget) -> dict:
    """Actus des gappers en attente, la séance la plus ancienne d'abord. Un gapper en échec n'est repris
    qu'au passage suivant (ATTEMPTS essais en tout)."""
    report = {"appels": 0, "archives": 0, "erreurs": 0, "hors_profondeur": 0}
    with db.transaction():
        report["hors_profondeur"] = db.conn.execute(
            "UPDATE gap_news SET status = 'hors_profondeur', note = 'sorti de la profondeur de Finnhub (~12 mois)' "
            "WHERE status = 'a_chercher' AND day < ?", (oldest(now).isoformat(),)).rowcount
    tried: set[tuple[str, str]] = set()
    while budget.left():
        rows = [r for r in db.conn.execute(
            "SELECT day, symbol, cutoff_ts, attempts FROM gap_news WHERE status = 'a_chercher' ORDER BY day, symbol "
            "LIMIT ?", (len(tried) + 50,)) if (r[0], r[1]) not in tried]
        if not rows:
            break
        for day, symbol, cut, attempts in rows:
            if not budget.left():
                break
            tried.add((day, symbol))
            start, end = news_window(date.fromisoformat(day), now)
            report["appels"] += 1
            try:
                items = await _call(finnhub.company_news_between, symbol, start, end)
            except UNEXPECTED as exc:
                budget.result(False)
                report["erreurs"] += 1
                db.conn.execute("UPDATE gap_news SET attempts = ?, status = ?, note = ? WHERE day = ? AND symbol = ?",
                                (attempts + 1, "erreur" if attempts + 1 >= ATTEMPTS else "a_chercher",
                                 f"Finnhub : {exc}"[:200], day, symbol))
                log.info("Labo : actus %s %s indisponibles (%s)", symbol, day, exc)
                continue
            budget.result(True)
            kept = keep_news(items, cut)
            db.conn.execute(
                "UPDATE gap_news SET status = 'ok', news_from = ?, news_to = ?, received = ?, news = ?, fetched_at = ?, "
                "note = NULL WHERE day = ? AND symbol = ?",
                (start.isoformat(), end.isoformat(), len(items), json.dumps(kept, ensure_ascii=False), now_iso(),
                 day, symbol))
            report["archives"] += 1
            heartbeat.beat()
    return report


READY_SYMBOLS_SQL = ("SELECT DISTINCT symbol FROM gap_news WHERE day NOT IN "
                     "(SELECT DISTINCT day FROM gap_news WHERE status = 'a_chercher') ORDER BY symbol")


async def fetch_profiles(db: LabDB, budget: _Budget, ready_only: bool = False) -> dict:
    """Nom, secteur et capitalisation (aujourd'hui) de chaque gapper archivé, une fois par symbole. `ready_only` :
    seulement les gappers des séances dont toutes les actus sont archivées."""
    report = {"appels": 0, "profils": 0, "erreurs": 0}
    done = db.keys(PROFILE_JOB)
    sql = READY_SYMBOLS_SQL if ready_only else "SELECT DISTINCT symbol FROM gap_news ORDER BY symbol"
    for (symbol,) in db.conn.execute(sql).fetchall():
        if symbol in done:
            continue
        if not budget.left():
            break
        report["appels"] += 1
        try:
            p = await _call(finnhub.profile, symbol)
            value = {"nom": p.get("name"), "secteur": p.get("industry"), "capitalisation": p.get("market_cap"),
                     "le": now_iso()}
        except SourceError as exc:
            if "profil inconnu" not in str(exc):
                budget.result(False)
                report["erreurs"] += 1
                attempts = (db.get(PROFILE_ATTEMPTS_JOB, symbol, 0) or 0) + 1
                db.set(PROFILE_ATTEMPTS_JOB, symbol, attempts)
                if attempts >= ATTEMPTS:
                    db.set(PROFILE_JOB, symbol, {"nom": None, "erreur": str(exc)[:200], "le": now_iso()})
                continue
            value = {"nom": None, "inconnu": True, "le": now_iso()}  # Finnhub sans profil (ETF, retirée…)
        except UNEXPECTED as exc:
            budget.result(False)
            report["erreurs"] += 1
            log.info("Labo : profil %s indisponible (%s)", symbol, exc)
            continue
        budget.result(True)
        db.set(PROFILE_JOB, symbol, value)
        report["profils"] += 1
        heartbeat.beat()
    return report


async def step(db: LabDB, now: datetime, seconds: float, collect: bool = True,
               days: int = POPULATION_DAYS) -> dict:
    """Étape du worker : gappers de `days` séances de plus (base seule), puis, hors séance US et dans la durée
    permise, calendrier des résultats, profils des séances aux actus complètes, actus et autres profils."""
    report: dict = {"seances": populate(db, now, days)}
    if not collect or seconds <= 0 or quiet(now) or not secret(finnhub.KEY_NAME):
        return report
    budget = _Budget(min(seconds, seconds_before_quiet(now)))
    report["resultats"] = await fetch_earnings(db, now, budget)
    first = await fetch_profiles(db, budget, ready_only=True)
    report["actus"] = await fetch_news(db, now, budget)
    rest = await fetch_profiles(db, budget)
    report["profils"] = {k: first[k] + rest[k] for k in first}
    return report


# --- Lectures permises par la déclaration n° 8 ------------------------------------------------------

def results_lookup(conn: sqlite3.Connection):
    """n° 8, mesure sans essai (FRSH) : fonction (symbole, séance) → True si la société a publié ses résultats
    depuis la veille (n'importe quelle heure de la séance d'avant ou des jours fermés qui suivent, ou avant
    l'ouverture du jour, « bmo »), False sinon, None si l'une de ces dates n'est pas archivée. None tout court
    si la table n'existe pas encore (base pas migrée)."""
    try:
        conn.execute("SELECT 1 FROM earnings_days LIMIT 1").fetchall()
    except sqlite3.Error:
        return None
    cache: dict[str, dict | None] = {}

    def by_symbol(x: date) -> dict | None:
        key = x.isoformat()
        if key not in cache:
            row = conn.execute("SELECT status, items FROM earnings_days WHERE day = ?", (key,)).fetchone()
            cache[key] = ({e["symbol"]: e for e in json.loads(row[1] or "[]")} if row and row[0] == "ok"
                          else None)
        return cache[key]

    def passed(symbol: str, day: str) -> bool | None:
        d = date.fromisoformat(day)
        prev = CO.previous_trading_day(d)
        found = False
        for k in range((d - prev).days + 1):
            x = prev + timedelta(days=k)
            index = by_symbol(x)
            if index is None:
                return None
            e = index.get(symbol)
            found = found or bool(e and (x < d or e.get("hour") == "bmo"))
        return found

    return passed


# --- Comptes (lecture seule, jamais le contenu) ----------------------------------------------------

def counts(conn: sqlite3.Connection) -> dict | None:
    """Avancement de l'archive, en nombres seulement ; None avant la première séance listée."""
    try:
        days = conn.execute("SELECT COUNT(*), MIN(key), MAX(key), SUM(value = 'null') FROM progress WHERE job = ?",
                            (POPULATION_JOB,)).fetchone()
        if not days[0]:
            return None
        rows = dict(conn.execute("SELECT status, COUNT(*) FROM gap_news GROUP BY status").fetchall())
        pending = conn.execute("SELECT MIN(day) FROM gap_news WHERE status = 'a_chercher'").fetchone()[0]
        earnings = dict(conn.execute("SELECT status, COUNT(*) FROM earnings_days GROUP BY status").fetchall())
        profiles = conn.execute("SELECT COUNT(*) FROM progress WHERE job = ?", (PROFILE_JOB,)).fetchone()[0]
        symbols = conn.execute("SELECT COUNT(DISTINCT symbol) FROM gap_news").fetchone()[0]
        replayed = conn.execute("SELECT COUNT(*) FROM progress WHERE job = ?", (KILL_JOB,)).fetchone()[0]
    except sqlite3.Error:
        return None
    return {"seances": days[0], "du": days[1], "au": days[2], "sans_journalier": days[3] or 0,
            "gappers": sum(rows.values()),
            "archives": rows.get("ok", 0), "a_chercher": rows.get("a_chercher", 0), "erreurs": rows.get("erreur", 0),
            "hors_profondeur": rows.get("hors_profondeur", 0), "prochaine_seance": pending,
            "dates_resultats": earnings.get("ok", 0), "dates_resultats_erreurs": earnings.get("erreur", 0),
            "profils": profiles, "societes": symbols, "rejouees": replayed}


def line(c: dict | None) -> str | None:
    """Ligne de !labo."""
    if not c:
        return None
    out = (f"Archive des actus (n° 8) : {c['archives']}/{c['gappers']} gappers sur {c['seances']} séances "
           f"({c['du']} → {c['au']})")
    if c["prochaine_seance"]:
        out += f" · prochaine séance {c['prochaine_seance']}"
    if c["sans_journalier"]:
        out += f" · {c['sans_journalier']} séances sans journalier en base"
    if c["erreurs"] or c["hors_profondeur"]:
        out += f" · {c['erreurs']} en erreur, {c['hors_profondeur']} perdus (hors profondeur)"
    out += f" · calendrier des résultats : {c['dates_resultats']} dates · profils {c['profils']}/{c['societes']}"
    return out + (f" · Killmode complet rejoué sur {c['rejouees']} séances" if c.get("rejouees") else "")
