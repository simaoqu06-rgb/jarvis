"""Statisticien du labo : mesures sur les trades simulés (sans réseau, sans jeton d'IA).

Une seule lecture de la table trades (SQLite, en flux, triée par heure de plan) alimente toutes les
mesures et tous les découpages ; les fonctions de calcul sont pures (testables sans base).

Mesures (sur les trades déclenchés, en R nets de coûts) :
- plans, taux de déclenchement, espérance (moyenne de r_net) avec intervalle de confiance à 95 % ;
- réussite (r_net > 0), TP1 avant le stop (après le TP1 le stop est à l'entrée : TP1 touché = TP1
  avant le stop d'origine), taux de TP2 et TP3 ;
- délais tenus : part des TP touchés au plus tard au délai médian estimé par le plan
  (plan.targets[i].minutes_median, mêmes minutes que le simulateur) ; un plan bien calibré tourne
  autour de 50 % ;
- calibration de la note : espérance par tranche de note composite (7 = [7 ; 8[), écart et z entre
  tranches voisines, pente en R par point de note ;
- pire série de pertes et plus forte baisse cumulée (en R, 1 R par trade, dans l'ordre des plans) ;
- capture du potentiel +10 (voir capture()).

Intervalle de confiance : les trades d'un même jour ne sont pas indépendants (même marché, mêmes
nouvelles) ; l'erreur type est donc « en grappes par jour » (sommes journalières), plus large et plus
honnête que l'erreur type naïve. Garde-fou : une tranche de moins de MIN_TRADES trades déclenchés est
marquée « echantillon_faible » et n'est jamais signalée comme dégradée.

Découpages, par marché : setup, note, heure du plan (heure de New York pour les actions, UTC pour la
crypto), jour de la semaine, version de méthode, origine (rejeu / direct), régime de marché (table
regimes, sans regard vers l'avenir) et période (20 derniers jours du marché contre le reste).
"""
import json
import math
import sqlite3
from datetime import date, datetime, timezone
from functools import lru_cache

from app.lab import collector as CO
from app.lab import seal as SEAL

MIN_TRADES = 30
Z95 = 1.959964
DEGRADE_Z = 3.2          # écart au reste du marché en erreurs types (≈ Bonferroni à 5 % sur ~80 tranches)
RECENT_DAYS = 20
MOVE_PCT = 8.0           # potentiel +10 : plus haut de séance ≥ ouverture + 8 %
DIMENSIONS = ("setup", "note", "heure", "jour_semaine", "version", "origine", "regime", "periode")
DEGRADE_DIMENSIONS = ("setup", "note", "heure", "jour_semaine", "regime", "origine", "periode")
WEEKDAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")

QUERY = """
SELECT t.market, t.day, t.planned_at, t.origin, t.method_version, t.triggered, t.r_net,
       t.tp1_hit, t.tp2_hit, t.tp3_hit, t.exit_reason, t.composite,
       t.minutes_to_tp1, t.minutes_to_tp2, t.minutes_to_tp3,
       json_extract(t.context, '$.setup'),
       json_extract(t.plan, '$.targets[0].minutes_median'),
       json_extract(t.plan, '$.targets[1].minutes_median'),
       json_extract(t.plan, '$.targets[2].minutes_median'),
       r.label
FROM trades t LEFT JOIN regimes r ON r.market = t.market AND r.day = t.day
"""


# --- Briques pures --------------------------------------------------------------------------

def cluster_se(day_sums: dict, mean: float, n: int) -> float | None:
    """Erreur type d'une moyenne de trades groupés par jour (estimateur en grappes, correction G/(G-1)).

    `day_sums` : {jour: [somme des R, nombre de trades]}. Un seul jour : erreur inconnue (None)."""
    g = len(day_sums)
    if n < 2 or g < 2:
        return None
    acc = 0.0
    for s, k in day_sums.values():
        u = s - mean * k
        acc += u * u
    return math.sqrt(acc * g / (g - 1)) / n


def mean_ci(day_sums: dict, total: float, n: int) -> tuple[float | None, float | None, list | None]:
    if not n:
        return None, None, None
    m = total / n
    se = cluster_se(day_sums, m, n)
    ci = [round(m - Z95 * se, 4), round(m + Z95 * se, 4)] if se is not None else None
    return m, se, ci


def _beta_cf(a: float, b: float, x: float) -> float:
    """Fraction continue de la fonction bêta incomplète (méthode de Lentz)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        for aa in (m * (b - m) * x / ((qam + m2) * (a + m2)), -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))):
            d = 1.0 + aa * d
            d = 1.0 / (d if abs(d) > tiny else tiny)
            c = 1.0 + aa / c
            c = c if abs(c) > tiny else tiny
            h *= d * c
        if abs(d * c - 1.0) < 1e-15:
            break
    return h


def beta_inc(a: float, b: float, x: float) -> float:
    """Fonction bêta incomplète régularisée I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _beta_cf(a, b, x) / a
    return 1.0 - front * _beta_cf(b, a, 1.0 - x) / b


def t_sf(t: float, df: float) -> float:
    """P(T ≥ t) pour la loi de Student à `df` degrés de liberté."""
    tail = 0.5 * beta_inc(df / 2.0, 0.5, df / (df + t * t))
    return tail if t >= 0 else 1.0 - tail


@lru_cache(maxsize=4096)
def t_quantile(p: float, df: int) -> float:
    """Quantile p (≥ 0,5) de la loi de Student, exact (bissection sur t_sf) ; l'infini sans degré de liberté.
    Déclaration n° 8, D4 : les jugements du labo sur G jours se font à G − 1 degrés de liberté."""
    if df < 1:
        return math.inf
    lo, hi = 0.0, 1.0
    while t_sf(hi, df) > 1.0 - p:
        hi *= 2.0
        if hi > 1e9:
            return math.inf
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if t_sf(mid, df) > 1.0 - p:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-10:
            break
    return (lo + hi) / 2.0


def p_one_sided(z: float) -> float:
    """P(Z ≥ z) sous la loi normale (erfc : précis même pour les très petites probabilités)."""
    return 0.5 * math.erfc(z / math.sqrt(2))


class Acc:
    """Accumulateur d'une tranche (ajout en O(1), aucune liste de trades gardée)."""

    __slots__ = ("plans", "trig", "s", "wins", "tp", "dl_ok", "dl_n", "days")

    def __init__(self):
        self.plans = self.trig = self.wins = 0
        self.s = 0.0
        self.tp = [0, 0, 0]
        self.dl_ok = [0, 0, 0]
        self.dl_n = [0, 0, 0]
        self.days: dict[str, list] = {}

    def add(self, day: str, r: float | None, tps: tuple, delays: tuple) -> None:
        self.plans += 1
        if r is None:
            return
        self.trig += 1
        self.s += r
        if r > 0:
            self.wins += 1
        d = self.days.get(day)
        if d is None:
            self.days[day] = [r, 1]
        else:
            d[0] += r
            d[1] += 1
        for i in range(3):
            if tps[i]:
                self.tp[i] += 1
            ok = delays[i]
            if ok is not None:
                self.dl_n[i] += 1
                self.dl_ok[i] += ok

    def mean_se(self) -> tuple[float | None, float | None]:
        m, se, _ = mean_ci(self.days, self.s, self.trig)
        return m, se

    def result(self, min_n: int = MIN_TRADES, detail: bool = False) -> dict:
        m, se, ci = mean_ci(self.days, self.s, self.trig)
        t = self.trig
        dl_n = sum(self.dl_n)
        out = {
            "plans": self.plans, "declenches": t,
            "taux_declenchement": round(t / self.plans, 4) if self.plans else None,
            "esperance_r": round(m, 4) if m is not None else None,
            "ic95": ci, "erreur_type": round(se, 4) if se is not None else None,
            "total_r": round(self.s, 2),
            "reussite": round(self.wins / t, 4) if t else None,
            "tp1_avant_stop": round(self.tp[0] / t, 4) if t else None,
            "tp2": round(self.tp[1] / t, 4) if t else None,
            "tp3": round(self.tp[2] / t, 4) if t else None,
            "delais_tenus": round(sum(self.dl_ok) / dl_n, 4) if dl_n else None,
            "jours": len(self.days),
            "echantillon_faible": t < min_n,
        }
        if detail:
            out["delais_par_tp"] = [{"tp": i + 1, "n": self.dl_n[i],
                                     "tenus": round(self.dl_ok[i] / self.dl_n[i], 4) if self.dl_n[i] else None}
                                    for i in range(3)]
        return out


class Curve:
    """Pire série de pertes et plus forte baisse cumulée (R) d'une suite de trades."""

    __slots__ = ("cum", "peak", "max_dd", "streak", "worst")

    def __init__(self):
        self.cum = self.peak = self.max_dd = 0.0
        self.streak = self.worst = 0

    def add(self, r: float) -> None:
        self.cum += r
        if self.cum > self.peak:
            self.peak = self.cum
        if self.peak - self.cum > self.max_dd:
            self.max_dd = self.peak - self.cum
        if r < 0:
            self.streak += 1
            if self.streak > self.worst:
                self.worst = self.streak
        else:
            self.streak = 0

    def result(self) -> dict:
        return {"pire_serie_pertes": self.worst, "baisse_max_r": round(self.max_dd, 2), "cumul_r": round(self.cum, 2)}


def note_bucket(composite) -> str:
    if composite is None:
        return "n/d"
    return str(max(0, min(10, int(math.floor(composite)))))


def delay_ok(hit, minutes, median) -> int | None:
    """1 si le TP touché l'a été au plus tard au délai médian prévu, 0 sinon, None sans objet."""
    if not hit or minutes is None or median is None:
        return None
    return 1 if minutes <= median else 0


def contrast(part: Acc, whole: Acc) -> dict | None:
    """Tranche contre le reste de son marché : écart d'espérance et z (erreurs types en grappes).

    La covariance entre la tranche et le reste (mêmes jours) est ignorée : elle est en général
    positive, le z obtenu est donc prudent (plutôt trop petit)."""
    n_c = whole.trig - part.trig
    if part.trig < 2 or n_c < 2:
        return None
    rest = {d: [v[0] - part.days.get(d, (0.0, 0))[0], v[1] - part.days.get(d, (0.0, 0))[1]]
            for d, v in whole.days.items()}
    rest = {d: v for d, v in rest.items() if v[1]}
    m_s, se_s = part.mean_se()
    m_c, se_c, _ = mean_ci(rest, whole.s - part.s, n_c)
    if se_s is None or se_c is None:
        return None
    se = math.sqrt(se_s ** 2 + se_c ** 2)
    diff = m_s - m_c
    return {"ecart_r": round(diff, 4), "z": round(diff / se, 2) if se > 0 else None, "reste_n": n_c,
            "reste_esperance_r": round(m_c, 4)}


def calibration(buckets: dict[str, Acc], min_n: int = MIN_TRADES) -> dict:
    """Espérance par tranche de note : une note de 8 bat-elle une note de 7 ?"""
    rows = []
    for key in sorted((k for k in buckets if k != "n/d"), key=int):
        acc = buckets[key]
        m, se = acc.mean_se()
        rows.append((int(key), acc.trig, m, se))
    pairs, respected, compared = [], 0, 0
    for (k1, n1, m1, se1), (k2, n2, m2, se2) in zip(rows, rows[1:]):
        if n1 < min_n or n2 < min_n or m1 is None or m2 is None:
            continue
        z = None
        if se1 is not None and se2 is not None and (se1 or se2):
            z = round((m2 - m1) / math.sqrt(se1 ** 2 + se2 ** 2), 2)
        compared += 1
        respected += m2 > m1
        pairs.append({"de": k1, "a": k2, "ecart_r": round(m2 - m1, 4), "z": z})
    usable = [(k, n, m) for k, n, m, _ in rows if n >= min_n and m is not None]
    slope = None
    if len(usable) >= 2:
        w = sum(n for _, n, _ in usable)
        kx = sum(k * n for k, n, _ in usable) / w
        my = sum(m * n for _, n, m in usable) / w
        var = sum(n * (k - kx) ** 2 for k, n, _ in usable)
        if var > 0:
            slope = round(sum(n * (k - kx) * (m - my) for k, n, m in usable) / var, 4)
    return {"paires": pairs, "ordre_respecte": f"{respected}/{compared}" if compared else None,
            "pente_r_par_point": slope}


# --- Lecture de la base ---------------------------------------------------------------------

def where(filters: dict, alias: str = "t") -> tuple[str, list]:
    """Filtres simples (marché, origine, version, dates) en SQL paramétré."""
    clauses, params = [], []
    for key, col in (("market", "market"), ("origin", "origin"), ("version", "method_version")):
        if filters.get(key):
            clauses.append(f"{alias}.{col} = ?")
            params.append(filters[key])
    if filters.get("since"):
        clauses.append(f"{alias}.day >= ?")
        params.append(filters["since"])
    if filters.get("until"):
        clauses.append(f"{alias}.day <= ?")
        params.append(filters["until"])
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def _recent_cutoffs(conn: sqlite3.Connection, filters: dict) -> dict[str, str]:
    out = {}
    for market in ("stock", "crypto"):
        if filters.get("market") and filters["market"] != market:
            continue
        sql, params = where({**filters, "market": market})
        row = conn.execute(f"SELECT DISTINCT day FROM trades t{sql} ORDER BY day DESC LIMIT 1 OFFSET ?",
                           (*params, RECENT_DAYS - 1)).fetchone()
        out[market] = row[0] if row else ""
    return out


class _Hours:
    """Heure locale du plan (New York pour les actions, UTC pour la crypto), mise en cache."""

    def __init__(self):
        self.cache: dict[tuple, str] = {}

    def __call__(self, market: str, ts: int) -> str:
        key = (market, ts)
        h = self.cache.get(key)
        if h is None:
            tz = CO.NEW_YORK if market == "stock" else timezone.utc
            dt = datetime.fromtimestamp(ts, tz)
            h = f"{dt.hour:02d}h{dt.minute:02d}" + (" NY" if market == "stock" else " UTC")
            if len(self.cache) < 100000:
                self.cache[key] = h
        return h


def summarize(rows, recent: dict[str, str] | None = None, min_n: int = MIN_TRADES) -> dict:
    """Toutes les mesures en une passe sur des lignes de QUERY (fonction pure, rows = itérable)."""
    recent = recent or {}
    total, markets = Acc(), {}
    slices: dict[str, dict[str, dict[str, Acc]]] = {}
    curves = {"global": Curve()}
    hours, weekdays = _Hours(), {}
    for row in rows:
        (market, day, planned_at, origin, version, triggered, r, tp1, tp2, tp3, _exit, composite,
         m1, m2, m3, setup, med1, med2, med3, regime) = row
        r = r if triggered else None
        tps = (tp1, tp2, tp3)
        delays = (delay_ok(tp1, m1, med1), delay_ok(tp2, m2, med2), delay_ok(tp3, m3, med3)) \
            if r is not None else (None, None, None)
        wd = weekdays.get(day)
        if wd is None:
            wd = weekdays[day] = WEEKDAYS[date.fromisoformat(day).weekday()]
        keys = {"setup": setup or "n/d", "note": note_bucket(composite), "heure": hours(market, planned_at),
                "jour_semaine": wd, "version": version, "origine": origin, "regime": regime or "inconnu",
                "periode": "recent" if day >= recent.get(market, "9999") else "ancien"}
        total.add(day, r, tps, delays)
        macc = markets.get(market)
        if macc is None:
            macc = markets[market] = Acc()
            slices[market] = {d: {} for d in DIMENSIONS}
            curves[market] = Curve()
        macc.add(day, r, tps, delays)
        ms = slices[market]
        for dim, value in keys.items():
            acc = ms[dim].get(value)
            if acc is None:
                acc = ms[dim][value] = Acc()
            acc.add(day, r, tps, delays)
        if r is not None:
            curves["global"].add(r)
            curves[market].add(r)
    out = {"global": total.result(min_n, detail=True) | curves["global"].result(), "marches": {}}
    degradations = []
    for market, macc in markets.items():
        par = {dim: {v: a.result(min_n) for v, a in sorted(values.items())} for dim, values in slices[market].items()}
        out["marches"][market] = {
            "mesures": macc.result(min_n, detail=True) | curves[market].result(),
            "par": par,
            "calibration_note": calibration(slices[market]["note"], min_n),
            "debut_recent": recent.get(market) or None,
        }
        for dim in DEGRADE_DIMENSIONS:
            values = slices[market][dim]
            if len(values) < 2:
                continue
            for value, acc in values.items():
                if acc.trig < min_n:
                    continue
                c = contrast(acc, macc)
                if not c or c["reste_n"] < min_n or c["z"] is None or c["z"] > -DEGRADE_Z:
                    continue
                m, _ = acc.mean_se()
                degradations.append({"marche": market, "dimension": dim, "valeur": value, "n": acc.trig,
                                     "esperance_r": round(m, 4), **c})
    out["degradations"] = sorted(degradations, key=lambda d: d["z"])
    return out


def compute(conn: sqlite3.Connection, min_n: int = MIN_TRADES, **filters) -> dict:
    """Statistiques complètes (filtres : market, origin, version, since, until)."""
    sql, params = where(filters)
    recent = _recent_cutoffs(conn, filters)
    cur = conn.execute(QUERY + sql + " ORDER BY t.planned_at, t.id", params)
    out = summarize(cur, recent, min_n)
    out["capture_potentiel"] = capture(conn, **filters)
    out["filtres"] = {k: v for k, v in filters.items() if v}
    out["seuil_echantillon"] = min_n
    return out


def capture(conn: sqlite3.Connection, threshold: float = MOVE_PCT, **filters) -> dict:
    """Capture du potentiel +10 : parmi les valeurs de l'univers du jour dont le plus haut de séance
    (5 min) a dépassé l'ouverture d'au moins `threshold` % (table day_moves ; crypto : journée UTC),
    combien avaient un plan, un plan déclenché, un TP1 touché. Et, parmi les actifs planifiés les
    mêmes jours, quelle part a fait ce mouvement (taux de base).

    Ouverture plutôt que clôture de la veille : un plan pris avant l'ouverture n'est jamais exécuté
    au-dessus de son prix limite, le gap n'est pas capturable ; ce qui l'est part de l'ouverture.
    Les jours du bloc scellé (seal.py) sont exclus : leurs trades n'apparaissent pas ici."""
    tsql, params = where(filters)
    join = tsql.replace(" WHERE ", " AND ") if tsql else ""
    msql, mparams = where({k: v for k, v in filters.items() if k in ("market", "since", "until")}, "m")
    mcond = msql.replace(" WHERE ", " AND ") if msql else ""
    row = conn.execute(
        f"""SELECT COUNT(*), SUM(plans > 0), SUM(trig > 0), SUM(tp1 > 0) FROM (
              SELECT m.market, m.day, m.symbol, COUNT(t.id) AS plans,
                     COALESCE(MAX(t.triggered), 0) AS trig, COALESCE(MAX(t.tp1_hit), 0) AS tp1
              FROM day_moves m LEFT JOIN trades t
                ON t.market = m.market AND t.day = m.day AND t.symbol = m.symbol{join}
              WHERE m.up_pct >= ? AND m.day NOT BETWEEN ? AND ?{mcond}
              GROUP BY m.market, m.day, m.symbol)""", (*params, threshold, *SEAL.bounds(), *mparams)).fetchone()
    movers, with_plan, trig, tp1 = (row[0] or 0), (row[1] or 0), (row[2] or 0), (row[3] or 0)
    planned = conn.execute(
        f"""SELECT COUNT(*) FROM (SELECT DISTINCT t.market, t.day, t.symbol FROM trades t{tsql}
              {'AND' if tsql else 'WHERE'} EXISTS (SELECT 1 FROM day_moves m
                  WHERE m.market = t.market AND m.day = t.day AND m.symbol = t.symbol))""", params).fetchone()[0]
    return {"seuil_pct": threshold, "mouvements": movers, "avec_plan": with_plan, "declenches": trig,
            "tp1": tp1, "taux_capture": round(tp1 / movers, 4) if movers else None,
            "actifs_planifies": planned, "part_des_plans": round(with_plan / planned, 4) if planned else None}


# --- Cache (écrit par le worker, lu par le serveur) ------------------------------------------

def store(conn: sqlite3.Connection, key: str, data: dict, trades: int, created_at: str) -> None:
    conn.execute("INSERT OR REPLACE INTO stats_cache VALUES (?, ?, ?, ?)",
                 (key, created_at, trades, json.dumps(data, ensure_ascii=False)))


def cached(conn: sqlite3.Connection, key: str = "courant") -> dict | None:
    try:
        row = conn.execute("SELECT created_at, trades, data FROM stats_cache WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    return json.loads(row[2]) | {"calcule_le": row[0], "trades_base": row[1]}
