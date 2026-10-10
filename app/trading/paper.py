"""Forward-test sur le compte fictif Alpaca : chaque valeur retenue par le scanner (GO comme NO GO)
y est passée en silence, pour apprendre quels critères paient vraiment.

Rien de tout ça n'arrive sur Discord trade par trade : pas de « tu as raté +12 % ». Seul un bilan
agrégé par critère part le vendredi soir ; JARVIS en tire des propositions de réglages chiffrées
que Simão approuve ou refuse (proposals.py).

Mécanique : 3 ordres « bracket » par plan (un tiers de la taille vers TP1, TP2 et TP3, même stop),
entrée à cours limité au prix du plan. Pas d'entrée dans la première séance : ordres annulés
(« non déclenché »). Au bout de `paper_horizon_sessions` séances, le reste est soldé (« expiré »).
Une seule position fictive par action à la fois. Le stop ne remonte pas à l'entrée après le TP2
comme dans le suivi réel : le résultat est donc un peu prudent.

Ce que le bilan mesure : les plans bruts du scanner (GO + NO GO, entrée limite au prix de pré-ouverture,
stop fixe, `paper_horizon_sessions` séances, avant frais), pas les GO confirmés que Simão trade (ceux-là
sont dans data/confirmations.jsonl, `!confirmations`). Les plans manuels sont comptés à part.
Propositions sur preuve seulement : intervalle de confiance en grappes par jour (stats.mean_ci du labo),
quantile de Student à G − 1 degrés de liberté corrigé de Bonferroni sur les RULES règles, MIN_SAMPLES
plans et MIN_DAYS séances au moins, sur tous les plans ou sur les GO seuls. Un filtre resserré doit
aussi perdre sur les plans qu'il retirerait vraiment (removed) ; un réglage du scanner ou de go_min
n'est proposé que si les GO confirmés relus ne disent pas le contraire.
"""
import json
import logging
import math
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

from app.core.http import SourceError
from app.lab import live as LAB
from app.lab import stats as STATS
from app.sources import alpaca
from app.trading import calendar as CAL
from app.trading import confirm as CONFIRM
from app.trading import scanner as SC
from app.trading.settings import DEFAULTS, settings

log = logging.getLogger(__name__)
PAPER_PATH = os.getenv("JARVIS_PAPER_PATH", "data/paper.json")
MIN_SAMPLES = 30  # en dessous, un groupe ne sert pas à proposer un réglage (MIN_TRADES du labo)
MIN_DAYS = 10  # et au moins autant de séances distinctes : l'erreur type est en grappes par jour
RULES = 6  # règles du bilan, pour la correction de Bonferroni de l'intervalle de confiance
GO_MIN_CAP = 8.5  # une proposition ne monte jamais go_min plus haut
CONFIRMED_MIN = 10  # GO confirmés d'une tranche avant d'en lire le sens
SNAPSHOT = ("go_min", "rsi_max", "scan_rvol_min", "scan_gap_min_pct", "scan_gap_rvol_min")
FEATURES = {"price", "rvol", "gap_pct", "atr_pct", "ma20", "ma50", "rsi"}  # ce que lit scanner.filter_reasons
FILTER_KEYS = {"scan_price_min", "scan_price_max", "scan_rvol_min", "scan_gap_min_pct", "scan_gap_rvol_min",
               "scan_atr_pct_min", "scan_above_ma20", "scan_above_ma50", "rsi_min", "rsi_max"}


class PaperBook:
    def __init__(self, path: str | None = PAPER_PATH):
        self.path = Path(path) if path else None
        self.entries: list[dict] = []
        if self.path and self.path.exists():
            try:
                self.entries = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                log.warning("Fichier du forward-test illisible, on repart de zéro")

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.entries, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def active(self) -> list[dict]:
        return [e for e in self.entries if e["status"] in ("pending", "open")]


def legs_for(qty_total: int, targets: list[dict]) -> list[tuple[int, dict]]:
    """Répartit la taille en parts (presque) égales sur les TP ; moins de TP si la taille est trop petite."""
    n = min(len(targets), qty_total)
    if n <= 0:
        return []
    base, extra = divmod(qty_total, n)
    return [(base + (1 if i < extra else 0), targets[i]) for i in range(n)]


def record(plan: dict, features: dict | None, origin: str, s=None, methode: str | None = None) -> dict:
    sc = plan["score"]
    return {
        "id": uuid.uuid4().hex[:8], "origin": origin, "created_at": plan["created_at"], "symbol": plan["symbol"],
        "side": plan["side"], "entry": plan["entry"], "stop": plan["stop"], "features": features or {},
        "decision": sc["decision"], "composite": sc["composite"],
        "parts": {k: v["score"] for k, v in sc["parts"].items()},
        "status": "pending", "legs": [], "result_r": None, "outcome": None,
        # Réglages en vigueur et méthode du labo au moment du plan : une règle se relit sous ce qui l'a produit.
        "settings": {k: s.get(k) for k in SNAPSHOT} if s is not None else None, "methode": methode,
    }


async def submit(book: PaperBook, plan: dict, features: dict | None = None, origin: str = "scanner",
                 s=settings) -> dict | None:
    """Passe un plan d'action sur le compte fictif. Renvoie l'entrée, ou None si rien n'a été passé."""
    if plan["market"] != "stock" or not alpaca.configured() or not s["paper_enabled"]:
        return None
    if any(e["symbol"] == plan["symbol"] for e in book.active()):
        return None
    account = await alpaca.account()
    equity = float(account.get("equity") or 0)
    risk_unit = abs(plan["entry"] - plan["stop"])
    qty_total = math.floor(equity * s["paper_risk_pct"] / 100 / risk_unit) if risk_unit else 0
    legs = legs_for(qty_total, plan["targets"])
    if not legs:
        return None
    try:  # arrondis du courtier vérifiés avant le moindre ordre (entrée = stop ou entrée = TP refusés)
        for _, target in legs:
            alpaca.bracket_prices(plan["entry"], target["price"], plan["stop"])
    except SourceError as exc:
        log.warning("Forward-test %s non passé : %s", plan["symbol"], exc)
        return None
    # Méthode du labo lue comme le scanner vient de le faire (même fichier du champion, relu s'il change).
    entry = record(plan, features, origin, s, LAB.current().get("version") if origin == "scanner" else None)
    for qty, target in legs:
        try:
            order = await alpaca.submit_bracket(plan["symbol"], qty, plan["side"], plan["entry"], target["price"],
                                                plan["stop"], client_order_id=f"jarvis-{entry['id']}-{target['name']}")
        except Exception as exc:
            await _rollback(book, entry, exc)
            raise
        entry["legs"].append({"name": target["name"], "qty": qty, "target": target["price"],
                              "predicted_minutes": target["minutes_median"], "order_id": order["id"],
                              "filled_at": None, "fill_price": None, "exit": None, "exit_price": None, "exit_at": None})
    book.entries.append(entry)
    book.save()
    return entry


async def _rollback(book: PaperBook, entry: dict, exc: Exception) -> None:
    """Une jambe refusée : on annule celles déjà passées pour ne pas laisser d'ordres orphelins.

    Celles qu'on n'a pas pu annuler restent suivies (entrée gardée, annulation retentée au passage suivant).
    """
    log.warning("Forward-test %s : jambe refusée (%s), annulation des %d jambes déjà passées",
                entry["symbol"], exc, len(entry["legs"]))
    left = []
    for leg in entry["legs"]:
        try:
            await alpaca.cancel_order(leg["order_id"])
        except SourceError as cancel_exc:
            log.warning("Annulation %s impossible : %s", leg["order_id"], cancel_exc)
            left.append(leg)
    if left:
        entry["legs"] = left
        entry["submit_error"] = str(exc)
        entry["cancel_pending"] = True  # check() retente l'annulation sans attendre la fin de séance
        book.entries.append(entry)
        book.save()


def _ts(iso: str | None) -> datetime | None:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")) if iso else None


def apply_order(leg: dict, order: dict) -> None:
    """Met à jour une jambe d'après l'ordre Alpaca (parent = entrée, enfants = TP et stop)."""
    if order.get("filled_at") and not leg["filled_at"]:
        leg["filled_at"] = order["filled_at"]
        leg["fill_price"] = float(order["filled_avg_price"])
    if order.get("status") in ("canceled", "expired", "rejected") and not leg["filled_at"]:
        leg["cancelled"] = True
        # Rempli en partie puis annulé : une position fictive reste ouverte sans être suivie ici.
        filled = float(order.get("filled_qty") or 0)
        if filled > 0:
            leg["partial_qty"] = filled
    leg["child_ids"] = [c["id"] for c in order.get("legs") or []]
    for child in order.get("legs") or []:
        if child.get("status") == "filled" and not leg["exit"]:
            leg["exit"] = "tp" if child.get("type") == "limit" else "stop"
            leg["exit_price"] = float(child["filled_avg_price"])
            leg["exit_at"] = child["filled_at"]


def leg_r(leg: dict, entry: dict) -> float | None:
    if leg["fill_price"] is None or leg["exit_price"] is None:
        return None
    sign = 1 if entry["side"] == "long" else -1
    risk = abs(entry["entry"] - entry["stop"])
    return sign * (leg["exit_price"] - leg["fill_price"]) / risk if risk else None


def finish(entry: dict) -> None:
    filled = [leg for leg in entry["legs"] if leg["fill_price"] is not None]
    if not filled:
        entry["outcome"], entry["result_r"] = "non déclenché", None
    else:
        # Jambe au cours de sortie inconnu (cliché Alpaca en échec) : écartée, pas comptée à 0 R.
        rs = [(r, leg["qty"]) for leg in filled if (r := leg_r(leg, entry)) is not None]
        entry["result_r"] = round(sum(r * q for r, q in rs) / sum(q for _, q in rs), 2) if rs else None
        tps = [leg["name"] for leg in filled if leg["exit"] == "tp"]
        entry["outcome"] = (f"{tps[-1]} touché" if tps else
                            "stop" if any(leg["exit"] == "stop" for leg in filled) else "expiré")
    entry["status"] = "done"


def partial_warnings(book: PaperBook) -> list[str]:
    """Une alerte par jambe remplie en partie puis annulée (position fictive orpheline, à surveiller)."""
    out = []
    for entry in book.entries:
        for leg in entry["legs"]:
            if leg.get("partial_qty") and not leg.get("partial_warned"):
                leg["partial_warned"] = True
                out.append(f"⚠️ Forward-test : {entry['symbol']} ({leg.get('name', 'jambe')}) rempli en partie "
                           f"({leg['partial_qty']:g} sur {leg['qty']}) puis annulé. Cette position fictive n'est pas "
                           f"suivie par JARVIS : à solder sur le compte Alpaca fictif si elle reste ouverte.")
    return out


async def check(book: PaperBook, now: datetime | None = None, s=settings) -> list[str]:
    """Relit les ordres fictifs, annule les entrées pas déclenchées dans la séance, solde à l'horizon.
    Renvoie les alertes à envoyer (remplissages partiels)."""
    now = now or datetime.now(timezone.utc)
    for entry in book.active():
        try:
            for leg in entry["legs"]:
                apply_order(leg, await alpaca.order(leg["order_id"]))
        except SourceError as exc:
            log.warning("Forward-test %s : %s", entry["symbol"], exc)
            continue
        if any(leg["fill_price"] is not None for leg in entry["legs"]):
            entry["status"] = "open"
        created = datetime.fromisoformat(entry["created_at"])
        # Clôture de la séance d'entrée (13 h les jours de séance courte), puis de la n-ième séance.
        entry_deadline = CAL.nth_session_close(created, 1) - timedelta(minutes=5)
        horizon = CAL.nth_session_close(created, s["paper_horizon_sessions"]) - timedelta(minutes=5)
        unfilled = [leg for leg in entry["legs"] if leg["fill_price"] is None]
        to_cancel = [leg for leg in unfilled if not leg.get("cancelled")]
        if (now >= entry_deadline or entry.get("cancel_pending")) and unfilled and not entry.get("entry_cancelled"):
            for leg in to_cancel:
                try:
                    await alpaca.cancel_order(leg["order_id"])
                    leg["cancelled"] = True
                except SourceError as exc:
                    log.warning("Annulation %s : %s", leg["order_id"], exc)
            # Seulement si toutes les annulations ont réussi ; sinon on retente au prochain passage.
            if all(leg.get("cancelled") for leg in unfilled):
                entry["entry_cancelled"] = True
        still_open = [leg for leg in entry["legs"] if leg["fill_price"] is not None and not leg["exit"]]
        if now >= horizon and still_open:
            for leg in still_open:
                try:
                    if not leg.get("children_cancelled"):
                        for child_id in leg.get("child_ids", []):  # TP et stop encore en attente
                            await alpaca.cancel_order(child_id)
                        leg["children_cancelled"] = True
                    await alpaca.close_position(entry["symbol"], leg["qty"])
                except SourceError as exc:
                    # Position pas soldée : la jambe reste ouverte, nouvelle tentative au prochain passage.
                    log.warning("Clôture fictive %s : %s", entry["symbol"], exc)
                    continue
                try:
                    snap = (await alpaca.snapshots([entry["symbol"]])).get(entry["symbol"]) or {}
                except SourceError as exc:
                    log.warning("Cours de clôture fictive %s : %s", entry["symbol"], exc)
                    snap = {}
                leg["exit"], leg["exit_price"], leg["exit_at"] = "expiré", snap.get("price"), now.isoformat(timespec="seconds")
            still_open = [leg for leg in entry["legs"] if leg["fill_price"] is not None and not leg["exit"]]
        if not still_open and (entry.get("entry_cancelled") or not unfilled):
            finish(entry)
    warnings = partial_warnings(book)
    book.save()
    return warnings


# --- Bilan et propositions de réglages ----------------------------------------------------

BUCKETS = {
    "decision": ("Verdict", lambda e: e["decision"]),
    "composite": ("Note composite", lambda e: "< 6" if e["composite"] < 6 else "6-7" if e["composite"] < 7
                  else "7-8" if e["composite"] < 8 else "≥ 8"),
    "rsi": ("RSI 14 j", lambda e: None if "rsi" not in e["features"] else "< 55" if e["features"]["rsi"] < 55
            else "55-65" if e["features"]["rsi"] < 65 else "≥ 65"),
    "rvol": ("Volume veille", lambda e: None if e["features"].get("rvol") is None else "< 1,5×" if e["features"]["rvol"] < 1.5
             else "1,5-3×" if e["features"]["rvol"] < 3 else "≥ 3×"),
    "gap": ("Gap pré-ouverture", lambda e: None if "gap_pct" not in e["features"] else "< 2 %" if e["features"]["gap_pct"] < 2
            else "2-5 %" if e["features"]["gap_pct"] < 5 else "≥ 5 %"),
    "catalyst": ("Note catalyseur", lambda e: None if e["parts"].get("catalyst") is None else "< 5" if e["parts"]["catalyst"] < 5
                 else "5-7" if e["parts"]["catalyst"] < 7 else "≥ 7"),
    # Mesure seule, jamais une proposition : elle chiffre ta règle « pas de filtre de liquidité ».
    "dollar_volume": ("Volume moyen 20 j en $ (sans filtre de liquidité, mesuré seulement)",
                      lambda e: None if e["features"].get("dollar_volume") is None
                      else "< 1 M$" if e["features"]["dollar_volume"] < 1e6 else "1-10 M$" if e["features"]["dollar_volume"] < 1e7
                      else "10-100 M$" if e["features"]["dollar_volume"] < 1e8 else "≥ 100 M$"),
}


def _ci(entries: list[dict]) -> tuple[float | None, float | None, int]:
    """Bornes de la moyenne des R en grappes par jour (stats.mean_ci du labo, jour de created_at), au quantile de
    Student à G − 1 degrés de liberté corrigé de Bonferroni sur les RULES règles ; sans bornes sur un seul jour."""
    days: dict[str, list] = {}
    for e in entries:
        cell = days.setdefault(e["created_at"][:10], [0.0, 0])
        cell[0] += e["result_r"]
        cell[1] += 1
    m, se, _ = STATS.mean_ci(days, sum(e["result_r"] for e in entries), len(entries))
    if m is None or se is None:
        return None, None, len(days)
    q = STATS.t_quantile(1 - 0.05 / (2 * RULES), len(days) - 1)
    return round(m - q * se, 3), round(m + q * se, 3), len(days)


def _group(entries: list[dict]) -> dict:
    rs = [e["result_r"] for e in entries]
    tp1 = [e for e in entries if any(leg["exit"] == "tp" for leg in e["legs"])]
    ci_low, ci_high, days = _ci(entries)
    return {"n": len(entries), "avg_r": round(sum(rs) / len(rs), 2), "win_rate": round(sum(1 for r in rs if r > 0) / len(rs), 2),
            "tp1_rate": round(len(tp1) / len(entries), 2), "days": days, "ci_low": ci_low, "ci_high": ci_high}


def stats(book: PaperBook, s=settings, confirmed: list[dict] | None = None) -> dict:
    """Bilan des plans du scanner (les plans manuels à part) et réglages à envisager. `confirmed` : lignes de
    data/confirmations.jsonl (relues si None), pour le sens chez les GO confirmés."""
    scan = [e for e in book.entries if e.get("origin", "scanner") == "scanner"]
    done = [e for e in scan if e["status"] == "done"]
    traded = [e for e in done if e["result_r"] is not None]
    go = [e for e in traded if e["decision"] == "GO"]
    manual = [e for e in book.entries if e.get("origin", "scanner") != "scanner" and e["status"] == "done"
              and e["result_r"] is not None]
    out = {"total": len(scan), "done": len(done), "traded": len(traded),
           "fill_rate": round(sum(1 for e in done if e["outcome"] != "non déclenché") / len(done), 2) if done else None,
           "overall": _group(traded) if traded else None, "go": _group(go) if go else None,
           "manual": _group(manual) if manual else None, "horizon": s["paper_horizon_sessions"],
           "by": {}, "timing": None, "suggestions": []}
    for key, (label, fn) in BUCKETS.items():
        groups: dict[str, list] = {}
        for e in traded:
            b = fn(e)
            if b is not None:
                groups.setdefault(b, []).append(e)
        out["by"][key] = {"label": label, "groups": {b: _group(es) for b, es in groups.items()}}
    # Délais : temps de cotation jusqu'au TP divisé par le délai prévu (1 = pile à l'heure). Seuls les TP touchés
    # comptent : ceux que l'horizon a coupés sont exclus, ce qui tire le ratio vers le bas.
    ratios = []
    for e in traded:
        for leg in e["legs"]:
            if leg["exit"] == "tp" and leg["predicted_minutes"] and leg["filled_at"] and leg.get("exit_at"):
                created = _ts(e["created_at"])
                minutes = CAL.market_minutes_between(max(created, _ts(leg["filled_at"])), _ts(leg["exit_at"]))
                ratios.append(minutes / leg["predicted_minutes"])
    if ratios:
        out["timing"] = {"n": len(ratios), "median_ratio": round(median(ratios), 2)}
    out["suggestions"] = suggestions(out, traded, s, confirm_log() if confirmed is None else confirmed)
    return out


def confirm_log() -> list[dict]:
    """Lignes de data/confirmations.jsonl ; une ligne illisible (écriture coupée) est sautée, pas tout le fichier."""
    try:
        lines = Path(CONFIRM.LOG_PATH).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def removed(entries: list[dict], s, key: str, value) -> list[dict]:
    """Plans du scanner qui passent les filtres actuels et que `key = value` écarterait (scanner.filter_reasons).

    Ne vaut que pour un resserrement (rien de mesurable pour un seuil abaissé) ; ignore les valeurs qui prendraient
    les places libérées dans la présélection, et les plans sans toutes leurs mesures (manuels)."""
    cur = DEFAULTS | (s.all() if hasattr(s, "all") else dict(s))
    cand = cur | {key: value}
    pool = [e for e in entries if e.get("origin", "scanner") == "scanner" and FEATURES <= (e.get("features") or {}).keys()]
    return [e for e in pool if SC.filter_reasons(e["features"], cand) and not SC.filter_reasons(e["features"], cur)]


def _proof(entries: list[dict]) -> dict | None:
    """Groupe qui peut fonder une proposition : MIN_SAMPLES plans sur MIN_DAYS séances, intervalle calculable."""
    if len(entries) < MIN_SAMPLES:
        return None
    grp = _group(entries)
    return grp if grp["days"] >= MIN_DAYS and grp["ci_high"] is not None else None


def _confirmed(rows: list[dict]) -> list[dict]:
    """GO confirmés et déclenchés de confirmations.jsonl, au format des entrées : R net à la taille de Simão quand
    la ligne le porte (confirme_r20), sinon R avant frais ; mesures (features) seulement si la ligne les a."""
    def num(x) -> bool:
        return isinstance(x, (int, float)) and not isinstance(x, bool)

    out = []
    for row in rows:
        r = row.get("confirme_r20") if row.get("confirme_r20") is not None else row.get("confirme_r")
        if (row.get("status") != "confirmé" or not num(r) or not isinstance(row.get("day"), str)
                or not num(row.get("composite"))):
            continue
        feats, parts = row.get("features"), row.get("parts")
        out.append({"created_at": row["day"], "result_r": r, "decision": "GO", "composite": row["composite"],
                    "features": {k: v for k, v in feats.items() if num(v)} if isinstance(feats, dict) else {},
                    "parts": {k: v for k, v in parts.items() if num(v)} if isinstance(parts, dict) else {},
                    "legs": []})
    return out


def _in(entries: list[dict], key: str, bucket: str) -> list[dict]:
    fn = BUCKETS[key][1]
    return [e for e in entries if fn(e) == bucket]


def _mean(entries: list[dict]) -> float:
    return sum(e["result_r"] for e in entries) / len(entries)


def suggestions(st: dict, traded: list[dict] | None = None, s=settings, confirmed: list[dict] | None = None) -> list[dict]:
    """Réglages à envisager, sur preuve seulement : la règle doit tenir (intervalle corrigé, MIN_SAMPLES plans,
    MIN_DAYS séances) sur tous les plans du scanner ou sur les GO seuls ; un filtre resserré doit aussi perdre sur
    les plans qu'il retirerait vraiment (removed) ; un réglage du scanner ou de go_min n'est pas proposé si les
    GO confirmés (CONFIRMED_MIN au moins dans la tranche) vont dans l'autre sens, et leur chiffre est montré."""
    traded = traded or []
    go = [e for e in traded if e["decision"] == "GO"]
    conf = _confirmed(confirmed or [])
    go_min = s["go_min"]

    def gap_only(es):  # en jeu par le gap seul, volume veille sous 1,5× : ce que scan_gap_rvol_min = 1,5 retire
        return [e for e in es if e["features"].get("gap_pct") is not None and e["features"]["gap_pct"] >= s["scan_gap_min_pct"]
                and s["scan_gap_rvol_min"] <= (e["features"].get("rvol") or 0) < min(1.5, s["scan_rvol_min"])]

    def band(es):  # tranche relative [go_min ; go_min + 0,5[ : elle suit go_min, pas de cliquet sur les mêmes plans
        return [e for e in es if go_min <= e["composite"] < go_min + 0.5]

    def rsi_test(es, grp):  # RSI ≥ 65 perdant, 55-65 gagnant
        mid = _in(es, "rsi", "55-65")
        return grp["ci_high"] < 0 and len(mid) >= MIN_SAMPLES and _group(mid)["avg_r"] > 0

    # (clé, valeur, libellé, population, test de la preuve, sens attendu chez les GO confirmés, fin du « why »)
    rules = []
    if s["rsi_max"] > 65:
        rules.append(("rsi_max", 65, "RSI ≥ 65", lambda es: _in(es, "rsi", "≥ 65"), rsi_test, -1, ""))
    if s["scan_gap_rvol_min"] < 1.5:
        rules.append(("scan_gap_rvol_min", 1.5, "volume veille < 1,5× (en jeu par le gap seul)", gap_only,
                      lambda es, grp: grp["ci_high"] < 0, -1, ""))
    rules.append((None, None, "gaps ≥ 5 %", lambda es: _in(es, "gap", "≥ 5 %"), lambda es, grp: grp["ci_high"] < -0.2, 0,
                  " ; une entrée sur repli serait à étudier"))
    if go_min > 6.5:
        rules.append(("go_min", 6.5, "notes 6-7", lambda es: _in(es, "composite", "6-7"), lambda es, grp: grp["ci_low"] > 0.2,
                      1, f", le seuil de {go_min:g} est peut-être trop strict"))
    if go_min < GO_MIN_CAP:
        rules.append(("go_min", min(go_min + 0.5, GO_MIN_CAP), f"notes {go_min:g}-{go_min + 0.5:g}", band,
                      lambda es, grp: grp["ci_high"] < 0, -1, ""))
    out = []
    for key, value, label, pop, test, sens, tail in rules:
        proof = None
        for name, es in (("tous les plans", traded), ("GO seuls", go)):
            grp = _proof(pop(es))
            if not grp or not test(es, grp):
                continue
            if key in FILTER_KEYS:  # un filtre resserré doit perdre sur les plans qu'il retirerait vraiment
                cut = _proof(removed(es, s, key, value))
                if not cut or cut["ci_high"] >= 0:
                    continue
            proof = (name, grp)
            break
        if not proof:
            continue
        name, grp = proof
        why = (f"{label} : {grp['avg_r']:+.2f} R en moyenne (IC {grp['ci_low']:+.2f} ; {grp['ci_high']:+.2f}) "
               f"sur {grp['n']} plans, {grp['days']} jours ({name})")
        if name == "tous les plans" and (sub := pop(go)):
            why += f" · GO seuls : {_mean(sub):+.2f} R sur {len(sub)}"
        if sens:
            cg = pop(conf)
            if len(cg) >= CONFIRMED_MIN and not (_mean(cg) < 0 if sens < 0 else _mean(cg) > 0):
                continue  # les GO confirmés disent le contraire : pas de proposition
            if not cg:
                why += " · GO confirmés : pas encore mesurés dans cette tranche"
            else:
                why += f" · GO confirmés : {_mean(cg):+.2f} R sur {len(cg)}"
                if len(cg) < CONFIRMED_MIN:
                    why += ", trop peu pour trancher"
        out.append({"key": key, "value": value, "why": why + tail, "population": name})
    timing = st.get("timing")
    if timing and timing["n"] >= MIN_SAMPLES and not 0.67 <= timing["median_ratio"] <= 1.5:
        out.append({"key": None, "value": None,
                    "why": f"les TP arrivent en {timing['median_ratio']:g}× le délai prévu (médiane sur {timing['n']}, "
                           "TP coupés par l'horizon exclus) : à recaler"})
    return out


def report_text(st: dict) -> str:
    h = st.get("horizon") or 2
    lines = [f"📊 **Bilan du forward-test** (compte fictif Alpaca) : plans bruts du scanner (GO + NO GO, entrée limite "
             f"au prix de pré-ouverture, stop fixe, {h} séance{'s' if h > 1 else ''}, avant frais)"]
    if not st["overall"]:
        lines.append(f"Pas encore de plan du scanner terminé ({st['total']} passés au total).")
        return "\n".join(lines)
    o = st["overall"]
    lines.append(f"{st['traded']} plans déclenchés sur {st['done']} terminés ({st['fill_rate']:.0%}) · "
                 f"{o['avg_r']:+.2f} R en moyenne · {o['win_rate']:.0%} gagnants · TP1 touché {o['tp1_rate']:.0%}")
    if st.get("go"):
        lines.append(f"-# GO seuls : {st['go']['n']} plans · {st['go']['avg_r']:+.2f} R en moyenne · "
                     f"{st['go']['win_rate']:.0%} gagnants")
    for key in ("decision", "composite", "rsi", "rvol", "gap", "catalyst", "dollar_volume"):
        b = st["by"].get(key)
        if b and b["groups"]:
            cells = " · ".join(f"{k} : {v['avg_r']:+.2f} R ({v['n']})" for k, v in sorted(b["groups"].items()))
            lines.append(f"-# {b['label']} : {cells}")
    if st["timing"]:
        lines.append(f"-# Délais : les TP arrivent en {st['timing']['median_ratio']:g}× le délai prévu (médiane, "
                     f"{st['timing']['n']} TP, en minutes de cotation ; les TP coupés par l'horizon sont exclus, "
                     "ce qui tire le ratio vers le bas)")
    if st.get("manual"):
        lines.append(f"-# Plans manuels (à part, hors propositions) : {st['manual']['n']} · "
                     f"{st['manual']['avg_r']:+.2f} R en moyenne")
    notes = [sg for sg in st["suggestions"] if not sg["key"]]
    changes = [sg for sg in st["suggestions"] if sg["key"]]
    for sg in notes:
        lines.append(f"• {sg['why']}")
    if changes:
        lines.append("Mes propositions de réglages suivent : rien ne change sans ton accord.")
    elif not notes:
        lines.append(f"-# Pas encore de preuve pour proposer un réglage ({MIN_SAMPLES} plans sur {MIN_DAYS} séances "
                     "au moins par groupe, et un intervalle de confiance net).")
    return "\n".join(lines)

