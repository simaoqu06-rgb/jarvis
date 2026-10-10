"""Déclaration n° 8, D1 : Killmode complet rejoué, règle de prix du labo et pilier catalyseur lu dans l'archive.

Le labo ne mesurait que le squelette de Killmode (stratégie killmode, déclaration n° 5), sans catalyseur. Pour
chaque séance dont l'archive des actus est complète (archive.py : actus de chaque gapper, calendrier des
résultats de D − 10 à D + 30, profil de chaque société), chaque gapper archivé passe par :
1. la règle de prix de Killmode, celle du labo (strategies.strategy_rows, stratégie killmode) sur les bougies
   de 5 min SIP d'Alpaca redemandées et gardées en mémoire seulement, comme le rattrapage : ordre posé à 9 h 45
   New York (fin du range de 15 min), sorties et coûts du labo, dont r_20e (mise de Simão) et r_3c (stress de
   3 cents) ; un gapper sans signal de la règle n'est pas noté. Amendement du 10 octobre 2026, avant toute
   séance calculée : prix réellement négociés, comme la n° 9 (bougies non ajustées, journalier remis à l'échelle
   de la clôture brute de la veille, traded_prices) ; une opération sur titre entre la veille et la séance
   écarte le gapper (compté à part) ;
2. le pilier catalyseur du direct, le même code : catalyst.check(symbole, 9 h 45) lit l'archive au lieu de
   Finnhub (ArchiveFinnhub, mêmes signatures), puis killmode.catalyst_codes. Le code est figé par son empreinte
   (CATALYST_CODE_N8, killmode.CATALYST_CODE le jour de la déclaration) : s'il change, la mesure s'arrête ;
3. le groupe, comme le journal du direct (killmode.group et clean) : « kill » (catalyseur réuni),
   « temoin_propre » (seulement « pas_de_positive »), « temoin_autre » (un veto : actus négatives, résultats
   dans 24 h, augmentation de capital) ; à part, ni KILL ni témoin : « donnees » quand une source manque (le
   pilier « données complètes » du direct : profil inconnu, calendrier non archivé) et « fenetre_coupee »
   quand la profondeur de Finnhub a coupé les 7 jours d'actus.
Mesure principale (essai killmode_complet) : espérance de r_20e des KILL déclenchés, IC en t à G − 1 jours.
Mesure secondaire (essai killmode_complet_ecart_temoin) : écart KILL − témoin propre apparié par jour, une valeur
par séance où les deux groupes ont un trade déclenché (moyenne des KILL − moyenne des témoins propres).
Mesure sans essai : les KILL par motif (killmode.call_motif : résultats, fond, mouvement).
r_20e et r_3c suivent killmode.real_r et stressed_r en vigueur, aux prix négociés : chaque ligne garde l'empreinte de
cette méthode de coût (cost_fingerprint) ; quand elle change (PR #44 : gain compté en actions entières), les séances déjà calculées
sont recalculées et ne comptent pas en attendant, pour que toutes les séances aient le même coût.
Le direct relit le catalyseur pendant toute la vie de l'ordre ; le labo le lit une fois, à 9 h 45.
"""
import hashlib
import json
import logging
import sqlite3
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from app.core.http import SourceError
from app.lab import archive as ARC
from app.lab import collector as CO
from app.lab import heartbeat
from app.lab import plus10 as P10
from app.lab import seal as SEAL
from app.lab import sources as SRC
from app.lab import strat_stats as SS
from app.lab import strategies as ST
from app.lab.db import LabDB
from app.trading import catalyst as C
from app.trading import killmode as K

log = logging.getLogger(__name__)
JOB = ARC.KILL_JOB
# Empreinte du pilier catalyseur (killmode.catalyst_fingerprint) déclarée avec la n° 8 : un autre code = une
# autre déclaration.
CATALYST_CODE_N8 = "37f15d7363"
GROUPS = {"kill": "🔪 KILL", "temoin_propre": "témoins propres", "temoin_autre": "témoins avec un veto",
          "donnees": "données incomplètes", "fenetre_coupee": "actus coupées par la profondeur de Finnhub"}
PAIRED = ("kill", "temoin_propre")
STOCK_BATCH = 100
DAILY_LOOKBACK_DAYS = 90
READY_SCAN = 50
EARNINGS_KEYS = ("date", "hour", "eps_estimate", "eps_actual", "revenue_estimate", "revenue_actual")
NEWS_KEYS = ("title", "url", "published_at", "source", "summary")
COLUMNS = ("day", "symbol", "groupe", "codes", "motif", "empreinte", "gap_pct", "atr_pct", "entry", "stop",
           "signal_at", "triggered", "exit_reason", "r_net", "r_20e", "r_3c", "actus", "cout")
# Base des prix des exécutions (amendement du 10 octobre 2026) : changer de base recalcule toutes les séances.
PRICES = "brut"
# Trades types (sorties du simulateur, actions) qui fixent la méthode de coût : leurs r_20e et r_3c changent dès
# que killmode.real_r ou stressed_r changent. Gros et petit nombre d'actions, sous 1 $, TP, stop, stop à
# l'entrée, clôture.
COST_PROBES = tuple(
    {"triggered": True, "fill_price": fill, "order_prices": {"entry": e, "stop": s, "targets": t},
     "exits": [{"part": part, "price": price, "reason": reason} for part, price, reason in exits]}
    for fill, e, s, t, exits in (
        (10.0, 10.0, 9.7, [10.3, 10.6, 10.9], ((1 / 3, 10.3, "tp1"), (1 / 3, 10.6, "tp2"), (1 / 3, 10.75, "cloture"))),
        (4.01, 4.0, 3.88, [4.12, 4.24, 4.36], ((1 / 3, 4.12, "tp1"), (2 / 3, 4.0, "stop_be"))),
        (0.8, 0.8, 0.76, [0.84, 0.88, 0.92], ((1.0, 0.76, "stop"),)),
        (120.0, 120.0, 112.0, [128.0, 136.0, 144.0], ((1 / 3, 128.0, "tp1"), (2 / 3, 120.0, "stop_be")))))


def frozen() -> bool:
    return K.CATALYST_CODE == CATALYST_CODE_N8


def cost_fingerprint() -> str:
    """Empreinte de la méthode de coût en vigueur : base des prix (PRICES), révision du simulateur des lignes
    (strategies.SIM_REV, n° 10) et r_20e et r_3c des COST_PROBES."""
    values = [PRICES, ST.SIM_REV] + [ST.cost_variants("stock", p) for p in COST_PROBES]
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()[:10]


class ArchiveFinnhub:
    """Finnhub tel que l'archive l'a gardé pour un gapper et une séance : les trois appels de catalyst.check,
    mêmes signatures et mêmes formes que app.sources.finnhub. Ce qui manque lève SourceError, comme Finnhub
    en panne, et le direct en ferait un pilier « données » manquant."""

    def __init__(self, profile: dict | None, calendar: dict[str, dict | None], news: list[dict]):
        self._profile, self._calendar, self._news = profile, calendar, news

    async def profile(self, symbol: str) -> dict:
        p = self._profile or {}
        if p.get("inconnu"):
            raise SourceError(f"{symbol} : profil inconnu")
        if not p.get("nom"):
            raise SourceError(f"{symbol} : profil non archivé")
        return {"symbol": symbol, "name": p["nom"], "industry": p.get("secteur"), "market_cap": p.get("capitalisation")}

    async def earnings(self, symbol: str, start: date, end: date) -> list[dict]:
        out, x = [], start
        while x <= end:
            index = self._calendar.get(x.isoformat())
            if index is None:
                raise SourceError(f"calendrier des résultats du {x} non archivé")
            out += [{k: e.get(k) for k in EARNINGS_KEYS} for e in index.get(symbol, [])]
            x += timedelta(days=1)
        return out

    async def company_news(self, symbol: str, limit: int, days: int = ARC.NEWS_DAYS) -> list[dict]:
        return [{k: n.get(k) for k in NEWS_KEYS} for n in self._news[:limit]]


async def catalyst(symbol: str, now: datetime, stub: ArchiveFinnhub) -> dict:
    """catalyst.check tel quel, Finnhub remplacé par l'archive le temps de l'appel. Le worker du labo est un
    processus à part (jamais le direct) et ne fait rien d'autre pendant ce temps."""
    saved = C.finnhub
    C.finnhub = stub
    try:
        return await C.check(symbol, now)
    finally:
        C.finnhub = saved


def classify(cat: dict, now: datetime, window_complete: bool) -> tuple[str, list[str]]:
    """Groupe du gapper (GROUPS) et codes du catalyseur (killmode.catalyst_codes)."""
    codes = K.catalyst_codes(cat, now)
    if cat.get("errors"):
        return "donnees", codes
    if not window_complete:
        return "fenetre_coupee", codes
    if not codes:
        return "kill", codes
    return ("temoin_propre" if codes == ["pas_de_positive"] else "temoin_autre"), codes


# --- Séances prêtes et calcul -------------------------------------------------------------------

def _window(day: date) -> list[date]:
    return [day + timedelta(days=k) for k in range(-ARC.EARNINGS_BEFORE, ARC.EARNINGS_AFTER + 1)]


def ready_days(db: LabDB, limit: int = READY_SCAN) -> list[date]:
    """Séances listées par l'archive, avec des gappers, pas encore calculées, dont rien n'est plus à chercher
    (actus, calendrier de D − 10 à D + 30, profils), la plus ancienne d'abord."""
    done = db.keys(JOB)
    earnings = dict(db.conn.execute("SELECT day, status FROM earnings_days"))
    pending = {d for (d,) in db.conn.execute("SELECT DISTINCT day FROM gap_news WHERE status = 'a_chercher'")}
    profiles = set(db.keys(ARC.PROFILE_JOB))
    out = []
    for key, n in sorted(db.keys(ARC.POPULATION_JOB).items()):
        if not n or key in done or key in pending or SEAL.sealed("replay", key):
            continue
        day = date.fromisoformat(key)
        if any(earnings.get(x.isoformat()) not in ("ok", "erreur") for x in _window(day)):
            continue
        symbols = [s for (s,) in db.conn.execute("SELECT symbol FROM gap_news WHERE day = ? AND status = 'ok'",
                                                 (key,))]
        if any(s not in profiles for s in symbols):
            continue
        out.append(day)
        if len(out) >= limit:
            break
    return out


def calendar(db: LabDB, day: date) -> dict[str, dict | None]:
    """Calendrier archivé de D − 10 à D + 30 : {date: {symbole: [publications]}} ; None pour une date en erreur."""
    out: dict[str, dict | None] = {}
    for x in _window(day):
        row = db.conn.execute("SELECT status, items FROM earnings_days WHERE day = ?", (x.isoformat(),)).fetchone()
        if not row or row[0] != "ok":
            out[x.isoformat()] = None
            continue
        index: dict[str, list] = defaultdict(list)
        for e in json.loads(row[1] or "[]"):
            index[e.get("symbol")].append(e)
        out[x.isoformat()] = dict(index)
    return out


def traded_prices(hist5: list[dict], session: list[dict], daily: list[dict],
                  gap_pct: float) -> tuple[list[dict] | None, str | None]:
    """Journalier ajusté remis à l'échelle des prix négociés (clôture brute de la veille / clôture ajustée), comme la
    n° 9 (plus10.symbol_rows) ; (None, raison) sans historique brut (« donnees ») ou quand le gap brut et le gap
    ajusté de l'archive divergent de plus de plus10.OP_RATIO (« operation_titre » entre la veille et la séance)."""
    raw_prev = hist5[-1]["c"] if hist5 else None
    if not raw_prev or raw_prev <= 0 or not daily or daily[-1]["c"] <= 0:
        return None, "donnees"
    if abs((session[0]["o"] / raw_prev) / (1 + gap_pct / 100) - 1) > P10.OP_RATIO:
        return None, "operation_titre"
    return P10.scale(daily, raw_prev / daily[-1]["c"]), None


def price_rule(day: date, symbol: str, hist5: list[dict], session: list[dict], daily: list[dict]) -> dict | None:
    """Ligne de la stratégie killmode du labo pour ce gapper (comme strategies.stock_day), None sans signal."""
    bars = CO.regular_hours(hist5[-ST.STOCK_WARMUP_BARS * 2:])[-ST.STOCK_WARMUP_BARS:] + session
    d = ST.Day("stock", day, bars, daily)
    prev = d.prev_close
    gap = round((session[0]["o"] / prev - 1) * 100, 3) if session and prev else None
    rows = ST.strategy_rows(d, symbol, None, gap, names=["killmode"])
    return rows[0] if rows else None


async def day_rows(db: LabDB, day: date, transport=None) -> tuple[list[dict], dict]:
    """Lignes d'une séance prête et ses comptes (gappers listés, archivés, signaux de la règle de prix)."""
    key, cost = day.isoformat(), cost_fingerprint()
    listed = db.conn.execute("SELECT COUNT(*) FROM gap_news WHERE day = ?", (key,)).fetchone()[0]
    gappers = db.conn.execute("SELECT symbol, news, news_from, gap_pct FROM gap_news WHERE day = ? AND status = 'ok' "
                              "ORDER BY symbol", (key,)).fetchall()
    stats = {"gappers": listed, "archives": len(gappers), "signaux": 0, "operations": 0, "sans_historique": 0}
    bounds = CO.session_ts(day)
    if not bounds or not gappers:
        return [], stats
    first = CO.session_ts(CO.sessions_back(day, 2))[0]
    cutoff = CO.ny_midnight(day)
    now = datetime.fromtimestamp(ARC.cutoff_ts(day), timezone.utc)
    cal = calendar(db, day)
    full_from = (day - timedelta(days=ARC.NEWS_DAYS)).isoformat()
    info = {s: (json.loads(news or "[]"), (news_from or key) <= full_from) for s, news, news_from, _ in gappers}
    gaps = {s: g for s, _, _, g in gappers}
    names = list(info)
    rows: list[dict] = []
    for i in range(0, len(names), STOCK_BATCH):
        batch = names[i:i + STOCK_BATCH]
        bars = await SRC.alpaca_bars(batch, CO.BASE_TF, datetime.fromtimestamp(first, timezone.utc),
                                     datetime.fromtimestamp(bounds[1], timezone.utc), transport=transport,
                                     adjustment="raw")
        heartbeat.beat()
        for symbol in batch:
            b5 = CO.regular_hours(bars.get(symbol) or [])
            session = [b for b in b5 if bounds[0] <= b["t"] < bounds[1]]
            if not session:
                continue
            hist = [b for b in b5 if b["t"] < bounds[0]]
            daily, anomaly = traded_prices(
                hist, session, db.get_bars("stock", symbol, 1440, cutoff - DAILY_LOOKBACK_DAYS * 86400, cutoff),
                gaps[symbol])
            if anomaly:
                stats["operations" if anomaly == "operation_titre" else "sans_historique"] += 1
                continue
            try:
                r = price_rule(day, symbol, hist, session, daily)
            except Exception as exc:  # noqa: BLE001 (comme le rattrapage : une action ne bloque pas la séance)
                log.warning("Labo : Killmode complet %s %s ignoré (%s: %s)", symbol, day, type(exc).__name__, exc)
                continue
            if r is None:
                continue
            news, complete = info[symbol]
            stub = ArchiveFinnhub(db.get(ARC.PROFILE_JOB, symbol), cal, news)
            cat = await catalyst(symbol, now, stub)
            groupe, codes = classify(cat, now, complete)
            rows.append({"day": key, "symbol": symbol, "groupe": groupe, "codes": json.dumps(codes),
                         "motif": K.call_motif(cat, now), "empreinte": K.CATALYST_CODE, "gap_pct": r["gap_open_pct"],
                         "atr_pct": r["atr_pct"], "entry": r["entry"], "stop": r["stop"], "signal_at": r["signal_at"],
                         "triggered": int(bool(r["triggered"])), "exit_reason": r["exit_reason"], "r_net": r["r_net"],
                         "r_20e": r.get("r_20e"), "r_3c": r.get("r_3c"), "actus": len(news), "cout": cost})
    stats["signaux"] = len(rows)
    return rows, stats


async def step(db: LabDB, max_seconds: float, transport=None) -> int | str:
    """Worker : calcule des séances prêtes dans le temps donné ; renvoie le nombre de séances faites. Rien si la
    définition du catalyseur a changé depuis la déclaration."""
    if max_seconds <= 0:
        return 0
    if not frozen():
        return f"arrêté : empreinte du catalyseur {K.CATALYST_CODE} ≠ {CATALYST_CODE_N8} (déclaration n° 8)"
    stale = [d for (d,) in db.conn.execute("SELECT DISTINCT day FROM kill_complet WHERE cout IS NOT ?",
                                           (cost_fingerprint(),))]
    if stale:  # méthode de coût changée : ces séances repassent (lignes gardées, hors mesure, jusque-là)
        with db.transaction():
            db.conn.executemany("DELETE FROM progress WHERE job = ? AND key = ?", [(JOB, d) for d in stale])
        log.info("Labo : Killmode complet, %d séances à recalculer (méthode de coût changée)", len(stale))
    started, done = time.monotonic(), 0
    for day in ready_days(db):
        if done and time.monotonic() - started >= max_seconds:
            break
        heartbeat.beat(f"labo : Killmode complet {day}")
        rows, stats = await day_rows(db, day, transport)
        with db.transaction():
            db.conn.execute("DELETE FROM kill_complet WHERE day = ?", (day.isoformat(),))
            db.conn.executemany(f"INSERT INTO kill_complet ({', '.join(COLUMNS)}) VALUES "
                                f"({', '.join('?' for _ in COLUMNS)})", [tuple(r[c] for c in COLUMNS) for r in rows])
            db.set(JOB, day.isoformat(), stats)
        log.info("Labo : Killmode complet %s, %d signaux sur %d gappers", day, stats["signaux"], stats["gappers"])
        done += 1
    return done


# --- Tableau (lecture seule) -----------------------------------------------------------------------

def stale_days(conn: sqlite3.Connection) -> int:
    """Lot 2 (C4) : séances dont des lignes sont à une autre méthode de coût (cost_fingerprint), à recalculer ; 0 sur une
    base sans Killmode complet."""
    try:
        return conn.execute("SELECT COUNT(DISTINCT day) FROM kill_complet WHERE cout IS NOT ?",
                            (cost_fingerprint(),)).fetchone()[0]
    except sqlite3.Error:
        return 0


def report(conn: sqlite3.Connection, trials: int) -> dict | None:
    """Mesures de la déclaration n° 8 sur les lignes de l'empreinte déclarée et du coût en vigueur ; None avant la
    première séance."""
    cost = cost_fingerprint()
    try:
        days = conn.execute("SELECT COUNT(*), MIN(key), MAX(key), COALESCE(SUM(json_extract(value, '$.operations')), 0) "
                            "FROM progress WHERE job = ? AND key NOT IN (SELECT day FROM kill_complet WHERE cout IS NOT ?)",
                            (JOB, cost)).fetchone()
        rows = conn.execute("SELECT day, groupe, motif, empreinte, triggered, r_20e, r_3c, r_net, cout "
                            "FROM kill_complet ORDER BY day").fetchall()
    except sqlite3.Error:
        return None
    stale = sum(1 for r in rows if r[8] != cost)
    if not days[0] and frozen() and not stale:
        return None
    groups = {g: {"signaux": 0, "declenches": 0, "r_20e": SS.Mean(), "r_3c": SS.Mean(), "r_net": SS.Mean()}
              for g in GROUPS}
    per_day: dict = defaultdict(lambda: {g: [0.0, 0] for g in PAIRED})
    motifs: dict = defaultdict(SS.Mean)
    other = 0
    for day, groupe, motif, fp, trig, r20, r3, rn, row_cost in rows:
        if row_cost != cost:
            continue  # à recalculer au coût en vigueur
        if fp != CATALYST_CODE_N8:
            other += 1
            continue
        a = groups[groupe]
        a["signaux"] += 1
        if not trig or r20 is None:
            continue
        a["declenches"] += 1
        a["r_20e"].add(day, r20)
        a["r_3c"].add(day, r3)
        a["r_net"].add(day, rn)
        if groupe in PAIRED:
            cell = per_day[day][groupe]
            cell[0] += r20
            cell[1] += 1
        if groupe == "kill":
            motifs[motif or "aucun"].add(day, r20)
    kill = groups["kill"]
    main = kill["r_20e"].result()
    halves = SS._halves(kill["r_20e"])
    stress = kill["r_3c"].result()["esperance_r"]
    holds = all(v is not None and v > 0 for v in (stress, *halves))
    seuil = SS.candidate_t(main["jours"], trials)
    paired = SS.Mean()
    for day, cells in per_day.items():
        (sk, nk), (st, nt) = cells["kill"], cells["temoin_propre"]
        if nk and nt:
            paired.add(day, sk / nk - st / nt)
    return {
        "empreinte": CATALYST_CODE_N8, "empreinte_du_code": K.CATALYST_CODE, "figee": frozen(),
        "seances": days[0], "du": days[1], "au": days[2], "autre_empreinte": other,
        "cout": cost, "a_recalculer": stale, "operations_sur_titre": days[3],
        "groupes": {g: {"signaux": a["signaux"], "declenches": a["declenches"], "r_20e": a["r_20e"].result(),
                        "r_3c": a["r_3c"].result(), "r_net": a["r_net"].result()} for g, a in groups.items()},
        "principal": main | {"seuil_t": seuil, "r_3c": stress, "moitie_1": halves[0], "moitie_2": halves[1],
                             "tient": holds,
                             "candidate": bool(main["ic95"] and main["ic95"][0] > 0 and SS.passes(main["z"], seuil)
                                               and main["n"] >= SS.MIN_N and holds)},
        "ecart_temoin_propre": SS._helps(paired.result(), trials),
        "motifs": {k: v.result() for k, v in sorted(motifs.items())},
    }


def text(rep: dict | None) -> str | None:
    """Ligne de !strategies."""
    if not rep:
        return None
    if not rep["figee"]:
        return (f"Killmode complet (n° 8) : ⚠️ la définition du catalyseur a changé depuis la déclaration "
                f"(empreinte {rep['empreinte_du_code']} au lieu de {rep['empreinte']}) : mesure arrêtée")
    g, main, gap = rep["groupes"], rep["principal"], rep["ecart_temoin_propre"]
    ci = f" [{SS._r(main['ic95'][0])} ; {SS._r(main['ic95'][1])}]" if main["ic95"] else ""
    flag = "✅ " if main["candidate"] else ""
    parts = [(f"{flag}🔪 KILL {SS._r(main['esperance_r'])} R à 20 €{ci} · {main['n']} trades, {main['jours']} j "
              f"(t {SS._z(main['z'])}, seuil {SS._z(main['seuil_t'])}) · stress 3 c {SS._r(main['r_3c'])} R"),
             (f"témoins propres {SS._r(g['temoin_propre']['r_20e']['esperance_r'])} R "
              f"({g['temoin_propre']['declenches']})"),
             (f"écart apparié {SS._r(gap['esperance_r'])} R sur {gap['jours']} séances (t {SS._z(gap['z'])}, seuil "
              f"{SS._z(gap['seuil_t'])}" + SS._relative(gap.get("aide"), main["esperance_r"]) + ")"),  # lot 2 (C7)
             (f"témoins avec veto {SS._r(g['temoin_autre']['r_20e']['esperance_r'])} R "
              f"({g['temoin_autre']['declenches']})")]
    if g["donnees"]["signaux"] or g["fenetre_coupee"]["signaux"] or rep.get("operations_sur_titre"):
        parts.append(f"écartés : {g['donnees']['signaux']} sans données complètes, "
                     f"{g['fenetre_coupee']['signaux']} aux actus coupées, "
                     f"{rep.get('operations_sur_titre') or 0} sur une opération sur titre")
    if rep.get("a_recalculer"):
        parts.append(f"{rep['a_recalculer']} signaux à recalculer (méthode de coût changée)")
    span = f" {rep['du']} → {rep['au']}" if rep["du"] else ""
    return (f"Killmode complet (n° 8, catalyseur rejoué sur l'archive, {rep['seances']} séances{span}) : "
            + " · ".join(parts))
