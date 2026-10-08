"""Labo v2 : stratégies à déclencheur, mesurées à côté de v1 (pré-enregistrées le 7 octobre 2026).

Pourquoi : v1 achète tout l'univers à heure fixe (9 h 10 New York, toutes les 4 h en crypto) au dernier
cours, sans attendre de signal, et perd −0,23 R par trade. Ses « setups » ne sont que des étiquettes
posées après coup. Ici, chaque stratégie lit les bougies de 5 min connues jusqu'à la clôture de la
bougie t et dit « je me déclenche » (ordre stop d'achat posé au-dessus de la bougie, valable
VALID_BARS bougies) ou rien. Rien n'est vu de l'avenir : la décision tombe à la clôture de la bougie
du signal, l'exécution part de la bougie suivante.

La liste est figée par écrit AVANT de regarder un seul résultat (docs/labo-strategies.md) : chaque
couple stratégie × sortie × marché compte comme un essai (trials()), gagnant ou non. Ajouter une
stratégie = une nouvelle déclaration datée, jamais une retouche silencieuse de celles-ci.

Stratégies (achat seulement, une position au plus par stratégie, actif et jour) :
- pullback : en tendance (clôture de la veille au-dessus de la MM 20 jours, EMA 20 de 5 min qui
  monte, prix au-dessus du VWAP), le prix revient toucher la zone (max de l'EMA 20 et du VWAP) à la
  bougie du signal ou la précédente, après une impulsion (un plus haut ≥ zone + 1 ATR parmi les 12
  bougies d'avant le contact, dans la séance pour les actions) ; bougie de confirmation :
  verte, clôture au-dessus de l'EMA 20, du VWAP et du plus haut de la bougie précédente. Entrée : stop
  d'achat 1 pas au-dessus de son plus haut ; stop sous le creux des 3 dernières bougies - 0,1 ATR ;
- orb15 / orb30 (actions) : range des 15 ou 30 premières minutes ; stop d'achat 1 pas au-dessus du
  plus haut, valable jusqu'à 12 h New York ; stop 1 pas sous le plus bas du range ;
- cassure_retest : clôture au-dessus du plus haut de la veille, puis, dans les 12 bougies, retour
  au niveau (plus bas ≤ niveau + 0,2 %) avec clôture au-dessus ; stop d'achat 1 pas au-dessus de la
  bougie du retest ; stop sous min(son plus bas, niveau) - 0,1 ATR.
Déclaration n° 2 (même jour, demande de Simão : les supports et résistances « comme dans la formation
EmperorBTC », chapitre S/R ; council : GO modifié) :
- zones S/R (zones(d)), calculées une fois au début de la fenêtre des signaux avec ce qui est connu
  alors : creux et sommets de réaction (3 bougies de chaque côté, comme v1) en 5 min confirmés avant la
  fenêtre (ZONE_5M_BARS) et en journalier sur les ZONE_DAILY_BARS dernières séances, points pivots de la veille, plus
  haut et plus bas de la veille. Les niveaux à moins de 0,5 ATR (5 min) du premier niveau d'une zone
  sont regroupés ; une zone n'est valide qu'avec au moins 2 contacts (« le plus de points possible »).
  Une zone n'a pas de rôle fixe : sous le prix elle est support, au-dessus résistance (une résistance
  cassée devient support) ;
- cassure_zone_retest (solution 1 du manuel) : clôture au-dessus d'une zone alors que la précédente
  était dessous, puis, dans les 12 bougies, retour sur la zone (plus bas entre bas de zone - 0,5 ATR et
  haut de zone + 0,1 ATR) avec clôture au-dessus ; stop d'achat 1 pas au-dessus de la bougie du retest ;
  stop sous min(son plus bas, bas de zone) - 0,1 ATR ;
- rebond_confirme (solution 2) : le prix, qui clôturait au-dessus d'une zone, la touche (même bande) et
  clôture sans l'avoir perdue (au-dessus du bas de zone) ; pas d'entrée au contact (le manuel : « chasse
  aux stops »), mais un stop d'achat 1 pas au-dessus du plus haut des 6 dernières bougies (la résistance
  la plus proche), valable 12 bougies, annulé si le stop est touché avant ; stop sous min(plus bas du
  contact, bas de zone) - 0,1 ATR ;
- sortie r_emperor (toutes les stratégies) : objectifs sur les prochaines résistances (zones au-dessus,
  0,1 ATR avant ; v1 garde ses propres objectifs), gestion v1, et sortie anticipée sur les signes
  de retournement du manuel (signals.exit_signals, code des alertes en direct) quand le trade est en gain,
  exécutée une bougie plus tard (c'est une alerte, Simão passe l'ordre à la main).
Le trident (Stoch RSI + VWAP + pivot) n'est pas repris : v1 le mesure déjà (−0,07 R en crypto).
Déclaration n° 3 (même soir, avis de GPT et Gemini, avant tout résultat de la n° 2, jamais déployée) :
- zones : un contact = un creux ou sommet de réaction (5 min ou journalier, plus haut et plus bas de la
  veille), suivi d'une réaction d'au moins ZONE_REACTION_ATR ATR dans les ZONE_REACTION_BARS bougies
  (5 min) ; deux contacts d'une zone ne comptent pour deux que s'ils sont séparés d'au moins
  ZONE_CONTACT_GAP (une congestion à plus bas égaux n'est qu'un contact ; un creux de 5 min du même jour
  qu'un creux journalier non plus). Les points pivots sortent des zones (ils viennent du trident) ;
- cassure_zone_retest : une clôture sous le bas de zone avant le retest annule la cassure ;
- rebond_confirme : la résistance à casser est le dernier sommet de réaction confirmé avant le contact
  (cassure de structure), et non plus le plus haut des 6 dernières bougies ; ordre valable jusqu'à la fin de la
  fenêtre des signaux, annulé si le stop est touché avant ;
- sorties EmperorBTC séparées : r_emperor (objectifs sur les résistances seuls), r_emperor_alerte (sortie à
  la bougie qui suit le premier signe, comme les alertes en direct), r_emperor_confirme (le signe n'est
  qu'un avertissement : sortie si une clôture passe sous le plus bas de la bougie du signe dans les
  EXIT_CONFIRM_BARS bougies, exécutée à la bougie suivante) ;
- témoin : moyenne de WITNESS_DRAWS tirages au lieu d'un seul.
Déclaration n° 4 (8 octobre 2026, lot B du plan, après le premier tableau de la n° 3) : les frais tuent les
stops serrés de 5 min (74 % des cassures de zone crypto paient au moins 0,4 R de frais). cassure_zone_retest,
la solution 1 du manuel, est rejouée telle quelle sur des bougies de 15 min (actions, crypto) et de 1 h
(crypto ; en actions, deux séances d'historique ne donnent pas assez de bougies de 1 h) : zones, cassure,
retest, ordre et stop se lisent sur ces bougies (Day.frame), donc stops plus larges en R ; la simulation, les
sorties et le témoin restent sur les bougies de 5 min, comme pour les autres.
Déclaration n° 5 (8 octobre 2026, demande de Simão : « JARVIS Killmode », trades à haute volatilité sur des setups
complets) : killmode (actions) est le squelette prix de Killmode, la partie qui se rejoue sans voir l'avenir.
Jour retenu seulement si le gap à l'ouverture est d'au moins KILL_GAP_PCT % et l'ATR 14 jours de la veille
d'au moins KILL_ATR_PCT % du cours ; range des 15 premières minutes clôturé au-dessus du VWAP sans combler le
gap (plus bas du range au-dessus de la clôture de la veille) ; stop d'achat 1 pas au-dessus du plus haut du
range, valable jusqu'à 11 h New York ; stop 1 pas sous son plus bas. Le catalyseur et la fiche société ne se
rejouent pas (Finnhub gratuit ne remonte qu'un an) : ils sont mesurés sur les appels de Killmode en direct
(app/trading/killmode.py, journal des appels), la même règle de prix servant aux deux.
Déclaration n° 6 (8 octobre 2026, demande de Simão : tester trois règles de sortie de Gemini, actions seulement) :
sur la gestion de référence (r_net), une règle de plus, une variable à la fois (STOCK_EXITS) :
- r_vwap : sortie de toute la position si une bougie close à partir de 10 h 30 New York finit sous le VWAP de
  séance, exécutée à la clôture de la bougie suivante (alerte, ordre passé à la main) ; sans condition de
  volume (le « volume vendeur » ne se lit pas sur des bougies OHLCV) ;
- r_1430 : time stop, sortie à 14 h 30 New York d'une position encore ouverte dont le TP1 n'est pas touché ;
- r_spy : sortie à la clôture de la bougie qui suit un avalement baissier de SPY en 5 min (indicators.engulfing).
Le filtre d'entrée VWAP proposé par GPT (pas d'entrée avant 10 h 30, puis seulement au-dessus du VWAP) n'est
pas un essai : la position de la décision par rapport au VWAP est gardée (sur_vwap) et le tableau donne
l'espérance par tranche horaire et VWAP, sans rien filtrer.
La n° 6 rend aussi Killmode mesurable pièce par pièce, sans essai de plus (fil « JARVIS Killmode ») : chaque
ligne actions garde atr_pct, vol15_adv (stratégies v2) et stop_avant_entree (kill_context, stop_before_entry) ;
le témoin de killmode tire ses entrées entre 9 h 45 et 11 h, pendant la vie de son ordre ; killmode et la n° 4
(POST_SEEN) ne sont jugées que sur les jours après SEEN_UNTIL, déjà lus quand elles ont été choisies.
Déclaration n° 7 (8 octobre 2026, demande de Simão : rejouer la confirmation des GO du scanner, PR #37, contre le GO
brut) : sur les plans GO de v1 (actions), deux bras jugés sur la seule sortie déclarée (r_sans_be : un tiers par
objectif, stop fixe, reste à la clôture), mêmes jours, mêmes coûts (go_rows) :
- go_brut : achat au marché à l'ouverture (première bougie de la séance), si elle est au-dessus du stop ;
- go_confirme : la règle du direct (confirm.evaluate, même code) lue sur les bougies de 5 min closes avant 11 h 30
  New York ; une fois confirmé, ordre stop d'achat au déclencheur, valable jusqu'à 11 h 30 comme le dit le message
  du direct ; pas de trade si le GO est annulé ou expire.
Deux stops, chacun là où il se rejoue à l'identique : celui des plans du labo (règle d'origine, tous les jours) et
le plancher du direct (PR #36, suffixe _plancher) : le plan refait par le code du direct (service._floor_stop,
objectifs et note recalculés, donc ses propres GO), seulement sur les jours calculés par le générateur ; le
rattrapage n'a pas les ~60 séances de bougies de 15 min qu'il faudrait. Le générateur le garde dans trades.plan
(« plancher ») pour les rattrapages des déclarations suivantes. Chaque ligne garde le statut du GO
(confirmation : confirme, annule, expire) ; strat_stats compare, GO par GO, « confirmé ou rien » au brut.
Déclaration n° 8 (8 octobre 2026, docs/labo-strategies.md) : trois bras de plus sur le GO confirmé au stop d'origine,
après le trade FRSH arrêté au stop (GO_FILTERS, go_filter, lus à la clôture de la bougie de confirmation, même ordre
que go_confirme quand le filtre passe, sinon pas d'ordre : statut « filtre », ou « indetermine » s'il ne peut pas être
jugé) : V2, VWAP plus haut qu'il y a 3 bougies et clôture ≥ VWAP + 0,25 ATR de 5 min ; V3, clôture au-dessus du plus
haut de la séance jusque-là ; V4, variation depuis l'ouverture meilleure que celle de SPY (index_move). Chaque trade
exécuté garde aussi r_3c (stress de 3 cents de Gemini) et r_20e (mise de Simão), cost_variants. Killmode complet
(kill_complet.py) compte pour deux essais (KILL_COMPLETE_TRIALS).
Objectifs : 1, 2 et 3 R. Fenêtre des signaux : actions de 9 h 45 à 15 h New York (soldé à la
clôture) ; crypto de 0 h à 20 h UTC (soldé 24 h après le signal).
v1 est rejouée avec les mêmes sorties et tests, sur ses propres objectifs : c'est la référence.

Pour chaque déclenchement, une seule ligne (db.STRAT_COLUMNS) avec :
- r_net : sortie de v1 (un tiers à chaque objectif, stop à l'entrée après TP1) ;
- r_sans_be, r_2r (un seul objectif à 2 R), r_suiveur (stop suiveur 1 R sous le plus haut) ;
- tests de robustesse : glissement ×2 et ×3, entrée une bougie plus tard, ordre limite à peine
  touché (moins de MISS_DEPTH) compté comme non exécuté ;
- témoin aléatoire : même jour, même fenêtre, même stop en %, mêmes objectifs en R, entrée au marché
  à des bougies tirées au hasard (graine fixe, moyenne de WITNESS_DRAWS tirages). Une stratégie qui ne
  bat pas son témoin n'a pas d'avantage d'entrée.
- n° 6, actions : atr_pct (ATR 14 jours de la veille en %), vol15_adv (volume des 15 premières minutes sur
  le volume journalier moyen, None pour v1 décidée à 9 h 10) et stop_avant_entree (1 si le stop est touché
  entre la décision et l'exécution d'un ordre stop, qui n'est pas annulé). Rien ne filtre sur ces colonnes.
"""
import hashlib
import random
from bisect import bisect_left
from datetime import date, datetime, time, timezone
from itertools import pairwise

from app.lab import collector as CO
from app.lab import simulator as SIM
from app.trading import confirm as CF
from app.trading import indicators as I
from app.trading import signals as S

REGISTERED_ON = "2026-10-08"
# Déclaration en vigueur (docs/labo-strategies.md). En changer relance le rattrapage de tous les jours déjà
# traités (progress_job), pour que chaque stratégie soit mesurée sur les mêmes jours.
DECLARATION = 8
# Dernière déclaration qui change les lignes crypto : les n° 7 et 8 (actions seulement) laissent le rattrapage
# crypto continuer celui de la n° 6. Une déclaration qui touche la crypto le remonte à son numéro.
CRYPTO_DECLARATION = 6
BAR = CO.BASE_TF * 60
REFERENCE = "v1"
STRATEGIES = {
    "pullback": {"label": "Pullback en tendance + confirmation", "markets": ("stock", "crypto")},
    "orb15": {"label": "Cassure du range d'ouverture (15 min)", "markets": ("stock",)},
    "orb30": {"label": "Cassure du range d'ouverture (30 min)", "markets": ("stock",)},
    "cassure_retest": {"label": "Cassure du plus haut de la veille puis retest", "markets": ("stock", "crypto")},
    # Déclaration n° 2 : zones S/R de la formation EmperorBTC.
    "cassure_zone_retest": {"label": "Cassure d'une zone S/R puis retest (EmperorBTC)", "markets": ("stock", "crypto")},
    "rebond_confirme": {"label": "Rebond sur une zone S/R puis cassure de la résistance (EmperorBTC)",
                        "markets": ("stock", "crypto")},
    # Déclaration n° 4 : la même solution 1, lue sur des bougies plus longues (stops plus larges en R).
    "cassure_zone_retest_15m": {"label": "Cassure d'une zone S/R puis retest, bougies de 15 min",
                                "markets": ("stock", "crypto"), "base": "cassure_zone_retest", "tf": 15},
    "cassure_zone_retest_1h": {"label": "Cassure d'une zone S/R puis retest, bougies de 1 h",
                               "markets": ("crypto",), "base": "cassure_zone_retest", "tf": 60},
    # Déclaration n° 5 : Killmode, sa partie prix (gap et volatilité forts, range d'ouverture tenu).
    "killmode": {"label": "Killmode : gap ≥ 4 % et ATR ≥ 4 %, cassure du range 15 min tenu", "markets": ("stock",)},
    # Déclaration n° 7 : GO de v1 achetés à l'ouverture ou après la confirmation du direct (go_rows, pas de signal),
    # avec le stop d'origine ou le plancher du direct ; une seule sortie.
    "go_brut": {"label": "GO v1 acheté à l'ouverture (stop d'origine)", "markets": ("stock",),
                "go": "origine", "arm": "brut", "exits": ("r_sans_be",)},
    "go_confirme": {"label": "GO v1 confirmé : repli vers l'entrée puis reprise du VWAP (stop d'origine)",
                    "markets": ("stock",), "go": "origine", "arm": "confirme", "exits": ("r_sans_be",)},
    "go_brut_plancher": {"label": "GO du direct acheté à l'ouverture (stop plancher 1/3 ATR jour)",
                         "markets": ("stock",), "go": "plancher", "arm": "brut", "exits": ("r_sans_be",)},
    "go_confirme_plancher": {"label": "GO du direct confirmé (stop plancher 1/3 ATR jour)", "markets": ("stock",),
                             "go": "plancher", "arm": "confirme", "exits": ("r_sans_be",)},
    # Déclaration n° 8 (trade FRSH arrêté au stop, fil « Avis sur FRSH en pré-market ») : le bras confirmé (stop
    # d'origine) avec un filtre de plus à la bougie de confirmation ; sans le filtre, pas d'ordre (go_rows).
    "go_confirme_v2": {"label": "GO confirmé + VWAP qui monte, clôture ≥ VWAP + 0,25 ATR (stop d'origine)",
                       "markets": ("stock",), "go": "origine", "arm": "confirme_v2", "filtre": "v2",
                       "exits": ("r_sans_be",)},
    "go_confirme_v3": {"label": "GO confirmé + clôture au-dessus du plus haut de la séance (stop d'origine)",
                       "markets": ("stock",), "go": "origine", "arm": "confirme_v3", "filtre": "v3",
                       "exits": ("r_sans_be",)},
    "go_confirme_v4": {"label": "GO confirmé + plus fort que SPY depuis l'ouverture (stop d'origine)",
                       "markets": ("stock",), "go": "origine", "arm": "confirme_v4", "filtre": "v4",
                       "exits": ("r_sans_be",)},
}
GO_STRATEGIES = tuple(k for k, v in STRATEGIES.items() if "go" in v)  # n° 7
GO_STATUS = {"confirmé": "confirme", "annulé": "annule"}               # n° 7 : sinon « expire »
GO_FILTERS = {v["filtre"]: k for k, v in STRATEGIES.items() if "filtre" in v}  # n° 8
GO_FILTER_VWAP_BARS = 3     # n° 8, V2 : VWAP plus haut qu'il y a 3 bougies
GO_FILTER_ATR = 0.25        # n° 8, V2 : clôture au moins 0,25 ATR (5 min) au-dessus du VWAP
# n° 8, D1 : Killmode complet (pilier catalyseur rejoué sur l'archive des actus, kill_complet.py) : son espérance
# à la mise de Simão, et son écart au témoin propre apparié par jour. Deux essais de plus, hors STRATEGIES.
KILL_COMPLETE_TRIALS = ({"market": "stock", "strategy": "killmode_complet", "exit": "r_20e"},
                        {"market": "stock", "strategy": "killmode_complet_ecart_temoin", "exit": "r_20e"})
LABELS = {REFERENCE: "v1 (achat à heure fixe, référence)", **{k: v["label"] for k, v in STRATEGIES.items()}}
EXITS = {
    "r_net": "sortie v1 : un tiers à chaque objectif, stop à l'entrée après TP1",
    "r_sans_be": "mêmes objectifs, sans remonter le stop",
    "r_2r": "un seul objectif à 2 R",
    "r_suiveur": "aucun objectif, stop suiveur 1 R sous le plus haut",
    "r_emperor": "EmperorBTC : objectifs sur les prochaines résistances, gestion v1",
    "r_emperor_alerte": "EmperorBTC + sortie à la bougie qui suit le premier signe de retournement "
                        "(alertes en direct)",
    "r_emperor_confirme": "EmperorBTC + signe de retournement confirmé par une clôture sous le plus bas de sa bougie",
}
# Déclaration n° 6 : règles de sortie de Gemini, actions seulement (VWAP de séance, horaires et indice américains).
STOCK_EXITS = {
    "r_vwap": "sortie v1 + sortie complète après une clôture sous le VWAP à partir de 10 h 30 New York",
    "r_1430": "sortie v1 + time stop à 14 h 30 New York si le TP1 n'est pas touché",
    "r_spy": "sortie v1 + sortie après un avalement baissier de SPY en 5 min",
}
STRESS = {
    "r_glisse_x2": "glissement ×2", "r_glisse_x3": "glissement ×3",
    "r_retard": "entrée une bougie plus tard", "r_manque": "ordre limite à peine touché non exécuté",
}
TARGETS_R = (1.0, 2.0, 3.0)
EMA_LEN, ATR_LEN, DAILY_SMA = 20, 14, 20
EMA_RISE_BARS = 6
IMPULSE_LOOKBACK, IMPULSE_ATR = 12, 1.0
STOP_BUFFER_ATR = 0.1
VALID_BARS = 3
RETEST_BARS, RETEST_TOL = 12, 0.002
# Déclarations n° 2 et 3 : zones S/R EmperorBTC (figées avant le premier résultat).
ZONE_SWING_SIDE = 3         # creux et sommets de réaction : 3 bougies de chaque côté (comme v1)
ZONE_DAILY_BARS = 40        # séances journalières où chercher les creux et sommets
ZONE_5M_BARS = {"stock": 150, "crypto": 288}  # bougies de 5 min avant la fenêtre (2 séances / 24 h), mêmes
                                              # en rejeu et en rattrapage
ZONE_MERGE_ATR = 0.5        # niveaux à moins de 0,5 ATR (5 min) du premier niveau de la zone : même zone
ZONE_MIN_CONTACTS = 2
ZONE_REACTION_ATR = 1.0     # n° 3 : un creux (sommet) de 5 min ne compte que si le prix s'en éloigne d'1 ATR
ZONE_REACTION_BARS = 12     # dans les 12 bougies qui suivent (connues avant la fenêtre)
ZONE_CONTACT_GAP = 3600     # n° 3 : deux contacts d'une zone à moins d'une heure (ou le même jour qu'un
                            # niveau journalier) n'en font qu'un
ZONE_TOUCH_ATR, ZONE_BREAK_ATR = 0.1, 0.5  # contact : plus bas entre bas - 0,5 ATR et haut + 0,1 ATR
TARGET_BUFFER_ATR = 0.1     # objectif un peu avant la résistance (comme v1)
MIN_TARGET_R = 0.5
EXIT_SIGNAL_BARS = 80       # bougies lues par les signes de retournement (divergence RSI sur 60)
EXIT_CONFIRM_BARS = 6       # n° 3 : délai de confirmation d'un signe de retournement (30 min)
WITNESS_DRAWS = 5           # n° 3 : tirages du témoin aléatoire, moyennés
MISS_DEPTH = 0.001          # ordre limite : traversée de moins de 0,1 % = probablement pas exécuté en réel
TRAIL_R = 1.0
STOCK_SIGNAL_FROM_MIN = 15  # actions : signaux à partir de 9 h 45 (après 3 bougies)
STOCK_SIGNAL_UNTIL = time(15, 0)
ORB_UNTIL = time(12, 0)
CRYPTO_SIGNAL_HOURS = 20    # crypto : signaux de 0 h à 20 h UTC, soldés 24 h après
CRYPTO_HORIZON = 86400
# Déclaration n° 5 : Killmode (figé avant le premier résultat).
KILL_GAP_PCT = 4.0          # gap à l'ouverture, en % de la clôture de la veille
KILL_ATR_PCT = 4.0          # ATR 14 jours connu la veille, en % de la clôture
KILL_RANGE_MIN = 15         # range d'ouverture
KILL_UNTIL = time(11, 0)    # ordre stop d'achat valable jusqu'à 11 h New York (17 h Paris)
VWAP_EXIT_FROM = time(10, 30)  # n° 6 : bougies closes à partir de 10 h 30 New York
TIME_STOP_AT = time(14, 30)    # n° 6
KILL_ADV_DAYS = 20             # n° 6 : volume journalier moyen des 20 séances d'avant (vol15_adv)
# n° 6, mesure de Killmode : dernier jour rejoué déjà lu quand Killmode et la n° 4 ont été choisis ; ces
# stratégies ne sont jugées que sur les jours suivants (strat_stats, « jours_suivants »).
SEEN_UNTIL = {"stock": "2025-01-27", "crypto": "2025-03-03"}
POST_SEEN = ("killmode", "cassure_zone_retest_15m", "cassure_zone_retest_1h")


def progress_job(market: str) -> str:
    """Clé de progression du labo v2 (frontière et curseurs du rattrapage) : la dernière déclaration qui change
    les lignes du marché."""
    return f"strats:{market}:d{CRYPTO_DECLARATION if market == 'crypto' else DECLARATION}"


def exits(market: str) -> dict:
    """Sorties mesurées sur un marché : les communes, plus celles des actions (déclaration n° 6)."""
    return {**EXITS, **STOCK_EXITS} if market == "stock" else EXITS


def strategy_exits(market: str, name: str) -> dict:
    """Sorties d'une stratégie : celles du marché, sauf liste propre (n° 7 : la seule sortie déclarée)."""
    own = STRATEGIES.get(name, {}).get("exits")
    return {e: exits(market)[e] for e in own} if own else exits(market)


def ref_exit(name: str) -> str:
    """Sortie de référence (classement, règle « candidate ») : r_net, ou la sortie déclarée seule (n° 7)."""
    own = STRATEGIES.get(name, {}).get("exits")
    return own[0] if own else "r_net"


def trials() -> list[dict]:
    """Essais déclarés : chaque stratégie (et v1) × sortie × marché, comptés gagnants ou non."""
    out = []
    for market in ("stock", "crypto"):
        names = [REFERENCE] + [k for k, v in STRATEGIES.items() if market in v["markets"]]
        out += [{"market": market, "strategy": n, "exit": e} for n in names for e in strategy_exits(market, n)]
    return out + list(KILL_COMPLETE_TRIALS)


# --- Indicateurs sur bougies de 5 min ---------------------------------------------------------

def ema(values: list[float], n: int) -> list[float]:
    k, out = 2 / (n + 1), []
    for v in values:
        out.append(v if not out else v * k + out[-1] * (1 - k))
    return out


def atr(bars: list[dict], n: int = ATR_LEN) -> list[float | None]:
    """ATR de Wilder ; None tant qu'il n'y a pas n bougies."""
    out: list[float | None] = []
    trs: list[float] = []
    cur = None
    for i, b in enumerate(bars):
        prev = bars[i - 1]["c"] if i else b["c"]
        tr = max(b["h"] - b["l"], abs(b["h"] - prev), abs(b["l"] - prev))
        if cur is None:
            trs.append(tr)
            if len(trs) == n:
                cur = sum(trs) / n
        else:
            cur = (cur * (n - 1) + tr) / n
        out.append(cur)
    return out


def vwap_from(bars: list[dict], start: int) -> list[float | None]:
    """VWAP ancré sur la bougie `start` (ouverture de séance, minuit UTC) ; None avant."""
    out: list[float | None] = [None] * len(bars)
    pv = vol = 0.0
    for i in range(start, len(bars)):
        b = bars[i]
        tp = (b["h"] + b["l"] + b["c"]) / 3
        pv += tp * b["v"]
        vol += b["v"]
        out[i] = pv / vol if vol else tp
    return out


class Day:
    """Bougies d'un actif autour d'un jour, indicateurs et bornes des fenêtres (indices)."""

    def __init__(self, market: str, day: date, bars: list[dict], daily_before: list[dict], bar: int = BAR):
        self.market, self.day, self.bars, self.bar = market, day, bars, bar
        self.t = [b["t"] for b in bars]
        self.o = [b["o"] for b in bars]
        self.h = [b["h"] for b in bars]
        self.l = [b["l"] for b in bars]
        self.c = [b["c"] for b in bars]
        if market == "stock":
            bounds = CO.session_ts(day)
            self.start_ts, self.close_ts = bounds if bounds else (0, 0)
            ny = CO.NEW_YORK
            self.signal_from_ts = self.start_ts + STOCK_SIGNAL_FROM_MIN * 60
            self.signal_until_ts = min(int(datetime.combine(day, STOCK_SIGNAL_UNTIL, ny).timestamp()), self.close_ts)
            self.orb_until_ts = int(datetime.combine(day, ORB_UNTIL, ny).timestamp())
            self.vwap_exit_ts = int(datetime.combine(day, VWAP_EXIT_FROM, ny).timestamp())
            self.time_stop_ts = int(datetime.combine(day, TIME_STOP_AT, ny).timestamp())
        else:
            self.start_ts = CO.utc_midnight(day)
            self.close_ts = self.start_ts + 86400
            self.signal_from_ts = self.start_ts
            self.signal_until_ts = self.start_ts + CRYPTO_SIGNAL_HOURS * 3600
            self.orb_until_ts = self.signal_until_ts
        self.d0 = bisect_left(self.t, self.start_ts)
        self.d1 = bisect_left(self.t, self.close_ts)
        # Bougies du signal : celles qui se ferment entre signal_from + 5 min et signal_until (en 5 min :
        # celles qui commencent dans la fenêtre ; en 15 min ou 1 h, mêmes heures de décision au plus tard).
        self.first = bisect_left(self.t, self.signal_from_ts - bar + BAR)
        self.last = bisect_left(self.t, self.signal_until_ts - bar + BAR)  # exclu
        self.ema = ema(self.c, EMA_LEN)
        self.atr = atr(bars)
        self.vwap = vwap_from(bars, self.d0)
        closes = [b["c"] for b in daily_before]
        self.trend_ok = len(closes) >= DAILY_SMA and closes[-1] > sum(closes[-DAILY_SMA:]) / DAILY_SMA
        self.daily = daily_before
        self.prev_high = daily_before[-1]["h"] if daily_before else None
        self.prev_close = daily_before[-1]["c"] if daily_before else None
        self._zones: list[tuple[float, float, int]] | None = None
        self._exit_signal: dict[int, bool] = {}
        self._swing_highs: list[tuple[int, float]] | None = None
        self._frames: dict[int, "Day"] = {}
        self.index_down: list[int] | None = None  # n° 6 : clôtures des avalements baissiers de SPY (stock_day)
        self.index_move: dict[int, float] | None = None  # n° 8, V4 : variation de SPY depuis l'ouverture, par bougie
        self._kill_ctx: dict | None = None

    def frame(self, tf: int | None) -> "Day":
        """Le même jour lu sur des bougies de `tf` minutes (déclaration n° 4) ; lui-même en 5 min."""
        if not tf or tf == CO.BASE_TF:
            return self
        if tf not in self._frames:
            self._frames[tf] = Day(self.market, self.day, CO.aggregate(self.bars, tf, self.market), self.daily,
                                   bar=tf * 60)
        return self._frames[tf]

    @property
    def zone_atr(self) -> float | None:
        """ATR de la dernière bougie close avant la fenêtre des signaux (zones et objectifs EmperorBTC)."""
        k = self.first - 1
        return self.atr[k] if 0 <= k < len(self.atr) else None

    def zones(self) -> list[tuple[float, float, int]]:
        """Zones S/R (bas, haut, contacts) connues au début de la fenêtre des signaux (déclaration n° 2)."""
        if self._zones is None:
            self._zones = zones(self)
        return self._zones

    def swing_highs(self) -> list[tuple[int, float]]:
        """Sommets de réaction (indice, prix) de toutes les bougies ; celui en j est connu à la clôture de
        j + ZONE_SWING_SIDE."""
        if self._swing_highs is None:
            self._swing_highs = I.swings(self.bars, side=ZONE_SWING_SIDE)[0]
        return self._swing_highs

    def exit_signal(self, k: int) -> bool:
        """Signe de retournement EmperorBTC (alertes en direct) à la clôture de la bougie k."""
        if k not in self._exit_signal:
            self._exit_signal[k] = bool(S.exit_signals(self.bars[max(0, k + 1 - EXIT_SIGNAL_BARS):k + 1], "long"))
        return self._exit_signal[k]

    @property
    def usable(self) -> bool:
        return self.d1 > self.d0 and self.first < self.last and self.first >= EMA_LEN

    def tick(self, price: float) -> float:
        return SIM.tick_size(self.market, price)

    def exit_at(self, decided_at: int) -> int:
        return self.close_ts if self.market == "stock" else decided_at + CRYPTO_HORIZON

    def after(self, ts: int) -> list[dict]:
        """Bougies commencées à `ts` ou après (ce que le simulateur a le droit de voir)."""
        return self.bars[bisect_left(self.t, ts):]


# --- Signaux (chacun ne lit que les bougies 0..i) -------------------------------------------

def _cand(d: Day, i: int, entry: float, stop: float, valid_until: int | None = None) -> dict | None:
    if stop >= entry or stop <= 0:
        return None
    decided = d.t[i] + d.bar
    return {"i": i, "decided_at": decided, "entry": entry, "stop": stop,
            "entry_until": valid_until or decided + VALID_BARS * d.bar}


def pullback(d: Day) -> list[dict]:
    if not d.trend_ok:
        return []
    out = []
    for i in range(max(d.first, EMA_RISE_BARS, 2), d.last):
        a = d.atr[i]
        v = d.vwap[i]
        if not a or v is None:
            continue
        e = d.ema[i]
        if not (e > d.ema[i - EMA_RISE_BARS] and d.c[i] > v):
            continue
        lo = max(d.d0, i - IMPULSE_LOOKBACK) if d.market == "stock" else max(0, i - IMPULSE_LOOKBACK)
        if lo >= i:
            continue
        zone = max(e, v)
        touch = i - 1 if d.l[i - 1] <= zone else i if d.l[i] <= zone else None
        if touch is None or touch <= lo or max(d.h[lo:touch]) < zone + IMPULSE_ATR * a:
            continue  # pas de contact, ou pas d'impulsion AVANT le contact
        if not (d.c[i] > d.o[i] and d.c[i] > e and d.c[i] > v and d.c[i] > d.h[i - 1]):
            continue
        c = _cand(d, i, d.h[i] + d.tick(d.h[i]), min(d.l[i - 2:i + 1]) - STOP_BUFFER_ATR * a)
        if c:
            out.append(c)
    return out


def orb(d: Day, minutes: int) -> list[dict]:
    if d.market != "stock":
        return []
    n = minutes // CO.BASE_TF
    if d.d1 - d.d0 <= n or d.t[d.d0] != d.start_ts:
        return []
    hi, lo = max(d.h[d.d0:d.d0 + n]), min(d.l[d.d0:d.d0 + n])
    c = _cand(d, d.d0 + n - 1, hi + d.tick(hi), lo - d.tick(lo), valid_until=d.orb_until_ts)
    return [c] if c else []


def cassure_retest(d: Day) -> list[dict]:
    level = d.prev_high
    if not level:
        return []
    out, b, i = [], None, max(d.first, 1)
    while i < d.last:
        if b is None:
            if d.c[i] > level:
                b = i
            i += 1
            continue
        if i - b > RETEST_BARS:
            b = None
            continue  # même bougie : peut-être une nouvelle cassure
        if d.l[i] <= level * (1 + RETEST_TOL) and d.c[i] > level and d.atr[i]:
            c = _cand(d, i, d.h[i] + d.tick(d.h[i]), min(d.l[i], level) - STOP_BUFFER_ATR * d.atr[i])
            if c:
                out.append(c)
            b = None
        i += 1
    return out


def _reacted(after: list[dict], price: float, high: bool, move: float) -> bool:
    """Le prix s'est éloigné d'au moins `move` du sommet (high) ou du creux dans les bougies `after`."""
    if not after:
        return False
    return price - min(b["l"] for b in after) >= move if high else max(b["h"] for b in after) - price >= move


def _contacts(spans: list[tuple[int, int]]) -> int:
    """Contacts distincts d'une zone : des moments (début, fin) séparés de moins de ZONE_CONTACT_GAP n'en
    font qu'un (congestion, ou creux de 5 min du jour d'un niveau journalier)."""
    n, end = 0, None
    for t0, t1 in sorted(spans):
        if end is None or t0 >= end + ZONE_CONTACT_GAP:
            n += 1
            end = t1
        else:
            end = max(end, t1)
    return n


def zones(d: Day) -> list[tuple[float, float, int]]:
    """Zones S/R de la formation EmperorBTC, avec ce qui est connu au début de la fenêtre (indice d.first) :
    un creux ou sommet en j n'est confirmé qu'à la clôture de j + ZONE_SWING_SIDE. Un contact = un creux
    ou sommet de réaction (5 min, avec une réaction d'au moins ZONE_REACTION_ATR ATR ; journalier ; plus
    haut et plus bas de la veille), daté pour ne pas compter deux fois le même moment (déclaration n° 3)."""
    a = d.zone_atr
    if not a:
        return []
    # Même durée d'historique quelle que soit la bougie (n° 4) : 150 bougies de 5 min = 50 de 15 min.
    known = d.bars[max(0, d.first - ZONE_5M_BARS[d.market] * BAR // d.bar):d.first]
    highs, lows = I.swings(known, side=ZONE_SWING_SIDE)
    levels = []  # (prix, début, fin) de chaque contact
    for points, high in ((highs, True), (lows, False)):
        for j, p in points:
            if _reacted(known[j + 1:j + 1 + ZONE_REACTION_BARS], p, high, ZONE_REACTION_ATR * a):
                levels.append((p, known[j]["t"], known[j]["t"] + d.bar))
    daily = d.daily[-ZONE_DAILY_BARS:]
    dh, dl = I.swings(daily, side=ZONE_SWING_SIDE)
    levels += [(p, daily[j]["t"], daily[j]["t"] + 86400) for j, p in dh + dl]
    if daily:
        prev = daily[-1]
        levels += [(prev["h"], prev["t"], prev["t"] + 86400), (prev["l"], prev["t"], prev["t"] + 86400)]
    out, group = [], []
    for lv in sorted(x for x in levels if x[0] > 0):
        if group and lv[0] - group[0][0] > ZONE_MERGE_ATR * a:
            out.append(group)
            group = []
        group.append(lv)
    if group:
        out.append(group)
    zs = [(g[0][0], g[-1][0], _contacts([(t0, t1) for _, t0, t1 in g])) for g in out]
    return [z for z in zs if z[2] >= ZONE_MIN_CONTACTS]


def _in_band(low: float, zone: tuple, a: float) -> bool:
    lo, hi, _ = zone
    return lo - ZONE_BREAK_ATR * a <= low <= hi + ZONE_TOUCH_ATR * a


def cassure_zone_retest(d: Day) -> list[dict]:
    out = []
    for zone in d.zones():
        lo, hi, _ = zone
        b, i = None, max(d.first, 1)
        while i < d.last:
            if b is None:
                if d.c[i] > hi >= d.c[i - 1]:
                    b = i
                i += 1
                continue
            if i - b > RETEST_BARS:
                b = None
                continue  # même bougie : peut-être une nouvelle cassure
            if d.c[i] < lo:
                b = None  # retour sous la zone avant le retest : fausse cassure (n° 3)
                i += 1
                continue
            a = d.atr[i]
            if a and _in_band(d.l[i], zone, a) and d.c[i] > hi:
                c = _cand(d, i, d.h[i] + d.tick(d.h[i]), min(d.l[i], lo) - STOP_BUFFER_ATR * a)
                if c:
                    out.append(c)
                b = None
            i += 1
    return sorted(out, key=lambda c: c["i"])


def rebond_confirme(d: Day) -> list[dict]:
    zs = sorted(d.zones(), key=lambda z: -z[1])
    highs = d.swing_highs()
    out = []
    for i in range(max(d.first, 1), d.last):
        a = d.atr[i]
        if not a:
            continue
        zone = next((z for z in zs if d.c[i - 1] > z[1] and _in_band(d.l[i], z, a) and d.c[i] >= z[0]), None)
        if zone is None:
            continue
        # Cassure de structure (n° 3) : le dernier sommet de réaction confirmé à la clôture du contact.
        k = bisect_left(highs, (i - ZONE_SWING_SIDE + 1,))
        level = next((p for j, p in reversed(highs[:k]) if j >= i - ZONE_5M_BARS[d.market] * BAR // d.bar
                      and p > d.c[i]), None)
        if level is None:
            continue
        entry = level + d.tick(level)
        stop = min(d.l[i], zone[0]) - STOP_BUFFER_ATR * a
        until = d.signal_until_ts
        for j in range(i + 1, len(d.t)):  # ordre annulé dès que le stop est touché avant l'entrée
            if d.t[j] >= until:
                break
            if d.l[j] <= stop:
                until = d.t[j]
                break
            if d.h[j] >= entry:
                break
        c = _cand(d, i, entry, stop, valid_until=until)
        if c:
            out.append(c)
    return out


def kill_context(d: Day) -> dict:
    """Ce que Killmode connaît à l'ouverture : gap (ouverture de la séance contre la clôture de la veille) et
    ATR 14 jours de la veille en % ; à 9 h 45, vol15_adv (n° 6) : volume des 15 premières minutes rapporté
    au volume journalier moyen des 20 séances d'avant. None quand la donnée manque. Calculé une fois par Day."""
    if d._kill_ctx is not None:
        return d._kill_ctx
    gap = atr_pct = vol15 = None
    opened = d.d1 > d.d0 and d.t[d.d0] == d.start_ts
    if d.prev_close and opened:
        gap = (d.o[d.d0] / d.prev_close - 1) * 100
    if len(d.daily) > ATR_LEN and d.daily[-1]["c"] > 0:
        atr_pct = atr(d.daily)[-1] / d.daily[-1]["c"] * 100
    n = KILL_RANGE_MIN // CO.BASE_TF
    adv = sum(b.get("v") or 0 for b in d.daily[-KILL_ADV_DAYS:]) / KILL_ADV_DAYS
    if opened and d.d1 - d.d0 >= n and len(d.daily) >= KILL_ADV_DAYS and adv > 0:
        vol15 = sum(b.get("v") or 0 for b in d.bars[d.d0:d.d0 + n]) / adv
    d._kill_ctx = {"gap_pct": gap, "atr_pct": atr_pct, "vol15_adv": vol15}
    return d._kill_ctx


def _pct_fr(x: float) -> str:
    return f"{x:+.1f}".replace(".", ",")


def killmode_check(d: Day) -> tuple[dict | None, list[str]]:
    """Déclaration n° 5 : gap ≥ KILL_GAP_PCT, ATR ≥ KILL_ATR_PCT, range de 15 min clôturé au-dessus du VWAP et
    au-dessus de la clôture de la veille ; stop d'achat au-dessus du range jusqu'à 11 h, stop sous le range.
    Renvoie le signal (ou None) et, sans signal, ce qui manque (Killmode en direct l'affiche tel quel)."""
    if d.market != "stock":
        return None, ["actions seulement"]
    ctx = kill_context(d)
    reasons = []
    if ctx["gap_pct"] is not None and ctx["gap_pct"] < KILL_GAP_PCT:
        reasons.append(f"gap {_pct_fr(ctx['gap_pct'])} % (il faut au moins +{KILL_GAP_PCT:g} %)")
    if ctx["atr_pct"] is None:
        reasons.append("ATR journalier inconnu (historique trop court)")
    elif ctx["atr_pct"] < KILL_ATR_PCT:
        reasons.append(f"ATR {ctx['atr_pct']:.1f} % (il faut au moins {KILL_ATR_PCT:g} %) : pas assez volatil".replace(".", ","))
    n = KILL_RANGE_MIN // CO.BASE_TF
    if ctx["gap_pct"] is None or d.d1 - d.d0 < n:
        return None, reasons + ["range des 15 premières minutes pas encore complet"]
    if reasons:
        return None, reasons
    k = d.d0 + n - 1
    hi, lo = max(d.h[d.d0:k + 1]), min(d.l[d.d0:k + 1])
    v = d.vwap[k]
    if v is None or d.c[k] <= v:
        return None, ["range clôturé sous le VWAP : les vendeurs tiennent l'ouverture"]
    if lo <= d.prev_close:
        return None, [f"gap comblé pendant le range (plus bas {lo:g} ≤ clôture de la veille {d.prev_close:g})"]
    until = int(datetime.combine(d.day, KILL_UNTIL, CO.NEW_YORK).timestamp())
    c = _cand(d, k, hi + d.tick(hi), lo - d.tick(lo), valid_until=until)
    return (c, []) if c else (None, ["range sans hauteur"])


def killmode(d: Day) -> list[dict]:
    c, _ = killmode_check(d)
    return [c] if c else []


SIGNALS = {"pullback": pullback, "orb15": lambda d: orb(d, 15), "orb30": lambda d: orb(d, 30),
           "cassure_retest": cassure_retest, "cassure_zone_retest": cassure_zone_retest,
           "rebond_confirme": rebond_confirme, "killmode": killmode}


# --- Simulation : sortie de référence, variantes, robustesse et témoin ---------------------

def _plan(entry: float, stop: float, targets: list[float]) -> dict:
    return {"side": "long", "entry": entry, "stop": stop, "targets": [{"price": p} for p in targets]}


def _r_targets(entry: float, stop: float, multiples=TARGETS_R) -> list[float]:
    risk = entry - stop
    return [entry + m * risk for m in multiples]


def _seed(*parts) -> random.Random:
    return random.Random(int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:16], 16))


def _sim(d: Day, plan: dict, decided_at: int, entry_until: int, exit_at: int, cost: dict, order: str,
         last_price: float, **kw) -> dict:
    return SIM.simulate(plan, d.after(decided_at), market=d.market, decided_at=decided_at,
                        entry_until=min(entry_until, exit_at), exit_at=exit_at, last_price=last_price,
                        cost=cost, entry_order=order, **kw)


def _missed(res: dict, d: Day) -> bool:
    """Ordre limite exécuté de justesse (traversée < MISS_DEPTH) : en réel, souvent pas servi."""
    if res.get("entry_mode") != "limite":
        return False
    k = bisect_left(d.t, res["filled_at"])
    entry = res["order_prices"]["entry"]
    return k < len(d.l) and (entry - d.l[k]) / entry < MISS_DEPTH


def emperor_targets(d: Day, entry: float, stop: float) -> list[float]:
    """Objectifs EmperorBTC : bas des prochaines zones S/R au-dessus de l'entrée, 0,1 ATR avant, à au moins
    0,5 R ; complétés jusqu'à trois par les multiples de R suivants (au moins 0,5 R plus loin)."""
    risk = entry - stop
    a = d.zone_atr
    out: list[float] = []
    for lo, _, _ in sorted(d.zones()):
        t = lo - TARGET_BUFFER_ATR * (a or 0)
        if t >= entry + MIN_TARGET_R * risk and (not out or t > out[-1]):
            out.append(t)
        if len(out) == 3:
            return out
    m = 1
    while len(out) < 3:
        t = entry + m * risk
        if not out or t >= out[-1] + MIN_TARGET_R * risk:
            out.append(t)
        m += 1
    return out


def _emperor(d: Day, plan: dict, decided_at: int, entry_until: int, exit_at: int, cost: dict, order: str,
             last_price: float) -> dict:
    """Sorties EmperorBTC (déclaration n° 3) : objectifs du plan et gestion v1 seuls (r_emperor) ; plus
    une sortie à la clôture de la bougie qui suit le premier signe de retournement vu en gain (alerte, puis
    ordre passé à la main : r_emperor_alerte) ; ou seulement si une clôture passe sous le plus bas de la
    bougie du signe dans les EXIT_CONFIRM_BARS bougies, exécutée à la bougie suivante (r_emperor_confirme)."""
    res = _sim(d, plan, decided_at, entry_until, exit_at, cost, order, last_price)
    if not res["triggered"]:
        return {"r_emperor": None, "r_emperor_alerte": None, "r_emperor_confirme": None}
    out = {"r_emperor": res["r_net"], "r_emperor_alerte": res["r_net"], "r_emperor_confirme": res["r_net"]}

    def early(k: int) -> float:
        return _sim(d, plan, decided_at, entry_until, min(exit_at, d.t[k + 1] + BAR), cost, order, last_price,
                    exit_reason_end="signal_sortie")["r_net"]

    k0 = bisect_left(d.t, res["filled_at"])
    m = bisect_left(d.t, res["closed_at"] - BAR)
    alert = confirmed = False
    for k in range(k0, min(m - 1, len(d.t) - 1)):
        if not (d.c[k] > res["fill_price"] and d.exit_signal(k)):
            continue
        if not alert:
            out["r_emperor_alerte"], alert = early(k), True
        q = next((q for q in range(k + 1, min(k + 1 + EXIT_CONFIRM_BARS, m, len(d.t) - 1)) if d.c[q] < d.l[k]), None)
        if q is not None:
            out["r_emperor_confirme"], confirmed = early(q), True
        if confirmed:
            break
    return out


def _gemini(d: Day, plan: dict, decided_at: int, entry_until: int, exit_at: int, cost: dict, order: str,
            last_price: float, res: dict) -> dict:
    """Sorties de la déclaration n° 6 (actions), chacune ajoutée seule à la gestion de référence : à la clôture
    de la bougie qui suit une clôture sous le VWAP à partir de 10 h 30 (r_vwap) ou un avalement baissier de
    SPY (r_spy) ; à 14 h 30 si le TP1 n'est pas touché (r_1430). Une règle qui ne se déclenche pas pendant
    que la position est ouverte laisse le résultat de référence. r_spy vaut None sans bougies de SPY."""
    out: dict = dict.fromkeys(STOCK_EXITS)
    if d.market != "stock" or not res["triggered"]:
        return out
    filled, closed = res["filled_at"], res["closed_at"]

    def cut(ts: int, reason: str) -> dict:
        return _sim(d, plan, decided_at, entry_until, min(exit_at, ts), cost, order, last_price,
                    exit_reason_end=reason)

    def after(ts: int | None, reason: str) -> float:
        """Règle vue à la clôture `ts` d'une bougie, position encore ouverte : sortie une bougie plus tard."""
        return cut(ts + BAR, reason)["r_net"] if ts is not None else res["r_net"]

    sig = None
    for k in range(bisect_left(d.t, max(filled, d.vwap_exit_ts - BAR)), len(d.t)):
        end = d.t[k] + BAR
        if end >= closed:
            break
        if d.vwap[k] is not None and d.c[k] < d.vwap[k]:
            sig = end
            break
    out["r_vwap"] = after(sig, "vwap")
    if filled < d.time_stop_ts < closed:
        early = cut(d.time_stop_ts, "time_stop")
        out["r_1430"] = res["r_net"] if early["tp_hits"] and early["tp_hits"][0] else early["r_net"]
    else:
        out["r_1430"] = res["r_net"]
    if d.index_down is not None:
        k = bisect_left(d.index_down, filled + 1)
        out["r_spy"] = after(d.index_down[k] if k < len(d.index_down) and d.index_down[k] < closed else None,
                             "indice")
    return out


def cost_variants(market: str, res: dict) -> dict:
    """n° 8, D2 : le trade exécuté `res` (sortie de référence) recompté autrement que par le simulateur.
    r_3c : stress de Gemini (3 cents de plus à l'entrée, de moins à chaque sortie au marché : stop, stop à
    l'entrée, clôture ; objectifs au prix de l'ordre ; commission IBKR par action), killmode.stressed_r.
    r_20e : à la mise de Simão (20 € de risque au change EURUSD_DEFAULT, un bracket IBKR par objectif avec son
    minimum par ordre, plan Fixe, glissement du labo), killmode.real_r. Actions seulement."""
    if market != "stock" or not res.get("triggered") or not res.get("exits"):
        return {"r_3c": None, "r_20e": None}
    from app.trading import killmode as K  # import tardif : killmode importe ce module

    o = res["order_prices"]
    call = {"entry": o["entry"], "stop": o["stop"], "targets": o["targets"] or [o["entry"]]}
    done = {"declenche": True, "remplissage": res["fill_price"], "eurusd": K.EURUSD_DEFAULT, "plan_ibkr": "fixe",
            "sorties": [{"part": e["part"], "prix": e["price"], "raison": e["reason"]} for e in res["exits"]]}
    return {"r_3c": K.stressed_r(call, done), "r_20e": K.real_r(call, done)}


def measure(d: Day, plan: dict, decided_at: int, entry_until: int, cost: dict, order: str, last_price: float,
            res: dict | None = None, emperor_plan: dict | None = None) -> dict:
    """Toutes les sorties et tous les tests d'un plan (ordre `order`) décidé à `decided_at`.
    `emperor_plan` : le plan aux objectifs EmperorBTC (par défaut le plan lui-même, cas de v1)."""
    exit_at = d.exit_at(decided_at)
    res = res or _sim(d, plan, decided_at, entry_until, exit_at, cost, order, last_price)
    out = {"triggered": int(res["triggered"]), "filled_at": res["filled_at"], "entry_mode": res["entry_mode"],
           "minutes_to_fill": res["minutes_to_fill"], "exit_reason": res["exit_reason"],
           "r_net": res["r_net"], "r_gross": res["r_gross"],
           "tp1_hit": int(bool(res["tp_hits"] and res["tp_hits"][0])), "mfe_r": res["mfe_r"], "mae_r": res["mae_r"]}
    if not res["triggered"]:
        return out
    entry, stop = plan["entry"], plan["stop"]
    out["r_sans_be"] = _sim(d, plan, decided_at, entry_until, exit_at, cost, order, last_price,
                            exit_mode="sans_be")["r_net"]
    out["r_2r"] = _sim(d, _plan(entry, stop, _r_targets(entry, stop, (2.0,))), decided_at, entry_until, exit_at,
                       cost, order, last_price)["r_net"]
    out["r_suiveur"] = _sim(d, plan, decided_at, entry_until, exit_at, cost, order, last_price,
                            exit_mode="suiveur", trail_r=TRAIL_R)["r_net"]
    out.update(_emperor(d, emperor_plan or plan, decided_at, entry_until, exit_at, cost, order, last_price))
    out.update(_gemini(d, plan, decided_at, entry_until, exit_at, cost, order, last_price, res))
    out["r_glisse_x2"] = SIM.net_r(res, cost, slip_factor=2.0)
    out["r_glisse_x3"] = SIM.net_r(res, cost, slip_factor=3.0)
    late = _sim(d, plan, decided_at + BAR, entry_until + BAR, exit_at, cost, order, last_price)
    out["r_retard"] = late["r_net"] if late["triggered"] else None
    out["r_manque"] = None if _missed(res, d) else res["r_net"]
    out.update(cost_variants(d.market, res))  # n° 8, D2
    return out


NO_WITNESS = {"temoin_r": None, "temoin_triggered": 0}


def witness(d: Day, key: tuple, risk_pct: float, multiples: list[float], cost_of, window: tuple[int, int],
            exit_mode: str = "v1") -> dict:
    """Entrées au marché à WITNESS_DRAWS bougies tirées au hasard dans `window` (indices), chacune avec le
    même stop en % et les mêmes objectifs en R que le trade, même sortie (v1, ou stop fixe pour la n° 7) ;
    temoin_r = leur moyenne."""
    lo, hi = window
    if hi <= lo or risk_pct <= 0 or any(m <= 0 for m in multiples):
        return NO_WITNESS
    rng = _seed(*key)
    picks = rng.sample(range(lo, hi), min(WITNESS_DRAWS, hi - lo))
    rs = []
    for k in picks:
        entry = d.o[k]
        stop = entry * (1 - risk_pct / 100)
        plan = _plan(entry, stop, _r_targets(entry, stop, multiples))
        decided = d.t[k]
        res = _sim(d, plan, decided, decided + BAR, d.exit_at(decided), cost_of(entry), "market", entry,
                   exit_mode=exit_mode)
        if res["triggered"] and res["r_net"] is not None:
            rs.append(res["r_net"])
    if not rs:
        return {"temoin_r": None, "temoin_triggered": 0}
    return {"temoin_r": round(sum(rs) / len(rs), 4), "temoin_triggered": 1}


def above_vwap(d: Day, decided_at: int) -> int | None:
    """n° 6 : 1 si la bougie de 5 min close à la décision finit au-dessus du VWAP de séance, 0 dessous,
    None avant l'ouverture (v1) ou sans bougie close à cet instant."""
    k = bisect_left(d.t, decided_at - BAR)
    if d.market != "stock" or k >= len(d.t) or d.t[k] + BAR != decided_at or d.vwap[k] is None:
        return None
    return int(d.c[k] > d.vwap[k])


def stop_before_entry(d: Day, plan: dict, decided_at: int, m: dict) -> int | None:
    """n° 6 : 1 si le stop du plan est touché entre la décision et la bougie d'exécution (exclue : l'ordre des
    prix y est inconnu) d'un ordre déclenché. L'ordre n'est pas annulé, au labo comme en direct : la colonne
    compte les « stop balayé puis cassure ». None sans exécution."""
    if not m.get("triggered") or m.get("filled_at") is None:
        return None
    k0, f = bisect_left(d.t, decided_at), bisect_left(d.t, m["filled_at"])
    return int(any(low <= plan["stop"] for low in d.l[k0:f]))


def _row(d: Day, symbol: str, strategy: str, slot: int, plan: dict, decided_at: int, m: dict, wit: dict,
         gap: float | None, rank) -> dict:
    entry, stop = plan["entry"], plan["stop"]
    ctx = kill_context(d) if d.market == "stock" else {}
    # v1 décide à 9 h 10, avant le volume des 15 premières minutes ; la n° 7 rejoue ses GO
    v2 = strategy != REFERENCE and strategy not in GO_STRATEGIES
    return {"symbol": symbol, "strategy": strategy, "slot": slot, "signal_at": decided_at,
            "entry": round(entry, 8), "stop": round(stop, 8), "risk_pct": round((entry - stop) / entry * 100, 4),
            **m, **wit, "gap_open_pct": gap, "rank": rank, "sur_vwap": above_vwap(d, decided_at),
            "atr_pct": _round(ctx.get("atr_pct")), "vol15_adv": _round(ctx.get("vol15_adv")) if v2 else None,
            "stop_avant_entree": stop_before_entry(d, plan, decided_at, m) if v2 else None}


def _round(x: float | None) -> float | None:
    return None if x is None else round(x, 3)


def strategy_rows(d: Day, symbol: str, rank=None, gap: float | None = None, names=None) -> list[dict]:
    """Une ligne par stratégie du marché : le premier signal dont l'ordre est exécuté, sinon le premier
    signal (non déclenché) ; rien si aucun signal."""
    if not d.usable:
        return []
    rows = []
    for name, spec in STRATEGIES.items():
        if d.market not in spec["markets"] or (names and name not in names) or "go" in spec:
            continue  # n° 7 : pas de signal, les lignes viennent des GO de v1 (go_rows)
        sd = d.frame(spec.get("tf"))  # bougies du signal (n° 4) ; simulation toujours en 5 min sur d
        if not sd.usable:
            continue
        chosen, busy_until = None, 0
        for cand in SIGNALS[spec.get("base", name)](sd):
            if cand["decided_at"] < busy_until:
                continue  # un ordre précédent était encore posé
            plan = _plan(cand["entry"], cand["stop"], _r_targets(cand["entry"], cand["stop"]))
            cost = SIM.costs(d.market, cand["entry"], rank)
            res = _sim(d, plan, cand["decided_at"], cand["entry_until"], d.exit_at(cand["decided_at"]), cost, "stop",
                       sd.c[cand["i"]])
            if chosen is None or res["triggered"]:
                chosen = (cand, plan, cost, res)
            if res["triggered"]:
                break
            busy_until = cand["entry_until"]
        if not chosen:
            continue
        cand, plan, cost, res = chosen
        emp = _plan(cand["entry"], cand["stop"], emperor_targets(sd, cand["entry"], cand["stop"]))
        m = measure(d, plan, cand["decided_at"], cand["entry_until"], cost, "stop", sd.c[cand["i"]], res, emp)
        risk_pct = (plan["entry"] - plan["stop"]) / plan["entry"] * 100
        # n° 6 : le témoin de Killmode tire ses entrées pendant la vie de son ordre (9 h 45-11 h), pas l'après-midi
        window = (d.first, bisect_left(d.t, cand["entry_until"]) if name == "killmode" else d.last)
        wit = witness(d, (d.market, symbol, d.day, name), risk_pct, list(TARGETS_R),
                      lambda p: SIM.costs(d.market, p, rank), window) if m["triggered"] else NO_WITNESS
        rows.append(_row(d, symbol, name, 0, plan, cand["decided_at"], m, wit, gap, rank))
    return rows


def v1_row(d: Day, symbol: str, plan: dict, decided_at: int, entry_until: int, last_price: float, cost: dict,
           res: dict | None = None, rank=None, gap: float | None = None, window: tuple[int, int] | None = None) -> dict:
    """v1 rejouée avec les sorties et tests du labo v2, sur ses propres objectifs (ordre limité)."""
    targets = [t["price"] for t in plan["targets"]][:3]
    p = _plan(plan["entry"], plan["stop"], targets)
    m = measure(d, p, decided_at, entry_until, cost, "limit", last_price, res)
    risk = plan["entry"] - plan["stop"]
    risk_pct = risk / plan["entry"] * 100 if plan["entry"] else 0
    multiples = [round((t - plan["entry"]) / risk, 4) for t in targets] if risk > 0 else []
    wit = witness(d, (d.market, symbol, d.day, REFERENCE, decided_at), risk_pct, multiples,
                  lambda px: SIM.costs(d.market, px, rank), window or (d.first, d.last)) if m["triggered"] else NO_WITNESS
    return _row(d, symbol, REFERENCE, decided_at, p, decided_at, m, wit, gap, rank)


# --- Déclaration n° 7 : confirmation des GO -------------------------------------------------

NO_TRADE = {"triggered": 0, "filled_at": None, "entry_mode": None, "minutes_to_fill": None,
            "exit_reason": "non_declenche", "r_net": None, "r_gross": None, "tp1_hit": 0, "mfe_r": None, "mae_r": None}


def confirm_event(d: Day, entry: float, stop: float, targets: list[float]) -> dict | None:
    """n° 7 : la règle du direct (confirm.evaluate, même code) sur les bougies de 5 min de la séance closes avant
    11 h 30 New York (le direct déclare le GO expiré dès 11 h 30, avant de lire la bougie qui s'y termine).
    Renvoie l'événement (confirmé, avec son déclencheur, ou annulé) ; None : expiré."""
    until = int(datetime.combine(d.day, CF.LAST_CHECK, CO.NEW_YORK).timestamp())
    go = {"status": "attente", "entry": entry, "stop": stop, "targets": targets, "last_bar": None}
    bars = [b for b in d.bars[d.d0:d.d1] if b["t"] + BAR < until]
    return CF.evaluate(go, bars, datetime.fromtimestamp(d.start_ts, timezone.utc))


def go_filter(d: Day, name: str, k: int) -> bool | None:
    """n° 8 : filtre FRSH lu à la clôture de la bougie de confirmation k ; None s'il ne peut pas être jugé.
    v2 : VWAP plus haut qu'il y a GO_FILTER_VWAP_BARS bougies et clôture ≥ VWAP + GO_FILTER_ATR × ATR 5 min ;
    v3 : clôture au-dessus du plus haut des bougies de la séance d'avant ;
    v4 : variation depuis l'ouverture meilleure que celle de SPY à la même bougie (pas d'ETF de secteur au labo)."""
    if not d.d0 < k < len(d.t):
        return None
    if name == "v2":
        j = k - GO_FILTER_VWAP_BARS
        if j < d.d0 or d.vwap[k] is None or d.vwap[j] is None or d.atr[k] is None:
            return None
        return d.vwap[k] > d.vwap[j] and d.c[k] >= d.vwap[k] + GO_FILTER_ATR * d.atr[k]
    if name == "v3":
        return d.c[k] > max(d.h[d.d0:k])
    if name == "v4":
        spy = (d.index_move or {}).get(d.t[k])
        if spy is None or d.o[d.d0] <= 0:
            return None
        return d.c[k] / d.o[d.d0] - 1 > spy
    raise ValueError(f"filtre {name} inconnu")


def index_move(index5: list[dict] | None, start_ts: int) -> dict[int, float] | None:
    """n° 8, V4 : variation de SPY depuis l'ouverture de la séance à la clôture de chaque bougie de 5 min."""
    bars = CO.regular_hours(sorted(index5 or [], key=lambda b: b["t"]))
    if not bars or bars[0]["t"] != start_ts or bars[0]["o"] <= 0:
        return None
    return {b["t"]: b["c"] / bars[0]["o"] - 1 for b in bars}


def go_measure(d: Day, plan: dict, decided_at: int, entry_until: int, cost: dict, order: str,
               last_price: float) -> dict:
    """n° 7 : la seule sortie déclarée (tiers aux objectifs, stop fixe, reste à la clôture : r_sans_be) et ses
    tests de robustesse ; r_net (gestion v1) n'est pas mesuré. Ordres au marché ou stop : rien à « manquer »."""
    exit_at = d.exit_at(decided_at)
    res = _sim(d, plan, decided_at, entry_until, exit_at, cost, order, last_price, exit_mode="sans_be")
    if not res["triggered"]:
        return dict(NO_TRADE)
    late = _sim(d, plan, decided_at + BAR, entry_until + BAR, exit_at, cost, order, last_price, exit_mode="sans_be")
    return {"triggered": 1, "filled_at": res["filled_at"], "entry_mode": res["entry_mode"],
            "minutes_to_fill": res["minutes_to_fill"], "exit_reason": res["exit_reason"], "r_net": None,
            "r_gross": res["r_gross"], "tp1_hit": int(bool(res["tp_hits"] and res["tp_hits"][0])),
            "mfe_r": res["mfe_r"], "mae_r": res["mae_r"], "r_sans_be": res["r_net"],
            "r_glisse_x2": SIM.net_r(res, cost, slip_factor=2.0), "r_glisse_x3": SIM.net_r(res, cost, slip_factor=3.0),
            "r_retard": late["r_net"] if late["triggered"] else None, "r_manque": res["r_net"],
            **cost_variants(d.market, res)}  # n° 8, D2


def go_plans(v1: dict | None) -> dict:
    """n° 7 : plans GO de v1 par stop : « origine » (plan du labo), « plancher » (plan du direct, seulement
    quand le générateur l'a calculé)."""
    if not v1:
        return {}
    out = {"origine": v1["plan"]} if v1.get("go") else {}
    floor = v1.get("plancher")
    if floor and floor.get("go"):
        out["plancher"] = floor["plan"]
    return out


def go_rows(d: Day, symbol: str, plans: dict, rank=None, gap: float | None = None) -> list[dict]:
    """n° 7 : pour chaque GO (par stop), une ligne par bras, avec le statut de la confirmation.
    Brut : achat au marché à l'ouverture si elle est au-dessus du stop. Confirmé : ordre stop d'achat au
    déclencheur, posé à la clôture de la bougie de confirmation, valable jusqu'à 11 h 30 New York."""
    if d.d1 <= d.d0:
        return []
    rows = []
    until = int(datetime.combine(d.day, CF.LAST_CHECK, CO.NEW_YORK).timestamp())
    k_until = bisect_left(d.t, until)
    for variant, plan in plans.items():
        suffix = "" if variant == "origine" else f"_{variant}"
        stop, targets = plan["stop"], [t["price"] for t in plan["targets"]][:3]
        if not targets or stop >= plan["entry"]:
            continue
        ev = confirm_event(d, plan["entry"], stop, targets)
        status = GO_STATUS.get(ev["kind"], "expire") if ev else "expire"
        opened, start = d.o[d.d0], d.t[d.d0]
        arms = {"brut": (_plan(opened, stop, targets), start, start + BAR, "market", opened, (d.first, d.last))}
        statuses = {"brut": status, "confirme": status}
        if status == "confirme":
            decided = ev["t"] + BAR
            arms["confirme"] = (_plan(ev["trigger"], stop, targets), decided, until, "stop",
                                d.c[bisect_left(d.t, ev["t"])], (d.first, max(d.first, k_until)))
        else:  # pas d'ordre : la ligne garde l'entrée du plan et l'heure où le GO est tombé
            arms["confirme"] = (_plan(plan["entry"], stop, targets), ev["t"] + BAR if ev else until, until, None,
                                None, None)
        for filt, name in GO_FILTERS.items():  # n° 8 : le même ordre, seulement si le filtre passe
            if STRATEGIES[name]["go"] != variant:
                continue
            arm = STRATEGIES[name]["arm"]
            ok = go_filter(d, filt, bisect_left(d.t, ev["t"])) if status == "confirme" else None
            statuses[arm] = status if status != "confirme" else "confirme" if ok else \
                "filtre" if ok is False else "indetermine"
            arms[arm] = arms["confirme"] if statuses[arm] == "confirme" else \
                (arms["confirme"][0], arms["confirme"][1], until, None, None, None)
        for arm, (p, decided, entry_until, order, last, window) in arms.items():
            name = f"go_{arm}{suffix}"
            entry = p["entry"]
            cost = SIM.costs("stock", entry, rank)
            m = (go_measure(d, p, decided, entry_until, cost, order, last) if order and entry > stop
                 else dict(NO_TRADE))
            wit = NO_WITNESS
            if m["triggered"]:
                risk = entry - stop
                wit = witness(d, (d.market, symbol, d.day, name), risk / entry * 100,
                              [round((t - entry) / risk, 4) for t in targets],
                              lambda px: SIM.costs("stock", px, rank), window, exit_mode="sans_be")
            rows.append(_row(d, symbol, name, 0, p, decided, m, wit, gap, rank) | {"confirmation": statuses[arm]})
    return rows


# --- Entrées du générateur ------------------------------------------------------------------

STOCK_WARMUP_BARS = 160  # deux séances : EMA 20, ATR 14 et impulsion du matin


def index_down(index5: list[dict] | None) -> list[int] | None:
    """n° 6 : heures de clôture des bougies de 5 min de SPY qui forment un avalement baissier avec la
    précédente (contiguë) ; None sans bougies de l'indice."""
    if not index5:
        return None
    bars = CO.regular_hours(sorted(index5, key=lambda b: b["t"]))
    return [cur["t"] + BAR for prev, cur in pairwise(bars)
            if cur["t"] - prev["t"] == BAR and I.engulfing(prev, cur) == "baissier"]


def stock_day(day: date, symbol: str, hist5: list[dict], session: list[dict], daily: list[dict],
              rank=None, v1: dict | None = None, index5: list[dict] | None = None) -> list[dict]:
    """Lignes du jour pour une action : v1 (si son plan existe) et les stratégies.

    `hist5` : bougies de 5 min avant la séance ; `session` : celles de la séance ; `daily` :
    journalier connu avant le jour ; `v1` : {plan, decided_at, entry_until, last_price, cost, result, go,
    plancher} (go et plancher : n° 7, voir go_plans) ; `index5` : bougies de 5 min de SPY pendant la séance
    (sortie r_spy, déclaration n° 6)."""
    bars = CO.regular_hours(hist5[-STOCK_WARMUP_BARS * 2:])[-STOCK_WARMUP_BARS:] + session
    d = Day("stock", day, bars, daily)
    d.index_down = index_down(index5)
    d.index_move = index_move(index5, d.start_ts)  # n° 8, V4
    prev = d.prev_close
    gap = round((session[0]["o"] / prev - 1) * 100, 3) if session and prev else None
    rows = strategy_rows(d, symbol, rank, gap)
    if v1 and d.d1 > d.d0:
        rows.append(v1_row(d, symbol, v1["plan"], v1["decided_at"], v1["entry_until"], v1["last_price"], v1["cost"],
                           v1.get("result"), rank, gap, (d.first, d.last)))
        rows += go_rows(d, symbol, go_plans(v1), rank, gap)  # n° 7
    return rows


def crypto_complete(day: date, now: int | None) -> bool:
    """Toutes les sorties du jour sont connues : signal au plus tard à 20 h UTC, horizon 24 h."""
    return now is None or now >= CO.utc_midnight(day) + (CRYPTO_SIGNAL_HOURS * 3600 + CRYPTO_HORIZON)


def crypto_day(day: date, symbol: str, bars5: list[dict], daily: list[dict], rank=None,
               v1s: list[dict] = ()) -> list[dict]:
    """Lignes du jour UTC pour une crypto : v1 (un par créneau de 4 h) et les stratégies."""
    start = CO.utc_midnight(day)
    lo = bisect_left([b["t"] for b in bars5], start - 86400)
    hi = bisect_left([b["t"] for b in bars5], start + 2 * 86400)
    d = Day("crypto", day, bars5[lo:hi], [b for b in daily if b["t"] < start])
    rows = strategy_rows(d, symbol, rank)
    for v in v1s:
        k0 = bisect_left(d.t, v["decided_at"])
        k1 = bisect_left(d.t, v["decided_at"] + 4 * 3600)
        rows.append(v1_row(d, symbol, v["plan"], v["decided_at"], v["entry_until"], v["last_price"], v["cost"],
                           v.get("result"), rank, None, (k0, k1)))
    return rows
