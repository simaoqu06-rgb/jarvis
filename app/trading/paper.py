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
from app.sources import alpaca
from app.trading import calendar as CAL
from app.trading.settings import settings

log = logging.getLogger(__name__)
PAPER_PATH = os.getenv("JARVIS_PAPER_PATH", "data/paper.json")
MIN_SAMPLES = 15  # en dessous, un groupe ne sert pas à proposer un réglage


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


def record(plan: dict, features: dict | None, origin: str) -> dict:
    sc = plan["score"]
    return {
        "id": uuid.uuid4().hex[:8], "origin": origin, "created_at": plan["created_at"], "symbol": plan["symbol"],
        "side": plan["side"], "entry": plan["entry"], "stop": plan["stop"], "features": features or {},
        "decision": sc["decision"], "composite": sc["composite"],
        "parts": {k: v["score"] for k, v in sc["parts"].items()},
        "status": "pending", "legs": [], "result_r": None, "outcome": None,
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
    entry = record(plan, features, origin)
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
}


def _group(entries: list[dict]) -> dict:
    rs = [e["result_r"] for e in entries]
    tp1 = [e for e in entries if any(leg["exit"] == "tp" for leg in e["legs"])]
    return {"n": len(entries), "avg_r": round(sum(rs) / len(rs), 2), "win_rate": round(sum(1 for r in rs if r > 0) / len(rs), 2),
            "tp1_rate": round(len(tp1) / len(entries), 2)}


def stats(book: PaperBook, s=settings) -> dict:
    done = [e for e in book.entries if e["status"] == "done"]
    traded = [e for e in done if e["result_r"] is not None]
    out = {"total": len(book.entries), "done": len(done), "traded": len(traded),
           "fill_rate": round(sum(1 for e in done if e["outcome"] != "non déclenché") / len(done), 2) if done else None,
           "overall": _group(traded) if traded else None, "by": {}, "timing": None, "suggestions": []}
    for key, (label, fn) in BUCKETS.items():
        groups: dict[str, list] = {}
        for e in traded:
            b = fn(e)
            if b is not None:
                groups.setdefault(b, []).append(e)
        out["by"][key] = {"label": label, "groups": {b: _group(es) for b, es in groups.items()}}
    # Délais : temps réel jusqu'au TP divisé par le délai prévu (1 = pile à l'heure).
    ratios = []
    for e in traded:
        for leg in e["legs"]:
            if leg["exit"] == "tp" and leg["predicted_minutes"] and leg["filled_at"]:
                created = _ts(e["created_at"])
                minutes = (_ts(leg["exit_at"]) - max(created, _ts(leg["filled_at"]))).total_seconds() / 60
                ratios.append(minutes / leg["predicted_minutes"])
    if ratios:
        out["timing"] = {"n": len(ratios), "median_ratio": round(median(ratios), 2)}
    out["suggestions"] = suggestions(out, s)
    return out


def suggestions(st: dict, s=settings) -> list[dict]:
    """Réglages à envisager, seulement quand un groupe a assez d'exemples et un écart net."""
    out = []

    def g(key, bucket):
        grp = st["by"].get(key, {}).get("groups", {}).get(bucket)
        return grp if grp and grp["n"] >= MIN_SAMPLES else None

    hi_rsi = g("rsi", "≥ 65")
    if hi_rsi and hi_rsi["avg_r"] < 0 < (g("rsi", "55-65") or {"avg_r": -1})["avg_r"] and s["rsi_max"] > 65:
        out.append({"key": "rsi_max", "value": 65, "why": f"RSI ≥ 65 : {hi_rsi['avg_r']:+.2f} R en moyenne sur {hi_rsi['n']} plans"})
    low_rvol = g("rvol", "< 1,5×")
    if low_rvol and low_rvol["avg_r"] < 0 and s["scan_rvol_min"] < 2:
        out.append({"key": "scan_rvol_min", "value": 2.0,
                    "why": f"volume veille < 1,5× : {low_rvol['avg_r']:+.2f} R sur {low_rvol['n']} plans"})
    big_gap = g("gap", "≥ 5 %")
    if big_gap and big_gap["avg_r"] < -0.2:
        out.append({"key": None, "value": None,
                    "why": f"gaps ≥ 5 % : {big_gap['avg_r']:+.2f} R sur {big_gap['n']} plans ; une entrée sur repli serait à étudier"})
    mid = g("composite", "6-7")
    if mid and mid["avg_r"] > 0.2 and s["go_min"] > 6:
        out.append({"key": "go_min", "value": 6.5, "why": f"notes 6-7 : {mid['avg_r']:+.2f} R sur {mid['n']} plans, le seuil de 7 est peut-être trop strict"})
    good = g("composite", "7-8")
    if good and good["avg_r"] < 0:
        out.append({"key": "go_min", "value": s["go_min"] + 0.5, "why": f"notes 7-8 : {good['avg_r']:+.2f} R sur {good['n']} plans"})
    timing = st.get("timing")
    if timing and timing["n"] >= MIN_SAMPLES and not 0.67 <= timing["median_ratio"] <= 1.5:
        out.append({"key": None, "value": None,
                    "why": f"les TP arrivent en {timing['median_ratio']:g}× le délai prévu (médiane sur {timing['n']}) : à recaler"})
    return out


def report_text(st: dict) -> str:
    lines = ["📊 **Bilan du forward-test** (compte fictif Alpaca, plans du scanner)"]
    if not st["overall"]:
        lines.append(f"Pas encore de plan terminé ({st['total']} passés au total).")
        return "\n".join(lines)
    o = st["overall"]
    lines.append(f"{st['traded']} plans déclenchés sur {st['done']} terminés ({st['fill_rate']:.0%}) · "
                 f"{o['avg_r']:+.2f} R en moyenne · {o['win_rate']:.0%} gagnants · TP1 touché {o['tp1_rate']:.0%}")
    for key in ("decision", "composite", "rsi", "rvol", "gap", "catalyst"):
        b = st["by"][key]
        if b["groups"]:
            cells = " · ".join(f"{k} : {v['avg_r']:+.2f} R ({v['n']})" for k, v in sorted(b["groups"].items()))
            lines.append(f"-# {b['label']} : {cells}")
    if st["timing"]:
        lines.append(f"-# Délais : les TP arrivent en {st['timing']['median_ratio']:g}× le délai prévu (médiane, {st['timing']['n']} TP)")
    notes = [sg for sg in st["suggestions"] if not sg["key"]]
    changes = [sg for sg in st["suggestions"] if sg["key"]]
    for sg in notes:
        lines.append(f"• {sg['why']}")
    if changes:
        lines.append("Mes propositions de réglages suivent : rien ne change sans ton accord.")
    elif not notes:
        lines.append(f"-# Pas encore assez d'exemples pour proposer un réglage ({MIN_SAMPLES} par groupe).")
    return "\n".join(lines)

