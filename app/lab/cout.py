"""Déclaration n° 8, D3 : R net des trades v1 d'actions recalculé une fois au coût du demi-pas.

Le simulateur compte désormais au moins un demi-pas de cotation par côté (simulator.stock_slippage). Les
trades v1 déjà journalisés (tables trades et trades_sealed) gardent leur résultat complet : leur R net est
recalculé avec la même formule (simulator.net_r) et le nouveau coût, sans rien rejouer, pour que le Labo 1
(statistiques, leçons, méthode, grade) ne mélange pas deux coûts. Le bloc scellé est recalculé sans être lu.
Les lignes du labo v2 sont refaites par le rattrapage de la déclaration n° 8. Chaque lot changé incrémente le
compteur rev:trades et note le modèle de coût (cout_rev) ; une fois fini, tous les caches qui lisent trades sont
invalidés, leur âge remis à zéro pour qu'ils se recalculent au passage suivant (I3).
"""
import json

from app.lab import empreinte as E
from app.lab import heartbeat
from app.lab import simulator as SIM
from app.lab.db import LabDB

JOB = "cout:n8"
TABLES = ("trades", "trades_sealed")
BATCH = 5000
CACHES = (("stats", "signature"), ("grade_calc", "signature"), ("lessons", "signature"), ("strats", "signature"),
          ("autopsie", "signature"))
AGES = (("stats", "at"), ("lessons", "at"), ("strats", "at"), ("autopsie", "at"))


def recost(result: dict, side: str = "long") -> dict | None:
    """Résultat recalculé au coût du jour (glissement et R net), ou None s'il ne change pas."""
    if not result.get("triggered") or not result.get("exits") or not result.get("order_prices") \
            or result.get("entry_liquidity") not in ("maker", "taker"):
        return None  # résultat d'un ancien format : laissé tel quel
    cost = SIM.costs("stock", result["order_prices"]["entry"])
    bps = round(cost["slippage"] * 10000, 2)
    if bps == result.get("slippage_bps"):
        return None
    return result | {"slippage_bps": bps, "r_net": SIM.net_r(result, cost, side)}


def step(db: LabDB, limit: int = BATCH) -> int:
    """Recalcule jusqu'à `limit` trades déclenchés d'actions de plus ; renvoie le nombre de trades changés."""
    if db.get(JOB, "fini"):
        return 0
    changed, left = 0, limit
    rev = E.cost_rev()
    for table in TABLES:
        last = db.get(JOB, table, 0) or 0
        if last < 0:
            continue  # table finie
        rows = db.conn.execute(f"SELECT id, side, result FROM {table} WHERE market = 'stock' AND triggered = 1 "
                               "AND id > ? ORDER BY id LIMIT ?", (last, left)).fetchall()
        with db.transaction():
            before = changed
            for tid, side, raw in rows:
                new = recost(json.loads(raw or "{}"), side or "long")
                if new is not None:
                    db.conn.execute(f"UPDATE {table} SET r_net = ?, result = ?, cout_rev = ? WHERE id = ?",
                                    (new["r_net"], json.dumps(new, ensure_ascii=False), rev, tid))
                    changed += 1
            if changed > before:
                db.bump("trades")
            db.set(JOB, table, rows[-1][0] if len(rows) == left else -1)
        heartbeat.beat()
        left -= len(rows)
        if left <= 0:
            return changed
    with db.transaction():
        db.set(JOB, "fini", True)
        for job, key in CACHES:
            db.set(job, key, None)
        for job, key in AGES:
            db.set(job, key, 0)
    return changed
