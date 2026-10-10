"""Autopsie des trades du labo : OÙ un trade échoue, pas seulement combien il perd (sans réseau, sans jeton).

Demande de Simão (10 octobre 2026, fil « JARVIS s'améliore-t-il vraiment », choix « Tout le socle ») : le
council du soir ne voyait que des totaux par marché. Le meilleur point (MFE) et le pire point (MAE) de chaque
trade, en R du risque initial, étaient enregistrés par le simulateur (simulator.py) sans qu'aucune statistique
ne les lise.

Ce que l'autopsie dit, par marché et setup pour v1 (table trades, version de génération), et par stratégie
du labo v2 (table strat_trades) :
- élan : parmi les trades « tranchés » (MFE ≥ +1 R, ou stop d'origine touché avant), la part qui touche
  +1 R d'abord. Sans aucun avantage (marche aléatoire, sans frais), elle vaut 50 % : c'est la référence
  qui ne dépend d'aucun choix de notre part. Même chose à +0,5 R (référence 1/1,5 ≈ 66,7 %). Intervalle
  à 95 % en grappes par jour (stats.mean_ci), comme partout au labo ;
- stops d'origine : part des trades, puis parmi eux « jamais partis » (MFE < +0,1 R), « gain rendu »
  (MFE ≥ +1 R avant de revenir au stop) et le MFE médian ;
- gagnants : part de ceux qui ont frôlé le stop (MAE ≤ −0,8 R) ou en ont fait la moitié (MAE ≤ −0,5 R) ;
- sans TP1 : part de ceux qui ont fait au moins 75 % du chemin vers le TP1 (v1 : objectif du plan ;
  stratégies : 1 R ; GO confirmés : objectif inconnu de la ligne, mesure absente).

Limites, écrites dans le rapport : MFE et MAE sont mesurés jusqu'à la sortie seulement. L'autopsie ne dit
donc pas si le prix serait allé au TP1 APRÈS un stop (il faut des horizons fixes, piste M2 du labo, à
déclarer). Comparer les stops aux trades gardés jusqu'à la clôture serait un biais du survivant : ce n'est
pas fait ici. Les repères (« gain rendu », « stop dans le bruit »…) sont des pistes pour une déclaration,
jamais des règles : rien ici ne filtre un trade ni ne change un stop. Bloc scellé exclu.

Grade indicatif des GO confirmés (même demande, avis de GPT : trois populations séparées) : la note de la
politique que Simão trade (GO du scanner confirmés après l'ouverture), calculée par grade.evaluate sur les
lignes de la déclaration n° 7 (sortie déclarée r_sans_be), par trade exécuté et par GO de départ (0 R
quand aucun ordre ne part). Indicatif : il ne change ni le grade officiel ni le risque proposé.
"""
import sqlite3
from collections import defaultdict
from datetime import datetime

from app.lab import seal as SEAL
from app.lab import stats as STATS
from app.lab.db import LabDB, now_iso

CACHE_KEY = "autopsie"
MIN_N = 30
JAMAIS_PARTI_R = 0.1
FROLE_R = -0.8
MOITIE_R = -0.5
PRESQUE_TP1 = 0.75
REF_ELAN = {"1r": 0.5, "05r": 1 / 1.5}
# Repères pour signaler une piste (pas des règles) ; affichés avec le rapport.
REPERES = {"gain_rendu": 0.20, "stop_dans_le_bruit": 0.25, "objectif_loin": 0.25}
GO_INDICATIF = {"go_confirme": "GO confirmés, stop d'origine",
                "go_confirme_plancher": "GO du direct confirmés, stop plancher (ce que JARVIS t'envoie)"}
MARKET_FR = {"stock": "actions", "crypto": "crypto"}


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    k = len(s) // 2
    return s[k] if len(s) % 2 else (s[k - 1] + s[k]) / 2


class Prop:
    """Part d'un événement, avec intervalle à 95 % en grappes par jour."""

    def __init__(self):
        self.days: dict = {}
        self.n = 0
        self.k = 0

    def add(self, day: str, hit: bool) -> None:
        d = self.days.setdefault(day, [0.0, 0])
        d[0] += hit
        d[1] += 1
        self.n += 1
        self.k += hit

    def result(self, ref: float | None = None) -> dict:
        if not self.n:
            return {"n": 0, "part": None, "ic95": None, "jours": 0}
        p, se, ci = STATS.mean_ci(self.days, float(self.k), self.n)
        out = {"n": self.n, "part": round(p, 4), "ic95": ci, "jours": len(self.days)}
        if ref is not None:
            out["hasard"] = round(ref, 4)
            out["z"] = round((p - ref) / se, 2) if se else None
        return out


class Autopsy:
    """Accumulateur d'un groupe (marché × setup, ou stratégie)."""

    def __init__(self):
        self.n = 0
        self.r_sum = 0.0
        self.days: set = set()
        self.elan = {k: Prop() for k in REF_ELAN}
        self.stops = 0
        self.stop_mfe: list[float] = []
        self.winners = 0
        self.win_mae: list[float] = []
        self.no_tp1 = 0
        self.no_tp1_near = 0
        self.no_tp1_known = 0

    def add(self, day: str, r: float, exit_reason: str | None, mfe: float, mae: float, tp1_hit,
            tp1_r: float | None) -> None:
        self.n += 1
        self.r_sum += r
        self.days.add(day)
        stopped = exit_reason == "stop"
        for key, level in (("1r", 1.0), ("05r", 0.5)):
            if mfe >= level:
                self.elan[key].add(day, True)
            elif stopped:
                self.elan[key].add(day, False)
        if stopped:
            self.stops += 1
            self.stop_mfe.append(mfe)
        if r > 0:
            self.winners += 1
            self.win_mae.append(mae)
        if not tp1_hit:
            self.no_tp1 += 1
            if tp1_r and tp1_r > 0:
                self.no_tp1_known += 1
                self.no_tp1_near += mfe >= PRESQUE_TP1 * tp1_r

    def result(self) -> dict:
        if self.n < MIN_N:
            return {"trades": self.n, "echantillon_faible": True}
        sm, wm = self.stop_mfe, self.win_mae

        def share(xs, cond):
            return round(sum(1 for x in xs if cond(x)) / len(xs), 4) if xs else None

        out = {
            "trades": self.n, "jours": len(self.days), "esperance_r": round(self.r_sum / self.n, 4),
            "elan_1r": self.elan["1r"].result(REF_ELAN["1r"]),
            "elan_05r": self.elan["05r"].result(REF_ELAN["05r"]),
            "stops": {"part": round(self.stops / self.n, 4), "n": self.stops,
                      "jamais_partis": share(sm, lambda x: x < JAMAIS_PARTI_R),
                      "touche_05r": share(sm, lambda x: x >= 0.5),
                      "gain_rendu": share(sm, lambda x: x >= 1.0),
                      "mfe_median_r": round(_median(sm), 3) if sm else None},
            "gagnants": {"part": round(self.winners / self.n, 4), "n": self.winners,
                         "frole_stop": share(wm, lambda x: x <= FROLE_R),
                         "moitie_du_stop": share(wm, lambda x: x <= MOITIE_R),
                         "mae_median_r": round(_median(wm), 3) if wm else None},
            "sans_tp1": {"part": round(self.no_tp1 / self.n, 4), "n": self.no_tp1,
                         "presque_tp1": round(self.no_tp1_near / self.no_tp1_known, 4) if self.no_tp1_known else None},
        }
        out["pistes"] = pistes(out)
        return out


def pistes(m: dict) -> list[str]:
    """Pistes de déclaration en clair (jamais des règles), d'après les repères et l'élan contre le hasard."""
    out = []
    e = m["elan_1r"]
    if e.get("ic95") and e["n"] >= MIN_N:
        if e["ic95"][1] < e["hasard"]:
            out.append(f"entrée sans élan : +1 R avant le stop {e['part']:.0%} du temps, moins que le hasard (50 %)")
        elif e["ic95"][0] > e["hasard"]:
            out.append(f"élan réel : +1 R avant le stop {e['part']:.0%} du temps, mieux que le hasard (50 %)")
    s, g, t = m["stops"], m["gagnants"], m["sans_tp1"]
    if s["gain_rendu"] is not None and s["gain_rendu"] >= REPERES["gain_rendu"]:
        out.append(f"gain rendu : {s['gain_rendu']:.0%} des stops avaient touché +1 R (gestion de sortie à tester)")
    if g["frole_stop"] is not None and g["frole_stop"] >= REPERES["stop_dans_le_bruit"]:
        out.append(f"stop dans le bruit : {g['frole_stop']:.0%} des gagnants sont passés à −0,8 R (stop plus large à tester)")
    if t["presque_tp1"] is not None and t["presque_tp1"] >= REPERES["objectif_loin"]:
        out.append(f"objectif loin : {t['presque_tp1']:.0%} des trades sans TP1 ont fait 75 % du chemin (TP1 plus proche à tester)")
    if s["jamais_partis"] is not None and s["jamais_partis"] >= 0.5:
        out.append(f"entrée trop tôt ? {s['jamais_partis']:.0%} des stops ne sont jamais passés au-dessus de +0,1 R")
    return out


# --- Lecture de la base ------------------------------------------------------------------------

V1_QUERY = """
SELECT market, day, r_net, exit_reason, mfe_r, mae_r, tp1_hit, json_extract(context, '$.setup'),
       json_extract(plan, '$.targets[0].r_multiple')
FROM trades
WHERE triggered = 1 AND r_net IS NOT NULL AND mfe_r IS NOT NULL AND mae_r IS NOT NULL
  AND day NOT BETWEEN ? AND ?{version}
"""
STRAT_QUERY = """
SELECT market, strategy, day, r_net, exit_reason, mfe_r, mae_r, tp1_hit
FROM strat_trades
WHERE triggered = 1 AND r_net IS NOT NULL AND mfe_r IS NOT NULL AND mae_r IS NOT NULL
  AND day NOT BETWEEN ? AND ?
"""


def v1_report(conn: sqlite3.Connection, version: str | None) -> dict:
    groups: dict = defaultdict(Autopsy)
    params: list = [*SEAL.bounds()]
    vsql = ""
    if version:
        vsql = " AND method_version = ?"
        params.append(version)
    for market, day, r, why, mfe, mae, tp1, setup, tp1_r in conn.execute(V1_QUERY.format(version=vsql), params):
        groups[(market, "ensemble")].add(day, r, why, mfe, mae, tp1, tp1_r)
        groups[(market, setup or "inconnu")].add(day, r, why, mfe, mae, tp1, tp1_r)
    out: dict = {}
    for (market, key), acc in sorted(groups.items(), key=lambda kv: -kv[1].n):
        out.setdefault(market, {})[key] = acc.result()
    return out


def strat_report(conn: sqlite3.Connection) -> dict:
    groups: dict = defaultdict(Autopsy)
    for market, strat, day, r, why, mfe, mae, tp1 in conn.execute(STRAT_QUERY, SEAL.bounds()):
        tp1_r = None if strat.startswith("go_") else 1.0  # GO : objectif du plan v1, absent de la ligne
        groups[(market, strat)].add(day, r, why, mfe, mae, tp1, tp1_r)
    out: dict = {}
    for (market, strat), acc in sorted(groups.items(), key=lambda kv: -kv[1].n):
        out.setdefault(market, {})[strat] = acc.result()
    return out


def go_indicatif(conn: sqlite3.Connection) -> dict:
    """Grade indicatif de la politique confirmée : par trade exécuté (grade.evaluate, FENETRE_JOURS derniers
    jours) et par GO de départ (0 R sans ordre), sur toute la période hors scellé."""
    from app.lab import grade as GRADE
    from app.lab import strategies as ST

    out = {}
    lo, hi = SEAL.bounds()
    for strat, label in GO_INDICATIF.items():
        col = ST.ref_exit(strat)
        days = [d for (d,) in conn.execute(
            f"SELECT DISTINCT day FROM strat_trades WHERE market = 'stock' AND strategy = ? AND triggered = 1 "
            f"AND {col} IS NOT NULL AND day NOT BETWEEN ? AND ? ORDER BY day DESC LIMIT ?",
            (strat, lo, hi, GRADE.FENETRE_JOURS))]
        cutoff = days[-1] if days else "9999"
        rows = [(i, day, r, tp1, None, None, None, None, None) for i, day, r, tp1 in conn.execute(
            f"SELECT id, day, {col}, tp1_hit FROM strat_trades WHERE market = 'stock' AND strategy = ? "
            f"AND triggered = 1 AND {col} IS NOT NULL AND day >= ? AND day NOT BETWEEN ? AND ? "
            f"ORDER BY COALESCE(filled_at, signal_at), id", (strat, cutoff, lo, hi))]
        m = GRADE.evaluate(rows)
        day_sums: dict = {}
        n = 0
        total = 0.0
        for day, r in conn.execute(
                f"SELECT day, CASE WHEN triggered = 1 THEN COALESCE({col}, 0) ELSE 0 END FROM strat_trades "
                f"WHERE market = 'stock' AND strategy = ? AND day NOT BETWEEN ? AND ?", (strat, lo, hi)):
            d = day_sums.setdefault(day, [0.0, 0])
            d[0] += r
            d[1] += 1
            n += 1
            total += r
        mean, _, ci = STATS.mean_ci(day_sums, total, n)
        out[strat] = {"nom": label, "sortie": col,
                      "par_trade": {k: m.get(k) for k in ("grade", "trades", "jours", "esperance_r", "ic95",
                                                          "moities_r", "serie_pertes", "serie_attendue",
                                                          "reussite", "tp1", "raison")},
                      "par_go": {"go": n, "jours": len(day_sums),
                                 "esperance_r": round(mean, 4) if mean is not None else None, "ic95": ci}}
    return out


def report(conn: sqlite3.Connection) -> dict:
    from app.lab import validator as V

    version = V.current_version(conn)
    return {"version": version, "v1": v1_report(conn, version), "strategies": strat_report(conn),
            "grade_indicatif": go_indicatif(conn),
            "references": {"elan_1r": "50 % : part sans avantage (marche aléatoire) de toucher +1 R avant −1 R",
                           "elan_05r": "66,7 % : même chose pour +0,5 R",
                           "reperes": REPERES},
            "limites": ["MFE et MAE sont mesurés jusqu'à la sortie : rien ici ne dit où serait allé le prix après "
                        "un stop (horizons fixes, piste M2, à déclarer).",
                        "Les pistes sont des hypothèses à pré-enregistrer, jamais des règles : choisir après coup "
                        "le stop ou l'objectif « optimal » vu ici serait du surapprentissage.",
                        "Bloc scellé exclu ; MFE/MAE bruts (avant frais), en R du risque initial."]}


def signature(conn: sqlite3.Connection) -> list:
    a = conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM trades").fetchone()
    b = conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM strat_trades").fetchone()
    return [*a, *b]


def cached(conn: sqlite3.Connection) -> dict | None:
    return STATS.cached(conn, CACHE_KEY)


def step(db: LabDB, now: datetime, max_age: float) -> int | None:
    """Passage du worker : recalcule quand les trades ont changé et que le cache a plus de `max_age` s."""
    sig = signature(db.conn)
    at = db.get("autopsie", "at", 0) or 0
    if sig == db.get("autopsie", "signature") or (cached(db.conn) and now.timestamp() - at < max_age):
        return None
    data = report(db.conn)
    with db.transaction():
        STATS.store(db.conn, CACHE_KEY, data, sig[0], now_iso())
        db.set("autopsie", "signature", sig)
        db.set("autopsie", "at", now.timestamp())
    return sig[0]


# --- Résumés (brief du soir, Discord) ---------------------------------------------------------

def _pct(x) -> str:
    return "n/d" if x is None else f"{x:.0%}"


def _elan(e: dict | None) -> str:
    if not e or e.get("part") is None:
        return "élan n/d"
    ci = e.get("ic95") or [None, None]
    rng = f" [{ci[0]:.0%} ; {ci[1]:.0%}]" if ci[0] is not None else ""
    return f"+1 R avant stop {e['part']:.0%}{rng} (hasard 50 %)"


def compact(data: dict | None, per_market: int = 6) -> dict | None:
    """Pour le brief du soir : l'essentiel par groupe, les groupes les plus fournis d'abord."""
    if not data:
        return None

    def slim(m: dict) -> dict:
        if m.get("echantillon_faible"):
            return {"trades": m["trades"], "echantillon_faible": True}
        e = m["elan_1r"]
        return {"trades": m["trades"], "jours": m["jours"], "esperance_r": m["esperance_r"],
                "elan_1r": {k: e.get(k) for k in ("part", "ic95", "z")},
                "stops": {k: m["stops"][k] for k in ("part", "jamais_partis", "gain_rendu", "mfe_median_r")},
                "gagnants_frole_stop": m["gagnants"]["frole_stop"], "presque_tp1": m["sans_tp1"]["presque_tp1"],
                "pistes": m["pistes"]}

    return {"v1": {mk: {k: slim(v) for k, v in list(groups.items())[:per_market]}
                   for mk, groups in (data.get("v1") or {}).items()},
            "strategies": {mk: {k: slim(v) for k, v in groups.items() if not v.get("echantillon_faible")}
                           for mk, groups in (data.get("strategies") or {}).items()},
            "grade_indicatif": data.get("grade_indicatif"), "limites": data.get("limites"),
            "calcule_le": data.get("calcule_le")}


def indicatif_lines(gi: dict | None) -> list[str]:
    lines = []
    for res in (gi or {}).values():
        t, g = res["par_trade"], res["par_go"]
        if t.get("esperance_r") is None:
            lines.append(f"-# {res['nom']} : non noté ({t.get('raison') or 'pas encore de trades'})")
            continue
        ci = t.get("ic95") or [None, None]
        per_go = f"{g['esperance_r']:+.3f} R par GO de départ" if g.get("esperance_r") is not None else "n/d par GO"
        lines.append(f"-# {res['nom']} : **{t['grade']}** indicatif · {t['esperance_r']:+.3f} R par trade "
                     f"(IC95 {ci[0]:+.3f} ; {ci[1]:+.3f}) sur {t['trades']} trades / {t['jours']} jours · {per_go}")
    return lines


def text(data: dict | None, market: str | None = None) -> str:
    """`!autopsie` (option `crypto` ou `actions`) : où les trades échouent, contre le hasard."""
    if not data:
        return ("🩺 Autopsie pas encore calculée (le worker du labo la calcule à son prochain passage, "
                "après le déploiement).")
    markets = [market] if market else ["stock", "crypto"]
    lines = ["🩺 **Autopsie des trades du labo** (où ils échouent ; pistes à déclarer, jamais des règles)"]
    for mk in markets:
        groups = (data.get("v1") or {}).get(mk) or {}
        if not groups:
            continue
        lines.append(f"\n**v1 {MARKET_FR[mk]}**")
        for key, m in list(groups.items())[:5]:
            if m.get("echantillon_faible"):
                continue
            s, g = m["stops"], m["gagnants"]
            lines.append(f"**{key}** · {m['trades']} trades, {m['esperance_r']:+.3f} R · {_elan(m['elan_1r'])}")
            lines.append(f"-# stops {_pct(s['part'])} (jamais partis {_pct(s['jamais_partis'])}, gain rendu "
                         f"{_pct(s['gain_rendu'])}) · gagnants qui frôlent le stop {_pct(g['frole_stop'])}")
            for p in m["pistes"][:2]:
                lines.append(f"-# 💡 {p}")
        strats = (data.get("strategies") or {}).get(mk) or {}
        best = [(k, v) for k, v in strats.items() if not v.get("echantillon_faible")]
        if best:
            lines.append(f"**Stratégies {MARKET_FR[mk]}** (élan contre le hasard)")
            for key, m in sorted(best, key=lambda kv: -(kv[1]["elan_1r"].get("part") or 0))[:6]:
                lines.append(f"-# {key} · {m['trades']} trades · {_elan(m['elan_1r'])}")
    gi = indicatif_lines(data.get("grade_indicatif"))
    if gi:
        lines.append("\n**Grade indicatif des GO confirmés** (ne change ni le grade ni le risque)")
        lines += gi
    lines.append("-# MFE/MAE mesurés jusqu'à la sortie : l'autopsie ne dit pas où serait allé le prix après un stop.")
    return "\n".join(lines)
