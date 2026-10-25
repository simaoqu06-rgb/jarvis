"""Export des trades du labo pour un recalcul externe (3e tour des avis de GPT et Gemini, 25 octobre 2026).

GPT : « les contrôles annoncés restent des contrôles rapportés par Claude ». Pour qu'un tiers recalcule lui-même les
moyennes, les IC en grappes par jour et l'écart au témoin, l'export donne une ligne par trade simulé de strat_trades
(signal déclenché ou non), hors bloc scellé, et pendant un rattrapage seulement les lignes de la déclaration en vigueur
(comme le tableau, strat_stats.declaration_view) :
- le CSV (COLUMNS, une définition par colonne dans DEFINITIONS) ;
- un manifeste : déclaration, essais, bloc scellé, empreintes du code et du modèle de coût, paramètres de commission,
  SHA-256 du CSV, recette d'agrégation et les moyennes que le tableau en tire (`controle`), pour vérifier qu'on retrouve
  les mêmes chiffres.
Lecture seule, aucune donnée personnelle : des actions publiques, des dates et des R simulés.

5e tour (audits de l'export par GPT et Gemini, 25/10/2026) : le manifeste donnait l'empreinte du coût de v1 au lieu de
celle des lignes du labo v2 (corrigé : `cout_rev_actuel` = empreinte.strat_cost_rev) et l'empreinte du code au moment
de l'export sans dire que chaque ligne garde la sienne (`revisions_lignes`) ; « z » devient « t » (le jugement compare à
un seuil de Student) ; le contrôle est donné aussi par origine (rejeu, direct) et avec la variance sous la nulle
imposée ; colonnes `temoin_absent` (motif) et `frais_3s_moins_1s_deg_r` (décomposition sortie / frais de la sortie
unique).
"""
import csv
import hashlib
import io
import json
import math
import sqlite3
from datetime import datetime, timezone

from app.lab import council as CNC
from app.lab import empreinte as E
from app.lab import seal as SEAL
from app.lab import strat_stats as SS
from app.lab import strategies as ST

GO_STRATEGIES = ("go_brut", "go_confirme", "go_confirme_v2")
COLUMNS = ("origin", "market", "symbol", "day", "strategy", "slot", "signal_at_utc", "filled_at_utc", "entry", "stop",
           "risk_pct", "triggered", "exit_reason", "rank", "gap_open_pct", "atr_pct", "r_gross", "r_net", "sortie_ref",
           "r_ref", "commission_ibkr_fixe_r", "r_ref_ibkr", "r_20e_fixe_3s", "r_20e_deg_3s", "r_20e_fixe_1s",
           "r_20e_deg_1s", "temoin_r", "temoin_triggered", "temoin_journee_r", "temoin_journee_triggered",
           "temoin_tirages", "temoin_absent", "frais_3s_moins_1s_deg_r", "regime", "declaration", "code_rev",
           "cout_rev")
DEFINITIONS = {
    "origin": "replay (rejeu historique) ou live (direct simulé)",
    "day": "séance (New York) du signal",
    "slot": "numéro du signal de la stratégie sur cette valeur ce jour-là",
    "signal_at_utc / filled_at_utc": "heure du signal et de l'exécution simulée (bougies de 5 min)",
    "entry / stop / risk_pct": "prix d'entrée prévu, stop, distance du stop en % de l'entrée",
    "triggered": "1 si l'ordre a été exécuté ; les lignes à 0 sont des signaux sans exécution",
    "rank": "rang de volume en dollars de la veille dans l'univers (1 = le plus échangé)",
    "gap_open_pct / atr_pct": "écart d'ouverture du jour et ATR journalier, en %",
    "r_gross / r_net": "R de la sortie v1 avant et après le glissement et les frais du simulateur",
    "sortie_ref / r_ref": "sortie de référence de la stratégie (strategies.ref_exit) et son R après frais du simulateur",
    "commission_ibkr_fixe_r": "2 × 0,005 $ par action (plafond 1 % du prix) rapportés au risque par action",
    "r_ref_ibkr": "r_ref − commission_ibkr_fixe_r : la mesure du classement (espérance « ibkr »)",
    "r_20e_*": "R à 20 € de risque, brackets IBKR avec minimum par ordre : barème Fixe ou Dégressif, 3 sorties (3s) ou "
               "une seule sur TP2 (1s)",
    "temoin_r": "témoin aléatoire apparié : même action, même jour, même stop en %, mêmes objectifs en R, mêmes coûts du "
                "simulateur, entrée tirée 5 à 30 min après la bougie d'exécution (actions, déclaration n° 10) ; moyenne "
                "des tirages",
    "temoin_journee_r": "ancien témoin, entrée tirée sur toute la séance (gardé pour mesurer l'effet de l'heure)",
    "temoin_tirages": "R de chaque tirage du témoin (JSON)",
    "temoin_absent": "« aucun tirage exécuté » : trade exécuté sans témoin. Presque toujours un GO acheté à l'ouverture "
                     "déjà au-dessus d'un objectif du plan de 9 h 10 : cet objectif est à 0 R ou moins depuis le prix "
                     "d'achat, aucun témoin n'est tiré (strategies.witness) et le simulateur vend au prix payé ; "
                     "sinon fenêtre de tirage vide (fin de séance)",
    "frais_3s_moins_1s_deg_r": "commissions au Dégressif d'un bracket par objectif moins celles d'un seul bracket, en R, "
                               "comptées au prix d'entrée comme r_20e_deg_1s : (r_20e_deg_1s − r_20e_deg_3s) − cette "
                               "colonne = l'effet de la sortie elle-même",
    "regime": "régime de marché du jour (table regimes)",
    "declaration / code_rev / cout_rev": "déclaration du labo, empreinte du code et du modèle de coût qui ont produit "
                                         "la ligne",
}
RECETTE = ("Sur les lignes triggered = 1 d'une stratégie : espérance = moyenne m de r_ref_ibkr ; écart au témoin = "
           "moyenne de r_ref − temoin_r sur les lignes avec temoin_triggered = 1. Erreur type en grappes par day "
           "(stats.cluster_se) : u_g = somme des R du jour g − m × nombre de trades du jour g, se = √(Σ u_g² × G / "
           "(G − 1)) / n, G = nombre de jours ; IC 95 % = m ± t(0,975 ; G − 1) × se ; t = m / se, jugé contre un seuil "
           "de Student à G − 1 degrés de liberté corrigé de Bonferroni sur les essais déclarés (strat_stats.candidate_t). "
           "t_nulle_imposee : même calcul avec u_g = somme des R du jour g (variance sous m = 0), pour information.")


def _utc(ts) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="minutes") if ts else ""


def _num(x) -> str:
    return "" if x is None else repr(x) if isinstance(x, float) else str(x)


def rows(conn: sqlite3.Connection, strategies: tuple[str, ...]) -> list[dict]:
    """Lignes de strat_trades des `strategies`, triées par jour, valeur et stratégie."""
    marks = ", ".join("?" * len(strategies))
    sql = ("SELECT s.origin, s.market, s.symbol, s.day, s.strategy, s.slot, s.signal_at, s.filled_at, s.entry, "
           "s.stop, s.risk_pct, s.triggered, s.exit_reason, s.rank, s.gap_open_pct, s.atr_pct, s.r_gross, s.r_net, "
           "s.r_sans_be, s.r_2r, s.r_suiveur, s.r_20e, s.r_20e_deg, s.r_20e_1s, s.temoin_r, s.temoin_triggered, "
           "s.temoin_journee_r, s.temoin_journee_triggered, s.temoin_tirages, r.label, s.declaration, s.code_rev, "
           "s.cout_rev FROM strat_trades s LEFT JOIN regimes r ON r.market = s.market AND r.day = s.day "
           f"WHERE s.strategy IN ({marks}) ORDER BY s.day, s.symbol, s.strategy, s.slot")
    out = []
    for row in conn.execute(sql, strategies):
        (origin, market, symbol, day, name, slot, signal_at, filled_at, entry, stop, risk_pct, trig, why, rank, gap,
         atr, gross, net, sans_be, r2, suiveur, r20, r20_deg, r20_1s, wit, wit_trig, day_wit, day_wit_trig, draws,
         label, decl, code, cost) = row
        refc = ST.ref_exit(name)
        ref = {"r_net": net, "r_sans_be": sans_be, "r_2r": r2, "r_suiveur": suiveur}.get(refc)
        stock = market == "stock"
        comm = (SS.ibkr_commission_r(entry, stop) or 0.0) if stock else 0.0
        out.append({
            "origin": origin, "market": market, "symbol": symbol, "day": day, "strategy": name, "slot": slot,
            "signal_at_utc": _utc(signal_at), "filled_at_utc": _utc(filled_at), "entry": entry, "stop": stop,
            "risk_pct": risk_pct, "triggered": trig, "exit_reason": why or "", "rank": rank, "gap_open_pct": gap,
            "atr_pct": atr, "r_gross": gross, "r_net": net, "sortie_ref": refc, "r_ref": ref,
            "commission_ibkr_fixe_r": comm if stock else None,
            "r_ref_ibkr": ref - comm if ref is not None else None, "r_20e_fixe_3s": r20, "r_20e_deg_3s": r20_deg,
            "r_20e_fixe_1s": r20_1s,
            "r_20e_deg_1s": SS.one_bracket_degressif(entry, stop, r20_1s) if stock else None,
            "temoin_r": wit, "temoin_triggered": wit_trig, "temoin_journee_r": day_wit,
            "temoin_journee_triggered": day_wit_trig, "temoin_tirages": draws or "",
            "temoin_absent": "" if not trig or wit_trig else "aucun tirage exécuté",
            "frais_3s_moins_1s_deg_r": _fees_split(entry, stop) if stock and trig else None, "regime": label or "",
            "declaration": decl, "code_rev": code or "", "cout_rev": cost or ""})
    return out


def _null_t(mean: SS.Mean) -> float | None:
    """t sous la nulle imposée (m = 0) : u_g = somme des R du jour g, sans retirer m × n_g."""
    sums = [s for s, _ in mean.clusters().values()]
    n, g = mean.n, len(sums)
    if n == 0 or g < 2:
        return None
    se = math.sqrt(sum(v * v for v in sums) * g / (g - 1)) / n
    return round(mean.total / n / se, 2) if se else None


def _result(m: SS.Mean) -> dict:
    r = m.result()
    return {"esperance_r": r["esperance_r"], "ic95": r["ic95"], "t": r["z"], "t_nulle_imposee": _null_t(m),
            "n": r["n"], "jours": r["jours"]}


def controle(lines: list[dict]) -> dict:
    """Les chiffres que le tableau tire de ces lignes (RECETTE), par stratégie, puis par origine (rejeu, direct) : à
    retrouver en recalculant."""
    acc: dict = {}
    for x in lines:
        if not x["triggered"]:
            continue
        for key in ("toutes", x["origin"]):
            a = acc.setdefault(x["strategy"], {}).setdefault(key, {"esperance_ibkr": SS.Mean(),
                                                                   "ecart_temoin": SS.Mean()})
            a["esperance_ibkr"].add(x["day"], x["r_ref_ibkr"])
            if x["temoin_triggered"] and x["temoin_r"] is not None and x["r_ref"] is not None:
                a["ecart_temoin"].add(x["day"], x["r_ref"] - x["temoin_r"])
    return {name: {k: v for k, v in {**{m: _result(acc_m) for m, acc_m in by["toutes"].items()},
                                     "par_origine": {o: {m: _result(acc_m) for m, acc_m in v.items()}
                                                     for o, v in by.items() if o != "toutes"}}.items()}
            for name, by in acc.items()}


def _fees_split(entry: float | None, stop: float | None) -> float | None:
    """Commissions au Dégressif de trois brackets moins celles d'un seul, en R, au prix d'entrée (comme
    strat_stats.one_bracket_degressif)."""
    if entry is None or stop is None:
        return None
    from app.lab import simulator as SIM
    from app.trading import killmode as K  # import tardif : killmode importe le labo

    entry, stop = SIM.round_stock(entry), SIM.round_stock(stop)
    risk = entry - stop
    if risk <= 0:
        return None
    q = K.shares(risk, K.EURUSD_DEFAULT)
    plan = K.IBKR_PLANS["degressif"]
    three = sum(2 * K.order_commission(n, entry, plan) for n in K._brackets(q, 3))
    return round((three - 2 * K.order_commission(q, entry, plan)) / (q * risk), 4)


def _revisions(lines: list[dict]) -> list[dict]:
    seen: dict = {}
    for x in lines:
        key = (x["code_rev"] or "inconnue", x["cout_rev"] or "inconnue")
        d = seen.setdefault(key, {"code_rev": key[0], "cout_rev": key[1], "lignes": 0, "du": x["day"], "au": x["day"]})
        d["lignes"] += 1
        d["du"], d["au"] = min(d["du"], x["day"]), max(d["au"], x["day"])
    return list(seen.values())


def export(conn: sqlite3.Connection, strategies: tuple[str, ...] = GO_STRATEGIES) -> dict:
    """{"manifest", "csv"} : le CSV des lignes et son manifeste (empreinte SHA-256 du CSV comprise)."""
    ST.register_council(CNC.load(conn))  # essais déclarés : les déclarations du council comptent
    with SS.declaration_view(conn, SS.partial(conn) or None):
        lines = rows(conn, strategies)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    for x in lines:
        writer.writerow({k: _num(v) for k, v in x.items()})
    text = buf.getvalue()
    manifest = {
        "genere_le": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "strategies": list(strategies), "lignes": len(lines),
        "declenchees": sum(1 for x in lines if x["triggered"]),
        "jours": len({x["day"] for x in lines}),
        "sha256_csv": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "declaration": ST.LATEST_DECLARATION, "essais_declares": len(ST.trials()),
        "bloc_scelle": SEAL.describe(),
        # Empreintes : celle du code et du coût au moment de l'export ; chaque ligne garde celles qui l'ont produite
        # (hachage du texte source des fonctions : une retouche sans effet la change aussi).
        "code_rev_actuel": E.code_rev("strat"), "cout_rev_actuel": E.strat_cost_rev("stock"),
        "revisions_lignes": _revisions(lines),
        "commission_ibkr_fixe": {"par_action_usd": SS.IBKR_PER_SHARE, "plafond_pct_prix": SS.IBKR_MAX_PCT * 100},
        "colonnes": DEFINITIONS, "recette": RECETTE, "controle": controle(lines),
    }
    return {"manifest": json.loads(json.dumps(manifest, default=str)), "csv": text}
