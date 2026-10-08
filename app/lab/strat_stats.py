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
que si la borne basse de son intervalle est au-dessus de zéro ET si elle bat son témoin avec un z corrigé
du nombre d'essais déclarés (Bonferroni, candidate_z() : ≈ 3,1 pour 60 essais ; déclaration n° 2,
council du 7 octobre : avec z ≥ 2 et 60 essais, on attendrait une fausse candidate par hasard).
Frais (brut − net) et espérance par tranche de rang de volume, sans rien filtrer : les ordres stop sur
cassure des petites valeurs glissent plus.
Déclaration n° 3 (avis de GPT et Gemini) : le simulateur ne compte aucune commission en actions (Alpaca),
alors que Simão passe ses ordres chez IBKR Pro (plan Fixe : 0,005 $ par action, plafonné à 1 % de l'ordre).
Pour les actions, l'espérance de référence (classement et règle « candidate ») est donc l'espérance nette
après cette commission estimée (commission_ibkr_r : deux fois 0,005 $ par action rapportés au risque par
action ; le minimum de 1 $ par ordre, qui dépend de la taille de position, est ignoré : c'est un plancher).
Espérance aussi par tranche de frais estimés avant l'entrée (glissement, frais et commission rapportés
au risque), sans rien filtrer : c'est la mesure qui dira si un plafond de frais en R vaut un challenger.
Rien de tout ça ne promeut quoi que ce soit : ni le scellé, ni la quantité de rejeu (au moins un an,
deux régimes) ne sont encore là.

Diagnostic v1 (Lot 1 du plan) : espérance par gap d'ouverture, par mode d'entrée, par raison de sortie,
et brut contre net de frais, pour vérifier « v1 achète la faiblesse » et « les frais expliquent la perte ».
"""
import json
import sqlite3
from collections import defaultdict
from statistics import NormalDist

from app.lab import derivs as DER
from app.lab import seal as SEAL
from app.lab import simulator as SIM
from app.lab import stats as STATS
from app.lab import strategies as ST

MIN_N = 30
ALPHA = 0.05
COLUMNS = ("r_net", "r_sans_be", "r_2r", "r_suiveur", "r_emperor", "r_emperor_alerte", "r_emperor_confirme",
           "r_glisse_x2", "r_glisse_x3", "r_retard", "r_manque", "r_gross")
RANK_BUCKETS = ((1, 50, "rang 1-50"), (51, 150, "rang 51-150"), (151, 10**9, "rang 151+"))
IBKR_PER_SHARE = 0.005      # IBKR Pro, plan Fixe (/mnt/project-files/trading/plateforme-actions-us.md)
IBKR_MAX_PCT = 0.01         # plafond : 1 % de la valeur de l'ordre
COST_BUCKETS = ((0.0, 0.1, "frais < 0,1 R"), (0.1, 0.2, "frais 0,1-0,2 R"), (0.2, 0.4, "frais 0,2-0,4 R"),
                (0.4, 1e9, "frais ≥ 0,4 R"))


def candidate_z(trials: int | None = None) -> float:
    """Seuil de z contre le témoin, corrigé du nombre d'essais déclarés (Bonferroni, unilatéral)."""
    n = trials or len(ST.trials())
    return round(NormalDist().inv_cdf(1 - ALPHA / n), 2)


def ibkr_commission_r(entry: float | None, stop: float | None) -> float | None:
    """Commission IBKR Fixe d'un aller-retour, en R : 2 × 0,005 $ par action (plafonnée à 1 % du prix)
    rapportés au risque par action. None si le plan est inconnu."""
    if not entry or stop is None or entry <= stop:
        return None
    return 2 * min(IBKR_PER_SHARE, IBKR_MAX_PCT * entry) / (entry - stop)


def expected_cost_r(market: str, entry: float | None, stop: float | None, rank) -> float | None:
    """Frais estimés avant l'entrée, en R : glissement et frais du simulateur (prise de liquidité, entrée et
    sortie), plus la commission IBKR en actions."""
    if not entry or stop is None or entry <= stop:
        return None
    c = SIM.costs(market, entry, rank)
    out = 2 * (c["slippage"] + c["taker"]) * entry / (entry - stop)
    return out + (ibkr_commission_r(entry, stop) or 0) if market == "stock" else out


def _cost_bucket(cost: float | None) -> str:
    if cost is None:
        return "frais inconnus"
    return next(label for lo, hi, label in COST_BUCKETS if lo <= cost < hi)


def _rank_bucket(rank) -> str:
    if rank is None:
        return "rang inconnu"
    return next(label for lo, hi, label in RANK_BUCKETS if lo <= rank <= hi)
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
    sql = (f"SELECT s.market, s.strategy, s.day, s.triggered, s.tp1_hit, s.temoin_r, s.temoin_triggered, s.rank, "
           f"s.entry, s.stop, s.symbol, s.signal_at, {', '.join('s.' + c for c in COLUMNS)}, r.label "
           "FROM strat_trades s LEFT JOIN regimes r ON r.market = s.market AND r.day = s.day")
    acc: dict = {}
    z_min = candidate_z()
    derivs = DER.load(conn)
    for row in conn.execute(sql):
        market, name, day, trig, tp1, wit, wit_trig, rank, entry, stop, symbol, signal_at = row[:12]
        values = dict(zip(COLUMNS, row[12:12 + len(COLUMNS)]))
        label = row[-1] or "inconnu"
        a = acc.setdefault((market, name), {"signaux": 0, "declenches": 0, "gagnants": 0, "tp1": 0,
                                            "sorties": defaultdict(Mean), "temoin": Mean(), "ecart": Mean(),
                                            "regimes": defaultdict(Mean), "rangs": defaultdict(Mean), "manques": 0,
                                            "ibkr": Mean(), "commission": Mean(), "frais": defaultdict(Mean),
                                            "funding": defaultdict(Mean), "oi": defaultdict(Mean)})
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
        a["rangs"][_rank_bucket(rank)].add(day, values["r_net"])
        comm = (ibkr_commission_r(entry, stop) or 0.0) if market == "stock" else 0.0
        ref = values["r_net"] - comm if values["r_net"] is not None else None
        a["ibkr"].add(day, ref)
        if market == "stock":
            a["commission"].add(day, comm)
        a["frais"][_cost_bucket(expected_cost_r(market, entry, stop, rank))].add(day, ref)
        if market == "crypto":  # lot B : positionnement des dérivés au signal, sans rien filtrer
            funding, oi_chg = DER.context(derivs, symbol, signal_at)
            a["funding"][DER.bucket(funding, DER.FUNDING_BUCKETS)].add(day, ref)
            a["oi"][DER.bucket(oi_chg, DER.OI_BUCKETS)].add(day, ref)
        if wit_trig and wit is not None and values["r_net"] is not None:
            a["temoin"].add(day, wit)
            a["ecart"].add(day, values["r_net"] - wit)
    for (market, name), a in acc.items():
        n = a["declenches"]
        main = a["sorties"]["r_net"].result()
        brut = a["sorties"]["r_gross"].result()["esperance_r"]
        ecart = a["ecart"].result()
        ibkr = a["ibkr"].result() if market == "stock" else None
        ref = ibkr or main
        item = {
            "nom": ST.LABELS.get(name, name), "signaux": a["signaux"], "declenches": n,
            "taux_declenchement": round(n / a["signaux"], 4) if a["signaux"] else None,
            "jours": main["jours"], **{k: main[k] for k in ("esperance_r", "ic95")},
            "reussite": round(a["gagnants"] / n, 4) if n else None, "tp1": round(a["tp1"] / n, 4) if n else None,
            "sorties": {c: a["sorties"][c].result() for c in ST.EXITS},
            "brut_r": brut,
            "frais_r": round(brut - main["esperance_r"], 4) if brut is not None and main["esperance_r"] is not None
            else None,
            "par_rang": {k: a["rangs"][k].result() for _, _, k in (*RANK_BUCKETS, (0, 0, "rang inconnu"))
                         if k in a["rangs"]},
            "commission_ibkr_r": a["commission"].result()["esperance_r"] if market == "stock" else None,
            "esperance_ibkr_r": ibkr["esperance_r"] if ibkr else None,
            "ic95_ibkr": ibkr["ic95"] if ibkr else None,
            "esperance_ref_r": ref["esperance_r"],
            "par_frais": {k: a["frais"][k].result() for _, _, k in (*COST_BUCKETS, (0, 0, "frais inconnus"))
                          if k in a["frais"]},
            "par_funding": {k: a["funding"][k].result() for _, _, k in (*DER.FUNDING_BUCKETS, (0, 0, "inconnu"))
                            if k in a["funding"]} if market == "crypto" else None,
            "par_oi": {k: a["oi"][k].result() for _, _, k in (*DER.OI_BUCKETS, (0, 0, "inconnu"))
                       if k in a["oi"]} if market == "crypto" else None,
            "robustesse": {c: a["sorties"][c].result() for c in ("r_glisse_x2", "r_glisse_x3", "r_retard", "r_manque")},
            "ordres_limites_retires": a["manques"],
            "temoin": {"esperance_r": a["temoin"].result()["esperance_r"], "ecart_r": ecart["esperance_r"],
                       "ic95": ecart["ic95"], "z": ecart["z"], "n": ecart["n"]},
            "regimes": {k: v.result() for k, v in sorted(a["regimes"].items())},
            "echantillon_faible": n < MIN_N,
        }
        ci = ref["ic95"]
        item["candidate"] = bool(ci and ci[0] > 0 and (ecart["z"] or 0) >= z_min and n >= MIN_N)
        out.setdefault(market, {})[name] = item
    for market, items in out.items():
        out[market] = dict(sorted(items.items(), key=lambda kv: -(kv[1]["esperance_ref_r"]
                                                                   if kv[1]["esperance_ref_r"] is not None else -99)))
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
    out = {"declaration": {"numero": ST.DECLARATION, "le": ST.REGISTERED_ON, "essais": len(ST.trials()),
                           "z_candidate": candidate_z(), "sorties": ST.EXITS, "robustesse": ST.STRESS,
                           "doc": "docs/labo-strategies.md"},
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
    decl = rep["declaration"]
    lines = [f"🧪 **Stratégies du labo v2** · {decl['essais']} essais déclarés (déclaration n° "
             f"{decl.get('numero', 1)}) · espérance nette en R par trade [IC 95 %], actions après commission "
             f"IBKR estimée · ✅ = IC au-dessus de 0 et témoin battu à z ≥ "
             f"{str(decl.get('z_candidate', 2)).replace('.', ',')}"]
    for market, items in (rep.get("strategies") or {}).items():
        lines.append(f"**{MARKET_FR.get(market, market)}**")
        for name, it in items.items():
            ibkr = it.get("esperance_ibkr_r") is not None
            ci = it.get("ic95_ibkr") if ibkr else it.get("ic95")
            ci_txt = f" [{_r(ci[0])} ; {_r(ci[1])}]" if ci else ""
            wit = it.get("temoin") or {}
            flag = "✅ " if it.get("candidate") else ""
            comm = f" · commission {_r(-(it.get('commission_ibkr_r') or 0))} R" if ibkr else ""
            lines.append(f"{flag}{it['nom']} : {_r(it['esperance_ibkr_r'] if ibkr else it['esperance_r'])} R{ci_txt}"
                         f" · {it['declenches']} trades, "
                         f"{it['jours']} j · réussite {round(100 * (it['reussite'] or 0))} % · frais "
                         f"{_r(-(it.get('frais_r') or 0)) if it.get('frais_r') is not None else 'n/d'} R{comm} · "
                         f"vs témoin {_r(wit.get('ecart_r'))} R (z {wit.get('z') if wit.get('z') is not None else 'n/d'})"
                         + (" · échantillon faible" if it.get("echantillon_faible") else ""))
    diag = (rep.get("diagnostic_v1") or {}).get("stock")
    if diag:
        gaps = " · ".join(f"{k} {_r(v['esperance_r'])} ({v['n']})" for k, v in diag["par_gap"].items())
        lines.append(f"Diagnostic v1 actions : brut {_r(diag['brut']['esperance_r'])} R, net "
                     f"{_r(diag['net']['esperance_r'])} R · {gaps}")
    lines.append(f"🔒 {rep['scelle']['texte']}")
    return "\n".join(lines)
