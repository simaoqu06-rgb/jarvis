"""Chasse aux ratés du potentiel +10 : les actions qui ont fait +10 % et que JARVIS n'a pas transformées en gain.

Demande de Simão (18 octobre 2026, « un JARVIS affamé d'argent ») : chaque jour, les actions de l'univers du labo
dont le plus haut de séance a dépassé l'ouverture d'au moins SEUIL_PCT % (table day_moves, colonne up_pct, comme
stats.capture et summary._movers) et la RAISON pour laquelle JARVIS ne les a pas prises, avec ce qui était visible
avant l'ouverture.

Fenêtre : les FENETRE_SEANCES dernières séances d'actions présentes dans day_moves, bloc scellé exclu (seal.bounds(),
comme stats.capture). Pour chaque mouvement, une seule raison, lue dans la table trades (plans de v1, version de
génération courante, comme summary.py via validator.current_version) :
- « pas de plan » : aucune ligne trades pour la valeur ce jour-là ;
- « plan non déclenché » : des plans, aucun déclenché (triggered = 0) ;
- « déclenché, stop avant TP1 » : déclenché, TP1 jamais touché (stop, ou sortie à la clôture sans TP1 : la
  sortie réelle est donnée dans le détail) ;
- « TP1 touché » : capturé.
Et si le scanner potentiel +10 (table plus10_trades, déclaration n° 9) l'avait listé : listes S+10 ou Rebond, rang
dans la liste (« la liste » = les TOP_LISTE premiers, les rangs suivants servent de comparaison).

Visible avant l'ouverture : le gap (day_moves.gap_pct) et le type de catalyseur (catalysts.catalyst_type, « à classer »
tant que la routine du soir ne l'a pas classé). Le volume relatif rvol30 n'est connu que 30 min après l'ouverture : il
est affiché, marqué « après l'ouverture ».

Agrégats : nombre de mouvements, répartition par raison, taux de capture (TP1 / mouvements) et, pour les ratés « pas de
plan », la répartition par tranche de gap COMPARÉE au taux de base de l'univers (part des séances de day_moves des mêmes
jours, dans chaque tranche de gap, qui ont fait ≥ +SEUIL_PCT %). La tranche à regarder davantage est celle où une séance
a le plus de chances d'être un +10 sans plan (ratés sans plan / séances de la tranche), avec au moins MIN_SEANCES
séances et MIN_RATES ratés.

Limites : c'est une lecture, comme l'autopsie ; rien ici ne filtre ni ne change un plan. Le plus haut de séance est
mesuré après coup : une action à +10 % au plus haut a pu finir en baisse, et personne n'aurait vendu au plus haut.
Vingt séances, c'est peu : la tranche « à regarder » est une piste à déclarer (une règle se juge en re-simulant tous les
signaux), jamais une preuve. Les mouvements du bloc scellé restent visibles ailleurs (seal.py) mais ne sont pas lus ici.
"""
import sqlite3
from collections import Counter
from datetime import datetime

from app.lab import empreinte as E
from app.lab import seal as SEAL
from app.lab import stats as STATS
from app.lab.db import LabDB, now_iso

CACHE_KEY = "rates"
FENETRE_SEANCES = 20
SEUIL_PCT = 10.0
MAX_DETAIL = 10
TOP_LISTE = 5          # plus10.TOP : les 5 premières de chaque liste sont « la liste »
MIN_SEANCES = 30       # séances d'une tranche de gap en dessous desquelles elle n'est pas désignée
MIN_RATES = 3          # ratés sans plan d'une tranche en dessous desquels elle n'est pas désignée
CATEGORIES = {"pas_de_plan": "pas de plan", "non_declenche": "plan non déclenché",
              "stop_avant_tp1": "déclenché, stop avant TP1", "tp1": "TP1 touché"}
GAP_BUCKETS = ((-1e9, 0.0, "gap < 0 %"), (0.0, 2.0, "gap 0-2 %"), (2.0, 5.0, "gap 2-5 %"),
               (5.0, 10.0, "gap 5-10 %"), (10.0, 1e9, "gap ≥ 10 %"))
GAP_INCONNU = "gap inconnu"
LISTES_FR = {"s10": "S+10", "rebond": "Rebond"}


def gap_bucket(gap: float | None) -> str:
    if gap is None:
        return GAP_INCONNU
    return next(label for lo, hi, label in GAP_BUCKETS if lo <= gap < hi)


def categorie(plans: int, triggered, tp1) -> str:
    """Raison du raté (ou « tp1 ») à partir des plans v1 de la valeur ce jour-là."""
    if not plans:
        return "pas_de_plan"
    if tp1:
        return "tp1"
    if triggered:
        return "stop_avant_tp1"
    return "non_declenche"


# --- Lecture de la base ------------------------------------------------------------------------

def window(conn: sqlite3.Connection, n: int = FENETRE_SEANCES) -> list[str]:
    """Les `n` dernières séances d'actions de day_moves, hors bloc scellé, de la plus ancienne à la plus récente."""
    rows = conn.execute("SELECT DISTINCT day FROM day_moves WHERE market = 'stock' AND day NOT BETWEEN ? AND ? "
                        "ORDER BY day DESC LIMIT ?", (*SEAL.bounds(), n)).fetchall()
    return sorted(r[0] for r in rows)


def _plans(conn, day: str, symbol: str, version: str | None) -> tuple:
    """Plans v1 de la valeur ce jour-là : nombre, déclenché, TP1, meilleur R net, sortie du premier déclenché."""
    vcond = " AND method_version = ?" if version else ""
    params = ("stock", day, symbol, *((version,) if version else ()))
    plans, trig, tp1, best = conn.execute(
        "SELECT COUNT(*), MAX(triggered), MAX(tp1_hit), MAX(r_net) FROM trades "
        f"WHERE market = ? AND day = ? AND symbol = ?{vcond}", params).fetchone()
    first = conn.execute(
        f"SELECT exit_reason FROM trades WHERE market = ? AND day = ? AND symbol = ?{vcond} AND triggered = 1 "
        "ORDER BY planned_at LIMIT 1", params).fetchone()
    return plans, trig, tp1, best, first[0] if first else None


def _scanner(conn, day: str, symbol: str) -> dict:
    """Listes du scanner +10 où la valeur figurait ce jour-là, avec son meilleur rang."""
    rows = conn.execute("SELECT liste, MIN(rang) FROM plus10_trades WHERE day = ? AND symbol = ? GROUP BY liste",
                        (day, symbol)).fetchall()
    return {liste: rang for liste, rang in rows}


def movers(conn: sqlite3.Connection, days: list[str], version: str | None) -> list[dict]:
    if not days:
        return []
    marks = ",".join("?" * len(days))
    rows = conn.execute(
        f"SELECT day, symbol, up_pct, gap_pct, rvol30 FROM day_moves WHERE market = 'stock' AND day IN ({marks}) "
        "AND up_pct >= ? ORDER BY day, up_pct DESC", (*days, SEUIL_PCT)).fetchall()
    out = []
    for day, symbol, up, gap, rvol in rows:
        plans, trig, tp1, best_r, sortie = _plans(conn, day, symbol, version)
        cat = conn.execute("SELECT catalyst_type FROM catalysts WHERE market = 'stock' AND day = ? AND symbol = ?",
                           (day, symbol)).fetchone()
        listes = _scanner(conn, day, symbol)
        out.append({"jour": day, "symbole": symbol, "hausse_pct": round(up, 1),
                    "categorie": categorie(plans, trig, tp1), "plans": plans,
                    "sortie": sortie, "meilleur_r": round(best_r, 2) if best_r is not None else None,
                    "scanner_plus10": {LISTES_FR.get(k, k): v for k, v in sorted(listes.items())},
                    "scanner_top": any(v <= TOP_LISTE for v in listes.values()),
                    "avant_ouverture": {"gap_pct": round(gap, 1) if gap is not None else None,
                                        "tranche_gap": gap_bucket(gap),
                                        "catalyseur": (cat[0] if cat else None) or "à classer"},
                    "apres_ouverture": {"rvol30": round(rvol, 2) if rvol is not None else None}})
    return out


def base_rates(conn: sqlite3.Connection, days: list[str], misses: list[dict]) -> dict:
    """Par tranche de gap : séances de l'univers ces jours-là, part à ≥ +SEUIL_PCT % (taux de base), ratés sans plan,
    leur part parmi les ratés sans plan et leur fréquence par séance de la tranche."""
    seances: Counter = Counter()
    hausses: Counter = Counter()
    if days:
        marks = ",".join("?" * len(days))
        for gap, up in conn.execute(f"SELECT gap_pct, up_pct FROM day_moves WHERE market = 'stock' AND day IN ({marks})",
                                    days):
            b = gap_bucket(gap)
            seances[b] += 1
            hausses[b] += (up or 0) >= SEUIL_PCT
    rates = Counter(m["avant_ouverture"]["tranche_gap"] for m in misses)
    total_s, total_r = sum(seances.values()), sum(rates.values())
    out = {}
    for label in [b[2] for b in GAP_BUCKETS] + [GAP_INCONNU]:
        n, k, r = seances[label], hausses[label], rates[label]
        if not n and not r:
            continue
        out[label] = {"seances": n, "part_des_seances": round(n / total_s, 4) if total_s else None,
                      "hausses": k, "taux_base": round(k / n, 4) if n else None,
                      "rates_sans_plan": r, "part_des_rates": round(r / total_r, 4) if total_r else None,
                      "rates_par_seance": round(r / n, 4) if n else None,
                      "echantillon_faible": n < MIN_SEANCES}
    return out


def a_regarder(tranches: dict) -> dict | None:
    """Tranche où une séance a le plus de chances d'être un +10 sans plan (assez de séances et de ratés)."""
    ok = [(label, t) for label, t in tranches.items()
          if label != GAP_INCONNU and t["seances"] >= MIN_SEANCES and t["rates_sans_plan"] >= MIN_RATES]
    if not ok:
        return None
    label, t = max(ok, key=lambda kv: (kv[1]["rates_par_seance"], kv[1]["rates_sans_plan"]))
    return {"tranche": label, "rates_sans_plan": t["rates_sans_plan"], "seances": t["seances"],
            "rates_par_seance": t["rates_par_seance"], "taux_base": t["taux_base"]}


def report(conn: sqlite3.Connection) -> dict:
    from app.lab import validator as V  # import tardif : comme autopsie.report

    version = V.current_version(conn)
    days = window(conn)
    moves = movers(conn, days, version)
    counts = Counter(m["categorie"] for m in moves)
    misses = [m for m in moves if m["categorie"] == "pas_de_plan"]
    tranches = base_rates(conn, days, misses)
    last = days[-1] if days else None
    detail = sorted((m for m in moves if m["jour"] == last), key=lambda m: -m["hausse_pct"])[:MAX_DETAIL]
    n = len(moves)
    rates = [m for m in moves if m["categorie"] != "tp1"]
    return {"version": version, "seuil_pct": SEUIL_PCT, "seances": len(days),
            "du": days[0] if days else None, "au": last, "mouvements": n,
            "par_categorie": {k: counts.get(k, 0) for k in CATEGORIES},
            "taux_capture": round(counts.get("tp1", 0) / n, 4) if n else None,
            "scanner_plus10": {"mouvements_listes": sum(1 for m in moves if m["scanner_plus10"]),
                               "mouvements_top": sum(1 for m in moves if m["scanner_top"]),
                               "rates_listes": sum(1 for m in rates if m["scanner_plus10"]),
                               "rates_top": sum(1 for m in rates if m["scanner_top"])},
            "par_jour": [{"jour": d, "mouvements": sum(1 for m in moves if m["jour"] == d),
                          "tp1": sum(1 for m in moves if m["jour"] == d and m["categorie"] == "tp1")} for d in days],
            "sans_plan_par_gap": tranches, "a_regarder": a_regarder(tranches),
            "derniere_seance": detail,
            "limites": ["Lecture seulement : rien ici ne filtre ni ne change un plan.",
                        "Plus haut de séance mesuré après coup : personne n'aurait vendu au plus haut.",
                        f"{FENETRE_SEANCES} séances seulement : la tranche à regarder est une piste à déclarer, "
                        "jamais une règle.",
                        "rvol30 n'est connu que 30 min après l'ouverture.",
                        "Bloc scellé exclu."]}


def signature(conn: sqlite3.Connection) -> list:
    moves = conn.execute("SELECT COUNT(*), COALESCE(MAX(day), '') FROM day_moves WHERE market = 'stock'").fetchone()
    trades = conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM trades").fetchone()
    p10 = conn.execute("SELECT COUNT(*), COALESCE(MAX(day), '') FROM plus10_trades").fetchone()
    typed = conn.execute("SELECT COUNT(*), COALESCE(MAX(classified_at), '') FROM catalysts "
                         "WHERE catalyst_type IS NOT NULL").fetchone()
    return [*trades, *moves, *p10, *typed, *E.sig(conn, "trades")]


def cached(conn: sqlite3.Connection) -> dict | None:
    return STATS.cached(conn, CACHE_KEY)


def step(db: LabDB, now: datetime, max_age: float) -> int | None:
    """Passage du worker : recalcule quand les mouvements, les trades, le scanner +10 ou les types de catalyseurs ont
    changé et que le cache a plus de `max_age` s (tout de suite après un déploiement) ; renvoie le nombre de
    mouvements."""
    sig = signature(db.conn)
    at = db.get("rates", "at", 0) or 0
    last = db.get("rates", "signature")
    if sig == last or (cached(db.conn) and now.timestamp() - at < max_age and not E.code_changed(last)):
        return None
    data = report(db.conn)
    with db.transaction():
        STATS.store(db.conn, CACHE_KEY, data, sig[0], now_iso())
        db.set("rates", "signature", sig)
        db.set("rates", "at", now.timestamp())
    return data["mouvements"]


# --- Discord -----------------------------------------------------------------------------------

def _pct(x) -> str:
    return "n/d" if x is None else f"{100 * x:.0f} %"


def _fr(x: float) -> str:
    return f"{x:g}".replace(".", ",")


def _ligne(m: dict) -> str:
    av, ap = m["avant_ouverture"], m["apres_ouverture"]
    gap = "gap n/d" if av["gap_pct"] is None else f"gap {av['gap_pct']:+g} %".replace(".", ",")
    rvol = "" if ap["rvol30"] is None else f" · rvol30 {_fr(ap['rvol30'])} (après l'ouverture)"
    scan = (" · scanner +10 : " + ", ".join(f"{k} rang {v}" for k, v in m["scanner_plus10"].items())
            if m["scanner_plus10"] else " · absent du scanner +10")
    sortie = f" ({m['sortie']})" if m["categorie"] == "stop_avant_tp1" and m["sortie"] else ""
    icone = "✅" if m["categorie"] == "tp1" else "❌"
    return (f"{icone} **{m['symbole']}** +{_fr(m['hausse_pct'])} % · {CATEGORIES[m['categorie']]}{sortie} · "
            f"{gap}, catalyseur {av['catalyseur']}{rvol}{scan}")


def text(data: dict | None) -> str:
    """`!rates` : les +10 % que JARVIS n'a pas transformés en gain, pourquoi, et la tranche de gap à regarder."""
    if not data:
        return ("🎣 Chasse aux ratés pas encore calculée (le worker du labo la calcule à son prochain passage, "
                "après le déploiement).")
    if not data.get("seances"):
        return "🎣 Chasse aux ratés : pas encore de séance d'actions dans les mouvements du jour."
    n = data["mouvements"]
    lines = [f"🎣 **Chasse aux ratés du potentiel +10** ({data['seances']} séances du {data['du']} au {data['au']}, "
             f"plus haut ≥ ouverture +{_fr(data['seuil_pct'])} %)"]
    if not n:
        lines.append("Aucun mouvement de ce niveau sur la fenêtre.")
        return "\n".join(lines)
    c = data["par_categorie"]
    lines.append(f"{n} mouvements · capturés (TP1) {c['tp1']} soit **{_pct(data['taux_capture'])}** · "
                 f"pas de plan {c['pas_de_plan']} · plan non déclenché {c['non_declenche']} · "
                 f"déclenché, stop avant TP1 {c['stop_avant_tp1']}")
    sc = data["scanner_plus10"]
    lines.append(f"-# Scanner +10 : {sc['mouvements_listes']} mouvements listés (top {TOP_LISTE} : "
                 f"{sc['mouvements_top']}), dont {sc['rates_listes']} ratés (top {TOP_LISTE} : {sc['rates_top']})")
    tr = data.get("sans_plan_par_gap") or {}
    if c["pas_de_plan"]:
        lines.append("**Ratés sans plan par gap** (contre le taux de base de l'univers, mêmes jours)")
        for label, t in tr.items():
            if not t["rates_sans_plan"] and not t["hausses"]:
                continue
            faible = " · échantillon faible" if t["echantillon_faible"] else ""
            lines.append(f"-# {label} : {t['rates_sans_plan']} ratés ({_pct(t['part_des_rates'])} des ratés) · "
                         f"taux de base {_pct(t['taux_base'])} ≥ +10 % sur {t['seances']} séances "
                         f"({_pct(t['part_des_seances'])} des séances){faible}")
    reg = data.get("a_regarder")
    if reg:
        lines.append(f"🔎 À regarder davantage : **{reg['tranche']}** ({reg['rates_sans_plan']} +10 sans plan sur "
                     f"{reg['seances']} séances, taux de base {_pct(reg['taux_base'])}) · piste à déclarer, pas une règle")
    detail = data.get("derniere_seance") or []
    if detail:
        lines.append(f"**Séance du {data['au']}**")
        lines += [_ligne(m) for m in detail]
    lines.append("-# Lecture seulement : rien ici ne filtre ni ne change un plan ; plus haut mesuré après coup.")
    return "\n".join(lines)
