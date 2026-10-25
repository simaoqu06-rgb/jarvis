"""Mesure Polymarket « Fed » : la cote de la prochaine décision de la Fed qui bouge pendant la nuit change-t-elle ce
que rapportent les cassures d'ouverture ? Mesure sans essai.

Demande de Simão (25 octobre 2026, fil « Évaluer l'IA Argona sur Polymarket ») : après l'avis sur Argona (un agent
qui parie seul sur Polymarket, compte public à −126 $ sur 113,7 k$ de volume), il veut tester les cotes Polymarket
« comme stat » pour les actions. Choix de la donnée (pas de pari : Polymarket est bloqué en France) :
- la Fed, parce que ses marchés sont les plus liquides de Polymarket, qu'ils existent pour chaque réunion depuis 2024
  et qu'ils bougent un jour sur deux, pas huit fois par an ;
- une seule variable, figée ici avant toute lecture : le REPRICING DE LA NUIT. Pour la séance J, la variation en
  points de base de la décision attendue à la prochaine réunion (moyenne des issues pondérée par leur cote) entre la
  clôture de J−1 (16 h New York) et 9 h 45 New York (ouverture + CUTOFF_MIN, le moment où le range de 15 min se ferme,
  comme la mesure 8-K). Tout est connu avant l'entrée : pas d'information future ;
- trois groupes, bornes figées : « colombe » (≤ −SEUIL_BPS, la Fed attendue plus basse), « neutre », « faucon »
  (≥ +SEUIL_BPS).

Lecture, sans rien filtrer ni noter (rien ici ne change un plan ni ne compte comme essai) : les trades déclenchés
d'orb15 et orb30 (strat_trades, le bloc scellé n'y est pas), par groupe : R brut, R à 20 €, R du témoin à la même
heure (des entrées au hasard après 9 h 45 : le mouvement du marché ce jour-là, sans signal), part au stop.
Le repricing est le même pour toutes les actions d'une séance : les trades d'un même jour ne sont pas indépendants.
La comparaison se fait donc JOUR PAR JOUR (moyenne des trades de chaque séance, puis écart des moyennes entre groupes,
z de Welch sur les jours) ; l'effectif qui compte est le nombre de séances, affiché avec l'effectif requis pour voir
+0,1 R. Deux écarts : sur le R brut (« les ORB marchent-ils mieux ? ») et sur R brut − témoin (« mieux que des entrées
au hasard le même jour ? », le mouvement d'ensemble du marché retiré).

Données : app/sources/polymarket.py, collectées hors séance US par l'étape « polymarket_collecte » du worker dans le
budget pm_calls. Réunions : calendrier FOMC ci-dessous (dates de décision, 14 h New York). Un événement Polymarket est
rattaché à la réunion de son mois (titre « Fed decision in <mois> », année de sa date de fin). Une séance sans cote
(réunion non trouvée, issues dont les cotes ne somment pas à environ 1) est comptée « sans_cote », hors groupes.

Limites : une cote Polymarket reflète surtout les futures sur les Fed funds (CME FedWatch), elle peut suivre
l'info au lieu de la précéder ; au pas d'une heure, la cote de 9 h 45 est celle de 9 h ; une issue « 50+ bps » compte
pour 50.
Suite : un écart net sur assez de séances peut devenir une déclaration (après la n° 11 complète), jugée au seuil du
labo et sur le bloc scellé. Sinon, la piste est abandonnée.
"""
import logging
import math
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from statistics import pstdev

from app.core.http import SourceError
from app.lab import collector as CO
from app.lab import empreinte as E
from app.lab import heartbeat
from app.lab import sources as SRC
from app.lab import stats as STATS
from app.lab.db import LabDB, now_iso
from app.sources import polymarket as pm

log = logging.getLogger(__name__)
CACHE_KEY = "polyfed"
CUTOFF_MIN = 15
SEUIL_BPS = 2.0
STALE_S = 24 * 3600         # cote de plus de 24 h au moment lu : ignorée (une nuit sans échange garde la cote)
SUM_MIN, SUM_MAX = 0.85, 1.15
FIRST_LOOKBACK_DAYS = 70    # première réunion de la liste : cotes lues depuis 70 jours avant
STRATEGIES = ("orb15", "orb30")
GROUPS = {"colombe": f"colombe (≤ −{SEUIL_BPS:g} pb)", "neutre": "neutre", "faucon": f"faucon (≥ +{SEUIL_BPS:g} pb)"}
EFFECT_R = 0.1
Z_POWER = 0.84
Z_LABO = 3.42
DISCOVERY_HOURS = 20
PAGES_MAX = 5
PAGE_SIZE = 100
ATTEMPTS = 3
PM_LIMIT = SRC.RateLimiter(5, 1.0)


class _BudgetSpent(Exception):
    """Budget d'appels épuisé pendant la découverte : elle reprend au passage suivant."""
# Dates de décision du FOMC (deuxième jour de la réunion, communiqué à 14 h New York), federalreserve.gov.
MEETINGS = tuple(date.fromisoformat(d) for d in (
    "2024-01-31", "2024-03-20", "2024-05-01", "2024-06-12", "2024-07-31", "2024-09-18", "2024-11-07", "2024-12-18",
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18", "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09"))


# --- Fonctions pures ------------------------------------------------------------------------

def next_meeting(day: date) -> date | None:
    """Réunion dont la décision est encore à venir à 9 h 45 le jour `day` (le jour même compte : décision à 14 h)."""
    return next((m for m in MEETINGS if m >= day), None)


def previous_meeting(meeting: date) -> date | None:
    i = MEETINGS.index(meeting)
    return MEETINGS[i - 1] if i else None


def match_meeting(title: str | None, end_date: str | None) -> date | None:
    """Réunion du calendrier d'un événement « Fed decision in <mois> [année] » : même mois, année du titre sinon de
    la date de fin de l'événement."""
    found = pm.meeting_month(title)
    if not found:
        return None
    month, year = found
    if year is None:
        try:
            year = date.fromisoformat(str(end_date)[:10]).year
        except ValueError:
            return None
    return next((m for m in MEETINGS if m.year == year and m.month == month), None)


def price_at(series: list[tuple[int, float]], ts: int) -> float | None:
    """Dernière cote connue à `ts` (jamais après), None si elle a plus de STALE_S."""
    best = None
    for t, p in series:
        if t > ts:
            break
        best = (t, p)
    if best is None or ts - best[0] > STALE_S:
        return None
    return best[1]


def expected_bps(outcomes: list[tuple[int, list[tuple[int, float]]]], ts: int) -> float | None:
    """Décision attendue (pb) à `ts` : moyenne des issues pondérée par leur cote, normalisée ; None si les cotes
    connues ne somment pas à environ 1 (issues manquantes ou figées)."""
    total = weighted = 0.0
    for value, series in outcomes:
        p = price_at(series, ts)
        if p is None:
            continue
        total += p
        weighted += p * value
    if not SUM_MIN <= total <= SUM_MAX:
        return None
    return weighted / total


def day_window(day: date) -> tuple[int, int] | None:
    """(clôture de la séance précédente, 9 h 45 de la séance) en epoch s, None si la Bourse est fermée."""
    bounds = CO.session_ts(day)
    prev = CO.session_ts(CO.previous_trading_day(day))
    if not bounds or not prev:
        return None
    return prev[1], bounds[0] + CUTOFF_MIN * 60


def group(delta: float | None) -> str:
    if delta is None:
        return "sans_cote"
    if delta <= -SEUIL_BPS:
        return "colombe"
    if delta >= SEUIL_BPS:
        return "faucon"
    return "neutre"


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 4) if xs else None


def _var(xs: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return sum((x - m) ** 2 for x in xs) / (len(xs) - 1)


def welch(a: list[float], b: list[float]) -> dict:
    """Écart des moyennes a − b et son z de Welch (ici : une valeur par séance)."""
    va, vb = _var(a), _var(b)
    if va is None or vb is None:
        return {"ecart": None, "z": None, "jours": len(a), "jours_ref": len(b)}
    diff = sum(a) / len(a) - sum(b) / len(b)
    se = math.sqrt(va / len(a) + vb / len(b))
    return {"ecart": round(diff, 4), "z": round(diff / se, 2) if se else None, "jours": len(a), "jours_ref": len(b)}


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return round(sxy / (sx * sy), 3) if sx and sy else None


def required_days(sigma: float | None, z: float) -> int | None:
    """Séances par groupe pour voir EFFECT_R au seuil z, puissance 80 %, sur l'écart type des moyennes par séance."""
    if not sigma:
        return None
    return math.ceil(2 * (z + Z_POWER) ** 2 * sigma ** 2 / EFFECT_R ** 2)


# --- Collecte (réseau) ----------------------------------------------------------------------

async def _call(fn, *args):
    return await SRC.with_retry(fn, *args, limiter=PM_LIMIT, tries=3)


def wanted_meetings(conn: sqlite3.Connection, today: date) -> list[date]:
    """Réunions dont la cote sert à une séance des trades mesurés (première séance → aujourd'hui)."""
    row = conn.execute(f"SELECT MIN(day) FROM strat_trades WHERE market = 'stock' AND strategy IN "
                       f"({','.join('?' * len(STRATEGIES))})", STRATEGIES).fetchone()
    if not row or not row[0]:
        return []
    first = next_meeting(date.fromisoformat(row[0]))
    last = next_meeting(today)
    return [m for m in MEETINGS if first and first <= m <= (last or MEETINGS[-1])]


def _store_event(db: LabDB, meeting: date, event: dict) -> int:
    n = 0
    start = str(event.get("startDate") or event.get("creationDate") or event.get("createdAt") or "")[:19]
    try:
        start_ts = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp()) if start else None
    except ValueError:
        start_ts = None
    for o in pm.outcomes(event):
        db.conn.execute(
            "INSERT INTO pm_markets (token, meeting, event_slug, label, bps, start_ts, status, covered_to_ts, "
            "attempts) VALUES (?, ?, ?, ?, ?, ?, 'ok', NULL, 0) ON CONFLICT (token) DO UPDATE SET "
            "meeting = excluded.meeting, event_slug = excluded.event_slug, label = excluded.label, bps = excluded.bps, "
            "start_ts = COALESCE(excluded.start_ts, pm_markets.start_ts)",
            (o["token"], meeting.isoformat(), str(event.get("slug") or "")[:120], o["label"], o["bps"], start_ts))
        n += 1
    return n


async def discover(db: LabDB, now: datetime, budget: int) -> dict:
    """Événements « Fed decision » de Polymarket par étiquette (ouverts et fermés), puis par slug pour les réunions
    encore sans marché. Une fois par DISCOVERY_HOURS au plus."""
    rep = {"appels": 0, "evenements": 0, "marches": 0}
    last = db.get("polyfed", "discovery_ts", 0) or 0
    if now.timestamp() - last < DISCOVERY_HOURS * 3600 or budget <= 0:
        return rep
    want = set(wanted_meetings(db.conn, now.date()))
    seen: set[str] = set()

    def keep(event) -> None:
        meeting = match_meeting(event.get("title"), event.get("endDate"))
        slug = str(event.get("slug") or event.get("id") or "")
        if meeting is None or meeting not in want or slug in seen:
            return
        seen.add(slug)
        rep["evenements"] += 1
        rep["marches"] += _store_event(db, meeting, event)

    errors: list[str] = []

    async def get(params) -> list[dict]:
        if rep["appels"] >= budget:
            raise _BudgetSpent
        rep["appels"] += 1
        try:
            return await _call(pm.events, params)
        except SourceError as exc:  # une étiquette ou un slug absent n'arrête pas la découverte
            errors.append(f"{params} : {exc}"[:200])
            log.info("Labo : événements Polymarket %s indisponibles (%s)", params, exc)
            return []

    try:
        for tag in pm.TAGS:
            for closed in ("true", "false"):
                for page in range(PAGES_MAX):
                    batch = await get({"tag_slug": tag, "closed": closed, "limit": PAGE_SIZE,
                                       "offset": page * PAGE_SIZE})
                    with db.transaction():
                        for event in batch:
                            keep(event)
                    if len(batch) < PAGE_SIZE:
                        break
        have = {date.fromisoformat(r[0]) for r in db.conn.execute("SELECT DISTINCT meeting FROM pm_markets")}
        for meeting in sorted(want - have):
            name = pm.MONTHS[meeting.month - 1]
            for slug in (f"fed-decision-in-{name}-{meeting.year}", f"fed-decision-in-{name}"):
                batch = await get({"slug": slug})
                with db.transaction():
                    for event in batch:
                        keep(event)
    except _BudgetSpent:
        return rep
    db.set("polyfed", "discovery_ts", now.timestamp())
    db.set("polyfed", "discovery", rep | {"erreurs": errors[:10]})
    return rep | ({"erreurs": len(errors)} if errors else {})


def _price_range(meeting: date, start_ts: int | None, now: datetime) -> tuple[int, int]:
    """Cotes utiles d'une issue : de la veille de la réunion précédente (ou FIRST_LOOKBACK_DAYS) au lendemain de la
    sienne, sans dépasser maintenant ni remonter avant la création du marché."""
    prev = previous_meeting(meeting) or meeting - timedelta(days=FIRST_LOOKBACK_DAYS)
    lo = CO.ny_midnight(prev - timedelta(days=1))
    hi = min(CO.ny_midnight(meeting + timedelta(days=1)), int(now.timestamp()))
    return max(lo, start_ts or 0), hi


async def fetch(db: LabDB, now: datetime, budget: int) -> dict:
    """Étape « polymarket_collecte » du worker : découverte des événements, puis cotes horaires des issues, par tranches
    de 7 jours, des réunions les plus récentes d'abord ; `budget` appels au plus."""
    rep = {"appels": 0, "tranches": 0, "erreurs": 0}
    if budget <= 0:
        return rep
    found = await discover(db, now, budget)
    rep["appels"] += found["appels"]
    rep["decouverte"] = found
    rows = db.conn.execute("SELECT token, meeting, start_ts, covered_to_ts, status, attempts FROM pm_markets "
                           "ORDER BY meeting DESC").fetchall()
    for token, meeting, start_ts, covered, status, attempts in rows:
        if status == "erreur" and (attempts or 0) >= ATTEMPTS:
            continue
        lo, hi = _price_range(date.fromisoformat(meeting), start_ts, now)
        t = max(lo, covered or 0)
        while t < hi:
            if rep["appels"] >= budget:
                return rep
            end = min(t + pm.CHUNK_S, hi)
            rep["appels"] += 1
            try:
                points = await _call(pm.history, token, t, end)
            except SourceError as exc:
                rep["erreurs"] += 1
                db.conn.execute("UPDATE pm_markets SET status = 'erreur', attempts = attempts + 1, note = ? "
                                "WHERE token = ?", (f"CLOB : {exc}"[:200], token))
                log.info("Labo : cotes Polymarket de %s indisponibles (%s)", token[:12], exc)
                break
            with db.transaction():
                db.conn.executemany("INSERT OR REPLACE INTO pm_prices (token, ts, p) VALUES (?, ?, ?)",
                                    [(token, ts, p) for ts, p in points])
                # Une réunion à venir garde une marge d'une heure : la dernière tranche est relue au passage suivant.
                done = end if end < int(now.timestamp()) - 3600 else end - 3600
                db.conn.execute("UPDATE pm_markets SET covered_to_ts = ?, status = 'ok', attempts = 0, note = NULL, "
                                "fetched_ts = ? WHERE token = ?", (done, int(now.timestamp()), token))
            rep["tranches"] += 1
            heartbeat.beat()
            t = end
    return rep


# --- Mesure (base seule) --------------------------------------------------------------------

def _series(conn: sqlite3.Connection) -> dict[str, list[tuple[int, list[tuple[int, float]]]]]:
    """Par réunion : [(pb de l'issue, cotes triées)]."""
    prices: dict[str, list] = defaultdict(list)
    for token, ts, p in conn.execute("SELECT token, ts, p FROM pm_prices ORDER BY token, ts"):
        prices[token].append((ts, p))
    out: dict[str, list] = defaultdict(list)
    for token, meeting, value in conn.execute("SELECT token, meeting, bps FROM pm_markets"):
        out[meeting].append((value, prices.get(token, [])))
    return out


def features(conn: sqlite3.Connection, days: list[str]) -> dict[str, dict]:
    """Par séance : réunion, décision attendue à la clôture de la veille et à 9 h 45, repricing, groupe."""
    series = _series(conn)
    out = {}
    for day in days:
        d = date.fromisoformat(day)
        meeting = next_meeting(d)
        window = day_window(d)
        e0 = e1 = None
        if meeting and window and series.get(meeting.isoformat()):
            outs = series[meeting.isoformat()]
            e0, e1 = expected_bps(outs, window[0]), expected_bps(outs, window[1])
        delta = round(e1 - e0, 3) if e0 is not None and e1 is not None else None
        out[day] = {"reunion": meeting.isoformat() if meeting else None, "attendu_veille": e0, "attendu_9h45": e1,
                    "repricing_pb": delta, "groupe": group(delta)}
    return out


def _tradable(conn: sqlite3.Connection, feats: dict[str, dict]) -> dict:
    out = {}
    for s in STRATEGIES:
        by_day: dict[str, list] = defaultdict(list)
        for day, gross, r20, wit, exit_reason in conn.execute(
                "SELECT day, r_gross, r_20e, temoin_r, exit_reason FROM strat_trades WHERE market = 'stock' "
                "AND strategy = ? AND triggered = 1 AND r_gross IS NOT NULL", (s,)):
            by_day[day].append((gross, r20, wit, exit_reason))
        cells = {g: {"trades": 0, "gross": [], "r20": [], "wit": [], "stops": 0, "jours_r": [], "jours_net": [],
                     "deltas": []} for g in GROUPS}
        sans_cote = {"jours": 0, "trades": 0}
        xs, ys, ws = [], [], []
        for day, rows in by_day.items():
            f = feats.get(day) or {"groupe": "sans_cote"}
            g = f["groupe"]
            if g not in GROUPS:
                sans_cote["jours"] += 1
                sans_cote["trades"] += len(rows)
                continue
            c = cells[g]
            c["trades"] += len(rows)
            c["gross"] += [r[0] for r in rows]
            c["r20"] += [r[1] for r in rows if r[1] is not None]
            c["wit"] += [r[2] for r in rows if r[2] is not None]
            c["stops"] += sum((r[3] or "").startswith("stop") for r in rows)
            day_r = sum(r[0] for r in rows) / len(rows)
            c["jours_r"].append(day_r)
            paired = [r[0] - r[2] for r in rows if r[2] is not None]
            if paired:
                c["jours_net"].append(sum(paired) / len(paired))
                ws.append(sum(paired) / len(paired))
            else:
                ws.append(None)
            c["deltas"].append(f["repricing_pb"])
            xs.append(f["repricing_pb"])
            ys.append(day_r)
        groups = {g: {"jours": len(c["jours_r"]), "trades": c["trades"], "r_brut": _mean(c["gross"]),
                      "r_20e": _mean(c["r20"]), "temoin_r": _mean(c["wit"]),
                      "part_stop": round(c["stops"] / c["trades"], 4) if c["trades"] else None,
                      "r_brut_par_jour": _mean(c["jours_r"]), "r_moins_temoin_par_jour": _mean(c["jours_net"])}
                  for g, c in cells.items()}
        all_days = [x for c in cells.values() for x in c["jours_r"]]
        sigma = pstdev(all_days) if len(all_days) > 1 else None
        pairs = [(x, w) for x, w in zip(xs, ws) if w is not None]
        out[s] = {"par_groupe": groups, "sans_cote": sans_cote,
                  "ecarts_par_jour": {
                      "faucon_moins_neutre": {"r_brut": welch(cells["faucon"]["jours_r"], cells["neutre"]["jours_r"]),
                                              "r_moins_temoin": welch(cells["faucon"]["jours_net"],
                                                                      cells["neutre"]["jours_net"])},
                      "colombe_moins_neutre": {"r_brut": welch(cells["colombe"]["jours_r"], cells["neutre"]["jours_r"]),
                                               "r_moins_temoin": welch(cells["colombe"]["jours_net"],
                                                                       cells["neutre"]["jours_net"])}},
                  "correlation": {"r_brut": pearson(xs, ys),
                                  "r_moins_temoin": pearson([p[0] for p in pairs], [p[1] for p in pairs]),
                                  "jours": len(xs)},
                  "ecart_type_jour": round(sigma, 3) if sigma else None,
                  "jours_requis": {"z_1_96": required_days(sigma, 1.96), "z_labo": required_days(sigma, Z_LABO)}}
    return out


def _coverage(conn: sqlite3.Connection, feats: dict[str, dict]) -> dict:
    markets = defaultdict(lambda: {"issues": 0, "cotes": 0, "erreurs": 0})
    for meeting, status, n in conn.execute(
            "SELECT m.meeting, m.status, (SELECT COUNT(*) FROM pm_prices p WHERE p.token = m.token) FROM pm_markets m"):
        c = markets[meeting]
        c["issues"] += 1
        c["cotes"] += n
        c["erreurs"] += status == "erreur"
    wanted = sorted({f["reunion"] for f in feats.values() if f["reunion"]})
    deltas = sorted(abs(f["repricing_pb"]) for f in feats.values() if f["repricing_pb"] is not None)

    def pct(q):
        return round(deltas[min(len(deltas) - 1, int(q * len(deltas)))], 2) if deltas else None

    groups = defaultdict(int)
    for f in feats.values():
        groups[f["groupe"]] += 1
    return {"reunions": {m: markets.get(m, {"issues": 0, "cotes": 0, "erreurs": 0}) for m in wanted},
            "reunions_sans_marche": [m for m in wanted if m not in markets],
            "seances": dict(groups), "repricing_abs_pb": {"p50": pct(0.5), "p90": pct(0.9), "max": pct(1.0)}}


def report(conn: sqlite3.Connection) -> dict:
    days = [r[0] for r in conn.execute(
        f"SELECT DISTINCT day FROM strat_trades WHERE market = 'stock' AND strategy IN "
        f"({','.join('?' * len(STRATEGIES))}) AND triggered = 1 ORDER BY day", STRATEGIES)]
    feats = features(conn, days)
    return {"variable": "repricing de la nuit : décision attendue à la prochaine réunion de la Fed, 9 h 45 − clôture "
                        "de la veille (points de base)",
            "seuil_pb": SEUIL_BPS, "groupes": GROUPS, "couverture": _coverage(conn, feats),
            "tradable": _tradable(conn, feats),
            "limites": ["Mesure sans essai : rien ici ne filtre ni ne change un plan.",
                        "Comparaison par séance (le repricing est le même pour toutes les actions d'un jour) : "
                        "l'effectif est le nombre de séances, pas de trades.",
                        "Une cote Polymarket suit surtout les futures Fed funds : elle peut refléter l'info sans la "
                        "précéder. Pas de 9 h 45 exact : cote horaire, la dernière avant 9 h 45.",
                        "Un écart ne devient une règle que par une déclaration, après la n° 11 complète, jugée au "
                        "seuil du labo et sur le bloc scellé.",
                        "Bloc scellé exclu (strat_trades ne le contient pas)."]}


def signature(conn: sqlite3.Connection) -> list:
    prices = conn.execute("SELECT COUNT(*), COALESCE(MAX(ts), 0) FROM pm_prices").fetchone()
    markets = conn.execute("SELECT COUNT(*) FROM pm_markets").fetchone()
    return [*prices, *markets, *E.sig(conn, "strat")]


def cached(conn: sqlite3.Connection) -> dict | None:
    return STATS.cached(conn, CACHE_KEY)


def step(db: LabDB, now: datetime, max_age: float) -> int | None:
    """Passage du worker : recalcule quand les cotes ou les trades du labo v2 ont changé et que le cache a plus de
    `max_age` s (tout de suite après un déploiement) ; renvoie le nombre de cotes."""
    sig = signature(db.conn)
    at = db.get("polyfed", "at", 0) or 0
    last = db.get("polyfed", "signature")
    if sig == last or (cached(db.conn) and now.timestamp() - at < max_age and not E.code_changed(last)):
        return None
    data = report(db.conn)
    with db.transaction():
        STATS.store(db.conn, CACHE_KEY, data, sig[0], now_iso())
        db.set("polyfed", "signature", sig)
        db.set("polyfed", "at", now.timestamp())
    return sig[0]


# --- Discord -----------------------------------------------------------------------------------

def _r(x) -> str:
    return "n/d" if x is None else f"{x:+.3f} R".replace(".", ",")


def _z(x) -> str:
    return "n/d" if x is None else f"{x:+.2f}".replace(".", ",")


def _n(x) -> str:
    return "n/d" if x is None else f"{x:,}".replace(",", " ")


def _pb(x) -> str:
    return "n/d" if x is None else f"{x:g} pb".replace(".", ",")


def text(data: dict | None) -> str:
    """`!fed` : ce que change le repricing de la Fed sur Polymarket pendant la nuit aux ORB (mesure sans essai)."""
    if not data:
        return ("🏛️ Mesure Polymarket Fed pas encore calculée (le worker du labo la calcule à son prochain passage "
                "après le déploiement ; les cotes se collectent hors séance US).")
    cov = data["couverture"]
    reunions = cov["reunions"]
    avec = sum(1 for c in reunions.values() if c["cotes"])
    rp = cov["repricing_abs_pb"]
    seances = cov["seances"]
    lines = [f"🏛️ **Mesure Polymarket Fed** (sans essai) · repricing de la nuit, seuil ±{data['seuil_pb']:g} pb",
             f"-# Réunions : {avec}/{len(reunions)} avec des cotes"
             + (f" · sans marché trouvé : {', '.join(cov['reunions_sans_marche'][:6])}"
                if cov["reunions_sans_marche"] else "")
             + " · séances : " + " · ".join(f"{g} {_n(seances.get(g, 0))}" for g in (*GROUPS, "sans_cote"))
             + f" · |repricing| médian {_pb(rp['p50'])}, p90 {_pb(rp['p90'])}"]
    if not avec:
        lines.append("Pas encore de cote collectée.")
        return "\n".join(lines)
    for s, t in data["tradable"].items():
        pg = t["par_groupe"]
        lines.append(f"**{s}** · " + " · ".join(
            f"{GROUPS[g]} {_n(pg[g]['jours'])} j / {_n(pg[g]['trades'])} trades ({_r(pg[g]['r_brut'])}, témoin "
            f"{_r(pg[g]['temoin_r'])})" for g in GROUPS))
        for key, label in (("faucon_moins_neutre", "faucon − neutre"), ("colombe_moins_neutre", "colombe − neutre")):
            e = t["ecarts_par_jour"][key]
            lines.append(f"-# {label}, par séance : R brut {_r(e['r_brut']['ecart'])} (z {_z(e['r_brut']['z'])}) · "
                         f"R − témoin {_r(e['r_moins_temoin']['ecart'])} (z {_z(e['r_moins_temoin']['z'])})")
        req = t["jours_requis"]
        lines.append(f"-# Corrélation repricing → R du jour {_z(t['correlation']['r_brut'])} sur "
                     f"{_n(t['correlation']['jours'])} séances · pour voir +0,1 R : {_n(req['z_1_96'])} séances par "
                     f"groupe à z 1,96, {_n(req['z_labo'])} au seuil du labo")
    lines.append("-# Mesure sans essai : un écart ne devient une règle que par une déclaration. Détail : "
                 "GET /api/lab/polyfed.")
    return "\n".join(lines)
