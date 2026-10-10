"""Ce que coûtent les filtres du scanner de 15 h 10, mesuré au labo, filtre par filtre (sans ordre ni appel d'API).

Le compte fictif (app/trading/paper.py) ne voit que les valeurs qui passent les filtres : il ne peut pas dire ce
qu'un filtre coûte. Ici, chaque trade v1 d'actions déclenché de la table trades (version de génération ; jamais
trades_sealed, jours scellés exclus) reçoit les mesures du scanner recalculées sur le journalier du labo, au prix
d'ouverture du jour, comme plus10.population (scanner.features sur les ~75 séances d'avant), puis ses critères
ratés (scanner.filter_reasons, réglages du moment). Groupes : « passe tout », « rate seulement <raison> » (une par
raison), « rate 2 ou plus », et « historique court » (moins de 51 séances, que le scanner écarte aussi, ou journalier
inutilisable). Pour chacun : trades, jours, R net moyen avec IC 95 % en t en grappes par jour (strat_stats.Mean),
part de gagnants, et la même chose sur les GO seuls. Aussi par tranche de prix (< 1 / 1-5 / 5-20 / ≥ 20 $).

Mesure seulement : rien ici ne change un réglage ni ne filtre un trade. Limites, écrites dans le résultat (LIMITES).
Le worker recalcule quand les trades ou les réglages changent, au plus toutes les stats_age secondes, hors séance
US ; résultat en cache (stats_cache, clé « filtres »), lu par le brief du soir (`filtres_scanner`) et GET
/api/lab/filtres.
"""
import json
import sqlite3
from bisect import bisect_left
from datetime import date, datetime

from app.lab import collector as CO
from app.lab import heartbeat
from app.lab import seal as SEAL
from app.lab import stats as STATS
from app.lab import strat_stats as SS
from app.lab.db import LabDB, now_iso
from app.trading import scanner as SC
from app.trading.settings import Settings

CACHE_KEY = "filtres"
REASONS = ("prix", "pas en jeu", "trop calme", "sous la MM20", "sous la MM50", "RSI")
PRICE_BUCKETS = ((0.0, 1.0, "< 1 $"), (1.0, 5.0, "1-5 $"), (5.0, 20.0, "5-20 $"), (20.0, 1e18, "≥ 20 $"))
LOOKBACK_DAYS = 110  # ~75 séances avant le jour, comme plus10 (MM50, RSI) et le scanner (100 jours Alpaca)
SETTING_KEYS = ("scan_price_min", "scan_price_max", "scan_rvol_min", "scan_gap_min_pct", "scan_gap_rvol_min",
                "scan_atr_pct_min", "scan_above_ma20", "scan_above_ma50", "rsi_min", "rsi_max")
LIMITES = [
    "R du plan v1 : entrée au dernier cours connu à 9 h 10 (ordre limite), pas au prix de pré-ouverture ; horizon "
    "de la séance, pas 2 séances comme le compte fictif.",
    "Mesures du scanner au prix d'ouverture (comme plus10), approximation du prix de pré-ouverture de 9 h 10 : le "
    "plan v1 entre au dernier cours de la veille, son gap serait nul.",
    "Univers du labo, approché : pas exactement les plus actives et plus fortes hausses d'Alpaca à 9 h 10.",
    "Catalyseur et vetos Finnhub (ETF, augmentation de capital, résultats) non rejoués ; filtres de la méthode du "
    "labo non appliqués.",
    "Journalier ajusté des divisions : une action regroupée plus tard paraît au-dessus de 5 $ alors qu'elle était "
    "en dessous ; la tranche < 5 $ est incomplète et le coût du minimum de 5 $ sous-estimé.",
    "Réglages du moment pour tous les jours : un réglage changé depuis n'est pas rejoué à sa valeur d'alors.",
]
QUERY = """
SELECT symbol, day, r_net, decision, json_extract(plan, '$.entry')
FROM trades
WHERE market = 'stock' AND triggered = 1 AND r_net IS NOT NULL AND day NOT BETWEEN ? AND ?{version}
ORDER BY symbol, day
"""


class Cell:
    """R net d'un groupe (tous les trades et GO seuls), en grappes par jour."""

    def __init__(self):
        self.all, self.go = SS.Mean(), SS.Mean()
        self.wins = {"all": 0, "go": 0}

    def add(self, day: str, r: float, go: bool) -> None:
        self.all.add(day, r)
        self.wins["all"] += r > 0
        if go:
            self.go.add(day, r)
            self.wins["go"] += r > 0

    def result(self) -> dict:
        def one(mean: SS.Mean, wins: int) -> dict:
            res = mean.result()
            return res | {"gagnants": round(wins / res["n"], 3) if res["n"] else None,
                          "echantillon_faible": res["n"] < SS.MIN_N}

        return {"tous": one(self.all, self.wins["all"]), "go": one(self.go, self.wins["go"])}


def price_bucket(price: float | None) -> str:
    if not price or price <= 0:
        return "prix inconnu"
    return next(label for lo, hi, label in PRICE_BUCKETS if lo <= price < hi)


def group(misses: list[str] | None) -> str:
    if misses is None:
        return "historique court"
    if not misses:
        return "passe tout"
    return f"rate seulement {misses[0]}" if len(misses) == 1 else "rate 2 ou plus"


def _daily(conn: sqlite3.Connection, symbol: str) -> tuple[list[int], list[dict]]:
    rows = [{"t": ts, "o": o, "h": h, "l": lo, "c": c, "v": v or 0} for ts, o, h, lo, c, v in conn.execute(
        "SELECT ts, o, h, l, c, v FROM bars WHERE market = 'stock' AND symbol = ? AND tf = 1440 ORDER BY ts", (symbol,))]
    return [r["t"] for r in rows], rows


def classified(conn: sqlite3.Connection, version: str | None, s) -> list[tuple]:
    """(jour, GO ?, r_net, prix, critères ratés ou None si l'historique ne suffit pas), trade par trade."""
    lo, hi = SEAL.bounds()
    params: list = [lo, hi]
    vsql = ""
    if version:
        vsql = " AND method_version = ?"
        params.append(version)
    out: list[tuple] = []
    current, ts, rows, seen = None, [], [], 0
    for symbol, day, r, decision, entry in conn.execute(QUERY.format(version=vsql), params).fetchall():
        if symbol != current:
            current, (ts, rows) = symbol, _daily(conn, symbol)
            seen += 1
            if seen % 200 == 0:
                heartbeat.beat()
        cutoff = CO.ny_midnight(date.fromisoformat(day))
        i, j = bisect_left(ts, cutoff - LOOKBACK_DAYS * 86400), bisect_left(ts, cutoff)
        today = rows[j] if j < len(rows) and CO.ny_day(rows[j]["t"]).isoformat() == day else None
        price = today["o"] if today and today["o"] and today["o"] > 0 else entry
        try:
            f = SC.features(rows[i:j], price, None, day) if price else None
        except (ArithmeticError, ValueError, TypeError):  # clôture nulle ou bougie incomplète : comme un historique court
            f = None
        out.append((day, decision == "GO", r, price, None if f is None else SC.filter_reasons(f, s)))
    return out


def report(conn: sqlite3.Connection, s=None) -> dict:
    from app.lab import validator as V

    s = s if s is not None else Settings()  # relu : un réglage approuvé côté serveur compte au calcul suivant
    version = V.current_version(conn)
    groups = {name: Cell() for name in ("passe tout", *(f"rate seulement {r}" for r in REASONS), "rate 2 ou plus",
                                        "historique court")}
    prices = {label: Cell() for _, _, label in PRICE_BUCKETS}
    rows = classified(conn, version, s)
    for day, go, r, price, misses in rows:
        groups[group(misses)].add(day, r, go)
        prices.setdefault(price_bucket(price), Cell()).add(day, r, go)
    return {"version": version, "trades": len(rows), "reglages": {k: s[k] for k in SETTING_KEYS},
            "groupes": {k: c.result() for k, c in groups.items()},
            "par_prix": {k: c.result() for k, c in prices.items()}, "limites": LIMITES}


def signature(conn: sqlite3.Connection, s=None) -> list:
    """Trades d'actions (nombre, dernier id, somme des R nets : cout.py les recalcule sur place) et réglages."""
    s = s if s is not None else Settings()
    row = conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0), ROUND(TOTAL(r_net), 6) FROM trades "
                       "WHERE market = 'stock'").fetchone()
    from app.lab import empreinte as E  # I3 : trades réécrits sur place, code des stats (déploiement : tout de suite)

    return [*row, json.dumps({k: s[k] for k in SETTING_KEYS}, sort_keys=True), *E.sig(conn, "trades")]


def cached(conn: sqlite3.Connection) -> dict | None:
    return STATS.cached(conn, CACHE_KEY)


def step(db: LabDB, now: datetime, max_age: float) -> int | None:
    """Passage du worker : recalcule quand les trades ou les réglages ont changé et que le cache a plus de `max_age` s
    (tout de suite après un déploiement)."""
    s = Settings()
    sig = signature(db.conn, s)
    from app.lab import empreinte as E

    at = db.get("filtres", "at", 0) or 0
    last = db.get("filtres", "signature")
    if sig == last or (cached(db.conn) and now.timestamp() - at < max_age and not E.code_changed(last)):
        return None
    data = report(db.conn, s)
    with db.transaction():
        STATS.store(db.conn, CACHE_KEY, data, data["trades"], now_iso())
        db.set("filtres", "signature", sig)
        db.set("filtres", "at", now.timestamp())
    return data["trades"]


def compact(data: dict | None) -> dict | None:
    """Pour le brief du soir : R net moyen, IC et jours de chaque groupe (GO seuls à côté), limites."""
    if not data:
        return None

    def slim(cell: dict) -> dict:
        t, g = cell["tous"], cell["go"]
        return {"n": t["n"], "jours": t["jours"], "esperance_r": t["esperance_r"], "ic95": t["ic95"],
                "gagnants": t["gagnants"], "go_n": g["n"], "go_esperance_r": g["esperance_r"], "go_ic95": g["ic95"]}

    return {"trades": data.get("trades"), "reglages": data.get("reglages"),
            "groupes": {k: slim(c) for k, c in (data.get("groupes") or {}).items() if c["tous"]["n"]},
            "par_prix": {k: slim(c) for k, c in (data.get("par_prix") or {}).items() if c["tous"]["n"]},
            "limites": data.get("limites"), "calcule_le": data.get("calcule_le")}
