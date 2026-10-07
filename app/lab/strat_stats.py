"""Tableau des stratégies du labo v2 et diagnostic de v1 (lecture seule, sans le bloc scellé).

Pour chaque marché et chaque stratégie (v1 comprise, en référence) :
- signaux, déclenchements, jours ;
- espérance nette en R de chaque sortie (strategies.EXITS), avec son intervalle de confiance à 95 %
  en grappes par jour (stats.mean_ci) : 54 jours de bourse, c'est 54 observations, pas 16 000 ;
- taux de réussite et TP1 : affichés, jamais utilisés pour classer (la crypto de v1 gagne 45 % de ses
  trades et perd −0,25 R par trade) ;
- robustesse (glissement ×2 et ×3, entrée retardée, ordres limites à peine touchés retirés) ;
- écart au témoin aléatoire (même stop, mêmes objectifs, entrée au hasard), apparié trade par trade,
  avec son z en grappes : sans écart positif net, la stratégie n'a pas d'avantage d'entrée ;
- espérance par régime de marché (table regimes).
Le classement se fait sur l'espérance de la sortie de référence, et une stratégie n'est « candidate »
que si la borne basse de son intervalle est au-dessus de zéro ET si elle bat son témoin (z ≥ 2).
Rien de tout ça ne promeut quoi que ce soit : ni le scellé, ni la quantité de rejeu (au moins un an,
deux régimes) ne sont encore là.

Diagnostic v1 (Lot 1 du plan) : espérance par gap d'ouverture, par mode d'entrée, par raison de sortie,
et brut contre net de frais, pour vérifier « v1 achète la faiblesse » et « les frais expliquent la perte ».
"""
import json
import sqlite3
from collections import defaultdict

from app.lab import seal as SEAL
from app.lab import stats as STATS
from app.lab import strategies as ST

MIN_N = 30
CANDIDATE_Z = 2.0
COLUMNS = ("r_net", "r_sans_be", "r_2r", "r_suiveur", "r_glisse_x2", "r_glisse_x3", "r_retard", "r_manque")
GAP_BUCKETS = ((-1e9, -2.0, "gap ≤ −2 %"), (-2.0, -0.5, "gap −2 à −0,5 %"), (-0.5, 0.5, "gap ±0,5 %"),
               (0.5, 2.0, "gap +0,5 à +2 %"), (2.0, 1e9, "gap ≥ +2 %"))


class Mean:
    """Moyenne en grappes par jour, en un passage."""

    __slots__ = ("days", "total", "n")

    def __init__(self):
        self.days: dict = defaultdict(lambda: [0.0, 0])
        self.total, self.n = 0.0, 0

    def add(self, day: str, r: float | None) -> None:
        if r is None:
            return
        cell = self.days[day]
        cell[0] += r
        cell[1] += 1
        self.total += r
        self.n += 1

    def result(self) -> dict:
        m, se, ci = STATS.mean_ci(self.days, self.total, self.n)
        return {"n": self.n, "esperance_r": round(m, 4) if m is not None else None, "ic95": ci,
                "z": round(m / se, 2) if m is not None and se else None, "jours": len(self.days)}


def _strategies(conn: sqlite3.Connection) -> dict:
    out: dict = {}
    sql = (f"SELECT s.market, s.strategy, s.day, s.triggered, s.tp1_hit, s.temoin_r, s.temoin_triggered, "
           f"{', '.join('s.' + c for c in COLUMNS)}, r.label FROM strat_trades s "
           "LEFT JOIN regimes r ON r.market = s.market AND r.day = s.day")
    acc: dict = {}
    for row in conn.execute(sql):
        market, name, day, trig, tp1, wit, wit_trig = row[:7]
        values = dict(zip(COLUMNS, row[7:7 + len(COLUMNS)]))
        label = row[-1] or "inconnu"
        a = acc.setdefault((market, name), {"signaux": 0, "declenches": 0, "gagnants": 0, "tp1": 0,
                                            "sorties": defaultdict(Mean), "temoin": Mean(), "ecart": Mean(),
                                            "regimes": defaultdict(Mean), "manques": 0})
        a["signaux"] += 1
        if not trig:
            continue
        a["declenches"] += 1
        a["gagnants"] += (values["r_net"] or 0) > 0
        a["tp1"] += bool(tp1)
        for c in COLUMNS:
            a["sorties"][c].add(day, values[c])
        a["manques"] += values["r_manque"] is None
        a["regimes"][label].add(day, values["r_net"])
        if wit_trig and wit is not None and values["r_net"] is not None:
            a["temoin"].add(day, wit)
            a["ecart"].add(day, values["r_net"] - wit)
    for (market, name), a in acc.items():
        n = a["declenches"]
        main = a["sorties"]["r_net"].result()
        ecart = a["ecart"].result()
        item = {
            "nom": ST.LABELS.get(name, name), "signaux": a["signaux"], "declenches": n,
            "taux_declenchement": round(n / a["signaux"], 4) if a["signaux"] else None,
            "jours": main["jours"], **{k: main[k] for k in ("esperance_r", "ic95")},
            "reussite": round(a["gagnants"] / n, 4) if n else None, "tp1": round(a["tp1"] / n, 4) if n else None,
            "sorties": {c: a["sorties"][c].result() for c in ("r_net", "r_sans_be", "r_2r", "r_suiveur")},
            "robustesse": {c: a["sorties"][c].result() for c in ("r_glisse_x2", "r_glisse_x3", "r_retard", "r_manque")},
            "ordres_limites_retires": a["manques"],
            "temoin": {"esperance_r": a["temoin"].result()["esperance_r"], "ecart_r": ecart["esperance_r"],
                       "ic95": ecart["ic95"], "z": ecart["z"], "n": ecart["n"]},
            "regimes": {k: v.result() for k, v in sorted(a["regimes"].items())},
            "echantillon_faible": n < MIN_N,
        }
        ci = item["ic95"]
        item["candidate"] = bool(ci and ci[0] > 0 and (ecart["z"] or 0) >= CANDIDATE_Z and n >= MIN_N)
        out.setdefault(market, {})[name] = item
    for market, items in out.items():
        out[market] = dict(sorted(items.items(), key=lambda kv: -(kv[1]["esperance_r"] if kv[1]["esperance_r"]
                                                                   is not None else -99)))
    return out


def _bucket(gap: float | None) -> str:
    if gap is None:
        return "gap inconnu"
    return next(label for lo, hi, label in GAP_BUCKETS if lo <= gap < hi)


def diagnostic_v1(conn: sqlite3.Connection) -> dict:
    """Espérance de v1 par gap d'ouverture, mode d'entrée et raison de sortie ; brut contre net."""
    out: dict = {}
    sql = ("SELECT market, day, r_net, r_gross, exit_reason, json_extract(context, '$.gap_open_pct'), "
           "json_extract(result, '$.entry_mode') FROM trades WHERE triggered = 1 AND r_net IS NOT NULL")
    acc: dict = {}
    for market, day, r, gross, reason, gap, mode in conn.execute(sql):
        a = acc.setdefault(market, {"net": Mean(), "brut": Mean(), "gap": defaultdict(Mean),
                                    "entree": defaultdict(Mean), "sortie": defaultdict(Mean)})
        a["net"].add(day, r)
        a["brut"].add(day, gross)
        if market == "stock":
            a["gap"][_bucket(gap)].add(day, r)
        a["entree"][mode or "inconnu"].add(day, r)
        a["sortie"][reason or "inconnu"].add(day, r)
    for market, a in acc.items():
        net, brut = a["net"].result(), a["brut"].result()
        out[market] = {
            "net": net, "brut": brut,
            "frais_r": round(brut["esperance_r"] - net["esperance_r"], 4)
            if net["esperance_r"] is not None and brut["esperance_r"] is not None else None,
            "par_gap": {k: a["gap"][k].result() for lo, hi, k in GAP_BUCKETS if k in a["gap"]},
            "par_entree": {k: v.result() for k, v in a["entree"].items()},
            "par_sortie": {k: v.result() for k, v in sorted(a["sortie"].items(), key=lambda kv: -kv[1].n)},
        }
    return out


def report(conn: sqlite3.Connection) -> dict:
    out = {"declaration": {"le": ST.REGISTERED_ON, "essais": len(ST.trials()), "sorties": ST.EXITS,
                           "robustesse": ST.STRESS, "doc": "docs/labo-strategies.md"},
           "scelle": SEAL.describe(), "strategies": {}, "diagnostic_v1": {}}
    try:  # base pas encore migrée par le worker (lecture seule) : tableau vide plutôt qu'une erreur
        out["strategies"] = _strategies(conn)
        out["diagnostic_v1"] = diagnostic_v1(conn)
    except sqlite3.Error as exc:
        out["erreur"] = f"base pas encore prête : {exc}"
    return out


# --- Cache (écrit par le worker, lu par le serveur) ------------------------------------------

CACHE_KEY = "strategies"


def signature(conn: sqlite3.Connection) -> list:  # worker seulement (base migrée)
    return list(conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM strat_trades").fetchone())


def cached(conn: sqlite3.Connection) -> dict | None:
    return STATS.cached(conn, CACHE_KEY)


# --- Discord ---------------------------------------------------------------------------------

MARKET_FR = {"stock": "Actions", "crypto": "Crypto"}


def _r(x) -> str:
    return "n/d" if x is None else f"{x:+.2f}".replace(".", ",")


def text(rep: dict | None) -> str:
    if not rep:
        return "🧪 **Stratégies du labo v2** : pas encore de mesure (le rejeu n'a pas encore traité de jour avec elles)."
    lines = [f"🧪 **Stratégies du labo v2** · {rep['declaration']['essais']} essais déclarés le "
             f"{rep['declaration']['le']} · espérance nette en R par trade [IC 95 %]"]
    for market, items in (rep.get("strategies") or {}).items():
        lines.append(f"**{MARKET_FR.get(market, market)}**")
        for name, it in items.items():
            ci = it.get("ic95")
            ci_txt = f" [{_r(ci[0])} ; {_r(ci[1])}]" if ci else ""
            wit = it.get("temoin") or {}
            flag = "✅ " if it.get("candidate") else ""
            lines.append(f"{flag}{it['nom']} : {_r(it['esperance_r'])} R{ci_txt} · {it['declenches']} trades, "
                         f"{it['jours']} j · réussite {round(100 * (it['reussite'] or 0))} % · "
                         f"vs témoin {_r(wit.get('ecart_r'))} R (z {wit.get('z') if wit.get('z') is not None else 'n/d'})"
                         + (" · échantillon faible" if it.get("echantillon_faible") else ""))
    diag = (rep.get("diagnostic_v1") or {}).get("stock")
    if diag:
        gaps = " · ".join(f"{k} {_r(v['esperance_r'])} ({v['n']})" for k, v in diag["par_gap"].items())
        lines.append(f"Diagnostic v1 actions : brut {_r(diag['brut']['esperance_r'])} R, net "
                     f"{_r(diag['net']['esperance_r'])} R · {gaps}")
    lines.append(f"🔒 {rep['scelle']['texte']}")
    return "\n".join(lines)
