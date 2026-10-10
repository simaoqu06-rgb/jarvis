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

Déclaration n° 6 (règles de sortie de Gemini, actions) : chaque sortie ajoutée est comparée à la sortie de
référence sur les mêmes trades (écart apparié, z en grappes par jour) ; elle « aide » si l'écart est positif
avec z ≥ candidate_z() et au moins MIN_N trades. Le filtre d'entrée VWAP de GPT n'est qu'une mesure :
espérance de référence par tranche (décision avant 10 h 30, après au-dessus du VWAP, après en dessous).
La n° 6 complète aussi la règle « candidate » comme le plan l'exigeait (plan-labo-v2.md, lot 1, point 7) :
la référence doit rester positive sous chaque test de robustesse (glissement ×2 et ×3, retard, ordres limites
manqués, nets de la commission IBKR) et sur chaque moitié chronologique des jours. Killmode et la n° 4
(strategies.POST_SEEN) sont jugées sur les jours après strategies.SEEN_UNTIL (« jours_suivants »). Mesure de
Killmode, sans essai : espérance des stratégies v2 actions par tranche de gap, d'ATR, de volume des 15 premières
minutes, de place pour 2 R et de stop touché avant l'entrée (TRANCHES, bornes figées) ; trois comparaisons qui
séparent sa sélection, ses conditions de prix et sa limite de 11 h (_kill_compare) ; part des actions en gap
≥ 4 % et ATR ≥ 4 % présentes dans l'univers du labo (universe.killmode_coverage).

Déclaration n° 7 (confirmation des GO du scanner, PR #37) : les bras go_* sont jugés sur leur seule sortie déclarée
(strategies.ref_exit : r_sans_be, stop fixe), comme les autres stratégies (IC, témoin, robustesse). Par stop (origine,
plancher du direct), GO par GO (_go_compare) : statut de la confirmation (confirmé, annulé, expiré) avec ce qu'aurait
fait le brut dans chaque groupe, et écart « confirmé ou rien » − brut (0 pour un bras sans trade) en grappes par jour ;
la confirmation « aide » si cet écart est positif avec z ≥ candidate_z() et au moins MIN_N GO. Le taux de réussite
est affiché, jamais utilisé pour conclure. Les mesures groupées de la n° 6 (sorties de Gemini, VWAP, tranches de
Killmode) restent sur les stratégies d'avant la n° 7.

Déclaration n° 8 (suites des avis de GPT et Gemini sur Killmode, et trade FRSH) :
- D4 : chaque jugement (« candidate », « aide ») compare m / se en grappes au seuil candidate_t(G, essais), le
  quantile de Student à G − 1 degrés de liberté (G = jours) corrigé de Bonferroni, et non plus à candidate_z() ;
  les IC 95 % sont en t eux aussi. Sur peu de jours, le seuil monte (148 essais : 3,81 à 30 jours, 3,63 à 50,
  3,51 à 100, contre 3,40 en loi normale).
- D2 : en actions, la robustesse exigée pour « candidate » comprend le stress de 3 cents de Gemini (r_3c,
  commission IBKR comprise : ROBUST_STOCK) ; l'espérance à la mise de Simão (r_20e : 20 €, bracket IBKR par
  objectif, minimum par ordre) est affichée et dit « rentable à 20 € », sans décider de « candidate ».
- Variantes FRSH du bras confirmé (stop d'origine) : pour chaque filtre, écart « filtré ou rien » − « confirmé
  ou rien » et − brut, GO par GO (_filter_compare) ; mesure sans essai, brut et confirmé selon que des résultats
  ont été publiés depuis la veille (archive du calendrier, _results_compare).
- D1 : Killmode complet (kill_complet.report) : KILL, témoins propres et écart apparié par jour.

Diagnostic v1 (Lot 1 du plan) : espérance par gap d'ouverture, par mode d'entrée, par raison de sortie,
et brut contre net de frais, pour vérifier « v1 achète la faiblesse » et « les frais expliquent la perte ».

Déclaration n° 10 (mesures corrigées, aucun essai de plus) :
- « candidate » garde sa définition (avantage de la règle, D2) ; `candidate_compte` exige en plus, en actions, une
  espérance positive à la mise de Simão (r_20e, ajouté à « robuste » sans entrer dans « tient ») : ✅ seulement
  alors, sinon « ☑️ avantage, perd à 20 € ».
- Crypto : grappes par semaine ISO (Mean `key`, les positions de 24 h se chevauchent d'un jour à l'autre ; G =
  semaines dans candidate_t), le se naïf par jour publié à côté avec le facteur d'inflation. Actions : la même
  lecture en grappes par semaine, affichée (« semaines »), sans effet sur le verdict.
- Témoin des actions à la même heure ; effet de l'heure (écart au témoin de la journée − écart au témoin de la
  même heure) et dispersion entre tirages (écart-type des moyennes par tirage) : descriptifs.
- se de l'espérance exposé (Mean.result, se_ibkr de _item) pour l'écart labo/direct de Killmode.
- Entrée de v1 (entree_v1_appariee) : « limite ou rien » contre l'achat à l'ouverture, sur les GO (go_brut ramené
  au risque prévu) et sur tous les plans (r_ouverture) ; descriptif.
"""
import json
import math
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime
from statistics import NormalDist

from app.lab import archive as ARC
from app.lab import collector as CO
from app.lab import derivs as DER
from app.lab import seal as SEAL
from app.lab import simulator as SIM
from app.lab import stats as STATS
from app.lab import strategies as ST
from app.lab import universe as U

MIN_N = 30
ALPHA = 0.05
COLUMNS = ("r_net", "r_sans_be", "r_2r", "r_suiveur", "r_emperor", "r_emperor_alerte", "r_emperor_confirme",
           "r_vwap", "r_1430", "r_spy", "r_glisse_x2", "r_glisse_x3", "r_retard", "r_manque", "r_gross",
           "r_3c", "r_20e", "r_be_tp2", "r_20e_deg", "r_20e_1s", "r_20e_2s")  # n° 10 : descriptives
RANK_BUCKETS = ((1, 50, "rang 1-50"), (51, 150, "rang 51-150"), (151, 10**9, "rang 151+"))
IBKR_PER_SHARE = 0.005      # IBKR Pro, plan Fixe (/mnt/project-files/trading/plateforme-actions-us.md)
IBKR_MAX_PCT = 0.01         # plafond : 1 % de la valeur de l'ordre
COST_BUCKETS = ((0.0, 0.1, "frais < 0,1 R"), (0.1, 0.2, "frais 0,1-0,2 R"), (0.2, 0.4, "frais 0,2-0,4 R"),
                (0.4, 1e9, "frais ≥ 0,4 R"))
VWAP_BUCKETS = ("avant 10 h 30", "après 10 h 30, au-dessus du VWAP", "après 10 h 30, sous le VWAP")
# n° 6 : la référence doit rester positive sous ces conditions (et sur chaque moitié des jours) pour qu'une
# stratégie soit « candidate » (plan-labo-v2.md, lot 1, point 7 : ×2 et ×3, retard, ordres limites manqués).
ROBUST = ("r_glisse_x2", "r_glisse_x3", "r_retard", "r_manque")
# n° 8, D2 : en actions, la référence doit aussi tenir sous le stress de 3 cents de Gemini (r_3c, commission
# IBKR déjà comptée dedans) ; r_20e (à la mise de Simão) est affiché, il ne décide pas de « candidate ».
ROBUST_STOCK = (*ROBUST, "r_3c")
WITH_COMMISSION = ("r_3c", "r_20e")  # colonnes qui comptent déjà la commission IBKR
# n° 10 : la politique que mesure le tableau, en une ligne (X1, point 4).
POLICY = ("entrée au signal (ordre stop ou limite ; v1 limitée au dernier cours connu ; GO brut au marché à "
          "l'ouverture, GO confirmé par ordre stop au déclencheur jusqu'à 11 h 30 New York) · stop à l'entrée après "
          "TP1 (stratégies et v1) ou stop fixe (GO, r_sans_be) ; ta règle, stop à l'entrée après TP2, en colonne "
          "r_be_tp2 · sortie au temps : clôture de la séance (actions), 24 h (crypto) · taille : en R du risque prévu, "
          "et à 20 € de risque chez IBKR (« à 20 € »)")
OPEN_BUCKETS = ((-1e9, 0.0, "ouverture ≤ entrée"), (0.0, 0.01, "ouverture +0 à +1 %"),
                (0.01, 0.03, "ouverture +1 à +3 %"), (0.03, 1e9, "ouverture > +3 %"))  # n° 10, bornes hautes comprises
# n° 6, mesure de Killmode : tranches figées avant tout résultat (bornes basses comprises, hautes exclues).
TRANCHES = {
    "gap": ((-1e9, -5.0, "gap < −5 %"), (-5.0, -2.0, "gap −5 à −2 %"), (-2.0, -0.5, "gap −2 à −0,5 %"),
            (-0.5, 0.5, "gap ±0,5 %"), (0.5, 2.0, "gap +0,5 à +2 %"), (2.0, 4.0, "gap +2 à +4 %"),
            (4.0, 5.0, "gap +4 à +5 %"), (5.0, 10.0, "gap +5 à +10 %"), (10.0, 1e9, "gap ≥ +10 %")),
    "atr": ((-1e9, 2.0, "ATR < 2 %"), (2.0, 4.0, "ATR 2-4 %"), (4.0, 6.0, "ATR 4-6 %"), (6.0, 10.0, "ATR 6-10 %"),
            (10.0, 1e9, "ATR ≥ 10 %")),
    "vol15": ((-1e9, 0.25, "vol15 < 0,25"), (0.25, 0.5, "vol15 0,25-0,5"), (0.5, 1.0, "vol15 0,5-1"),
              (1.0, 1e9, "vol15 ≥ 1")),
    "place_2r": ((-1e9, 0.5, "2 R < 0,5 ATR"), (0.5, 1.0, "2 R entre 0,5 et 1 ATR"), (1.0, 1e9, "2 R ≥ 1 ATR")),
    "stop_avant_entree": ((0, 1, "stop intact avant l'entrée"), (1, 2, "stop touché avant l'entrée")),
}
GO_STATUS_FR = {"confirme": "confirmé", "annule": "annulé", "expire": "expiré"}  # n° 7
GO_FILTER_STATUS_FR = {"filtre": "refusé par le filtre", "indetermine": "filtre non jugeable"}  # n° 8
GO_FILTERS_FR = {"v2": "V2, VWAP qui monte", "v3": "V3, cassure du plus haut de séance", "v4": "V4, plus fort que SPY"}
RESULTS_FR = {True: "résultats publiés depuis la veille", False: "sans résultats publiés", None: "calendrier non archivé"}
GO_VARIANTS_FR = {"origine": "stop d'origine", "plancher": "stop plancher du direct"}
_vwap_from: dict[str, int] = {}


def _vwap_bucket(day: str, signal_at: int | None, above: int | None) -> str | None:
    """n° 6 : tranche horaire et VWAP de la décision (actions) ; None si on ne sait pas."""
    if signal_at is None:
        return None
    if day not in _vwap_from:
        _vwap_from[day] = int(datetime.combine(date.fromisoformat(day), ST.VWAP_EXIT_FROM, CO.NEW_YORK).timestamp())
    if signal_at < _vwap_from[day]:
        return VWAP_BUCKETS[0]
    return None if above is None else VWAP_BUCKETS[1] if above else VWAP_BUCKETS[2]


def candidate_z(trials: int | None = None) -> float:
    """Seuil de z contre le témoin, corrigé du nombre d'essais déclarés (Bonferroni, unilatéral) : la limite
    du seuil en t quand les jours sont très nombreux (affichage)."""
    n = trials or len(ST.trials())
    return round(NormalDist().inv_cdf(1 - ALPHA / n), 2)


def candidate_t(days: int, trials: int | None = None) -> float | None:
    """n° 8, D4 : seuil du jugement sur `days` jours, Bonferroni sur les essais déclarés, loi de Student à
    days − 1 degrés de liberté (l'écart-type est estimé sur les jours) ; None sous deux jours (pas jugeable)."""
    n = trials or len(ST.trials())
    return round(STATS.t_quantile(1 - ALPHA / n, days - 1), 2) if days >= 2 else None


def passes(t: float | None, seuil: float | None) -> bool:
    """t (m / se en grappes) au-dessus du seuil en t ; faux quand l'un des deux manque."""
    return t is not None and seuil is not None and t >= seuil


def robust(market: str) -> tuple[str, ...]:
    return ROBUST_STOCK if market == "stock" else ROBUST


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


def _week(day: str) -> str:
    y, w, _ = date.fromisoformat(day).isocalendar()
    return f"{y}-W{w:02d}"


class Mean:
    """Moyenne en grappes par jour, en un passage ; n° 10 : grappes par `key(jour)` (semaine ISO en crypto), le se
    naïf par jour publié à côté."""

    __slots__ = ("days", "total", "n", "key")

    def __init__(self, key=None):
        self.days: dict = defaultdict(lambda: [0.0, 0])
        self.total, self.n = 0.0, 0
        self.key = key

    def by(self, key) -> "Mean":
        """La même moyenne en grappes par `key(jour)` (sensibilité)."""
        out = Mean(key)
        out.days, out.total, out.n = self.days, self.total, self.n
        return out

    def clusters(self) -> dict:
        if self.key is None:
            return self.days
        out: dict = defaultdict(lambda: [0.0, 0])
        for day, (s, n) in self.days.items():
            cell = out[self.key(day)]
            cell[0] += s
            cell[1] += n
        return out

    def add(self, day: str, r: float | None) -> None:
        if r is None:
            return
        cell = self.days[day]
        cell[0] += r
        cell[1] += 1
        self.total += r
        self.n += 1

    def result(self) -> dict:
        """Espérance, IC 95 % en t à G − 1 degrés de liberté (n° 8, D4 ; G = grappes : jours, ou semaines) et
        statistique m / se (gardée sous la clé « z ») ; n° 10 : se, et avec `key` le se naïf par jour (se_jour) et
        le facteur d'inflation se / se_jour."""
        groups = self.clusters()
        m, se, _ = STATS.mean_ci(groups, self.total, self.n)
        ci = None
        if m is not None and se is not None:
            q = STATS.t_quantile(0.975, len(groups) - 1)
            ci = [round(m - q * se, 4), round(m + q * se, 4)]
        out = {"n": self.n, "esperance_r": round(m, 4) if m is not None else None, "ic95": ci,
               "z": round(m / se, 2) if m is not None and se else None, "jours": len(self.days),
               "grappes": len(groups), "se": round(se, 4) if se is not None else None}
        if self.key is not None:
            naive = STATS.mean_ci(self.days, self.total, self.n)[1]
            out |= {"se_jour": round(naive, 4) if naive is not None else None,
                    "inflation_se": round(se / naive, 2) if se is not None and naive else None}
        return out


def _acc(market: str | None = None) -> dict:
    key = _week if market == "crypto" else None  # n° 10 (L5) : semaines ISO en crypto

    def mean() -> Mean:
        return Mean(key)

    return {"signaux": 0, "declenches": 0, "gagnants": 0, "tp1": 0, "sorties": defaultdict(mean), "temoin": mean(),
            "ecart": mean(), "regimes": defaultdict(mean), "rangs": defaultdict(mean), "manques": 0, "ibkr": mean(),
            "commission": mean(), "frais": defaultdict(mean), "funding": defaultdict(mean), "oi": defaultdict(mean),
            "ecarts": defaultdict(mean), "vwap": defaultdict(mean), "robuste": defaultdict(mean),
            "tranches": defaultdict(lambda: defaultdict(mean)), "ecart_journee": mean(), "effet_heure": mean(),
            "tirages": defaultdict(lambda: [0.0, 0])}


def _feed(a: dict, r: dict) -> None:
    """Ajoute une ligne déclenchée aux accumulateurs d'une stratégie (`r` : champs préparés par _strategies)."""
    day, values, ref, comm = r["day"], r["values"], r["ref"], r["comm"]
    a["declenches"] += 1
    a["gagnants"] += (values[r["refc"]] or 0) > 0
    a["tp1"] += bool(r["tp1"])
    for c in COLUMNS:
        a["sorties"][c].add(day, values[c])
    a["manques"] += values["r_manque"] is None
    a["regimes"][r["label"]].add(day, ref)
    a["rangs"][_rank_bucket(r["rank"])].add(day, ref)
    a["ibkr"].add(day, ref)
    for c in robust(r["market"]):  # n° 6 et 8 : la règle « candidate » exige que la référence tienne sous ces conditions
        a["robuste"][c].add(day, values[c] - (0.0 if c in WITH_COMMISSION else comm) if values[c] is not None else None)
    if r["market"] == "stock":
        a["commission"].add(day, comm)
        a["robuste"]["r_20e"].add(day, values.get("r_20e"))  # n° 10 : candidate_compte, hors de « tient »
    a["frais"][r["frais"]].add(day, ref)
    for c in r["ecarts"]:
        a["ecarts"][c].add(day, r["ecarts"][c])
    if r["vwap"]:
        a["vwap"][r["vwap"]].add(day, ref)
    for family, band in r["bandes"].items():
        a["tranches"][family][band].add(day, ref)
    if r["market"] == "crypto":
        a["funding"][r["funding"]].add(day, ref)
        a["oi"][r["oi"]].add(day, ref)
    if r["ecart_temoin"] is not None:
        a["temoin"].add(day, r["temoin"])
        a["ecart"].add(day, r["ecart_temoin"])
    if r.get("journee") is not None:  # n° 10, actions : témoin de la journée (d'avant), et l'effet de l'heure
        x = values[r["refc"]]
        a["ecart_journee"].add(day, x - r["journee"] if x is not None else None)
        if r["ecart_temoin"] is not None:
            a["effet_heure"].add(day, r["temoin"] - r["journee"])
    for j, x in enumerate(r.get("tirages") or ()):  # n° 10 : R de chaque tirage du témoin
        if x is not None:
            cell = a["tirages"][j]
            cell[0] += x
            cell[1] += 1


def _halves(mean: Mean) -> tuple[float | None, float | None]:
    """Espérance des deux moitiés chronologiques des jours (⌊J/2⌋ premiers jours, puis le reste)."""
    days = sorted(mean.days)
    if len(days) < 2:
        return None, None

    def avg(part: list[str]) -> float | None:
        n = sum(mean.days[d][1] for d in part)
        return round(sum(mean.days[d][0] for d in part) / n, 4) if n else None

    return avg(days[:len(days) // 2]), avg(days[len(days) // 2:])


def _item(market: str, name: str, a: dict, trials: int) -> dict:
    n = a["declenches"]
    go = name in ST.GO_STRATEGIES  # n° 7 : une seule sortie déclarée, pas de mesures de la n° 6
    main = a["sorties"][ST.ref_exit(name)].result()
    brut = a["sorties"]["r_gross"].result()["esperance_r"]
    ecart = a["ecart"].result()
    ibkr = a["ibkr"].result() if market == "stock" else None
    ref = ibkr or main
    rob = {c: a["robuste"][c].result()["esperance_r"] for c in robust(market)}
    halves = _halves(a["ibkr"])
    holds = all(v is not None and v > 0 for v in (*rob.values(), *halves))
    item = {
        "nom": ST.LABELS.get(name, name), "signaux": a["signaux"], "declenches": n,
        "taux_declenchement": round(n / a["signaux"], 4) if a["signaux"] else None,
        "jours": main["jours"], **{k: main[k] for k in ("esperance_r", "ic95")},
        "reussite": round(a["gagnants"] / n, 4) if n else None, "tp1": round(a["tp1"] / n, 4) if n else None,
        "sorties": {c: a["sorties"][c].result() for c in ST.strategy_exits(market, name)},
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
        "ecarts_sorties": {c: _helps(a["ecarts"][c].result(), trials) for c in ST.STOCK_EXITS}
        if market == "stock" and not go else None,
        "par_vwap": {k: a["vwap"][k].result() for k in VWAP_BUCKETS if k in a["vwap"]}
        if market == "stock" and not go else None,
        "tranches": _tranches(a["tranches"]) if market == "stock" and name != ST.REFERENCE and not go else None,
        "robustesse": {c: a["sorties"][c].result() for c in robust(market)},
        "mise_20e": a["sorties"]["r_20e"].result() if market == "stock" else None,  # n° 8, D2
        "robuste": {**rob, "moitie_1": halves[0], "moitie_2": halves[1], "tient": holds},
        "ordres_limites_retires": a["manques"],
        "temoin": {"esperance_r": a["temoin"].result()["esperance_r"], "ecart_r": ecart["esperance_r"],
                   "ic95": ecart["ic95"], "z": ecart["z"], "n": ecart["n"],
                   "journee": a["ecart_journee"].result() if market == "stock" else None,  # n° 10, descriptifs
                   "effet_heure": a["effet_heure"].result() if market == "stock" else None,
                   "dispersion_tirages": _spread(a["tirages"])},
        "regimes": {k: v.result() for k, v in sorted(a["regimes"].items())},
        "echantillon_faible": n < MIN_N,
        "se": main["se"], "se_ibkr": ibkr["se"] if ibkr else None,  # n° 10 (L5)
    }
    ci = ref["ic95"]
    item["seuil_t"] = candidate_t(ecart["grappes"], trials)  # n° 8, D4 ; n° 10 : G = semaines en crypto
    item["candidate"] = bool(ci and ci[0] > 0 and passes(ecart["z"], item["seuil_t"]) and n >= MIN_N and holds)
    m20 = item["mise_20e"]
    item["rentable_20e"] = bool(m20 and m20["esperance_r"] is not None and m20["esperance_r"] > 0)
    if market == "stock":  # n° 10 (L3) : r_20e affiché avec la robustesse, hors de « tient »
        item["robuste"]["r_20e"] = a["robuste"]["r_20e"].result()["esperance_r"]
    item["candidate_compte"] = item["candidate"] and (market != "stock" or (item["robuste"]["r_20e"] or 0) > 0)
    if market == "stock":  # n° 10 (L5) : sensibilité en grappes par semaine, sans effet sur le verdict
        week, week_gap = a["ibkr"].by(_week).result(), a["ecart"].by(_week).result()
        item["semaines"] = {"grappes": week["grappes"], "ic95_ibkr": week["ic95"], "ecart_z": week_gap["z"],
                            "seuil_t": candidate_t(week_gap["grappes"], trials)}
    else:  # n° 10 (L5) : grappes par semaine ISO ; le se naïf par jour à côté
        item["grappes_semaine"] = {"semaines": ecart["grappes"], "se_jour": ecart.get("se_jour"),
                                   "inflation_se": ecart.get("inflation_se"),
                                   "z_jour": round(ecart["esperance_r"] / ecart["se_jour"], 2)
                                   if ecart["esperance_r"] is not None and ecart.get("se_jour") else None,
                                   "inflation_se_esperance": main.get("inflation_se")}
    return item


def _spread(cells: dict) -> dict:
    """n° 10 : dispersion entre tirages du témoin, écart-type des moyennes par tirage (tirage k de chaque trade)."""
    means = [s / n for s, n in cells.values() if n]
    sd = math.sqrt(sum((x - sum(means) / len(means)) ** 2 for x in means) / (len(means) - 1)) if len(means) > 1 \
        else None
    return {"tirages": len(means), "ecart_type_r": round(sd, 4) if sd is not None else None}


def _tranches(acc: dict) -> dict:
    return {family: {label: acc[family][label].result() for _, _, label in bands if label in acc[family]}
            for family, bands in TRANCHES.items()}


def _bands(gap, atr_pct, vol15, risk_pct, stop_before) -> dict:
    """n° 6 : tranche de chaque mesure de Killmode connue à la décision (bornes figées dans la déclaration)."""
    room = 2 * risk_pct / atr_pct if risk_pct is not None and atr_pct else None
    values = {"gap": gap, "atr": atr_pct, "vol15": vol15, "place_2r": room, "stop_avant_entree": stop_before}
    out = {}
    for family, x in values.items():
        if x is not None:
            out[family] = next(label for lo, hi, label in TRANCHES[family] if lo <= x < hi)
    return out


def _strategies(conn: sqlite3.Connection) -> dict:
    out: dict = {}
    sql = (f"SELECT s.market, s.strategy, s.day, s.triggered, s.tp1_hit, s.temoin_r, s.temoin_triggered, s.rank, "
           f"s.entry, s.stop, s.symbol, s.signal_at, s.sur_vwap, s.gap_open_pct, s.atr_pct, s.vol15_adv, "
           f"s.stop_avant_entree, s.risk_pct, s.confirmation, {', '.join('s.' + c for c in COLUMNS)}, "
           "s.temoin_journee_r, s.temoin_journee_triggered, s.temoin_tirages, r.label "
           "FROM strat_trades s LEFT JOIN regimes r ON r.market = s.market AND r.day = s.day")
    acc: dict = {}
    trials = len(ST.trials())
    derivs = DER.load(conn)
    pooled = {"ecarts": defaultdict(Mean), "par_vwap": defaultdict(Mean)}  # n° 6, toutes stratégies actions
    pooled_bands: dict = defaultdict(lambda: defaultdict(Mean))           # n° 6, stratégies v2 actions
    sessions: dict = defaultdict(dict)                                      # n° 6 : orb15 et killmode par séance
    gos: dict = defaultdict(dict)                                           # n° 7 : bras de chaque GO, par stop
    for row in conn.execute(sql):
        (market, name, day, trig, tp1, wit, wit_trig, rank, entry, stop, symbol, signal_at, above, gap, atr_pct,
         vol15, stop_before, risk_pct, status) = row[:19]
        values = dict(zip(COLUMNS, row[19:19 + len(COLUMNS)]))
        day_wit, day_wit_trig, draws = row[19 + len(COLUMNS):-1]  # n° 10
        refc = ST.ref_exit(name)
        keys = [(market, name)]
        if name in ST.POST_SEEN and day > ST.SEEN_UNTIL[market]:
            keys.append((market, name, "suivants"))
        for key in keys:
            if key not in acc:
                acc[key] = _acc(market)
            acc[key]["signaux"] += 1
        comm = (ibkr_commission_r(entry, stop) or 0.0) if market == "stock" else 0.0
        ref = values[refc] - comm if values[refc] is not None else None
        spec = ST.STRATEGIES.get(name, {})
        if "go" in spec:
            gos[(spec["go"], symbol, day)][spec["arm"]] = {"ref": ref, "status": status, "symbol": symbol}
        if market == "stock" and name in ("orb15", "killmode"):
            sessions[(symbol, day)][name] = {"trig": bool(trig), "ref": ref, "gap": gap, "atr": atr_pct,
                                             "ecart": values["r_net"] - wit
                                             if trig and wit_trig and wit is not None and values["r_net"] is not None
                                             else None}
        if not trig:
            continue
        r = {"market": market, "day": day, "tp1": tp1, "rank": rank, "values": values, "label": row[-1] or "inconnu",
             "ref": ref, "refc": refc, "comm": comm, "frais": _cost_bucket(expected_cost_r(market, entry, stop, rank)),
             "ecarts": {}, "vwap": None, "bandes": {}, "temoin": wit,
             "ecart_temoin": values[refc] - wit if wit_trig and wit is not None and values[refc] is not None
             else None, "journee": day_wit if day_wit_trig else None, "tirages": json.loads(draws) if draws else None}
        if market == "stock" and "go" not in spec:  # n° 6 : sorties de Gemini, VWAP et mesures de Killmode
            r["ecarts"] = {c: values[c] - values["r_net"] for c in ST.STOCK_EXITS
                           if values[c] is not None and values["r_net"] is not None}
            r["vwap"] = _vwap_bucket(day, signal_at, above)
            if name != ST.REFERENCE:
                r["bandes"] = _bands(gap, atr_pct, vol15, risk_pct, stop_before)
            for c, x in r["ecarts"].items():
                pooled["ecarts"][c].add(day, x)
            if r["vwap"]:
                pooled["par_vwap"][r["vwap"]].add(day, ref)
            for family, band in r["bandes"].items():
                pooled_bands[family][band].add(day, ref)
        if market == "crypto":  # lot B : positionnement des dérivés au signal, sans rien filtrer
            funding, oi_chg = DER.context(derivs, symbol, signal_at)
            r["funding"] = DER.bucket(funding, DER.FUNDING_BUCKETS)
            r["oi"] = DER.bucket(oi_chg, DER.OI_BUCKETS)
        for key in keys:
            _feed(acc[key], r)
    for key, a in acc.items():
        if len(key) == 3:
            continue
        market, name = key
        item = _item(market, name, a, trials)
        if name in ST.POST_SEEN:  # n° 6 : jugée sur les jours qui suivent ceux déjà lus quand elle a été choisie
            sub = acc.get((market, name, "suivants"))
            item["jours_vus_jusqu_au"] = ST.SEEN_UNTIL[market]
            item["jours_suivants"] = _item(market, name, sub, trials) if sub else None
            item["candidate"] = bool(item["jours_suivants"] and item["jours_suivants"]["candidate"])
            item["candidate_compte"] = bool(item["jours_suivants"] and item["jours_suivants"]["candidate_compte"])
        out.setdefault(market, {})[name] = item
    for market, items in out.items():
        out[market] = dict(sorted(items.items(), key=lambda kv: -(kv[1]["esperance_ref_r"]
                                                                   if kv[1]["esperance_ref_r"] is not None else -99)))
    if pooled["ecarts"] or pooled["par_vwap"]:
        out["_n6"] = {"ecarts": {c: pooled["ecarts"][c].result() for c in ST.STOCK_EXITS if c in pooled["ecarts"]},
                      "par_vwap": {k: pooled["par_vwap"][k].result() for k in VWAP_BUCKETS if k in pooled["par_vwap"]}}
    if pooled_bands or sessions:
        out["_kill"] = {"tranches": _tranches(pooled_bands), **_kill_compare(sessions),
                        "couverture": coverage(conn)}
    if gos:
        out["_n7"] = _go_compare(gos, trials, ARC.results_lookup(conn))
    return out


def _go_compare(gos: dict, trials: int, passed=None) -> dict:
    """n° 7, par stop : statut de la confirmation de chaque GO, ce qu'a fait le brut dans chaque groupe (et le
    confirmé, quand son ordre est exécuté), et écart « confirmé ou rien » − brut par GO (0 pour un bras sans trade,
    après commission IBKR), en grappes par jour.
    n° 8 : pour chaque filtre FRSH (stop d'origine), écart « filtré ou rien » − « confirmé ou rien » et − brut, GO
    par GO (sans les GO où le filtre n'a pas pu être jugé) ; et, mesure sans essai, brut et confirmé selon que des
    résultats ont été publiés entre la veille et l'ouverture (`passed`(symbole, jour), archive du calendrier)."""
    out = {}
    for variant in [v for v in GO_VARIANTS_FR if any(k[0] == v for k in gos)]:
        groups = {st: {"go": 0, "executes": 0, "brut": Mean(), "confirme": Mean()} for st in GO_STATUS_FR}
        diff = Mean()
        for (v, _, day), arms in gos.items():
            b, c = arms.get("brut"), arms.get("confirme")
            if v != variant or not b or not c or c["status"] not in groups:
                continue
            g = groups[c["status"]]
            g["go"] += 1
            g["brut"].add(day, b["ref"])
            if c["ref"] is not None:
                g["executes"] += 1
                g["confirme"].add(day, c["ref"])
            diff.add(day, (c["ref"] or 0.0) - (b["ref"] or 0.0))
        out[variant] = {
            "go": sum(g["go"] for g in groups.values()),
            "par_statut": {GO_STATUS_FR[st]: {"go": g["go"], "executes": g["executes"], "brut": g["brut"].result(),
                                              "confirme": g["confirme"].result() if st == "confirme" else None}
                           for st, g in groups.items() if g["go"]},
            "ecart_par_go": _helps(diff.result(), trials)}
        filters = _filter_compare(gos, variant, trials)
        if filters:
            out[variant]["filtres"] = filters
        if passed is not None:
            out[variant]["par_resultats"] = _results_compare(gos, variant, passed)
    return out


def _filter_compare(gos: dict, variant: str, trials: int) -> dict:
    """n° 8 : effet de chaque filtre FRSH sur le bras confirmé, GO par GO."""
    out = {}
    for filt, name in ST.GO_FILTERS.items():
        arm = ST.STRATEGIES[name]["arm"]
        if ST.STRATEGIES[name]["go"] != variant:
            continue
        statuses: dict = defaultdict(int)
        vs_conf, vs_brut = Mean(), Mean()
        for (v, _, day), arms in gos.items():
            f, c, b = arms.get(arm), arms.get("confirme"), arms.get("brut")
            if v != variant or not f or not c or not b:
                continue
            statuses[f["status"]] += 1
            if f["status"] == "indetermine":
                continue
            vs_conf.add(day, (f["ref"] or 0.0) - (c["ref"] or 0.0))
            vs_brut.add(day, (f["ref"] or 0.0) - (b["ref"] or 0.0))
        if statuses:
            out[filt] = {"statuts": {**{GO_STATUS_FR[k]: statuses[k] for k in GO_STATUS_FR if statuses[k]},
                                     **{GO_FILTER_STATUS_FR[k]: statuses[k] for k in GO_FILTER_STATUS_FR
                                        if statuses[k]}},
                         "ecart_au_confirme": _helps(vs_conf.result(), trials),
                         "ecart_au_brut": _helps(vs_brut.result(), trials)}
    return out


def _results_compare(gos: dict, variant: str, passed) -> dict:
    """n° 8, mesure sans essai (FRSH) : brut et confirmé (exécutés) selon les résultats publiés avant l'ouverture."""
    groups: dict = {}
    for (v, symbol, day), arms in gos.items():
        b, c = arms.get("brut"), arms.get("confirme")
        if v != variant or not b or not c:
            continue
        g = groups.setdefault(passed(symbol, day), {"go": 0, "brut": Mean(), "confirme": Mean()})
        g["go"] += 1
        g["brut"].add(day, b["ref"])
        if c["ref"] is not None:
            g["confirme"].add(day, c["ref"])
    return {RESULTS_FR[k]: {"go": g["go"], "brut": g["brut"].result(), "confirme": g["confirme"].result()}
            for k, g in groups.items()}


def _diff(a: Mean, b: Mean) -> dict:
    """Écart des espérances de deux groupes, z approché (erreurs en grappes, groupes supposés indépendants)."""
    m1, se1, _ = STATS.mean_ci(a.days, a.total, a.n)
    m2, se2, _ = STATS.mean_ci(b.days, b.total, b.n)
    if m1 is None or m2 is None:
        return {"ecart_r": None, "z": None}
    se = math.hypot(se1 or 0, se2 or 0)
    return {"ecart_r": round(m1 - m2, 4), "z": round((m1 - m2) / se, 2) if se else None}


def _kill_compare(sessions: dict) -> dict:
    """n° 6 : trois comparaisons qui décomposent Killmode (orb15 est son squelette : même entrée, même stop).
    1. sélection : écart orb15 − témoin sur les séances à gap et ATR forts, contre les autres ;
    2. conditions de prix : parmi ces séances, orb15 quand Killmode a une ligne, contre sans ;
    3. limite de 11 h : par séance à ligne Killmode, R Killmode − R orb15 (0 sans trade). Les deux ordres sont
       identiques jusqu'à 11 h : l'écart vient des seuls trades d'orb15 exécutés entre 11 h et 12 h."""
    sel = {"forts": Mean(), "autres": Mean()}
    cond = {"avec": Mean(), "sans": Mean()}
    limit, differ = Mean(), 0
    for (_, day), s in sessions.items():
        o, k = s.get("orb15"), s.get("killmode")
        if o and o["gap"] is not None and o["atr"] is not None:
            strong = o["gap"] >= ST.KILL_GAP_PCT and o["atr"] >= ST.KILL_ATR_PCT
            if o["ecart"] is not None:
                sel["forts" if strong else "autres"].add(day, o["ecart"])
            if strong and o["trig"]:
                cond["avec" if k else "sans"].add(day, o["ref"])
        if k:
            rk = k["ref"] if k["trig"] else 0.0
            ro = o["ref"] if o and o["trig"] else 0.0
            if rk is not None and ro is not None:
                limit.add(day, rk - ro)
                differ += rk != ro
    return {"selection": {"gap_atr_forts": sel["forts"].result(), "autres": sel["autres"].result(),
                          **_diff(sel["forts"], sel["autres"])},
            "conditions": {"avec_killmode": cond["avec"].result(), "sans": cond["sans"].result(),
                           **_diff(cond["avec"], cond["sans"])},
            "limite_11h": limit.result() | {"seances_differentes": differ}}


def coverage(conn: sqlite3.Connection) -> dict | None:
    """n° 6 : part des actions ouvertes en gap ≥ 4 % avec un ATR ≥ 4 % présentes dans l'univers du labo, par
    prix de la veille (comptes du worker, universe.coverage_step) ; None tant que rien n'est compté."""
    totals: dict = {}
    days = 0
    for (value,) in conn.execute("SELECT value FROM progress WHERE job = ?", (U.COVERAGE_JOB,)):
        counts = json.loads(value)
        days += bool(counts)  # vide : séance sans journalier en base
        for label, (total, present) in counts.items():
            cell = totals.setdefault(label, [0, 0])
            cell[0] += total
            cell[1] += present
    if not totals:
        return None
    total, present = sum(c[0] for c in totals.values()), sum(c[1] for c in totals.values())
    return {"seances": days, "gappers": total, "dans_univers": present,
            "part": round(present / total, 4) if total else None,
            "par_prix": {label: {"gappers": totals[label][0], "dans_univers": totals[label][1],
                                 "part": round(totals[label][1] / totals[label][0], 4) if totals[label][0] else None}
                         for _, _, label in U.PRICE_BUCKETS if label in totals}}


def _helps(res: dict, trials: int) -> dict:
    """Écart apparié à une référence ; « aide » si positif avec t ≥ seuil (candidate_t, sur ses jours) et assez
    de trades."""
    seuil = candidate_t(res.get("grappes", res["jours"]), trials)  # n° 10 : semaines en crypto
    return res | {"seuil_t": seuil, "aide": bool(res["esperance_r"] is not None and res["esperance_r"] > 0
                                                 and passes(res["z"], seuil) and res["n"] >= MIN_N)}


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


def partial(conn: sqlite3.Connection) -> dict:
    """I2 : marchés en rattrapage d'une nouvelle déclaration, jours restants et lignes d'une autre déclaration (ou
    d'avant le suivi, NULL) : tant qu'il en reste, elles sortent du tableau.

    Lot 2 (C3) : l'exclusion ne dépend plus des jours restants. Avant, d'anciennes lignes au-delà du dernier journalier
    (ou d'un jour que le rattrapage n'a pas pu refaire) revenaient dans le tableau dès que remaining() tombait à 0.
    Un marché est « partiel » s'il reste des jours du REJEU à rattraper, ou d'anciennes lignes une fois la déclaration
    en route (curseurs du rattrapage posés, ou lignes de la déclaration déjà écrites, ou jours encore dus). Un jour du
    direct qui attend seulement son calcul (journalier de la séance suivante, puis rattrapage) n'est pas une révision
    en cours : seul, il ne rend pas le tableau partiel ; il est compté dans les jours restants quand le marché l'est."""
    from app.lab import rattrapage as RAT  # import tardif : tire runner et generator

    out = {}
    for market in ("stock", "crypto"):
        decl = ST.CRYPTO_DECLARATION if market == "crypto" else ST.DECLARATION
        try:
            old = conn.execute("SELECT COUNT(*) FROM strat_trades WHERE market = ? AND declaration IS NOT ?",
                               (market, decl)).fetchone()[0]
        except sqlite3.OperationalError:  # base pas encore migrée (lecture seule) : rien ne dit la déclaration
            continue
        left = RAT.remaining(conn, market)
        if not left["replay"] and not (old and (left["live"] or _started(conn, market, decl))):
            continue
        out[market] = {"declaration": decl, "jours_rattrapage": sum(left.values()), "lignes_exclues": old}
    return out


def _started(conn: sqlite3.Connection, market: str, decl: int) -> bool:
    """La déclaration du marché a commencé à écrire : curseurs du rattrapage ou frontière posés, ou lignes écrites."""
    return bool(conn.execute("SELECT 1 FROM progress WHERE job = ? LIMIT 1", (ST.progress_job(market),)).fetchone()
                or conn.execute("SELECT 1 FROM strat_trades WHERE market = ? AND declaration = ? LIMIT 1",
                                (market, decl)).fetchone())


@contextmanager
def declaration_view(conn: sqlite3.Connection, part: dict | None):
    """I2 : pendant un rattrapage (`part` = partial), une vue temporaire du même nom masque strat_trades pour cette
    connexion (lecture seule comprise) et ne garde que les lignes de la déclaration en vigueur ; retirée à la sortie.
    Pour tout ce qui lit strat_trades le temps d'un rapport (tableau, autopsie)."""
    try:
        if part:
            keep = " OR ".join(f"(market = '{m}' AND declaration IS NOT {int(p['declaration'])})" for m, p in part.items())
            conn.execute("DROP VIEW IF EXISTS temp.strat_trades")
            conn.execute(f"CREATE TEMP VIEW strat_trades AS SELECT * FROM main.strat_trades WHERE NOT ({keep})")
        yield
    finally:
        conn.execute("DROP VIEW IF EXISTS temp.strat_trades")


def _open_bucket(x: float) -> str:
    return next(label for lo, hi, label in OPEN_BUCKETS if lo < x <= hi)


def _pairs() -> dict:
    return {"paires": 0, "limite_executees": 0, "ouverture_executees": 0, "limite": Mean(), "ouverture": Mean(),
            "ecart": Mean()}


def _pair(g: dict, day: str, limit: float | None, opened: float | None) -> None:
    g["paires"] += 1
    g["limite_executees"] += limit is not None
    g["ouverture_executees"] += opened is not None
    limit, opened = limit or 0.0, opened or 0.0
    g["limite"].add(day, limit)
    g["ouverture"].add(day, opened)
    g["ecart"].add(day, opened - limit)


def _pairs_result(g: dict) -> dict:
    return {k: g[k].result() if isinstance(g[k], Mean) else g[k] for k in g}


def entree_v1_appariee(conn: sqlite3.Connection) -> dict | None:
    """n° 10 (L4), descriptif : l'entrée de v1 prise de deux façons, plan par plan, en grappes par jour (une branche
    non exécutée compte 0, après la sortie à stop fixe, r_sans_be).
    - GO (sans rejeu) : « limite ou rien » (v1) contre l'achat à l'ouverture (go_brut, ramené au risque prévu de v1 :
      × (ouverture − stop) / (entrée − stop)), par écart ouverture / entrée ; à part, les GO ouverts au-dessus du
      TP1 (plan gardé du Labo 1 ; en révision 1 du simulateur, go_brut y vendait sous son prix d'achat).
    - Tous les plans : la même chose avec r_ouverture (n° 10), par gap d'ouverture (le prix d'ouverture n'est pas
      gardé dans la ligne de v1)."""
    v1s, gos = {}, {}
    for origin, symbol, day, name, trig, r, r_open, entry, stop, gap in conn.execute(
            "SELECT origin, symbol, day, strategy, triggered, r_sans_be, r_ouverture, entry, stop, gap_open_pct "
            "FROM strat_trades WHERE market = 'stock' AND strategy IN (?, 'go_brut')", (ST.REFERENCE,)):
        (v1s if name == ST.REFERENCE else gos)[(origin, symbol, day)] = (trig, r, r_open, entry, stop, gap)
    if not v1s:
        return None
    tp1 = {(o, s, d): t / e for o, s, d, e, t in conn.execute(
        "SELECT origin, symbol, day, json_extract(plan, '$.entry'), json_extract(plan, '$.targets[0].price') "
        "FROM trades WHERE market = 'stock'") if (o, s, d) in gos and e and t}
    go, above, by_open = _pairs(), _pairs(), defaultdict(_pairs)
    plans, by_gap = _pairs(), defaultdict(_pairs)
    for key, (trig, r, r_open, entry, stop, gap) in v1s.items():
        if not entry or stop is None or entry <= stop or (trig and r is None):
            continue
        day, limit = key[2], r if trig else None
        _pair(plans, day, limit, r_open)
        _pair(by_gap[_bucket(gap)], day, limit, r_open)
        g = gos.get(key)
        if not g or g[3] is None or g[4] is None or (g[0] and g[1] is None):
            continue
        opened = g[1] * (g[3] - g[4]) / (entry - stop) if g[0] else None
        ratio = g[3] / entry - 1
        if key in tp1 and ratio >= tp1[key] - 1:
            _pair(above, day, limit, opened)
            continue
        _pair(go, day, limit, opened)
        _pair(by_open[_open_bucket(ratio)], day, limit, opened)
    return {"go": _pairs_result(go) | {"par_ouverture": {k: _pairs_result(by_open[k]) for _, _, k in OPEN_BUCKETS
                                                          if k in by_open},
                                       "au_dessus_tp1": _pairs_result(above)},
            "plans": _pairs_result(plans) | {"par_gap": {k: _pairs_result(by_gap[k]) for _, _, k in
                                                         (*GAP_BUCKETS, (0, 0, "gap inconnu")) if k in by_gap}}}


def revisions(conn: sqlite3.Connection) -> dict:
    """Lot 2 (C6), descriptif : lignes lues par le tableau, par marché, selon la révision du code (code_rev) et celle du
    modèle de coût (cout_rev) qui les ont produites ; « inconnue » : écrites avant la colonne. Plusieurs révisions ne
    disent pas que les chiffres ont changé, seulement qu'il faut le vérifier (un rattrapage dure plusieurs jours, les
    déploiements sont fréquents)."""
    out: dict = {}
    try:
        rows = conn.execute("SELECT market, code_rev, cout_rev, COUNT(*) FROM strat_trades "
                            "GROUP BY market, code_rev, cout_rev").fetchall()
    except sqlite3.OperationalError:  # base pas encore migrée (lecture seule)
        return out
    for market, code, cost, n in rows:
        m = out.setdefault(market, {"code": {}, "cout": {}})
        for kind, rev in (("code", code), ("cout", cost)):
            m[kind][rev or "inconnue"] = m[kind].get(rev or "inconnue", 0) + n
    return out


def report(conn: sqlite3.Connection) -> dict:
    # import tardif : kill_complet et plus10 importent ce module
    from app.lab import kill_complet as KC
    from app.lab import plus10 as P10

    trials = len(ST.trials())
    out = {"declaration": {"numero": ST.LATEST_DECLARATION, "le": ST.REGISTERED_ON, "essais": trials,
                           "z_candidate": candidate_z(), "seuil": "t à G − 1 jours (candidate_t), Bonferroni",
                           "sorties": ST.EXITS, "sorties_actions": ST.STOCK_EXITS,
                           "robustesse": ST.STRESS, "doc": "docs/labo-strategies.md", "politique": POLICY},
           "scelle": SEAL.describe(), "strategies": {}, "sorties_gemini": None, "killmode": None,
           "confirmation_go": None, "killmode_complet": None, "potentiel_plus10": None, "diagnostic_v1": {},
           "entree_v1": None, "revisions": {}}
    try:  # base pas encore migrée par le worker (lecture seule) : tableau vide plutôt qu'une erreur
        # I2 : pendant un rattrapage, le tableau ne lit que les lignes de la déclaration en vigueur (declaration_view).
        out["partiel"] = partial(conn) or None
        with declaration_view(conn, out["partiel"]):
            out["strategies"] = _strategies(conn)
            out["sorties_gemini"] = out["strategies"].pop("_n6", None)
            out["killmode"] = out["strategies"].pop("_kill", None)
            out["confirmation_go"] = out["strategies"].pop("_n7", None)
            out["killmode_complet"] = KC.report(conn, trials)
            out["potentiel_plus10"] = P10.report(conn, trials)
            out["diagnostic_v1"] = diagnostic_v1(conn)
            out["entree_v1"] = entree_v1_appariee(conn)  # n° 10 (L4)
            out["revisions"] = revisions(conn)  # lot 2 (C6), descriptif
    except sqlite3.Error as exc:
        out["erreur"] = f"base pas encore prête : {exc}"
    return out


# --- Cache (écrit par le worker, lu par le serveur) ------------------------------------------

CACHE_KEY = "strategies"


def signature(conn: sqlite3.Connection) -> list:  # worker seulement (base migrée)
    sig = list(conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM strat_trades").fetchone())
    sig.append(conn.execute("SELECT COUNT(*) FROM progress WHERE job = ?", (U.COVERAGE_JOB,)).fetchone()[0])
    # n° 8 : Killmode complet (lignes au coût en vigueur) et calendrier des résultats archivé (GO selon les
    # résultats publiés) ; import tardif : kill_complet importe ce module
    from app.lab import kill_complet as KC

    cost = KC.cost_fingerprint()
    sig += [cost, conn.execute("SELECT COUNT(*) FROM kill_complet WHERE cout = ?", (cost,)).fetchone()[0]]
    sig.append(conn.execute("SELECT COUNT(*) FROM progress WHERE job = 'plus10:n9'").fetchone()[0])  # n° 9
    sig.append(conn.execute("SELECT COUNT(*) FROM earnings_days WHERE status = 'ok'").fetchone()[0])
    # I3 : lignes réécrites sur place (strat_trades ; trades pour le diagnostic v1) et code des stats
    from app.lab import empreinte as E

    return sig + E.sig(conn, "trades", "strat")


def cached(conn: sqlite3.Connection) -> dict | None:
    return STATS.cached(conn, CACHE_KEY)


# --- Discord ---------------------------------------------------------------------------------

MARKET_FR = {"stock": "Actions", "crypto": "Crypto"}


def _r(x) -> str:
    return "n/d" if x is None else f"{x:+.2f}".replace(".", ",")


def _z(x) -> str:
    return "n/d" if x is None or math.isinf(x) else f"{x:g}".replace(".", ",")


def _after_seen(it: dict) -> str:
    """n° 6 : Killmode et la n° 4 sont jugés sur les jours qui suivent ceux déjà lus quand ils ont été choisis."""
    if "jours_vus_jusqu_au" not in it:
        return ""
    sub = it.get("jours_suivants")
    seen = date.fromisoformat(it["jours_vus_jusqu_au"]).strftime("%d/%m/%Y")
    if not sub or not sub["declenches"]:
        return f" · jugée après le {seen} : pas encore de trade"
    ref = sub["esperance_ibkr_r"] if sub.get("esperance_ibkr_r") is not None else sub["esperance_r"]
    return f" · jugée après le {seen} : {_r(ref)} R, {sub['declenches']} trades, z témoin {_z(sub['temoin']['z'])}"


def _kill_text(kill: dict) -> str:
    """n° 6 : décomposition de Killmode (mesures, pas des essais) et couverture de l'univers."""
    sel, cond, lim = kill["selection"], kill["conditions"], kill["limite_11h"]
    parts = [f"gap et ATR forts {_r(sel['ecart_r'])} R sur l'écart d'orb15 à son témoin (z {_z(sel['z'])})",
             f"conditions de prix {_r(cond['ecart_r'])} R (z {_z(cond['z'])})",
             f"limite de 11 h {_r(lim['esperance_r'])} R par séance (z {_z(lim['z'])}, "
             f"{lim['seances_differentes']} séance(s) changée(s) sur {lim['n']})"]
    cov = kill.get("couverture")
    if cov and cov["part"] is not None:
        parts.append(f"univers : {round(100 * cov['part'])} % des actions en gap ≥ 4 % et ATR ≥ 4 % "
                     f"({cov['dans_univers']}/{cov['gappers']}, {cov['seances']} séances)")
    return "Killmode décomposé (n° 6, mesure) : " + " · ".join(parts)


def _n10_text(it: dict) -> str:
    """n° 10, descriptifs : effet de l'heure sur le témoin, dispersion entre tirages, lecture en semaines."""
    wit, parts = it.get("temoin") or {}, []
    heure = wit.get("effet_heure") or {}
    if heure.get("n"):
        parts.append(f"effet de l'heure {_r(heure['esperance_r'])} R (t {_z(heure['z'])})")
    spread = (wit.get("dispersion_tirages") or {}).get("ecart_type_r")
    if spread is not None:
        parts.append(f"tirages ±{_r(spread).lstrip('+')} R")
    week = it.get("semaines")
    if week and week.get("ecart_z") is not None:
        parts.append(f"en semaines t {_z(week['ecart_z'])} (seuil {_z(week['seuil_t'])})")
    weeks = it.get("grappes_semaine")
    if weeks and weeks.get("inflation_se") is not None:
        parts.append(f"{weeks['semaines']} semaines, se ×{_z(weeks['inflation_se'])} contre les jours "
                     f"(t par jour {_z(weeks['z_jour'])})")
    return "".join(f" · {p}" for p in parts)


def _entree_text(e: dict) -> str:
    """n° 10 (L4) : l'entrée de v1, limite ou rien contre l'achat à l'ouverture (mesure, pas un essai)."""
    go, plans = e["go"], e["plans"]
    return (f"Entrée de v1 (n° 10, mesure) : achat à l'ouverture − limite ou rien {_r(plans['ecart']['esperance_r'])} R "
            f"par plan (t {_z(plans['ecart']['z'])}, {plans['paires']} plans, {plans['limite_executees']} limites "
            f"exécutées) · sur les GO {_r(go['ecart']['esperance_r'])} R (t {_z(go['ecart']['z'])}, {go['paires']} GO"
            f", {go['au_dessus_tp1']['paires']} ouverts au-dessus du TP1 à part)")


def _revisions_text(market: str, revs: dict) -> str | None:
    """Lot 2 (C6) : « N révisions du code / du coût » d'un marché, seulement quand il y en a plus d'une."""
    code, cost = revs.get("code") or {}, revs.get("cout") or {}
    if len(code) < 2 and len(cost) < 2:
        return None

    def detail(d: dict) -> str:
        return ", ".join(f"{rev} {n:,}".replace(",", " ") for rev, n in sorted(d.items(), key=lambda kv: -kv[1]))

    return (f"ℹ️ Lignes {MARKET_FR.get(market, market).lower()} de {len(code)} révision{'s' if len(code) > 1 else ''} "
            f"du code ({detail(code)}) / {len(cost)} du coût ({detail(cost)}) : descriptif, à vérifier si les chiffres "
            "bougent")


def _t(res: dict) -> str:
    """« t x, seuil y » d'un écart jugé par _helps (n° 8, D4)."""
    return f"t {_z(res['z'])}, seuil {_z(res.get('seuil_t'))}"


def _go_text(variant: str, res: dict) -> str:
    """n° 7 : une ligne par stop, la confirmation contre le brut, GO par GO ; n° 8 : filtres FRSH et résultats."""
    diff = res["ecart_par_go"]
    parts = [f"{res['go']} GO · confirmé ou rien − brut {_r(diff['esperance_r'])} R par GO ({_t(diff)}"
             + (", ✅ la confirmation aide" if diff.get("aide") else "") + ")"]
    for label, g in res["par_statut"].items():
        part = f"{label}s {g['go']} : brut {_r(g['brut']['esperance_r'])} R"
        if g["confirme"] is not None:
            part += f", confirmé {_r(g['confirme']['esperance_r'])} R sur {g['executes']} exécutés"
        parts.append(part)
    for filt, f in (res.get("filtres") or {}).items():
        c = f["ecart_au_confirme"]
        parts.append(f"{GO_FILTERS_FR.get(filt, filt)} : filtré − confirmé {_r(c['esperance_r'])} R par GO ({_t(c)}"
                     + (", ✅ le filtre aide" if c.get("aide") else "") + f"), "
                     f"{f['statuts'].get(GO_FILTER_STATUS_FR['filtre'], 0)} refusés")
    for label, g in (res.get("par_resultats") or {}).items():
        parts.append(f"{label} {g['go']} : brut {_r(g['brut']['esperance_r'])} R, confirmé "
                     f"{_r(g['confirme']['esperance_r'])} R")
    return f"Confirmation des GO (n° 7, {GO_VARIANTS_FR.get(variant, variant)}) : " + " · ".join(parts)


def text(rep: dict | None) -> str:
    if not rep:
        return "🧪 **Stratégies du labo v2** : pas encore de mesure (le rejeu n'a pas encore traité de jour avec elles)."
    decl = rep["declaration"]
    lines = [f"🧪 **Stratégies du labo v2** · {decl['essais']} essais déclarés (déclaration n° "
             f"{decl.get('numero', 1)}) · espérance nette en R par trade [IC 95 %], actions après commission "
             f"IBKR estimée · ✅ = IC au-dessus de 0, témoin battu à t ≥ seuil (loi de Student sur les jours, "
             f"{str(decl.get('z_candidate', 2)).replace('.', ',')} sur beaucoup de jours, plus haut sur peu), "
             f"robuste (glissement ×2 et ×3, retard, stress de 3 c en actions, deux moitiés des jours) et positive "
             f"à 20 € en actions ; ☑️ = la même chose, mais perd à 20 € · crypto en grappes par semaine"]
    if decl.get("politique"):
        lines.append(f"Politique mesurée : {decl['politique']}")
    for market, p in (rep.get("partiel") or {}).items():  # I2
        n, m = p["jours_rattrapage"], p["lignes_exclues"]
        old = (f"{m} ligne{'s' if m > 1 else ''} de l'ancienne déclaration exclue{'s' if m > 1 else ''}" if m
               else "aucune ligne d'une autre déclaration")
        days = (f"{n} jour{'s' if n > 1 else ''} en rattrapage" if n
                else "aucun jour en rattrapage")  # C3 : d'anciennes lignes sans jour à refaire restent exclues
        lines.append(f"⏳ Tableau partiel ({MARKET_FR.get(market, market).lower()}) : {days}, {old} "
                     f"(n° {p['declaration']} seule en attendant)")
    for market, revs in (rep.get("revisions") or {}).items():  # lot 2 (C6), descriptif
        line = _revisions_text(market, revs)
        if line:
            lines.append(line)
    for market, items in (rep.get("strategies") or {}).items():
        lines.append(f"**{MARKET_FR.get(market, market)}**")
        for name, it in items.items():
            ibkr = it.get("esperance_ibkr_r") is not None
            ci = it.get("ic95_ibkr") if ibkr else it.get("ic95")
            ci_txt = f" [{_r(ci[0])} ; {_r(ci[1])}]" if ci else ""
            wit = it.get("temoin") or {}
            flag = ("✅ " if it.get("candidate_compte", it.get("candidate"))  # n° 10 ; tableau d'avant : candidate
                    else "☑️ avantage, perd à 20 € · " if it.get("candidate") else "")
            comm = f" · commission {_r(-(it.get('commission_ibkr_r') or 0))} R" if ibkr else ""
            m20 = it.get("mise_20e") or {}
            stake = f" · à 20 € {_r(m20.get('esperance_r'))} R" if m20.get("n") else ""
            lines.append(f"{flag}{it['nom']} : {_r(it['esperance_ibkr_r'] if ibkr else it['esperance_r'])} R{ci_txt}"
                         f" · {it['declenches']} trades, "
                         f"{it['jours']} j · réussite {round(100 * (it['reussite'] or 0))} % · frais "
                         f"{_r(-(it.get('frais_r') or 0)) if it.get('frais_r') is not None else 'n/d'} R{comm}{stake} · "
                         f"vs témoin {_r(wit.get('ecart_r'))} R (t {_z(wit.get('z'))}, seuil {_z(it.get('seuil_t'))})"
                         + _n10_text(it) + (" · échantillon faible" if it.get("echantillon_faible") else "")
                         + _after_seen(it))
    n6 = rep.get("sorties_gemini")
    if n6:
        names = {"r_vwap": "VWAP après 10 h 30", "r_1430": "time stop 14 h 30", "r_spy": "avalement de SPY"}
        stock = (rep.get("strategies") or {}).get("stock") or {}
        parts = []
        for c, res in n6["ecarts"].items():
            helped = sum(bool(((it.get("ecarts_sorties") or {}).get(c) or {}).get("aide")) for it in stock.values())
            parts.append(f"{names.get(c, c)} {_r(res['esperance_r'])} R (z {_z(res['z'])}"
                         + (f", ✅ sur {helped} stratégie{'s' if helped > 1 else ''}" if helped else "") + ")")
        if parts:
            lines.append("Sorties de Gemini (n° 6), écart à la sortie v1, toutes stratégies actions : "
                         + " · ".join(parts))
        if n6["par_vwap"]:
            lines.append("Entrée selon l'heure et le VWAP (mesure, pas un essai) : " + " · ".join(
                f"{k} {_r(v['esperance_r'])} R ({v['n']})" for k, v in n6["par_vwap"].items()))
    kill = rep.get("killmode")
    if kill:
        lines.append(_kill_text(kill))
    for variant, res in (rep.get("confirmation_go") or {}).items():
        lines.append(_go_text(variant, res))
    if rep.get("killmode_complet"):
        from app.lab import kill_complet as KC  # import tardif

        lines.append(KC.text(rep["killmode_complet"]))
    if rep.get("potentiel_plus10"):
        from app.lab import plus10 as P10  # import tardif

        lines.append(P10.text(rep["potentiel_plus10"]))
    diag = (rep.get("diagnostic_v1") or {}).get("stock")
    if diag:
        gaps = " · ".join(f"{k} {_r(v['esperance_r'])} ({v['n']})" for k, v in diag["par_gap"].items())
        lines.append(f"Diagnostic v1 actions : brut {_r(diag['brut']['esperance_r'])} R, net "
                     f"{_r(diag['net']['esperance_r'])} R · {gaps}")
    if rep.get("entree_v1"):
        lines.append(_entree_text(rep["entree_v1"]))
    lines.append(f"🔒 {rep['scelle']['texte']}")
    return "\n".join(lines)
