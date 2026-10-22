"""Mesure 8-K : un dépôt SEC d'avant 9 h 45 change-t-il ce qui reste à gagner après ? Mesure sans essai.

Demande de Simão (22 octobre 2026, fil « Skills de trading pour JARVIS »), plan arrêté avec Gemini au deuxième tour
(/mnt/project-files/trading/debat-gemini-8k-2026-10-22.md) :
- à 9 h 45, le volume des 15 premières minutes se voit déjà : la question n'est pas « un 8-K fait-il du volume ? »
  mais « À VOLUME ÉGAL, un 8-K change-t-il le mouvement restant ? » ;
- seul compte ce qui se passe après l'entrée : un 8-K d'avant l'ouverture est déjà dans le gap ;
- 100 événements par case décrivent, ils ne prouvent rien : pour voir +0,1 R avec un écart type de 1,2 R, il faut
  ≈ 2 260 trades par groupe (z 1,96, puissance 80 %), ≈ 5 200 au seuil du labo (z 3,42). La mesure affiche
  l'effectif requis calculé sur l'écart type observé.

Fenêtre d'un 8-K pour la séance J (figée ici) : acceptée par la SEC entre la clôture de la séance précédente et
9 h 45 New York (ouverture + CUTOFF_MIN), le moment où le range de 15 min se ferme. Seuls les 8-K comptent (pas les
8-K/A, des corrections souvent anciennes) ; une société qui ne dépose que des 6-K (émetteur étranger) est écartée,
elle n'a jamais de 8-K. Groupe de la séance, par priorité : « resultats » (item 2.02), « contrat » (1.01),
« communique » (7.01 ou 8.01), « autre » (tout autre 8-K), « aucun ».

Deux lectures, sans rien filtrer ni noter (rien ici ne change un plan ni ne compte comme essai) :
1. Tradable : les trades déclenchés d'orb15 et orb30 (strat_trades, le bloc scellé n'y est pas) par tranche de
   vol15_adv (bornes de la déclaration n° 6 : < 0,25, 0,25-0,5, 0,5-1, ≥ 1) et par groupe : R brut, R à 20 €, R du
   témoin à la même heure (des entrées au hasard après 9 h 45 : le mouvement restant sans signal), MFE et MAE médians,
   part des sorties au stop et des fausses cassures (stop sans avoir vu +1 R). Puis l'écart « avec 8-K − sans 8-K »
   à volume égal (moyenne des écarts par tranche, pondérée par n₁n₀/(n₁+n₀)), son z, et l'effectif requis.
2. Taux de base : toutes les séances de l'univers du labo (day_moves, bloc scellé exclu) par tranche de gap et par
   groupe : part qui touche +8 % et +10 % depuis l'ouverture. C'est la journée entière, 9 h 30-9 h 45 compris :
   pas tradable, sert à voir si un type de 8-K fait souvent bouger.

Données : app/sources/edgar.py (heure d'acceptation SEC, codes d'item), collectées hors séance US par l'étape
« sec8k » du worker dans le budget sec_calls, seulement si SEC_USER_AGENT est défini. Une séance n'est lue que si
les dépôts de la valeur sont couverts depuis au moins COVER_MARGIN_DAYS jours avant elle et jusqu'après son 9 h 45.

Heure SEC (22 octobre 2026, voir edgar.py) : environ deux tiers des heures du JSON sont en retard de 4 h (5 h l'hiver),
sans moyen de savoir lesquelles. Lue en UTC, l'heure n'avance jamais un dépôt. Un 8-K lu pendant la séance J
(après 9 h 45) dont l'heure en retard tomberait avant 9 h 45 (lu vers 10 h-13 h 45, déposé peut-être à 6 h-9 h 45)
rend la séance J ambiguë : elle est écartée des deux groupes (« heure_ambigue »), au lieu d'aller dans « aucun »
alors qu'elle a peut-être un 8-K. Un 8-K lu après la clôture de J−1 mais peut-être déposé pendant la séance J−1
reste dans la fenêtre de J : il était public avant l'ouverture, ce n'est pas de l'information future.
Heure exacte : la collecte lit « Accepted » sur la page d'index de chaque 8-K ambigu (au moins un quart du budget
SEC), et d'abord sur CALIBRATION_N 8-K de contrôle de chaque sorte : retards certains et heures justes certaines.
Elle ne sert que si ces contrôles confirment l'heure attendue (CALIBRATION_MIN), et seulement quand elle tombe sur
l'une des deux heures possibles (à MATCH_S près) : la séance quitte alors les écartées, dans son groupe.

Limites : tickers actuels seulement (une valeur radiée ou renommée n'a pas de CIK, elle est comptée « sans CIK ») ;
un 8-K reprend souvent un communiqué déjà diffusé, il peut arriver après l'info ; les retards certains de l'heure
SEC sont comptés (« heures douteuses »).
"""
import logging
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from statistics import median, pstdev

from app.config import secret
from app.core.http import SourceError
from app.lab import collector as CO
from app.lab import empreinte as E
from app.lab import heartbeat
from app.lab import potentiel as PO
from app.lab import seal as SEAL
from app.lab import sources as SRC
from app.lab import stats as STATS
from app.lab.db import LabDB, now_iso
from app.sources import edgar

log = logging.getLogger(__name__)
CACHE_KEY = "sec8k"
CUTOFF_MIN = 15
COVER_MARGIN_DAYS = 7
STRATEGIES = ("orb15", "orb30")
GROUPS = {"resultats": "résultats (2.02)", "contrat": "contrat (1.01)", "communique": "communiqué (7.01, 8.01)",
          "autre": "autre 8-K", "aucun": "aucun 8-K"}
VOL_BUCKETS = ((0.0, 0.25, "vol15 < 0,25"), (0.25, 0.5, "vol15 0,25-0,5"), (0.5, 1.0, "vol15 0,5-1"),
               (1.0, 1e18, "vol15 ≥ 1"))
VOL_INCONNU = "vol15 inconnu"
EFFECT_R = 0.1            # écart visé dans le débat (brut ≥ +0,1 R)
Z_POWER = 0.84            # puissance 80 %
Z_LABO = 3.42             # seuil du labo (déclaration n° 11)
TICKERS_MAX_AGE_DAYS = 7
REFRESH_DAYS = 7          # une valeur déjà lue est relue au plus une fois par semaine (≈ 6 000 valeurs dans l'univers)
ATTEMPTS = 3
SEC_LIMIT = SRC.RateLimiter(4, 1.0)
EXACT_SHARE = 4           # au moins un quart du budget SEC pour les heures exactes des 8-K ambigus
CALIBRATION_N = 20        # 8-K de contrôle par sorte (retard certain, heure juste certaine)
CALIBRATION_MIN = 0.95    # part des contrôles confirmés exigée avant d'utiliser une heure exacte
MATCH_S = 120             # écart toléré entre l'heure exacte et l'une des deux heures possibles


# --- Fonctions pures ------------------------------------------------------------------------

def group(items: set[str]) -> str:
    if "2.02" in items:
        return "resultats"
    if "1.01" in items:
        return "contrat"
    if items & {"7.01", "8.01"}:
        return "communique"
    return "autre"


def window_day(ts: int) -> str | None:
    """Séance dont la fenêtre (clôture précédente → 9 h 45) contient `ts`, None pendant une séance après 9 h 45."""
    d = CO.ny_day(ts)
    for _ in range(15):
        bounds = CO.session_ts(d)
        if bounds:
            if ts < bounds[0] + CUTOFF_MIN * 60:
                prev = CO.session_ts(CO.previous_trading_day(d))
                return d.isoformat() if prev and ts >= prev[1] else None
            if ts < bounds[1]:
                return None
        d += timedelta(days=1)
    return None


def vol_bucket(v: float | None) -> str:
    if v is None:
        return VOL_INCONNU
    return next((label for lo, hi, label in VOL_BUCKETS if lo <= v < hi), VOL_INCONNU)


def required_n(sigma: float | None, z: float) -> int | None:
    """Trades par groupe pour voir EFFECT_R au seuil z, puissance 80 % (deux groupes de même taille)."""
    if not sigma:
        return None
    return math.ceil(2 * (z + Z_POWER) ** 2 * sigma ** 2 / EFFECT_R ** 2)


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 4) if xs else None


def _var(xs: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return sum((x - m) ** 2 for x in xs) / (len(xs) - 1)


def stratified(pairs: list[tuple[list[float], list[float]]]) -> dict:
    """Écart moyen « avec − sans » à volume égal : un écart par tranche, pondéré par n₁n₀/(n₁+n₀) ; z approché en
    supposant les trades indépendants (pas de grappes par jour : c'est une lecture, pas un test du labo)."""
    num = den = var = 0.0
    n1 = n0 = 0
    for with_, without in pairs:
        v1, v0 = _var(with_), _var(without)
        if v1 is None or v0 is None:
            continue
        w = len(with_) * len(without) / (len(with_) + len(without))
        diff = sum(with_) / len(with_) - sum(without) / len(without)
        num += w * diff
        den += w
        var += w * w * (v1 / len(with_) + v0 / len(without))
        n1, n0 = n1 + len(with_), n0 + len(without)
    if not den:
        return {"ecart": None, "z": None, "n_avec": n1, "n_sans": n0}
    se = math.sqrt(var) / den
    return {"ecart": round(num / den, 4), "z": round(num / den / se, 2) if se else None, "n_avec": n1, "n_sans": n0}


class Cell:
    def __init__(self):
        self.gross: list[float] = []
        self.r20: list[float] = []
        self.witness: list[float] = []
        self.mfe: list[float] = []
        self.mae: list[float] = []
        self.stops = 0
        self.false_breaks = 0

    def add(self, gross, r20, witness, mfe, mae, exit_reason) -> None:
        self.gross.append(gross)
        if r20 is not None:
            self.r20.append(r20)
        if witness is not None:
            self.witness.append(witness)
        if mfe is not None:
            self.mfe.append(mfe)
        if mae is not None:
            self.mae.append(mae)
        if (exit_reason or "").startswith("stop"):
            self.stops += 1
            if mfe is not None and mfe < 1:
                self.false_breaks += 1

    def to_dict(self) -> dict:
        n = len(self.gross)
        return {"n": n, "r_brut": _mean(self.gross), "r_20e": _mean(self.r20), "temoin_r": _mean(self.witness),
                "mfe_med": round(median(self.mfe), 3) if self.mfe else None,
                "mae_med": round(median(self.mae), 3) if self.mae else None,
                "part_stop": round(self.stops / n, 4) if n else None,
                "part_fausse_cassure": round(self.false_breaks / n, 4) if n else None}


# --- Collecte (réseau) ----------------------------------------------------------------------

def _sealed_sql() -> tuple[str, tuple]:
    lo, hi = SEAL.bounds()
    return "NOT (day BETWEEN ? AND ?)", (lo, hi)


def wanted(conn: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    """Actions que la mesure lit, avec leur première et leur dernière séance (bloc scellé exclu)."""
    clause, params = _sealed_sql()
    out: dict[str, tuple[str, str]] = {}
    rows = conn.execute(f"SELECT symbol, MIN(day), MAX(day) FROM day_moves WHERE market = 'stock' AND {clause} "
                        "GROUP BY symbol", params).fetchall()
    rows += conn.execute("SELECT symbol, MIN(day), MAX(day) FROM strat_trades WHERE market = 'stock' "
                         f"AND strategy IN ({','.join('?' * len(STRATEGIES))}) GROUP BY symbol",
                         STRATEGIES).fetchall()
    for symbol, lo, hi in rows:
        a, b = out.get(symbol, (lo, hi))
        out[symbol] = (min(a, lo), max(b, hi))
    return out


def _cutoff(day: str) -> int:
    bounds = CO.session_ts(date.fromisoformat(day))
    return bounds[0] + CUTOFF_MIN * 60 if bounds else 0


def due(db: LabDB, want: dict[str, tuple[str, str]], tickers_ts: float, now: datetime) -> list[str]:
    """Valeurs à (re)lire : jamais lues, couvertes trop tard, lues avant le 9 h 45 de leur dernière séance et il y a
    plus de REFRESH_DAYS jours (en attendant, leurs séances récentes sont « non couvertes »), en erreur (moins de
    ATTEMPTS essais) ou sans CIK avant la dernière liste des tickers. Les jamais lues d'abord, puis les plus récentes."""
    known = {r[0]: r[1:] for r in db.conn.execute(
        "SELECT symbol, status, covered_from, fetched_ts, attempts FROM sec_symbols")}
    todo = []
    for symbol, (lo, hi) in want.items():
        row = known.get(symbol)
        if row is None:
            todo.append((0, hi, symbol))
            continue
        status, covered, fetched, attempts = row
        need_from = (date.fromisoformat(lo) - timedelta(days=COVER_MARGIN_DAYS)).isoformat()
        stale = (fetched or 0) < _cutoff(hi) and now.timestamp() - (fetched or 0) > REFRESH_DAYS * 86400
        if status == "ok" and (stale or (covered and covered > need_from)):
            todo.append((1, hi, symbol))
        elif status == "erreur" and (attempts or 0) < ATTEMPTS:
            todo.append((1, hi, symbol))
        elif status == "sans_cik" and (fetched or 0) < tickers_ts:
            todo.append((1, hi, symbol))
    todo.sort(key=lambda t: t[1], reverse=True)
    todo.sort(key=lambda t: t[0])  # tri stable : jamais lues d'abord, puis les plus récentes
    return [s for _, _, s in todo]


async def _call(fn, *args):
    return await SRC.with_retry(fn, *args, limiter=SEC_LIMIT, tries=3)


def _store_filings(db: LabDB, cik: int, filings: list[dict]) -> None:
    db.conn.executemany(
        "INSERT OR REPLACE INTO sec_filings (accession, cik, form, items, accepted_ts, filing_date, suspect) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(f["accession"], cik, f["form"], ",".join(f["items"]), f["accepted_ts"], f["filing_date"],
          int(f["suspect"])) for f in filings])


def _mark(db: LabDB, symbol: str, cik, status: str, covered: str | None, now: datetime, note: str | None = None,
          attempts: int = 0) -> None:
    db.conn.execute(
        "INSERT INTO sec_symbols (symbol, cik, status, covered_from, fetched_at, fetched_ts, attempts, note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (symbol) DO UPDATE SET cik = excluded.cik, "
        "status = excluded.status, covered_from = excluded.covered_from, fetched_at = excluded.fetched_at, "
        "fetched_ts = excluded.fetched_ts, attempts = excluded.attempts, note = excluded.note",
        (symbol, cik, status, covered, now.isoformat(timespec="seconds"), int(now.timestamp()), attempts, note))


def _exact_queue(conn: sqlite3.Connection) -> list[tuple[str, int, str]]:
    """8-K dont l'heure exacte reste à lire, en (numéro, CIK, sorte) : d'abord les contrôles qui manquent (retards
    certains, heures justes certaines), puis les 8-K ambigus des séances que la mesure lit, les plus récents d'abord.
    Rien si le contrôle est complet et raté."""
    want = wanted(conn)
    ranges: dict[int, tuple[str, str]] = {}
    for symbol, cik in conn.execute("SELECT symbol, cik FROM sec_symbols WHERE status = 'ok'"):
        if symbol in want:
            lo, hi = want[symbol]
            a, b = ranges.get(cik, (lo, hi))
            ranges[cik] = (min(a, lo), max(b, hi))
    if not ranges:
        return []
    done = {r[0]: r[1:] for r in conn.execute("SELECT accession, kind, status, attempts FROM sec_exact")}
    have = Counter(kind for kind, status, _ in done.values() if status != "erreur")
    if min(have["controle_retard"], have["controle_juste"]) >= CALIBRATION_N and not _exact(conn)[1]["fiable"]:
        return []  # contrôle complet et raté : la page d'index ne sert pas, inutile de lire les ambigus
    controls = {"controle_retard": [], "controle_juste": []}
    todo = []
    seal_lo, seal_hi = SEAL.bounds()
    since = min(lo for lo, _ in ranges.values())
    since_ts = int(datetime.combine(date.fromisoformat(since), time(0), edgar.NEW_YORK).timestamp()) - 86400
    for acc, cik, ts, suspect in conn.execute(
            "SELECT accession, cik, accepted_ts, suspect FROM sec_filings WHERE form = '8-K' AND accepted_ts >= ? "
            "ORDER BY accepted_ts DESC", (since_ts,)):
        prev = done.get(acc)
        if cik not in ranges or (prev and (prev[1] != "erreur" or prev[2] >= ATTEMPTS)):
            continue
        at = datetime.fromtimestamp(ts, edgar.NEW_YORK).time()
        alt = edgar.shifted_back(ts)
        if suspect or not edgar.EDGAR_OPEN <= at < edgar.EDGAR_CLOSE:
            if alt is None:  # ni l'heure lue ni l'heure en retard quand EDGAR est ouvert : cas à part, pas un contrôle
                continue
            kind = "controle_retard"
        elif alt is None:
            kind = "controle_juste"
        else:
            day = None if window_day(ts) else window_day(alt)
            lo, hi = ranges[cik]
            if day and lo <= day <= hi and not seal_lo <= day <= seal_hi:
                todo.append((acc, cik, "ambigu"))
            continue
        if len(controls[kind]) < CALIBRATION_N - have[kind]:
            controls[kind].append((acc, cik, kind))
    return controls["controle_retard"] + controls["controle_juste"] + todo


async def fetch(db: LabDB, now: datetime, budget: int) -> dict:
    """Étape « sec8k » du worker : CIK et dépôts des valeurs dues, puis heures exactes des 8-K ambigus (au moins un
    quart du budget), `budget` appels SEC au plus."""
    report = {"appels": 0, "valeurs": 0, "sans_cik": 0, "erreurs": 0, "heures_exactes": 0}
    if budget <= 0:
        return report
    if not secret(edgar.KEY_NAME):
        return report | {"note": f"{edgar.KEY_NAME} absent : rien n'est appelé"}
    tickers = db.get("sec", "tickers") or {}
    tickers_ts = db.get("sec", "tickers_ts", 0) or 0
    if not tickers or now.timestamp() - tickers_ts > TICKERS_MAX_AGE_DAYS * 86400:
        report["appels"] += 1
        try:
            tickers = await _call(edgar.tickers)
        except SourceError as exc:
            log.info("Labo : liste des tickers SEC indisponible (%s)", exc)
            return report | {"erreurs": 1}
        tickers_ts = now.timestamp()
        db.set("sec", "tickers", tickers)
        db.set("sec", "tickers_ts", tickers_ts)
    want = wanted(db.conn)
    for symbol in due(db, want, tickers_ts, now):
        if report["appels"] >= budget - budget // EXACT_SHARE:
            break
        lo = date.fromisoformat(want[symbol][0]) - timedelta(days=COVER_MARGIN_DAYS)
        cik = tickers.get(edgar.ticker(symbol))
        if cik is None:
            _mark(db, symbol, None, "sans_cik", None, now)
            report["sans_cik"] += 1
            continue
        try:
            report["appels"] += 1
            sub = await _call(edgar.submissions, cik)
            filings, covered = list(sub["recent"]), sub["oldest"]
            if covered and covered > lo.isoformat():
                names = edgar.pages_needed(sub["files"], lo)
                for name in names:
                    report["appels"] += 1
                    filings += await _call(edgar.page, name)
                # Toutes les pages d'archive lues (ou aucune) : tout l'historique de la société est là.
                if len(names) == len(sub["files"]):
                    covered = lo.isoformat()
                else:
                    covered = min([covered] + [f["filing_date"] for f in filings if f["filing_date"]])
            if not covered:  # aucun dépôt : rien à manquer, la valeur est couverte
                covered = lo.isoformat()
        except (SourceError, ValueError, TypeError, KeyError) as exc:
            attempts = (db.conn.execute("SELECT attempts FROM sec_symbols WHERE symbol = ?", (symbol,)).fetchone()
                        or (0,))[0] + 1
            _mark(db, symbol, cik, "erreur", None, now, f"SEC : {exc}"[:200], attempts)
            report["erreurs"] += 1
            log.info("Labo : dépôts SEC de %s indisponibles (%s)", symbol, exc)
            continue
        with db.transaction():
            _store_filings(db, cik, filings)
            _mark(db, symbol, cik, "ok", covered, now)
        report["valeurs"] += 1
        heartbeat.beat()
    for acc, cik, kind in (_exact_queue(db.conn) if report["appels"] < budget else []):
        if report["appels"] >= budget:
            break
        report["appels"] += 1
        prev = db.conn.execute("SELECT attempts FROM sec_exact WHERE accession = ?", (acc,)).fetchone()
        try:
            exact = await _call(edgar.accepted_exact, cik, acc)
            status, attempts = ("ok" if exact else "illisible"), 0
        except SourceError as exc:
            exact, status, attempts = None, "erreur", (prev[0] if prev else 0) + 1
            log.info("Labo : page d'index SEC de %s indisponible (%s)", acc, exc)
        with db.transaction():
            db.conn.execute(
                "INSERT INTO sec_exact (accession, kind, accepted_ts, status, fetched_ts, attempts) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (accession) DO UPDATE SET kind = excluded.kind, "
                "accepted_ts = excluded.accepted_ts, status = excluded.status, fetched_ts = excluded.fetched_ts, "
                "attempts = excluded.attempts", (acc, kind, exact, status, int(now.timestamp()), attempts))
        report["heures_exactes"] += 1
        heartbeat.beat()
    return report


# --- Mesure (base seule) --------------------------------------------------------------------

def _symbols(conn: sqlite3.Connection) -> dict[str, tuple]:
    return {r[0]: r[1:] for r in conn.execute("SELECT symbol, cik, status, covered_from, fetched_ts FROM sec_symbols")}


def _late(ts: int) -> int:
    """Heure d'acceptation si le JSON est en retard, sans condition d'ouverture d'EDGAR (contrôles)."""
    return ts + int(datetime.fromtimestamp(ts, edgar.NEW_YORK).utcoffset().total_seconds())


def _exact(conn: sqlite3.Connection) -> tuple[dict, dict]:
    """Heures exactes des 8-K ambigus ({numéro: epoch}) et leur contrôle. Sur des 8-K dont l'heure juste est connue
    (retard certain, heure juste certaine), part où la page d'index donne l'heure attendue : les heures exactes ne
    servent que si chaque contrôle a au moins CALIBRATION_N // 2 dépôts et CALIBRATION_MIN de confirmés."""
    exact, check, status = {}, {"controle_retard": [0, 0], "controle_juste": [0, 0]}, Counter()
    for acc, kind, true, st, ts in conn.execute(
            "SELECT e.accession, e.kind, e.accepted_ts, e.status, f.accepted_ts FROM sec_exact e "
            "JOIN sec_filings f ON f.accession = e.accession"):
        status[st] += 1
        if st != "ok" or true is None or ts is None:
            continue
        if kind in check:
            check[kind][0] += 1
            check[kind][1] += abs(true - (_late(ts) if kind == "controle_retard" else ts)) <= MATCH_S
        else:
            exact[acc] = true
    reliable = all(n >= CALIBRATION_N // 2 and ok >= CALIBRATION_MIN * n for n, ok in check.values())
    info = {"controles": {k: {"n": n, "confirmes": ok} for k, (n, ok) in check.items()}, "fiable": reliable,
            "lues": status["ok"], "illisibles": status["illisible"], "erreurs": status["erreur"],
            "avant_ouverture": 0, "pendant_seance": 0, "incoherentes": 0}
    return (exact if reliable else {}), info


def _windows(conn: sqlite3.Connection) -> tuple[dict, set, dict, dict]:
    """Par CIK : séance → codes d'item des 8-K de sa fenêtre ; CIK sans aucun 8-K mais avec des 6-K ; qualité ; par CIK,
    séances ambiguës (un 8-K lu hors fenêtre dont l'heure en retard tomberait dans leur fenêtre, sans heure exacte).

    La qualité compte aussi les 8-K par heure de New York (JSON lu en UTC), tous et ceux dont la date New York est la
    date de dépôt : sert à vérifier la lecture de l'heure SEC (un 8-K se dépose surtout vers 6-9 h et 16-17 h 30 ;
    un creux entre 6 h et 10 h trahirait un JSON en retard de 4 h)."""
    by_cik: dict[int, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    ambiguous: dict[int, set] = defaultdict(set)
    has_8k, has_6k = set(), set()
    quality = {"8k": 0, "8k_fenetre": 0, "8k_heure_ambigue": 0, "heures_douteuses": 0, "sans_heure": 0,
               "heures_ny": [0] * 24, "heures_ny_meme_jour": [0] * 24}
    exact, quality["heure_exacte"] = _exact(conn)
    hx = quality["heure_exacte"]
    for acc, cik, form, items, ts, filing_date, suspect in conn.execute(
            "SELECT accession, cik, form, items, accepted_ts, filing_date, suspect FROM sec_filings"):
        if form == "6-K":
            has_6k.add(cik)
            continue
        has_8k.add(cik)
        if form != "8-K":
            continue
        quality["8k"] += 1
        quality["heures_douteuses"] += bool(suspect)
        if ts is None:
            quality["sans_heure"] += 1
            continue
        at = datetime.fromtimestamp(ts, edgar.NEW_YORK)
        quality["heures_ny"][at.hour] += 1
        if at.date().isoformat() == filing_date:
            quality["heures_ny_meme_jour"][at.hour] += 1
        day = window_day(ts)
        if day:
            quality["8k_fenetre"] += 1
            by_cik[cik][day].update(edgar.items(items) or ["?"])
            continue
        alt = edgar.shifted_back(ts)
        alt_day = window_day(alt) if alt else None
        if not alt_day:
            continue
        true = exact.get(acc)
        if true is not None and abs(true - alt) <= MATCH_S:
            hx["avant_ouverture"] += 1
            quality["8k_fenetre"] += 1
            by_cik[cik][alt_day].update(edgar.items(items) or ["?"])
        elif true is not None and abs(true - ts) <= MATCH_S:
            hx["pendant_seance"] += 1
        else:
            hx["incoherentes"] += true is not None
            quality["8k_heure_ambigue"] += 1
            ambiguous[cik].add(alt_day)
    return by_cik, has_6k - has_8k, quality, ambiguous


def classify(symbol: str, day: str, syms: dict, windows: dict, foreign: set, ambiguous: dict | None = None) -> str:
    """Groupe de la séance, ou la raison de l'écarter (« sans_donnee », « sans_cik », « etranger », « non_couvert »,
    « heure_ambigue »)."""
    row = syms.get(symbol)
    if row is None:
        return "sans_donnee"
    cik, status, covered, fetched = row
    if status == "sans_cik":
        return "sans_cik"
    if status != "ok":
        return "sans_donnee"
    if cik in foreign:
        return "etranger"
    need = (date.fromisoformat(day) - timedelta(days=COVER_MARGIN_DAYS)).isoformat()
    if not covered or covered > need or (fetched or 0) < _cutoff(day):
        return "non_couvert"
    if ambiguous and day in ambiguous.get(cik, ()):
        return "heure_ambigue"
    items = windows.get(cik, {}).get(day)
    return group(items) if items else "aucun"


EXCLUSIONS = ("sans_donnee", "sans_cik", "etranger", "non_couvert", "heure_ambigue")


def _tradable(conn, syms, windows, foreign, ambiguous) -> dict:
    cells: dict = {s: defaultdict(lambda: defaultdict(Cell)) for s in STRATEGIES}
    excluded: dict = {s: defaultdict(int) for s in STRATEGIES}
    gross_all: dict = {s: [] for s in STRATEGIES}
    rows = conn.execute(
        "SELECT strategy, day, symbol, r_gross, r_20e, temoin_r, mfe_r, mae_r, exit_reason, vol15_adv "
        f"FROM strat_trades WHERE market = 'stock' AND strategy IN ({','.join('?' * len(STRATEGIES))}) "
        "AND triggered = 1 AND r_gross IS NOT NULL", STRATEGIES)
    for strategy, day, symbol, gross, r20, wit, mfe, mae, exit_reason, vol in rows:
        g = classify(symbol, day, syms, windows, foreign, ambiguous)
        if g in EXCLUSIONS:
            excluded[strategy][g] += 1
            continue
        cells[strategy][vol_bucket(vol)][g].add(gross, r20, wit, mfe, mae, exit_reason)
        gross_all[strategy].append(gross)
    out = {}
    for s in STRATEGIES:
        labels = [b[2] for b in VOL_BUCKETS] + [VOL_INCONNU]
        tranches = {label: {g: cells[s][label][g].to_dict() for g in GROUPS} for label in labels}
        totals = {}
        for g in GROUPS:
            gross = [x for label in labels for x in cells[s][label][g].gross]
            totals[g] = {"n": len(gross), "r_brut": _mean(gross)}

        def compare(attr, s=s):
            pairs = []
            for label in [b[2] for b in VOL_BUCKETS]:
                with_ = [x for g in GROUPS if g != "aucun" for x in getattr(cells[s][label][g], attr)]
                pairs.append((with_, list(getattr(cells[s][label]["aucun"], attr))))
            return stratified(pairs)

        def compare_stop(s=s):
            pairs = []
            for label in [b[2] for b in VOL_BUCKETS]:
                with_ = [c for g in GROUPS if g != "aucun" for c in (cells[s][label][g],)]
                w = [1.0] * sum(c.stops for c in with_) + [0.0] * sum(len(c.gross) - c.stops for c in with_)
                c0 = cells[s][label]["aucun"]
                pairs.append((w, [1.0] * c0.stops + [0.0] * (len(c0.gross) - c0.stops)))
            return stratified(pairs)

        sigma = pstdev(gross_all[s]) if len(gross_all[s]) > 1 else None
        out[s] = {"tranches": tranches, "par_groupe": totals, "exclus": dict(excluded[s]),
                  "a_volume_egal": {"r_brut": compare("gross"), "temoin_r": compare("witness"),
                                    "mae": compare("mae"), "part_stop": compare_stop()},
                  "ecart_type_r_brut": round(sigma, 3) if sigma else None,
                  "effectif_requis": {"z_1_96": required_n(sigma, 1.96), "z_labo": required_n(sigma, Z_LABO)}}
    return out


def _base_rate(conn, syms, windows, foreign, ambiguous) -> dict:
    clause, params = _sealed_sql()
    cells: dict = defaultdict(lambda: defaultdict(lambda: [0, 0, 0]))
    excluded: dict = defaultdict(int)
    for day, symbol, up, gap in conn.execute(
            f"SELECT day, symbol, up_pct, gap_pct FROM day_moves WHERE market = 'stock' AND {clause}", params):
        if up is None:
            continue
        g = classify(symbol, day, syms, windows, foreign, ambiguous)
        if g in EXCLUSIONS:
            excluded[g] += 1
            continue
        c = cells[PO.gap_bucket(gap) or "gap inconnu"][g]
        c[0] += 1
        c[1] += up >= PO.ZONE_PCT
        c[2] += up >= PO.PLUS10_PCT
    labels = [b[2] for b in PO.GAP_BUCKETS] + ["gap inconnu"]
    return {"par_gap": {label: {g: {"n": cells[label][g][0],
                                    "zone_pct": PO._pct(cells[label][g][1], cells[label][g][0]),
                                    "plus10_pct": PO._pct(cells[label][g][2], cells[label][g][0])}
                                for g in GROUPS} for label in labels if label in cells},
            "exclus": dict(excluded)}


def report(conn: sqlite3.Connection) -> dict:
    syms = _symbols(conn)
    windows, foreign, quality, ambiguous = _windows(conn)
    status = defaultdict(int)
    for _, st, _, _ in syms.values():
        status[st] += 1
    status["a_lire"] = len(set(wanted(conn)) - set(syms))
    return {"valeurs": dict(status), "qualite": quality, "fenetre": "clôture précédente → 9 h 45",
            "groupes": GROUPS, "tradable": _tradable(conn, syms, windows, foreign, ambiguous),
            "taux_de_base": _base_rate(conn, syms, windows, foreign, ambiguous),
            "limites": ["Mesure sans essai : rien ici ne filtre ni ne change un plan.",
                        "Un écart ne devient une règle que par une déclaration (n° 12), jugée au seuil du labo et sur "
                        "le bloc scellé, après la n° 11 complète.",
                        "z approché, trades supposés indépendants (pas de grappes par jour).",
                        "Tickers actuels seulement (valeurs radiées ou renommées : « sans CIK »).",
                        "Un 8-K reprend souvent un communiqué déjà diffusé ; environ deux tiers des heures SEC sont en "
                        "retard de 4-5 h (jamais en avance) : séances où ce retard changerait la fenêtre écartées "
                        "(« heure_ambigue »), sauf heure exacte lue sur la page EDGAR et contrôlée.",
                        "Taux de base : journée entière depuis l'ouverture, 9 h 30-9 h 45 compris, pas tradable.",
                        "Bloc scellé exclu."]}


def signature(conn: sqlite3.Connection) -> list:
    filings = conn.execute("SELECT COUNT(*) FROM sec_filings").fetchone()
    exact = conn.execute("SELECT COUNT(*), COALESCE(MAX(fetched_ts), 0) FROM sec_exact").fetchone()
    syms = conn.execute("SELECT COUNT(*), COALESCE(MAX(fetched_ts), 0) FROM sec_symbols").fetchone()
    moves = conn.execute("SELECT COUNT(*), COALESCE(MAX(day), '') FROM day_moves WHERE market = 'stock'").fetchone()
    return [*filings, *syms, *moves, *exact, *E.sig(conn, "strat")]


def cached(conn: sqlite3.Connection) -> dict | None:
    return STATS.cached(conn, CACHE_KEY)


def step(db: LabDB, now: datetime, max_age: float) -> int | None:
    """Passage du worker : recalcule quand les dépôts, les valeurs, les mouvements ou les trades du labo v2 ont changé
    et que le cache a plus de `max_age` s (tout de suite après un déploiement) ; renvoie le nombre de 8-K."""
    sig = signature(db.conn)
    at = db.get("sec8k", "at", 0) or 0
    last = db.get("sec8k", "signature")
    if sig == last or (cached(db.conn) and now.timestamp() - at < max_age and not E.code_changed(last)):
        return None
    data = report(db.conn)
    with db.transaction():
        STATS.store(db.conn, CACHE_KEY, data, sig[0], now_iso())
        db.set("sec8k", "signature", sig)
        db.set("sec8k", "at", now.timestamp())
    return data["qualite"]["8k"]


# --- Discord -----------------------------------------------------------------------------------

def _r(x) -> str:
    return "n/d" if x is None else f"{x:+.3f} R".replace(".", ",")


def _sigma(x) -> str:
    return "n/d" if x is None else f"{x:.2f} R".replace(".", ",")


def _n(x) -> str:
    return "n/d" if x is None else f"{x:,}".replace(",", " ")


def text(data: dict | None) -> str:
    """`!8k` : à volume égal, ce que change un 8-K d'avant 9 h 45 au mouvement restant (mesure sans essai)."""
    if not data:
        return ("📑 Mesure 8-K pas encore calculée (le worker du labo la calcule à son prochain passage, après le "
                "déploiement ; la collecte SEC demande SEC_USER_AGENT dans .env).")
    v, q = data["valeurs"], data["qualite"]
    lines = [f"📑 **Mesure 8-K** (sans essai) · fenêtre {data['fenetre']} New York",
             f"-# Valeurs : {v.get('ok', 0)} lues, {v.get('sans_cik', 0)} sans CIK, {v.get('erreur', 0)} en erreur, "
             f"{_n(v.get('a_lire'))} jamais lues · {_n(q['8k'])} 8-K, dont {_n(q['8k_fenetre'])} dans une fenêtre et "
             f"{_n(q.get('8k_heure_ambigue'))} à heure ambiguë (séance écartée) · heures douteuses "
             f"{_n(q['heures_douteuses'])}"]
    if not q["8k"]:
        lines.append("Pas encore de 8-K collecté.")
        return "\n".join(lines)
    hours = q.get("heures_ny")
    if hours:
        lines.append("-# 8-K par heure New York (JSON lu en UTC) : "
                     + " · ".join(f"{h} h {_n(c)}" for h, c in enumerate(hours) if c))
    hx = q.get("heure_exacte")
    if hx and hx["lues"] + hx["illisibles"] + hx["erreurs"]:
        c = hx["controles"]
        lines.append(f"-# Heure exacte (page EDGAR) : contrôles confirmés {c['controle_retard']['confirmes']}/"
                     f"{c['controle_retard']['n']} retards, {c['controle_juste']['confirmes']}/"
                     f"{c['controle_juste']['n']} heures justes ({'utilisée' if hx['fiable'] else 'pas encore utilisée'})"
                     f" · ambigus résolus : {_n(hx['avant_ouverture'])} avant l'ouverture, "
                     f"{_n(hx['pendant_seance'])} pendant la séance · {_n(hx['illisibles'])} pages illisibles")
    for s, t in data["tradable"].items():
        eq = t["a_volume_egal"]
        rb = eq["r_brut"]
        lines.append(f"**{s}** · avec 8-K {_n(rb['n_avec'])} trades, sans {_n(rb['n_sans'])}")
        z = "n/d" if rb["z"] is None else f"{rb['z']:+.2f}".replace(".", ",")
        stop = eq["part_stop"]["ecart"]
        stop = "n/d" if stop is None else f"{100 * stop:+.1f} pts".replace(".", ",")
        lines.append(f"-# À volume égal, avec − sans : R brut {_r(rb['ecart'])} (z {z}) · témoin "
                     f"{_r(eq['temoin_r']['ecart'])} · MAE {_r(eq['mae']['ecart'])} · part au stop {stop}")
        pg = t["par_groupe"]
        lines.append("-# " + " · ".join(f"{GROUPS[g]} {_n(pg[g]['n'])} ({_r(pg[g]['r_brut'])})" for g in GROUPS))
        req = t["effectif_requis"]
        lines.append(f"-# Pour voir +0,1 R (écart type {_sigma(t['ecart_type_r_brut'])}) : {_n(req['z_1_96'])} trades par "
                     f"groupe à z 1,96, {_n(req['z_labo'])} au seuil du labo")
    lines.append("-# Mesure sans essai : un écart ne devient une règle que par une déclaration (n° 12). Détail : "
                 "GET /api/lab/sec8k.")
    return "\n".join(lines)

