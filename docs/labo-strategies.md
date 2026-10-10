# Labo v2 : déclaration des stratégies (7 octobre 2026)

Ce document est écrit **avant** le premier résultat. Il fixe ce que le labo v2 mesure, pour que
personne (Simão, Claude, la routine du soir) ne puisse ensuite choisir après coup la stratégie ou la
sortie qui « aurait marché ». Ajouter une stratégie, changer un seuil ou une sortie = une nouvelle
déclaration datée dans ce fichier, jamais une retouche silencieuse. Code : `app/lab/strategies.py`.

## Pourquoi

v1 achète toutes les actions de l'univers à 9 h 10 New York (et chaque crypto toutes les 4 h) au dernier
cours connu, sans attendre de signal. Elle perd −0,23 R par trade, et sa note /10 ne trie rien. Ses
« setups » (contre-tendance, cassure de pivot…) sont des étiquettes posées après coup. Le labo v2 mesure
des stratégies qui **attendent leur déclencheur**, à côté de v1, sur les mêmes jours et les mêmes coûts.
Plan complet et council : `/mnt/project-files/labo/plan-labo-v2.md` (fichiers du projet).

## Règles communes

- Bougies de 5 min ; la décision tombe à la clôture de la bougie du signal ; l'exécution part de la
  bougie suivante (rien n'est vu de l'avenir).
- Achat seulement. Une position au plus par stratégie, actif et jour : le premier signal dont l'ordre
  est exécuté. Un ordre non exécuté reste posé `VALID_BARS` = 3 bougies, puis le signal suivant compte.
- Entrée par **ordre stop d'achat** 1 pas au-dessus du niveau (un ordre qu'on peut poser à l'avance
  chez IBKR), frais et glissement « taker ».
- Objectifs à 1, 2 et 3 R.
- Fenêtres : actions, signaux de 9 h 45 à 15 h New York, tout soldé à la clôture ; crypto, signaux de
  0 h à 20 h UTC, soldés 24 h après le signal.
- Coûts : ceux du simulateur v1 (glissement selon le prix ou le rang de volume, frais OKX).

## Stratégies

| Nom | Marchés | Déclencheur | Stop |
|---|---|---|---|
| `pullback` | actions, crypto | Tendance : clôture de la veille au-dessus de la MM 20 jours, EMA 20 (5 min) plus haute que 6 bougies avant, clôture au-dessus du VWAP. Le prix touche la zone (max de l'EMA 20 et du VWAP) à la bougie du signal ou la précédente, après une impulsion (un plus haut ≥ zone + 1 ATR 14 parmi les 12 bougies d'avant le contact, dans la séance pour les actions). Confirmation : bougie verte qui clôture au-dessus de l'EMA 20, du VWAP et du plus haut de la bougie précédente. Entrée 1 pas au-dessus de son plus haut. | Creux des 3 dernières bougies − 0,1 ATR |
| `orb15` | actions | Range des 15 premières minutes. Entrée 1 pas au-dessus du plus haut, ordre valable jusqu'à 12 h New York. | 1 pas sous le plus bas du range |
| `orb30` | actions | Même chose sur 30 minutes. | 1 pas sous le plus bas du range |
| `cassure_retest` | actions, crypto | Clôture au-dessus du plus haut de la veille ; dans les 12 bougies, retour au niveau (plus bas ≤ niveau + 0,2 %) avec clôture au-dessus. Entrée 1 pas au-dessus de la bougie du retest. | min(plus bas du retest, niveau) − 0,1 ATR |
| `v1` (référence) | actions, crypto | Plan v1 inchangé (heure fixe, ordre limité au dernier cours, objectifs sur les niveaux). | Celui de v1 |

## Sorties (une variable à la fois)

| Colonne | Sortie |
|---|---|
| `r_net` | Référence : un tiers à chaque objectif, stop à l'entrée après TP1 (gestion v1) |
| `r_sans_be` | Mêmes objectifs, le stop ne remonte pas |
| `r_2r` | Un seul objectif à 2 R |
| `r_suiveur` | Aucun objectif, stop suiveur 1 R sous le plus haut atteint |

**Essais déclarés : 32** (actions : v1 + 4 stratégies × 4 sorties ; crypto : v1 + 2 stratégies × 4 sorties).
Chacun compte, gagnant ou non, dans toute correction statistique future.

## Tests de robustesse (sur la sortie de référence)

- `r_glisse_x2`, `r_glisse_x3` : glissement multiplié par 2 et par 3 ;
- `r_retard` : ordre posé une bougie plus tard ;
- `r_manque` : un ordre limité (v1) traversé de moins de 0,1 % est compté comme non exécuté.

Depuis la déclaration n° 6, ces tests comptent dans la règle « candidate » (voir « Lecture des résultats »).

## Témoin aléatoire

Pour chaque trade déclenché : même jour, même fenêtre, même stop en %, mêmes objectifs en R, entrée au
marché à une bougie tirée au hasard (graine fixe, reproductible). L'écart stratégie − témoin, apparié
trade par trade, dit si l'**entrée** apporte quelque chose. Une stratégie qui ne bat pas son témoin n'a
pas d'avantage d'entrée, quelle que soit son espérance. Exception depuis la n° 6 : le témoin de `killmode`
tire ses entrées entre 9 h 45 et 11 h, la vie de son ordre (de même, depuis la n° 7, celui de `go_confirme`
entre 9 h 45 et 11 h 30, avec la sortie à stop fixe de ces bras).

## Lecture des résultats

- Classement par **espérance nette en R** avec son intervalle de confiance à 95 % en grappes par jour.
  Le taux de réussite est affiché, jamais utilisé pour classer.
- « Candidate » (✅) = borne basse de l'intervalle au-dessus de zéro, au moins 30 trades, et écart au
  témoin avec z ≥ 2 (relevé au seuil corrigé du nombre d'essais par la déclaration n° 2). Depuis la n° 6,
  il faut aussi que l'espérance de référence reste positive sous chaque test de robustesse (nets de la
  commission IBKR) et sur chaque moitié chronologique des jours. Ce n'est pas une promotion.
- Aucune promotion avant : au moins un an de rejeu couvrant au moins deux régimes de marché, puis une
  seule ouverture du bloc scellé, puis le forward-test.

## Déclaration n° 2 (7 octobre 2026, 19 h UTC) : supports et résistances de la formation EmperorBTC

À ce moment, la déclaration n° 1 avait mesuré un seul jour crypto ; le choix vient de la demande de
Simão, pas de ce résultat. Sa demande : que JARVIS établisse ses supports et résistances comme dans la
formation EmperorBTC et s'en serve pour l'entrée, la sortie et les confirmations. v1 calcule déjà des
niveaux (stop sous le dernier creux, objectifs sur les résistances), mais elle entre à heure fixe, sans
attendre la confirmation. Le challenger du labo ne fait que filtrer v1 : entrer sur les niveaux demande
donc de nouvelles stratégies, mesurées comme les autres.

**Ce que dit le chapitre S/R du manuel** (et que le code suit) : les supports et résistances sont des
zones, pas des lignes, tracées sur les creux et sommets de réaction en touchant le plus de points
possible ; une résistance cassée devient support ; entrer directement au contact se fait chasser
(« chasse aux stops ») ; d'où deux entrées : **solution 1** cassure, retest, confirmation ;
**solution 2** rebond sur le support, puis cassure d'une résistance au-dessus. Les chiffres de v1
vont dans son sens : le « rebond sur pivot » (entrée sans confirmation) perd −0,10 R en crypto
(6 447 trades), et toutes les étiquettes perdent en actions.

**Zones S/R** (`strategies.zones`), calculées une fois au début de la fenêtre des signaux, avec ce qui
est connu alors :

- creux et sommets de réaction (plus bas ou plus haut que les 3 bougies de chaque côté, comme v1) en
  5 min, confirmés avant la fenêtre (150 bougies pour les actions, soit deux séances ; 288 pour la
  crypto, soit 24 h), et en journalier sur les 40 dernières séances ;
- points pivots de la veille (P, S1-S3, R1-R3), plus haut et plus bas de la veille ;
- niveaux regroupés quand ils sont à moins de 0,5 ATR (5 min, dernière bougie avant la fenêtre) du
  premier niveau de la zone ; une zone n'est valide qu'avec **au moins 2 contacts** ;
- une zone n'a pas de rôle fixe : sous le prix elle est support, au-dessus résistance.

| Nom | Marchés | Déclencheur | Stop |
|---|---|---|---|
| `cassure_zone_retest` | actions, crypto | Solution 1. Clôture au-dessus d'une zone alors que la précédente était dessous ; dans les 12 bougies, retour sur la zone (plus bas entre bas de zone − 0,5 ATR et haut de zone + 0,1 ATR) avec clôture au-dessus. Stop d'achat 1 pas au-dessus de la bougie du retest, valable 3 bougies. | min(plus bas du retest, bas de zone) − 0,1 ATR |
| `rebond_confirme` | actions, crypto | Solution 2. Le prix, qui clôturait au-dessus d'une zone, la touche (même bande) et clôture sans la perdre (au-dessus du bas de zone) ; la plus haute zone touchée compte. Pas d'entrée au contact : stop d'achat 1 pas au-dessus du plus haut des 6 dernières bougies (la résistance la plus proche), valable 12 bougies, **annulé si le stop est touché avant**. | min(plus bas du contact, bas de zone) − 0,1 ATR |

**Sortie EmperorBTC** (`r_emperor`, sur toutes les stratégies et sur v1) : objectifs sur les prochaines
résistances (bas des zones au-dessus de l'entrée, 0,1 ATR avant, à au moins 0,5 R ; complétés jusqu'à
trois par les multiples de R suivants ; v1 garde ses propres objectifs), gestion v1 (un tiers par
objectif, stop à l'entrée après TP1), et sortie anticipée sur les signes de retournement du manuel
(`signals.exit_signals`, le code des alertes en direct : divergence RSI, anomalie de Weis, avance sous
le volume moyen, bougie de retournement avec volume) quand le trade est en gain. Soldée à la clôture de
la bougie qui suit le signal : c'est une alerte, Simão passe l'ordre à la main.

Pas repris : le trident du jour (Stoch RSI + VWAP + pivot), déjà mesuré par v1 (−0,07 R en crypto,
négatif en actions) ; l'entrée directe au contact d'un support (déconseillée par le manuel).

**Essais déclarés : 60** (actions : v1 + 6 stratégies × 5 sorties ; crypto : v1 + 4 stratégies × 5
sorties ; porté à 84 par la déclaration n° 3).

**Règle de lecture renforcée** (s'applique aussi à la déclaration n° 1, avant tout résultat) :
« candidate » exige désormais un écart au témoin avec **z ≥ seuil corrigé du nombre d'essais**
(Bonferroni unilatéral à 5 % : 2,96 pour 32 essais, 3,14 pour 60), en plus de la borne basse de
l'intervalle au-dessus de zéro et de 30 trades. Avec z ≥ 2 et 60 essais, on attendrait environ une
fausse candidate par hasard. Le tableau montre aussi les frais (brut − net) et l'espérance par tranche
de rang de volume, sans rien filtrer.

Les jours déjà traités sous la déclaration n° 1 sont recalculés avec toutes les stratégies par le
rattrapage (`strategies.progress_job` change avec le numéro de déclaration), pour que chaque stratégie
soit mesurée sur les mêmes jours.

**Council** (Sceptique, Pragmatique, Critique : GO modifié à l'unanimité). Retenu : pas de trident,
paramètres figés ici avant le premier rejeu, sortie codée une seule fois et exécutée une bougie plus
tard, seuil corrigé, zones calculées seulement avec ce qui est connu avant la fenêtre. Désaccord le plus
fort : le Sceptique jugeait la cassure de zone trop proche de `cassure_retest` (plus haut de la veille)
et voulait rester vers 40 essais ; gardée parce que c'est la solution 1 du manuel sur ses vraies zones,
le seuil corrigé payant le prix des essais en plus. Point à suivre : les frais pèsent lourd (v1
actions : brut −0,07 R, net −0,23 R ; crypto : brut +0,00 R, net −0,13 R) ; un stop serré en 5 min
les grossit en R.

## Déclaration n° 3 (7 octobre 2026, soir) : avis de GPT et Gemini sur la n° 2

La déclaration n° 2 était fusionnée mais pas encore déployée : **aucun de ses résultats n'existait**.
Simão a soumis la n° 2 à GPT et Gemini ; chaque objection a été vérifiée dans le code avant d'être
retenue ou écartée. Cette déclaration remplace la n° 2 là où elles diffèrent.

**Ce qui vient du manuel, ce qui est une formalisation de JARVIS.** Viennent du chapitre S/R : zones
plutôt que lignes, creux et sommets de réaction, résistance cassée devenue support, solution 1
(cassure, retest, confirmation) et solution 2 (rebond, puis cassure d'une résistance au-dessus), sortie
sur anomalie de Weis. Sont des choix de JARVIS pour rendre la méthode programmable, à juger comme des
hypothèses : 3 bougies de chaque côté pour un creux, 5 min + journalier, au moins 2 contacts, largeur
de 0,5 ATR, plus haut et plus bas de la veille, 12 bougies de retest, stop d'achat au-dessus de la
bougie de retest comme « confirmation ».

**Changements** (tous figés ici, avant le premier résultat) :

- **Zones : contacts distincts et réaction minimale.** Le détecteur de creux compte des plus bas égaux :
  six bougies collées au même plus bas faisaient six « contacts ». Désormais un contact est daté ; deux
  contacts à moins d'une heure, ou un creux de 5 min le même jour qu'un niveau journalier, n'en font
  qu'un. Un creux (sommet) de 5 min ne compte que si le prix s'en est éloigné d'au moins 1 ATR dans les
  12 bougies suivantes, connues avant la fenêtre. Les points pivots sortent des zones : ils viennent du
  trident, écarté par la n° 2.
- **`cassure_zone_retest`** : une clôture sous le bas de zone avant le retest annule la cassure (fausse
  cassure). La bougie suivante peut lancer une nouvelle cassure.
- **`rebond_confirme` : cassure de structure.** La résistance à casser est le dernier sommet de réaction
  confirmé à la clôture du contact (3 bougies de chaque côté, dans les mêmes 150 / 288 bougies), au lieu
  du plus haut des 6 dernières bougies, nombre arbitraire relevé par GPT et Gemini. L'ordre reste posé
  jusqu'à la fin de la fenêtre des signaux, annulé si le stop est touché avant.
- **Sortie EmperorBTC en trois variantes**, au lieu d'une :
  - `r_emperor` : objectifs sur les prochaines résistances et gestion v1, sans sortie sur signal
    (cibles dures, exécution immédiate : la proposition de Gemini) ;
  - `r_emperor_alerte` : plus une sortie à la clôture de la bougie qui suit le premier signe de
    retournement vu en gain (le comportement des alertes en direct) ;
  - `r_emperor_confirme` : le signe n'est qu'un avertissement (le manuel : un volume faible n'est « pas
    haussier », pas « baissier » ; une divergence se lit avec d'autres signes) ; sortie seulement si une
    clôture passe sous le plus bas de la bougie du signe dans les 6 bougies, exécutée à la bougie
    suivante (la proposition de GPT).
- **Témoin : moyenne de 5 tirages** par trade (même actif, jour, fenêtre, stop en %, objectifs en R),
  au lieu d'un seul, pour qu'un tirage chanceux ne fasse pas la comparaison.
- **Commission IBKR.** Le simulateur ne compte aucune commission en actions (modèle Alpaca), alors que
  Simão passe ses ordres chez IBKR Pro, plan Fixe : 0,005 $ par action, plafonné à 1 % de l'ordre. Pour
  les actions, le classement et la règle « candidate » se lisent donc sur l'espérance nette **après**
  cette commission (deux fois 0,005 $ par action, rapportés au risque par action). Le minimum de 1 $ par
  ordre dépend de la taille de position et n'est pas compté : l'estimation est un plancher. Exemple : un
  stop de 5 cents coûte 0,2 R de commission à lui seul.
- **Frais par tranche** : espérance par tranche de frais estimés avant l'entrée (glissement, frais et
  commission rapportés au risque : < 0,1 R, 0,1-0,2, 0,2-0,4, ≥ 0,4 R), sans rien filtrer.

**Essais déclarés : 84** (12 couples stratégie × marché × 7 sorties) ; seuil « candidate » contre le
témoin : **z ≥ 3,24** (Bonferroni unilatéral à 5 % : l'hypothèse est « la stratégie bat le témoin »,
pas « elle en diffère » ; en bilatéral ce serait 3,43).

**Déjà en place, gardé tel quel** : comparaison appariée stratégie − témoin trade par trade, intervalle
en grappes par jour (`strat_stats`) ; témoin qui recalcule son stop et ses objectifs depuis sa propre
entrée (GPT supposait le contraire) ; délai d'une bougie sur les sorties sur signal (Simão passe l'ordre
à la main, et sortir dans la bougie même du signal serait voir l'avenir) ; bloc scellé.

**Écarté, avec la raison** :

- filtre de contexte macro (CPI, tensions géopolitiques) : pas codable sans recul après coup, une
  douzaine de jours de CPI par an, et le contrôle catalyseur existe déjà ; l'espérance par régime de
  marché est déjà affichée ;
- entrée en ordre limité sur la zone pour payer moins de frais : c'est l'entrée au contact que le
  manuel déconseille, v1 entre déjà ainsi et fait pire en actions (ordres limités −0,28 R contre
  −0,21 R à l'ouverture), et les frais « maker » d'OKX (0,08 %) restent un coût ; IBKR Fixe facture
  pareil dans les deux cas ;
- plusieurs largeurs de zone en variantes : chaque variante abaisse la puissance du test (plus
  d'essais) ;
- plafond de frais en R ou concentration sur les valeurs les plus volatiles : idée gardée, mais d'abord
  mesurée par les tranches de frais ; si elle tient, ce sera un challenger « v1 + filtre » déclaré, à
  faire valider par Simão (il écarterait surtout les petites valeurs à stop serré).

Les jours déjà traités sont recalculés par le rattrapage (`progress_job` change avec le numéro).

**Council** (Sceptique, Pragmatique, Critique : GO à l'unanimité). Sceptique : chaque règle ajoutée est
un degré de liberté, acceptable parce qu'elles sont toutes figées avant le premier résultat et payées par
le seuil à 3,24. Pragmatique : surcoût mesuré d'environ 5 % par jour rejoué ; une seule mise en service
pour les n° 2 et 3. Critique : la commission IBKR rend la barre plus haute pour les actions, et c'est
voulu : un avantage qui ne survit pas aux frais réels de Simão n'en est pas un.

## Déclaration n° 4 (8 octobre 2026) : stops plus larges, lot B du plan

Contrairement aux n° 2 et 3, celle-ci vient **après** un premier résultat : le tableau de la n° 3 au
8 octobre (oct.-déc. 2024 seulement, 64 jours d'actions, 62 de crypto, régime haussier). Aucune candidate ;
avant frais tout est proche de 0 R ; l'espérance baisse avec la tranche de frais (≥ 0,4 R de frais : −0,5 à
−0,96 R), et 74 % des cassures de zone crypto paient au moins 0,4 R de frais, parce qu'un stop de 5 min est
étroit. La cassure de zone crypto est la seule stratégie qui bat son témoin (+0,16 R, z 2,9, sous le seuil
3,24). Le choix de la stratégie rejouée ci-dessous s'appuie donc sur ces trois mois : c'est un biais assumé,
que corrigent le reste du rejeu (encore 21 mois), la règle « au moins un an et deux régimes » et le bloc
scellé, toujours fermé. Simão a choisi ce lot sur la carte du 8 octobre (« Lot B réduit »).

| Nom | Marchés | Ce qui change |
|---|---|---|
| `cassure_zone_retest_15m` | actions, crypto | `cassure_zone_retest` (règles de la n° 3, inchangées) lue sur des bougies de 15 min |
| `cassure_zone_retest_1h` | crypto | la même sur des bougies de 1 h |

- Zones, cassure, fausse cassure, retest, ordre stop d'achat et stop se calculent sur les bougies longues
  (agrégées depuis les 5 min, alignées sur 9 h 30 pour les actions, sur l'heure UTC pour la crypto) : ATR,
  12 bougies de retest, ordre valable 3 bougies, tout en bougies de 15 min ou 1 h. Le stop se place donc
  sous la bougie de retest de 15 min ou 1 h : plus large, moins de frais en R. L'historique des zones couvre
  la même durée qu'en 5 min (150 bougies de 5 min = 50 de 15 min).
- Une bougie longue n'est utilisée qu'une fois close : la décision tombe à sa clôture, entre 9 h 50 et 15 h
  New York (actions) et entre 0 h 05 et 20 h UTC (crypto), comme en 5 min.
- Simulation, sorties, robustesse et témoin restent sur les bougies de 5 min, identiques aux autres
  stratégies ; le test « entrée retardée » garde un retard de 5 min (le délai d'un ordre passé à la main).
- Pas de 1 h en actions : le labo garde deux séances d'historique de 5 min, soit 13 bougies de 1 h, trop
  peu pour l'EMA 20 et les zones.

**Essais déclarés : 105** (84 + 3 couples × 7 sorties) ; seuil « candidate » : **z ≥ 3,30**.

**Mesures sans essai en plus** (rien n'est filtré ni noté, comme les tranches de frais) :

- crypto : espérance de chaque stratégie par tranche de **funding** (dernier taux réglé avant le signal) et
  de **variation d'open interest sur 24 h** (dernier point de 4 h clos avant le signal), lus sur le
  perpétuel USDT de Bybit (`app/lab/derivs.py` ; historique public sans clé, OKX n'en garde que 3 mois). Une
  tranche qui tiendrait deviendrait un challenger déclaré, à faire valider par Simão ;
- actions : **potentiel +10** (`app/lab/potentiel.py`, `!potentiel`) : part des séances de l'univers du labo
  qui touchent +8 % (et +10 %) depuis l'ouverture, par tranche de gap d'ouverture, et fiches catalyseurs
  classées par type. Le scanner affiche la ligne de son gap sous chaque plan. Limite au 8 octobre : Finnhub
  gratuit ne remonte qu'environ 12 mois et le rejeu en est au début 2025 : presque toutes les fiches sont
  « hors profondeur » et une trentaine seulement sont classées ; les types se rempliront quand le rejeu atteindra les 12 derniers mois.

Les jours déjà traités sont recalculés par le rattrapage (`progress_job` change avec le numéro).

**Council** (Sceptique, Pragmatique, Critique : GO modifié). Sceptique : choisir la stratégie sur trois mois
de résultats, c'est chercher le gagnant après coup ; accepté parce qu'on n'ajoute qu'une stratégie, payée
par le seuil (3,30) et jugée sur 21 mois qu'elle n'a pas vus, et parce que les filtres funding et open
interest restent des mesures, pas des essais. Pragmatique : deux stratégies de plus coûtent peu (les
bougies longues viennent des 5 min déjà chargées) ; le rattrapage refait les jours déjà traités, comme à la
n° 3. Critique : pas de 1 h en actions plutôt que de rallonger l'historique (et les appels Alpaca) du rejeu ;
le témoin reste celui des autres stratégies, pour que l'écart mesure bien l'entrée.

## Déclaration n° 5 (8 octobre 2026) : JARVIS Killmode

Demande de Simão : une version parallèle de JARVIS, « Killmode », pour les trades à haute volatilité, qui ne
propose que des setups complets (catalyseur, lecture de graphique, données complètes, fiche société), qu'il
allume lui-même, hors de toute routine, et qui a sa place au labo. Le code en direct est
`app/trading/killmode.py` (`!kill …` sur Discord, éteint par défaut). Aucun résultat n'a orienté ces
paramètres : ils sont posés avant le premier rejeu de la stratégie.

Killmode a deux parties, mesurées chacune là où elle peut l'être sans voir l'avenir :

| Nom | Marchés | Règle |
|---|---|---|
| `killmode` | actions | Jour retenu si le gap à l'ouverture est d'au moins **+4 %** et l'ATR 14 jours connu la veille d'au moins **4 %** du cours. Range des 15 premières minutes : sa dernière bougie clôture au-dessus du VWAP de la séance et son plus bas reste au-dessus de la clôture de la veille (gap pas comblé). Ordre stop d'achat 1 pas au-dessus du plus haut du range, valable jusqu'à **11 h New York** ; stop 1 pas sous le plus bas du range ; objectifs 1, 2 et 3 R. |

- **Rejeu** : la règle de prix ci-dessus, comme stratégie du labo v2 (mêmes sorties, tests de robustesse,
  témoin, commission IBKR). La même fonction (`strategies.killmode_check`) sert en direct : ce que Simão reçoit
  et ce que le labo mesure ne peuvent pas diverger.
- **Journal des appels** : le catalyseur (actu positive de moins de 48 h ou résultats battus, sans
  augmentation de capital ni résultats dans les 24 h) et la fiche société ne se rejouent pas, Finnhub gratuit
  ne remontant qu'un an. Chaque 🔪 KILL proposé en direct est noté (`data/killmode_calls.json`) et soldé après
  la clôture par `!kill bilan` avec le simulateur du labo : ordre stop d'achat posé à l'heure de l'appel,
  sortie v1, soldé à la clôture, net de la commission IBKR. Lecture à partir de 30 appels soldés.
- En direct, un setup dont le prix est déjà à plus de 0,5 R au-dessus de l'entrée n'est pas proposé (on ne
  court pas après) ; le journal mesure ce qui a été proposé, à l'heure où il l'a été.

**Essais déclarés : 112** (105 + 1 couple × 7 sorties) ; seuil « candidate » : **z ≥ 3,32**.
Les jours déjà traités sont recalculés par le rattrapage (`progress_job` change avec le numéro).

**Council** (Sceptique, Pragmatique, Critique : GO modifié). Sceptique : « setup complet » ne veut pas dire
« setup gagnant » ; la note composite de v1, sans sa part catalyseur, n'a rien prédit (GO ≈ NO GO ≈
−0,24 R), donc Killmode est une hypothèse à mesurer, pas un avantage acquis. Pragmatique : une seule stratégie
de plus (7 essais), qui réutilise bougies, simulateur et témoin ; le rattrapage refait les jours déjà traités,
comme à la n° 4. Critique : la tonalité des actus est lue par mots-clés et l'univers du labo compte peu de
petites valeurs, où les gaps de +4 % sont les plus fréquents : le rejeu verra moins de trades Killmode que la
réalité, d'où le journal des appels en direct pour juger le mode complet.

**Corrections du 8 octobre (fil « JARVIS Killmode », écrites avec la n° 6, avant tout résultat de Killmode)** :
- « Aucun résultat n'a orienté ces paramètres » : vrai pour la règle de Killmode elle-même, faux pour son
  squelette. Son entrée et son stop sont ceux d'`orb15`, lue sur 64 jours avant de le choisir, et le seuil de
  gap a été posé après la lecture du potentiel +10 (70 jours). D'où le jugement sur les jours suivants (n° 6).
- « Ce que Simão reçoit et ce que le labo mesure ne peuvent pas diverger » : faux au moment de la n° 5. Le
  direct exigeait 51 séances d'historique contre 15, triait d'abord sur un ATR simple et sur le gap du moment
  (clôture IEX), et lit Twelve Data au lieu d'Alpaca SIP. Les trois premiers écarts sont corrigés dans la PR
  du fil Killmode ; le direct lit toujours Twelve Data (bougies en temps réel), noté dans chaque appel du
  journal.
- « La note composite de v1, qui compte déjà le catalyseur » : vrai en direct, faux au rejeu, où la note est
  calculée sans sa part catalyseur (`app/lab/status.py`, limites).
- Fait à écrire : l'ordre stop d'achat n'est pas annulé si le stop est touché avant l'entrée, au labo
  (`simulator.py`) comme en direct. La colonne `stop_avant_entree` de la n° 6 compte ces cas.
- Règle de lecture du journal des appels (codée dans `!kill bilan`, PR du fil Killmode) : à 30 appels 🔪 KILL
  déclenchés et soldés, intervalle à 95 % en grappes par jour. Borne haute sous zéro : « JARVIS recommande
  d'éteindre Killmode ; à toi de décider ». Borne basse au-dessus de zéro : on continue. Sinon on continue
  jusqu'à 100 appels ; à 100 sans écart démontré à zéro, JARVIS dit qu'il ne voit pas d'avantage et Simão
  décide. Les appels sont comptés par règle (`killmode.RULE` et seuils) : une déclaration qui change la règle
  de Killmode passe `killmode.RULE` à son numéro. Le journal tient aussi un groupe « témoin catalyseur »
  (mêmes piliers sauf le catalyseur), seule mesure possible de l'apport du catalyseur, qui ne se rejoue pas.
- Règle de lecture revue le 8 octobre (avis de GPT et Gemini passés au council, fil « JARVIS Killmode »),
  avant tout KILL soldé (aucun ne pouvait l'être avant 16 h 20 New York ce jour-là) :
  - le chiffre qui décide est le R aux frais réels de Simão : 20 € de risque, un bracket IBKR par objectif
    (une entrée et une sortie, chacune avec son minimum), glissement du labo d'au moins un demi-pas par côté ;
  - au 30e KILL déclenché, un audit : borne haute de l'IC 95 % sous zéro → « JARVIS recommande d'éteindre » ;
    sinon « pas d'alerte », jamais un verdict positif ;
  - au 100e, la lecture finale, toujours faite : avantage démontré seulement si la borne basse tient en grappes
    par jour, par semaine et avec au moins 3 c de glissement par côté ; si l'alerte du 30e a été écartée, le pari se
    mesure sur les KILL 31 à 100 ;
  - une borne de dégât à chaque bilan dès 10 KILL : IC 99,9 % entièrement sous zéro → « JARVIS recommande
    d'éteindre » (environ 2 % de fausse alerte sur la vie du journal à espérance nulle, simulé) ;
  - IC en t à G − 1 degrés de liberté (G = séances) ; lots figés (les premiers KILL dans l'ordre des appels) ;
  - le journal ne voit qu'une catastrophe ou un gros écart (à 100 KILL, environ ±0,3 R en absolu et ±0,5 R
    pour l'écart au témoin) : l'apport du catalyseur se juge au labo, le journal juge l'exécution ;
  - les appels notés avant ce changement sont d'une version à part du direct, affichés et jamais lus par la
    règle.
- Méthode du solde n° 2 (8 octobre, second avis de GPT et Gemini, fil « JARVIS Killmode »), écrite avant tout
  solde : aucun `!kill bilan` n'avait encore tourné. Elle ne touche ni la règle de prix, ni les piliers, ni la
  règle de lecture :
  - toute la séance est soldée sur les bougies SIP d'1 min non ajustées, en une requête, gardées sur disque
    (`data/killmode_bars`) ; un changement de méthode re-solde tout le journal sur les mêmes prix
    (`killmode.SETTLE_METHOD`), et l'ancien solde est gardé. La méthode 1 n'a jamais soldé d'appel : un appel
    fait dans la dernière minute d'une bougie de 5 min demandait ses minutes avec un début après la fin ;
  - l'ordre stop d'achat est actif à la première minute pleine 60 s après l'appel, pour chaque KILL et chaque
    témoin ;
  - une minute dont l'ordre des prix est inconnu et change l'issue est jouée dans le pire ordre, et c'est ce
    solde qui décide : stop avant objectif, plus bas de la minute d'entrée après l'exécution, un seul objectif
    avant un retour à l'entrée ; un objectif traversé dans la minute d'entrée compte (elle ouvre sous l'entrée,
    son plus haut vient après) ; un objectif posé s'exécute à son prix, et au prix d'entrée s'il est déjà
    dépassé quand le bracket est posé. Le meilleur ordre est affiché (`r_fav`), avec la part des KILL qui
    changent selon l'ordre ; une alerte qui ne tient plus dans le meilleur ordre le dit, sans changer la règle ;
  - le R aux frais réels compte le gain bracket par bracket en actions entières, comme les frais ; ce qui reste à
    la clôture est vendu en un seul ordre (TP et SL annulés d'abord), donc un seul minimum ;
  - « avec 3 c de glissement par côté » se calcule à la taille de Simão (mêmes brackets et minimums IBKR) ;
  - l'écart au labo (`r_ibkr`) rejoue l'appel comme le labo : bougies de 5 min dès la suivante, résolution du
    labo (`simulator.simulate`, `resolution="v1"`, inchangée) ;
  - l'écart KILL − témoin propre mesure la valeur prédictive du filtre catalyseur à règle de prix égale, pas
    l'effet causal d'une actu.
- Méthode du solde n° 3 (8 octobre, troisième avis de GPT et Gemini, fil « JARVIS Killmode »), écrite avant tout
  solde : aucun `!kill bilan` n'avait encore tourné. Elle ne touche ni la règle de prix, ni les piliers, ni la
  règle de lecture, ni le labo (`resolution="v1"` inchangée) ; tout le journal est re-soldé sur les mêmes
  bougies (`SETTLE_METHOD = 3`) :
  - les minutes qui décident d'un chemin (déclenchement de l'achat, sortie par un stop, minute ambiguë) sont
    relues sur les transactions et le NBBO SIP d'Alpaca et gardées sur disque (`data/killmode_ticks`) ; chaque
    transaction devient une bougie o = h = l = c ;
  - ne déclenchent que les transactions qui font un dernier cours chez IBKR (méthode « Last », le défaut des
    actions US) : conditions de vente qui ne font pas de dernier cours écartées (lot incomplet, prix moyen, hors
    séquence, heures étendues…), NBBO en vigueur valide et contenant la transaction à 0,5 % près. Les transactions
    sont prises telles que diffusées : une transaction annulée après coup compte (IBKR l'a vue avant l'annulation),
    une correction insérée après coup ne compte pas ;
  - l'ordre au marché part après la transaction qui le déclenche : l'achat se fait au pire de l'ask en vigueur et
    de l'ask suivant (jamais sous la transaction), le stop au pire des deux bids (jamais au-dessus) ; un objectif
    déjà dépassé quand le bracket est posé part au bid, jamais sous son prix. Un prix mesuré ne reçoit pas le
    demi-pas de glissement ; les 3 c du stress s'y ajoutent ;
  - une minute restée sans transactions (refus d'Alpaca) est jouée dans **tous** les ordres possibles de ses
    minutes ambiguës (jusqu'à 256 chemins, au-delà c'est dit), dont « objectifs puis retour à l'entrée » pour une
    minute d'entrée touchée des deux côtés ou un objectif suivi d'un retour à l'entrée. Les chemins sont classés
    sur le chiffre qui décide (R aux frais réels, prix mesurés) : le pire décide, le meilleur est affiché. Le pire
    ordre minute par minute n'était pas le pire résultat (GPT : l'autre ordre peut garder la position ouverte
    jusqu'à un gap). La minute est retentée à chaque bilan pendant 7 jours après la séance ; au-delà, le bilan
    la dit jamais lue ;
  - un objectif doit être dépassé d'un pas de son propre prix, comparé en pas entiers (en flottants, 16,01 + 0,01
    dépasse 16,02, et un plus haut à 16,02 ne comptait pas) ; une minute qui ouvre au-delà d'un objectif
    l'exécute avant tout le reste de la minute (au prix de l'objectif, ou au prix d'entrée s'il était déjà dépassé
    à l'achat) ; une clôture de minute à l'entrée ou sous elle après un objectif rend le retour à l'entrée
    certain, dans tous les ordres ;
  - le bilan affiche le résultat avec des objectifs exigés à 2 pas au-delà (`r_2t`, stress de Gemini sur les
    exécutions partielles) à côté de « avec 3 c », la part des KILL au prix mesuré, le glissement mesuré à
    l'entrée et au stop, et les minutes encore à lire ;
  - non modélisé : le plancher de prix des ordres au marché d'IBKR (une suite d'ordres limites, non publiée), une
    suspension qui dure jusqu'à la clôture (marquée « données incomplètes » quand la séance n'a plus de bougie
    dans sa dernière demi-heure ; deux dernières minutes depuis la méthode 4), le prix du fixing de clôture.
- Méthode du solde n° 4 (9 octobre, quatrième avis de GPT, audit des scénarios « pessimistes », fil « JARVIS
  Killmode »). Écrite après le premier `!kill bilan` (9 octobre vers 1 h 45 UTC, méthode 3) mais sans en voir
  les chiffres. Elle ne touche ni la règle de prix, ni les piliers, ni la règle de lecture, ni le labo
  (`resolution="v1"` et l'empreinte des coûts inchangées). Tout le journal est re-soldé sur les mêmes bougies
  et transactions (`SETTLE_METHOD = 4`) :
  - l'ordre est actif à la première minute pleine 60 s après l'**envoi** du message (`envoye_a`, à la seconde
    supérieure), pas après le début de l'analyse ; les appels notés avant gardent `at` ;
  - sur la transaction qui remplit l'achat, un objectif que le prix a déjà dépassé, ou que le bid atteint, est
    exécuté sur-le-champ si le pire bid de l'ordre au marché (cotation en vigueur et suivante) l'atteint ; si aucun
    ne l'atteint, il reste posé et s'exécute quand le prix le dépasse ensuite d'un pas (avant : il partait sur
    cette transaction même, jamais sous son prix, et une transaction pile à l'objectif ne l'exécutait pas) ; si une
    seule des deux cotations l'atteint, ou si aucune cotation n'est lue, les ordres liés deviennent actifs sous
    l'une ou l'autre : les deux chemins sont joués (minute « objectif_pose ») et le pire décide. Exécuté sur cette
    transaction, il est au prix mesuré (le bid, jamais sous son prix ni au-dessus de la transaction), et le stop
    remonté à l'entrée attend une transaction suivante ;
  - après deux bougies qui commencent à 5 min ou plus l'une de l'autre (une pause LULD de 5 min laisse au moins
    4 minutes pleines sans transaction), si la minute de la reprise a une transaction de réouverture (condition
    5, ou O), c'était une suspension : les cotations d'avant la première transaction de cette minute sont celles
    de la pause, sans exécution possible, et ne donnent plus le prix de l'ordre au marché qui part à la reprise (une seule cotation
    large pendant la pause pouvait inventer des dizaines de R de perte, ou un achat très haut) ; le déclenchement
    les lit encore, comme IBKR. Sans transaction de réouverture, l'action était seulement calme : son NBBO était
    exécutable et le prix reste le pire des deux cotations ;
  - un trade ouvert à la clôture sans bougie dans ses deux dernières minutes est « données incomplètes »
    (avant : la dernière demi-heure ; une pause LULD des dix dernières minutes ne rouvre pas) ;
  - « avec 3 c » devient « avec au moins 3 c » (1 % du prix sous 1 $, inchangé) et n'est jamais plus doux que
    le chiffre qu'il éprouve : sur un prix non mesuré au marché, le glissement du labo s'il dépasse 3 c
    (au-dessus de 100 $), 3 c en plus d'un prix mesuré ; les objectifs gardent le glissement du chiffre sans
    stress (avant : au prix de l'ordre), ce qui change r_3c de tout trade sorti sur un objectif. Le coût affiché
    dans le message d'un KILL suit la même règle ;
  - l'écart KILL − témoin propre a un IC en grappes par jour communes aux deux groupes (un jour de marché pèse
    sur les deux), correction et t au plus petit nombre de jours des deux groupes (avant : deux groupes
    indépendants) ; un groupe dont tous les ordres ont expiré (0 R partout) ne supprime plus l'IC ;
  - diagnostics comptés, sans changer le chiffre : trous (4 minutes pleines ou plus sans transaction, jusqu'à
    l'expiration de l'ordre s'il n'y a plus de bougie) pendant que l'ordre ou la position vit, sorties déclenchées
    entre la transaction qui déclenche l'achat et la cotation suivante (la fin de sa minute sans cotation lue ;
    TP et SL liés peut-être pas encore actifs : IBKR ne les active qu'une fois l'achat rempli), achats
    plus gros que la taille de l'ask retenu, et une ligne des indéterminés par groupe (insoldables, en attente,
    données incomplètes, minutes non lues, trous) ;
  - le pire de tous les ordres a été vérifié par deux oracles indépendants (plus de 200 000 séquences, à
    plusieurs tailles, prix mesurés et non mesurés) : aucun désaccord, contre-exemple A de GPT compris. Le test
    `tests/test_killmode_oracle.py` en garde un échantillon (bougies d'1 min sans transactions lues) ;
  - les messages suivent le journal : stop d'achat en GTD jusqu'à 11:00:00 heure de New York (SL et TP liés en
    ordres du jour), vente du reste au marché à la dernière minute de la séance après avoir annulé TP et SL ;
  - toujours non modélisés : exécutions partielles et carnet au-delà du meilleur ask, état limite LULD avant
    une pause (les transactions à la bande sont lues, la pause est comptée comme trou), fixing de clôture.

## Déclaration n° 6 (8 octobre 2026) : trois règles de sortie de Gemini, actions

Demande de Simão (carte du 8 octobre, 00 h 44 UTC, « Les tester », fil « Avis sur un actif et positions
ouvertes ») : mesurer trois règles que Gemini présente comme acquises, avant que JARVIS en utilise une.
Ce qu'on en sait avant de mesurer : Gemini cite une baisse de 15 à 20 % du taux de réussite sous le VWAP
sans source, et justifie le time stop par « ±0,03 R, comme tu l'as mesuré », qui est l'écart entre nos
sorties, pas le résultat de garder après 14 h 30 ; GPT a retiré ces deux règles faute de preuve. Au tableau
de la n° 3 (octobre-décembre 2024), les sept sorties finissent à moins de 0,05 R les unes des autres en
actions. Demande complète : `/mnt/project-files/labo/demande-tests-regles-gemini.md`.

Chaque règle s'ajoute **seule** à la gestion de référence (`r_net` : un tiers à chaque objectif, stop à
l'entrée après TP1), sur les mêmes trades, pour toutes les stratégies actions et v1 :

| Colonne | Règle | Exécution |
|---|---|---|
| `r_vwap` | Une bougie de 5 min close à partir de 10 h 30 New York finit sous le VWAP de séance : sortie de toute la position | à la clôture de la bougie suivante |
| `r_1430` | Position encore ouverte à 14 h 30 New York sans TP1 touché : sortie | à 14 h 30 |
| `r_spy` | Avalement baissier de SPY en 5 min (bougie rouge dont le corps englobe celui de la verte précédente) | à la clôture de la bougie suivante |

- Une bougie de retard pour le VWAP et SPY : ce sont des alertes, Simão passe l'ordre à la main (même
  convention que `r_emperor_alerte`). Le time stop est une heure connue d'avance : pas de retard.
- Une règle qui ne se déclenche pas pendant que la position est ouverte laisse le résultat de `r_net`.
- Pas de condition de volume sur le VWAP : le « volume vendeur » de Gemini ne se lit pas sur des bougies
  OHLCV, et une variante de plus coûterait des essais. SPY et pas QQQ : c'est déjà l'indice du labo, et en
  prendre un seul évite de choisir après coup. Les bougies de 5 min de SPY de la séance sont collectées en
  plus (une requête Alpaca par jour) ; un jour sans elles, `r_spy` n'est pas mesuré.
- Non testé : la sortie forcée à 14 h 30 sans condition (GPT) ; la « sortie à la clôture si ni stop ni
  objectif » est déjà la règle du labo (tout est soldé à la clôture).

**Jugement, fixé avant le premier résultat** : pour chaque stratégie, écart apparié de la règle à `r_net`,
trade par trade, avec son z en grappes par jour. La règle « aide » si l'écart est positif avec z au-dessus
du seuil « candidate » et au moins 30 trades. Rien ne change dans JARVIS en direct avant la règle de
promotion habituelle (un an de rejeu, deux régimes, une ouverture du bloc scellé, le forward-test).

**Essais déclarés : 139** (112 + 9 couples actions × 3 sorties, Killmode compris) ; seuil « candidate » : **z ≥ 3,38**.

**Mesure sans essai** : le filtre d'entrée VWAP que GPT propose (pas d'entrée avant 10 h 30, puis seulement
au-dessus du VWAP). La position de la décision par rapport au VWAP est gardée (`sur_vwap`) et le tableau
donne l'espérance de référence par tranche : décision avant 10 h 30, après au-dessus du VWAP, après en
dessous (v1, décidée à 9 h 10, est toujours « avant »). Tester le filtre comme essai aurait doublé les
stratégies actions ; une tranche qui tiendrait deviendrait une déclaration à part.

Les jours déjà traités sont recalculés par le rattrapage (`progress_job` change avec le numéro).

### Mesure de Killmode, sans essai de plus (fil « JARVIS Killmode », 8 octobre)

Le fil Killmode a passé au council les astuces de Gemini et de GPT (GO modifié) : aucune règle de prix ne
change, aucune stratégie ni sortie de plus, donc toujours **139 essais**. Ce qui suit rend la mesure de
Killmode décomposable. Elle part avec la n° 6 : une colonne ajoutée plus tard coûterait un rattrapage complet
de plus (toutes les bougies de 5 min relues à la source).

- **Trois colonnes** sur chaque ligne actions, sans rien filtrer : `atr_pct` (ATR 14 jours de la veille en %
  du cours, v1 comprise), `vol15_adv` (volume des 15 premières minutes rapporté au volume journalier moyen des
  20 séances d'avant, connu à 9 h 45, donc pas pour v1 décidée à 9 h 10) et `stop_avant_entree` (1 si le stop
  est touché entre la décision et la bougie d'exécution exclue, sur un ordre stop déclenché : le « stop
  balayé puis cassure » que l'ordre de Killmode, valable jusqu'à 11 h sans annulation, achète déjà ; c'est
  l'idée « piège d'ouverture » de Gemini, mesurée sans essai). Crypto : vides.
- **Témoin de Killmode** limité à la vie de son ordre (entrées tirées entre 9 h 45 et 11 h) : son ordre
  expire à 11 h, un témoin tiré jusqu'à 15 h mêlerait l'effet du signal et celui de l'heure. Son z ne se
  compare donc plus à celui d'`orb15` (voté 2 contre 1, le Critique voulait garder le témoin commun).
- **Jours suivants** : `killmode`, `cassure_zone_retest_15m` et `cassure_zone_retest_1h` ne sont jugées que
  sur les jours après le **27 janvier 2025** (actions) et le **3 mars 2025** (crypto), derniers jours rejoués
  affichés par le labo quand elles ont été choisies (8 octobre, 1 h 45 UTC). Leur « candidate » se calcule
  sur ces jours seulement (`jours_suivants` dans le tableau, avec les mêmes clés).
- **Robustesse dans la règle « candidate »**, pour toutes les stratégies : la référence doit rester positive
  sous le glissement ×2 et ×3, l'entrée retardée et les ordres limites manqués (nets de la commission IBKR),
  et sur chaque moitié chronologique des jours. Le plan l'exigeait (lot 1, point 7 : « une stratégie qui ne
  tient pas sous ces conditions est écartée ») et le tableau ne l'appliquait pas. Aucune stratégie n'est
  candidate aujourd'hui : aucun statut ne tombe après coup. Limite : le glissement suit des paliers de prix
  et pèse peu en R sur les ranges larges des gappers, le test est donc faible pour Killmode.
- **Tranches**, bornes figées ici, sur les lignes déclenchées des stratégies v2 actions (v1 exclue), en
  espérance de référence avec intervalle en grappes, par stratégie et toutes ensemble :
  gap à l'ouverture < −5, −5 à −2, −2 à −0,5, ±0,5, +0,5 à +2, +2 à +4, +4 à +5, +5 à +10, ≥ +10 % ;
  ATR < 2, 2-4, 4-6, 6-10, ≥ 10 % ; `vol15_adv` < 0,25, 0,25-0,5, 0,5-1, ≥ 1 ; place pour 2 R
  (2 × risque en % / ATR en %) < 0,5, 0,5-1, ≥ 1 ; stop touché avant l'entrée ou non.
- **Trois comparaisons** (z en grappes par jour ; pour deux groupes, z approché en les supposant
  indépendants) : 1. la sélection, écart `orb15` − témoin sur les séances à gap ≥ +4 % et ATR ≥ 4 % contre
  les autres ; 2. les conditions de prix, parmi ces séances, `orb15` quand Killmode a une ligne contre sans ;
  3. la limite de 11 h, par séance où Killmode a une ligne, R Killmode − R `orb15` (0 sans trade).
- **Couverture de l'univers** : pour chaque séance rejouée (hors bloc scellé), parmi toutes les actions du
  journalier Alpaca ouvertes en gap ≥ +4 % avec un ATR de Wilder ≥ 4 %, la part présente dans l'univers du
  labo, par prix de la veille (< 1 $, 1-5, 5-20, ≥ 20 $). Comptage seulement (`universe.killmode_coverage`,
  quelques séances par passage du worker). Si la part est faible, Simão choisira entre un univers de gappers
  réservé à Killmode et rien.
- Personne ne lit ces tranches ni ces comparaisons avant le déploiement : les lignes d'`orb15` à gap ≥ +4 %
  sont déjà en base et contiennent presque tous les futurs trades Killmode avec le même R. Un seuil qui en
  sortirait serait une déclaration suivante, jugée sur les jours suivants.

Avis de ce fil sur ces propositions (esprit critique, avant de les écrire) : gardées telles quelles, les
colonnes, le témoin, les jours suivants, les tranches et la couverture. Deux points corrigés :
- la règle de robustesse compte aussi le glissement ×3 et les ordres limites manqués, comme le plan
  l'écrit, au lieu du seul ×2 proposé ;
- la comparaison de la limite de 11 h telle que proposée (R Killmode − R `orb15` sur les séances où Killmode
  s'exécute) vaut toujours zéro. Les deux ordres sont identiques jusqu'à 11 h, donc quand Killmode s'exécute,
  `orb15` s'exécute à la même bougie avec le même résultat. L'effet de la limite vient des seuls trades
  d'`orb15` exécutés entre 11 h et 12 h, d'où la mesure par séance avec 0 sans trade.
Aussi, sur la remarque du fil, les ventilations par régime et par rang se lisent désormais en espérance de
référence (après commission IBKR en actions), comme le classement.

**Council** (Sceptique, Pragmatique, Critique : GO modifié). Sceptique : les chiffres de Gemini sont sans
source et l'étude que cite GPT (Gao, Han, Li et Zhou, « Market intraday momentum », Journal of Financial
Economics, 2018) va plutôt contre le time stop : la première demi-heure prédit en partie la dernière. On
s'attend à ce qu'aucune règle n'aide ; c'est justement pour cela qu'on mesure avant de les appliquer.
Pragmatique : le coût est faible (mêmes trades simulés, une requête SPY par jour, seuil de 3,32 à 3,38) et
la réponse arrive avec le rattrapage. Critique : une variable à la fois sur `r_net` ; volume et QQQ écartés
pour ne pas multiplier les variantes ; le filtre d'entrée reste une mesure.

## Déclaration n° 7 (8 octobre 2026) : confirmation des GO du scanner, brut contre confirmé

Demande de Simão (fil « Avis sur FRSH en pré-market », carte « Oui, déclaration n° 7 », 8 octobre) : rejouer au
labo la confirmation des GO introduite en direct par la PR #37 (`app/trading/confirm.py`, `!confirmations`), contre
le GO acheté sans attendre. Demande complète : `/mnt/project-files/labo/demande-declaration-7-confirmation-go.md`.
Ce qu'on en sait avant de mesurer : rien au labo. v1 achète au dernier cours avec un ordre limité, sans
confirmation, et sa note ne trie rien (GO ≈ NO GO ≈ −0,24 R au premier constat). L'idée « ça augmente forcément
le taux de réussite » ne dit rien de l'espérance : l'entrée confirmée est plus haute (risque plus grand, moins de R
jusqu'aux objectifs) et les GO qui filent au TP1 sans repli sont annulés.

**Population** : les plans GO de v1 (actions, achat), tels que le labo les fait à 9 h 10 New York sur son univers.
Les filtres propres au scanner (prix ≥ 5 $, ATR ≥ 2,5 %, tendance, 3 à 5 valeurs), le catalyseur et les leçons ne
se rejouent pas.

**Deux bras**, sur les mêmes GO, les mêmes jours et avec les mêmes coûts (`strategies.go_rows`) :

| Bras | Entrée | Pas de trade |
|---|---|---|
| `go_brut` | Au marché à l'ouverture (première bougie de la séance) | Ouverture au niveau du stop ou dessous |
| `go_confirme` | La règle du direct, avec le **même code** (`confirm.evaluate`) sur les bougies de 5 min closes avant 11 h 30 New York : annulé dès qu'une bougie clôture sous le stop ou que son plus haut touche le TP1 ; repli quand un plus bas atteint l'entrée du plan ; confirmé quand, après le repli, une bougie finie après 9 h 45 clôture au-dessus du VWAP de séance (déclencheur = son plus haut + 0,01, on attend la suivante s'il dépasse le TP1). Ordre stop d'achat au déclencheur, posé à la clôture de cette bougie, exécuté à la première bougie dont le plus haut l'atteint, à max(déclencheur, ouverture), **valable jusqu'à 11 h 30** | GO annulé, expiré, ou ordre pas atteint avant 11 h 30 |

- **Sortie déclarée, la même pour les deux** : `r_sans_be` (un tiers à chaque objectif du plan, stop fixe, reste
  vendu à la clôture ; une bougie qui touche un objectif et le stop compte comme stop). Aucune autre sortie n'est
  mesurée sur ces bras : `r_net` reste vide.
- R rapporté au risque prévu de chaque bras : ouverture − stop pour le brut, déclencheur − stop pour le confirmé
  (le risque qui fixe la taille de l'ordre en direct).
- Robustesse sur `r_sans_be` : glissement ×2 et ×3, entrée une bougie plus tard (brut : à 9 h 35 ; confirmé : ordre
  posé une bougie plus tard). Ordres au marché ou stop : rien à « manquer ».
- Témoin : même stop en %, mêmes objectifs en R, même sortie à stop fixe ; entrées tirées de 9 h 45 à 15 h pour le
  brut, de 9 h 45 à 11 h 30 pour le confirmé (la vie de son ordre, comme Killmode).
- Le direct déclare le GO expiré dès 11 h 30, avant de lire la bougie qui finit à 11 h 30 : le labo fait pareil.
- Écart avec la demande : l'ordre confirmé expire à 11 h 30, comme le dit le message du direct, alors que la mesure
  du direct le laissait courir après. Corrigé côté direct par la PR #39.

**Deux stops**, chacun là où il se rejoue à l'identique :
- **origine** (`go_brut`, `go_confirme`) : le stop et les objectifs des plans du labo, sur tous les jours ;
- **plancher du direct** (`go_brut_plancher`, `go_confirme_plancher`) : le plan que le direct fait depuis la PR #36,
  refait avec son code (`service._floor_stop` : stop à au moins 1/3 de l'ATR journalier, objectifs recalculés sur ce
  risque, plan noté avec, donc ses propres GO). Seulement sur les jours que le générateur calcule à partir du
  déploiement (rejeu et direct du labo) : le rattrapage relit deux séances de 5 min par action, alors que refaire le
  plan demande ~60 séances de bougies de 15 min par action et par jour. Le générateur garde ce plan dans
  `trades.plan` (« plancher ») pour que les déclarations suivantes le retrouvent.

Chaque ligne garde le statut du GO (`confirmation` : `confirme`, `annule`, `expire`).

**Jugement, fixé avant le premier résultat** :
- chaque bras est jugé comme une stratégie, sur sa sortie déclarée : espérance nette après commission IBKR, IC à
  95 % en grappes par jour, témoin, robustesse, règle « candidate » ;
- la confirmation « aide » si l'écart **« confirmé ou rien » − brut**, GO par GO (0 pour un bras sans trade), est
  positif avec z au-dessus du seuil « candidate » et au moins 30 GO. C'est la comparaison qui compte pour 20 € par
  trade : elle tient compte des trades que la confirmation évite ;
- ventilation par statut (confirmé, annulé, expiré) avec ce qu'a fait le brut dans chaque groupe, et le confirmé sur
  ses ordres exécutés ;
- taux de réussite affiché, jamais utilisé pour conclure. Rien ne change dans le direct sur ce résultat avant la
  règle de promotion habituelle.

**Essais déclarés : 143** (139 + 2 bras × 2 stops, une seule sortie) ; seuil « candidate » : **z ≥ 3,39**.

Le rattrapage repart pour les actions seulement : la n° 7 ne change aucune ligne crypto, dont le rattrapage continue
celui de la n° 6 (`CRYPTO_DECLARATION`).

Avis de ce fil (esprit critique, avant d'écrire) : la comparaison est gardée telle que demandée. Corrigé : la lecture
« ça augmente forcément le taux de réussite » (le taux montera sans doute, l'espérance est l'inconnue), et
l'expiration de l'ordre à 11 h 30. Ajouté : l'écart par GO avec 0 sans trade, sans lequel un bras qui trade moins
paraîtrait meilleur sur sa seule espérance par trade.

**Council** (Sceptique, Pragmatique, Critique : GO modifié, les deux stops). Sceptique : la note ne trie rien au
labo, et confirmer un mauvais tri peut surtout réduire la perte en tradant moins, sans créer d'espérance positive ;
l'écart par GO le montrera. Pragmatique : la règle est le code du direct, sans second code à maintenir ; le plan au
plancher coûte un deuxième calcul de plan quand le plancher joue (rejeu plus lent, accepté par Simão) ; quatre
essais de plus seulement. Critique : un seul stop aurait été plus simple, mais le stop change les deux bras
(annulation sous le stop, risque en R) et Simão trade le plancher depuis la PR #36 ; un stop plancher avec les
objectifs d'origine serait un plan que personne ne trade, d'où le plan refait en entier, exact, sur moins de jours.

## Archive des actus des gappers, avant la déclaration n° 8 (8 octobre 2026)

Demande du fil « JARVIS Killmode » (avis de GPT et Gemini), choix de Simão « Archiver » sur la carte D1 :
rejouer le pilier catalyseur de Killmode. Finnhub gratuit ne garde qu'environ un an d'actus et perd une
séance par jour, donc l'archive passe avant la déclaration, **sans rien lire**.

- Population : toutes les actions de la base (journalier Alpaca), pas l'univers du labo, ouvertes en gap
  ≥ 3,5 % (4 % moins une marge pour l'écart entre ouverture journalière et première bougie de 5 min) avec un
  ATR de Wilder 14 j ≥ 4 %, sur chaque séance non scellée encore dans la profondeur de Finnhub, la plus
  ancienne d'abord (du 9 octobre 2025 au 2 avril 2026, puis les séances après le bloc scellé).
- Archivé : les actus publiées au plus tard à 9 h 45 New York sur les 7 jours d'avant (la fenêtre de
  `catalyst.check`), le calendrier des résultats de toutes les sociétés de J − 10 à J + 30, et le profil de
  chaque société (nom, pour reconnaître ses actus).
- Rien n'est lu : ni tonalité, ni critère, ni résultat de trade. Aucune route ne montre ces tables ; `!labo`
  et `/api/lab/status` n'en donnent que les comptes. Le bloc scellé n'est pas archivé.
- Limites déjà connues, à reprendre dans la déclaration : calendrier et profils tels que Finnhub les donne
  aujourd'hui (remplis après coup) ; une société retirée de la cote peut ne plus avoir de profil ; une liste
  vide vaut aussi bien « aucune actu » que « symbole inconnu de Finnhub » ; la fidélité des heures de
  publication est à vérifier sur un échantillon ; la profondeur réelle du calendrier des résultats en
  gratuit n'est pas connue (les dates en échec ou vides un jour de semaine le diront).

## Déclaration n° 8 (8 octobre 2026) : Killmode complet, coûts réels, jugement en t, variantes FRSH

Écrite avant toute lecture de l'archive : aucune actu, aucune tonalité, aucun résultat de Killmode complet n'a été
lu pour l'écrire. Demandes : `/mnt/project-files/labo/demande-killmode-avis-gpt-gemini.md` (D1 à D4, fil « JARVIS
Killmode », avis de GPT et Gemini, plan complet dans `/mnt/project-files/trading/killmode-avis-gpt-gemini.md`) et
l'ajout du 8 octobre 16 h 10 de `/mnt/project-files/labo/demande-declaration-7-confirmation-go.md` (trade FRSH arrêté
au stop). Aucune règle de prix ne change ; le bloc scellé n'est ni lu ni archivé.

### D1 : Killmode complet, le pilier catalyseur rejoué (`app/lab/kill_complet.py`)

Le labo ne mesurait que le squelette prix de Killmode (n° 5). Le journal du direct, lui, ne verra un écart KILL −
témoin qu'au-delà d'environ 0,5 R, même à 100 KILL. L'archive (section précédente) permet de rejouer le catalyseur.

**Critère, figé avant lecture** :
- Séances : celles de l'archive (non scellées, du 9 octobre 2025 au 2 avril 2026, puis après le bloc scellé), une
  fois tout archivé pour la séance : actus de chaque gapper (ou en échec), calendrier des résultats de J − 10 à
  J + 30, profil de chaque société. Les gappers dont les actus sont en échec ou perdues sont comptés, pas mesurés.
- Prix : la stratégie `killmode` du labo, le même code (`strategies.strategy_rows`), sur les bougies de 5 min SIP
  d'Alpaca relues et gardées en mémoire : gap ≥ 4 % (première bougie contre la clôture de la veille), ATR ≥ 4 %,
  range de 15 min tenu au-dessus du VWAP sans combler le gap, ordre stop d'achat posé à **9 h 45** New York jusqu'à
  11 h, sortie de Killmode (un tiers par objectif, stop à l'entrée après TP1, solde à la clôture). Un gapper sans
  signal n'est pas noté.
- Catalyseur : `catalyst.check(symbole, 9 h 45)` **tel quel**, Finnhub remplacé par l'archive le temps de l'appel
  (mêmes signatures, ce qui manque lève une erreur comme Finnhub en panne), puis `killmode.catalyst_codes`. Le code
  est figé par son empreinte `37f15d7363` (`killmode.CATALYST_CODE`, catalyst.py et mots de mouvement) : si elle
  change, la mesure s'arrête et `!strategies` le dit ; changer le catalyseur demande une nouvelle déclaration.
- Groupes, comme le journal du direct : **KILL** (aucun code), **témoin propre** (seulement « aucune actu
  positive »), **témoin avec veto** (actus négatives, résultats dans 24 h, augmentation de capital). À part, ni KILL
  ni témoin : **données incomplètes** (profil inconnu ou calendrier en échec : le pilier « données » du direct
  tombe aussi) et **actus coupées** (la profondeur de Finnhub a coupé les 7 jours avant la séance).
- **Mesure principale (essai `killmode_complet`)** : espérance de `r_20e` des KILL déclenchés, à la mise de Simão
  (20 € de risque, change 1,15, un bracket IBKR par objectif avec son minimum de 1 $ par ordre, plan Fixe,
  glissement du labo avec le plancher d'un demi-pas). IC 95 % en t sur les jours. **Candidate** si la borne basse
  de l'IC est au-dessus de 0, t ≥ seuil (D4), au moins 30 trades, et si l'espérance reste positive sous le stress
  de 3 cents et sur chaque moitié chronologique des jours. Le coût réel décide ici parce que Killmode est ce que
  Simão trade, à cette taille.
- `r_20e` et `r_3c` sont ceux de `killmode.real_r` et `stressed_r` **en vigueur** (depuis la PR #44 : gain compté
  bracket par bracket en actions entières, reste vendu en un seul ordre à la clôture). Chaque ligne garde
  l'empreinte de cette méthode de coût (`kill_complet.cost_fingerprint`, calculée sur des trades types) : si elle
  change, les séances déjà calculées sont recalculées et ne comptent pas en attendant (`!strategies` dit combien),
  pour que toutes les séances aient le même coût. Un changement de méthode après une première lecture se note ici.
- **Amendement du 10 octobre 2026, avant toute séance calculée** (aucune ligne de Killmode complet n'existait :
  l'archive attendait encore ses profils) : exécutions aux **prix réellement négociés**, comme la n° 9. Bougies de
  5 min non ajustées (`adjustment=raw`), journalier remis à l'échelle de la clôture brute de la veille
  (`kill_complet.traded_prices`). Un gapper dont le gap brut et le gap ajusté de l'archive divergent de plus de 25 %
  (opération sur titre entre la veille et la séance) est écarté et compté à part. Raison : avec les bougies ajustées
  des divisions connues aujourd'hui, le demi-pas, les 3 cents, les actions entières, la commission par action, le
  minimum de 1 $ et la règle sous 1 $ étaient calculés sur des prix qu'aucun trader n'a vus, ce qui sous-estime les
  coûts avant un regroupement d'actions (fréquent sur les petites valeurs qui gappent). La base des prix entre dans
  l'empreinte de coût : une ligne calculée sur une autre base serait recalculée. Ni le critère, ni les groupes, ni
  les seuils ne changent.
- **Mesure secondaire (essai `killmode_complet_ecart_temoin`)** : écart KILL − témoin propre **apparié par jour** :
  une valeur par séance où les deux groupes ont au moins un trade déclenché (moyenne des KILL − moyenne des témoins
  propres, en `r_20e`). Le catalyseur « aide » si cet écart est positif avec t ≥ seuil sur ces séances et au moins
  30 séances.
- Sans essai : les KILL par motif (`call_motif` : résultats battus, actu de fond, simple mouvement du cours), les
  témoins avec veto, les écartés.

### Variantes FRSH du bras confirmé (n° 7)

Trois bras de plus sur le GO confirmé **au stop d'origine**, chacun un filtre lu à la clôture de la bougie de
confirmation ; s'il passe, le même ordre que `go_confirme`, sinon pas d'ordre (statut « refusé par le filtre », ou
« non jugeable » quand la donnée manque). Pas sur le stop plancher : il n'existe que sur les jours du générateur, et
doubler les essais pour une poignée de jours affaiblirait tout le reste.

| Bras | Filtre | Verdict |
|---|---|---|
| `go_confirme_v2` | VWAP plus haut qu'il y a 3 bougies **et** clôture ≥ VWAP + 0,25 × ATR de 5 min | Améliorer : l'idée (un VWAP plat ne confirme rien) tient, mais 3 bougies et 0,25 ATR sont pris sur un seul trade ; déclarés une fois, jamais réglés ensuite |
| `go_confirme_v3` | Clôture au-dessus du plus haut de la séance jusque-là | Garder : une confirmation qui ne reprend pas le plus haut reste sous l'offre |
| `go_confirme_v4` | Variation depuis l'ouverture meilleure que celle de SPY à la même bougie | Améliorer : SPY seulement, le labo n'a pas les ETF de secteur (IGV pour FRSH) |

Jugement : chaque bras comme une stratégie (sa sortie déclarée `r_sans_be`), et, GO par GO, l'écart « filtré ou
rien » − « confirmé ou rien » (le filtre « aide » s'il est positif avec t ≥ seuil et au moins 30 GO) et − brut.
Mesure sans essai : brut et confirmé selon que la société a publié ses résultats depuis la veille (séance d'avant,
week-end, ou avant l'ouverture), lu dans le calendrier archivé ; « calendrier non archivé » pour les GO hors de
l'archive (rejeu de 2024-2025). L'entrée en indice n'a pas de source au labo : non mesurée.

### D2 : stress de 3 cents et mise de Simão

Chaque trade exécuté du labo v2 en actions garde `r_3c` (Gemini : 3 cents de plus au remplissage, de moins à chaque
sortie au marché, stop, stop à l'entrée et clôture ; objectifs au prix de l'ordre ; sous 1 $, 1 % du prix ;
commission IBKR par action comprise) et `r_20e` (mise de Simão, comme Killmode). **Tranché : en actions, une
stratégie n'est « candidate » que si sa référence reste positive sous le stress de 3 cents**, en plus du glissement
×2 et ×3, du retard, des ordres manqués et des deux moitiés. `r_20e` est affiché (« à 20 € ») avec un drapeau
« rentable à 20 € » qui ne décide pas : il dépend de la taille du compte (6 minimums de 1 $ pèsent 0,1 à 0,3 R sur
~23 $ de risque), pas de l'avantage de la règle. Ce chiffre affiché suit `killmode.real_r` au moment du rejeu : si
la méthode change en cours de rattrapage, l'affichage mélange deux versions jusqu'au rattrapage suivant (seul
Killmode complet, où il décide, est recalculé).

### D3 : plancher d'un demi-pas

Le glissement par côté des actions n'est plus jamais sous un demi-pas de cotation : `max(pb × prix, pas / 2)`
(`simulator.stock_slippage`, la règle de `_half_tick_slip` de Killmode). Ça change les actions entre 1 et 3,33 $ et
entre 5 et 8,33 $ ; les tableaux des n° 3 à 7 y étaient un peu optimistes. Les lignes du labo v2 sont refaites par le
rattrapage ; les trades v1 déjà journalisés (visibles et scellés) sont recalculés une fois, sans rien rejouer
(`app/lab/cout.py`, étape « cout » du worker), puis les caches des statistiques, des leçons et du grade sont vidés.

### D4 : jugement en t sur les jours

Toutes les règles « candidate » et « aide » comparent m / se (erreur en grappes par jour) au quantile de Student à
G − 1 degrés de liberté (G = jours), corrigé de Bonferroni sur les essais déclarés (`strat_stats.candidate_t`) ; les
IC 95 % sont en t aussi. Seuils à 148 essais : **3,81 à 30 jours, 3,63 à 50, 3,51 à 100, 3,40 sur beaucoup de jours**
(l'ancien seuil en z). Sous deux jours, rien n'est jugeable.

### Essais

**148** (143 + 3 bras FRSH + 2 pour Killmode complet). Le rattrapage repart pour les actions seulement ; la crypto
garde celui de la n° 6 (49 essais, rien n'y change).

### Limites

- Calendrier des résultats et BPA publiés tels que Finnhub les donne au moment de l'archive (remplis après coup :
  une date déplacée ne se voit pas) ; profils d'aujourd'hui (une société renommée peut perdre ses actus citées par
  l'ancien nom) ; liste d'actus vide = « aucune actu » ou « symbole inconnu de Finnhub ».
- Fidélité des heures de publication de Finnhub non vérifiée : à contrôler sur un échantillon avant toute
  conclusion positive.
- Le direct relit le catalyseur pendant toute la vie de l'ordre ; le labo le lit une fois, à 9 h 45 : une actu de
  10 h ne fait pas un KILL au labo.
- Population : les actions qu'Alpaca connaît encore (sociétés retirées de la cote en partie absentes).
- Puissance : au plus ~120 séances avant le bloc scellé. Un « pas d'avantage démontré » sera la lecture la plus
  probable, et ne voudra pas dire « pas d'avantage ».

### Avis et council

Position de l'analyste, sur les faits, avant l'avis de Simão : D1 est la seule façon de mesurer ce qui distingue
Killmode d'une cassure de range ; D2 à D4 corrigent des coûts et un seuil trop doux, ce qui ne peut que retirer des
candidates. Les variantes FRSH viennent d'un seul trade : elles se mesurent, elles ne se corrigent pas en direct.

**Sceptique** : un seul trade (FRSH) a dicté trois filtres, dont les paramètres de V2 ; et le catalyseur est rejoué
avec un calendrier rempli après coup. Réponse : V2 est déclarée une fois sans réglage ultérieur, et le Bonferroni
compte ses essais ; le calendrier n'est lu que pour les publications passées (connues à 9 h 45) et les dates à
venir, pas pour les BPA futurs ; la limite est écrite. **Pragmatique** : cinq essais pour trois idées et la mesure
qui compte le plus (Killmode complet) ; tout réutilise le code du direct (catalyst.check, catalyst_codes,
confirm.evaluate, real_r), sans second code à tenir. **Critique** : exiger le stress de 3 c pour « candidate » peut
écarter une vraie stratégie sur les grosses valeurs (3 c ≈ le glissement du labo à 100 $, 5 fois plus à 10 $) ;
accepté, parce que Simão trade surtout des petites valeurs volatiles et que ses `!prends … kill` mesureront le vrai
glissement. GO modifié : D2 tranché « oui » ; `r_20e` affiché sans décider, sauf pour Killmode complet.

## Déclaration n° 9 (9 octobre 2026) : scanner « potentiel +10 »

Fil « Améliorer le scanner », choix de Simão « Scanner +10 ». Brouillon, deux tours d'avis de GPT et Gemini (accord des
deux) et réaction : `/mnt/project-files/labo/declaration-9-scanner-plus10.md` et `reaction-avis-declaration-9.md`. Écrite
avant tout résultat de trade de ces listes. Code : `app/lab/plus10.py` (labo), `app/trading/plus10.py` (direct).

**Pourquoi** : le scanner de 15 h 10 cherche des valeurs calmes en tendance (MM20/MM50, RSI 45-75, prix ≥ 5 $) ; le
8 octobre, 4 valeurs sur 137 sont passées, la première à −0,07 % de gap. La mesure `potentiel` (54 754 séances de
l'univers du labo) montre que l'amplitude vient du gap : +8 % depuis l'ouverture touché dans 2,5 % des séances au gap de
±0,5 %, 32 % de +5 à +10 %, 47 % au-delà de +10 %, 48 % à −5 % ou pire. Toucher +8 % mesure l'amplitude, pas un gain :
la déclaration teste des entrées.

**Listes, figées** (toutes les actions de la base, actifs inactifs d'Alpaca compris, sans filtre de prix, de liquidité
ni de capitalisation ; gap d'ouverture contre la clôture de la veille et ATR de Wilder 14 j sur les séances terminées,
journalier ajusté des divisions ; 15 séances au moins, celles de moins de 30 comptées à part) :

| Liste | Critère | Classement |
|---|---|---|
| S+10 | gap ≥ +5 % et ATR ≥ 5 % | les 5 plus forts gaps (rangs 6+ : comparaison) |
| Rebond | gap ≤ −5 % et ATR ≥ 5 % | les 5 plus forts gaps en baisse |

Les seuils de 5 % sont **exploratoires** (choisis après la mesure `potentiel`, sur l'amplitude, jamais sur un résultat
de trade) : gelés ici, aucun autre seuil ne sera essayé après coup.

**Essais (4, total 152)** : entrées déjà codées (`strategies.strategy_rows`, mêmes coûts, sorties, robustesse et
témoin), jugées sur le top 5 avec la règle « candidate » en vigueur (n° 8 : IC en t, écart au témoin au seuil corrigé,
30 trades, robustesse dont le stress de 3 c, deux moitiés), référence `r_net` moins la commission IBKR :
`plus10_orb15` et `plus10_kill` (règle de prix de Killmode) sur S+10, `rebond_orb15` et `rebond_orb30` sur Rebond.
Les lignes du labo v2 ne changent pas (`DECLARATION` reste 8, pas de rattrapage) ; `LATEST_DECLARATION` = 9.

**Prix réellement négociés** : les exécutions se rejouent sur les bougies de 5 min SIP **non ajustées**
(`adjustment=raw`) et le journalier est remis à l'échelle de ces prix ; si le gap brut et le gap ajusté diffèrent de
plus de 25 % (en rapport), une opération sur titre a eu lieu entre la veille et le jour : valeur écartée des trades,
comptée à part. Les n° 5 et 8 se rejouent sur des bougies ajustées (`adjustment=split`) : non changé ici, à reprendre
dans une déclaration suivante.

**Mesures sans essai** (`!strategies`, `GET /api/lab/strategies`, clé `potentiel_plus10`) :
- amplitude depuis l'ouverture (+8 %, +10 %, −8 %) : base, S+10 et Rebond (top 5, rangs 6+), filtres actuels de
  15 h 10 rejoués au gap d'ouverture (`scanner.features` et `filter_reasons`) : le coût de ces filtres ;
- par tranche de prix de la veille (< 1 $, 1-5 $, 5-20 $, ≥ 20 $), et `r_net`, `r_3c`, `r_20e` par tranche de prix
  d'entrée ;
- ce qui reste à prendre après la décision (9 h 45, 10 h pour orb30), MFE et MAE depuis l'entrée simulée, heure du
  plus haut et du plus bas, lequel de +8 % et −8 % arrive en premier : rien d'avant l'entrée n'est crédité ;
- classement : top 5 contre rangs 6+ et contre **20 tirages pré-déclarés** de 5 valeurs des rangs 6+ du même jour
  (graines fixes ; moyenne et dispersion, par trade et écart par jour, jamais le meilleur tirage) ;
- données : éligibles sans bougies de 5 min par liste, rang et tranche ; opérations sur titre ; gaps de plus de 30 %
  listés pour un audit ; valeurs devenues inactives chez Alpaca ;
- concordance du direct : part du top 5 du labo retrouvée dans la liste confirmée du direct (`data/plus10_scans.jsonl`).

**En direct** (mesuré, ni GO ni note) : à 15 h 10 Paris, une liste d'attente « 🎯 Potentiel +10 » au gap de
pré-ouverture, jamais présentée comme testée ; à 9 h 46 New York, la liste confirmée au gap d'ouverture (population :
50 plus fortes hausses et 50 plus fortes baisses du screener, 100 plus actives, liste d'attente), avec les niveaux
d'orb15 et de la règle Killmode lus sur les bougies IEX en temps réel ; à 10 h 01, ceux d'orb30 pour Rebond.

**Limites** : la liste de 9 h 10 n'est pas rejouable (pas de pré-ouverture historique) ; le direct ne voit pas toute
la base (concordance mesurée) et lit le range sur IEX (le labo, sur le SIP) ; bougies de sociétés disparues parfois
absentes chez Alpaca (comptées) ; scissions et dividendes exceptionnels non ajustés (audit des gaps > 30 %) ; sous 1 $,
le stress de 3 c (1 % par côté) peut rester sous l'écart réel achat-vente ; pas de catalyseur dans les essais ; environ
380 séances non scellées : « pas d'avantage démontré » reste la lecture la plus probable et ne voudra pas dire « pas
d'avantage ».

**Council** : Analyste, garder (le défaut du scanner est sa cible, pas ses réglages). Sceptique : `plus10_kill` recoupe
Killmode complet ; l'essai mesure ce que le classement et le seuil ajoutent, les rangs 6+ et les tirages le diront.
Pragmatique : 4 essais, aucune règle de prix nouvelle. Critique : petits prix et Rebond après une mauvaise nouvelle ;
jugés par le stress de 3 c, le témoin et les tranches de prix. GO, modifié après GPT et Gemini.

## Déclaration n° 10 (10 octobre 2026) : mesures corrigées, sans essai de plus

Écrite avant tout chiffre recalculé : aucune ligne du labo n'a été lue pour l'écrire. Sources : la vérification du code
du labo (points I1, L1 à L5 et X1) et l'audit `/mnt/project-files/labo/audit-infos-futures-2026-10-10.md` (ligne
« Bougies ajustées des divisions »). La n° 10 ne crée **aucun essai** : elle corrige la façon de mesurer et re-mesure
les essais existants, plus des colonnes descriptives qui ne décident de rien et préparent la n° 11. Code :
`app/lab/simulator.py` (`sim_rev`), `strategies.py`, `rattrapage.py`, `strat_stats.py`.

### Ce qui change, et pourquoi

1. **Simulateur, révision 2** (`strategies.SIM_REV = 2`, toutes les lignes du labo v2, n° 9 et Killmode complet
   compris) :
   - prix comparés en pas entiers avec une tolérance (I1) : un objectif à 16,01 $ et un plus haut de 16,02 $ ne
     comptaient pas (16,01 + 0,01 vaut 16,020000000000003 en flottants), et 23 % des niveaux au cent entre 20 et
     100 $ demandaient deux pas au lieu d'un ; désormais un pas exact suffit, à tous les cents (objectif, ordre
     limité, ouverture au-delà d'un objectif) ;
   - bougie d'entrée symétrique (L2) : un ordre stop rempli en cours de bougie compte les objectifs qu'elle traverse,
     comme le témoin entré à l'ouverture ; avant, la stratégie prenait le stop de cette bougie mais pas ses objectifs ;
   - retour à l'entrée dans la bougie d'entrée compté seulement s'il est certain (L2b), pour la stratégie comme pour le
     témoin ; les bougies suivantes restent jouées au pire ;
   - un objectif déjà dépassé à l'achat sort au prix d'achat (L4) : acheté 10,30 $ avec un TP1 à 10,05 $, le tiers
     sortait à 10,05 $ (−0,46 R sur un trade qui n'a rien perdu) ;
   - nouvelle sortie `be_tp2` (stop à l'entrée après TP2, ta règle de suivi), mesurée seulement (X1).
   Le Labo 1 (`trades`, generator.py) reste en révision 1 : c'est le benchmark figé, ses chiffres ne bougent pas.
2. **Témoin des actions à la même heure** (L1) : le témoin tirait son entrée entre 9 h 45 et 15 h, alors que les ORB et
   les GO entrent tôt ; l'écart au témoin mêlait le signal et l'heure. Désormais, pour chaque trade exécuté (essais de
   la n° 9 compris), le témoin
   entre 5 à 30 min **après** l'exécution (`witness_window`, `WITNESS_SPAN` = 6 bougies, jamais la dernière bougie de
   la séance ; une exécution à 15 h 50 ou 15 h 55 n'a pas de témoin). Jamais avant l'exécution : un tirage d'avant
   achète sous le déclencheur en « sachant » que le prix va l'atteindre. L'ancien témoin est gardé
   (`temoin_journee_r`). La crypto garde son témoin (horizon fixe de 24 h).
3. **Tirages du témoin : 10 au lieu de 5** (`WITNESS_DRAWS`). Coût mesuré sur des journées synthétiques : 12,7 ms par
   action avec 5 tirages, 14,8 ms avec 10 (+16 %, 40 marches aléatoires) ; 15 à 17 ms contre 19 à 21 ms sur une séance
   de tendance (+25 %) ; en crypto, +3 à 4 ms par paire. Soit moins d'une seconde de plus par séance de 300 actions,
   contre plusieurs minutes de téléchargement : gardé. Chaque tirage est publié (`temoin_tirages`).
4. **Actions aux prix réellement négociés** (audit, divisions) : bougies de 5 min non ajustées (`adjustment=raw`, SPY
   compris), journalier et plan v1 remis à l'échelle de la dernière clôture brute quand une division a eu lieu depuis
   (écart de plus de 5 % entre prix brut et prix ajusté, `rattrapage.SPLIT_TOL` ; en deçà, rien n'est touché). Une
   valeur dont le gap brut et le gap ajusté divergent de plus de 25 % (opération sur titre entre la veille et la
   séance) est écartée, comme à la n° 9. Avant, le demi-pas, les 3 cents, les actions entières, la commission et le
   minimum de 1 $ étaient calculés sur des prix qu'aucun trader n'a vus. Le générateur n'écrit plus les lignes des
   actions : le rattrapage les fait toutes, jusqu'au dernier jour traité (rejeu et direct) dont le journalier est
   en base (sans lui, l'opération sur titre du jour même ne se voit pas).

### Ce qui ne change pas

Le Labo 1 et ses trades, les stratégies, les seuils, les sorties déclarées, la règle « candidate » (D2 de la n° 8 ;
en crypto, ses grappes deviennent des semaines, voir plus bas), le bloc scellé (ni lu, ni déplacé). **Essais : 152,
inchangé.**

### Colonnes descriptives (aucune ne décide)

| Colonne | Ce qu'elle mesure | Ce qu'on y lira |
|---|---|---|
| `r_be_tp2` | v1 et bras GO, stop à l'entrée après TP2 (ta règle), reste à la clôture | ce que coûte ou rapporte ta règle face à la sortie déclarée |
| `r_ouverture` | chaque plan v1 (exécuté ou non) acheté au marché à l'ouverture de 9 h 30, stop fixe | si v1 perd par sa méthode ou par son ordre limité, qui n'achète un gap haussier que s'il échoue |
| `r_20e_deg` | `r_20e` au barème IBKR Dégressif | l'écart entre Fixe et Dégressif à ta taille |
| `r_20e_1s`, `r_20e_2s` | `r_20e` avec un seul bracket (TP2) ou deux (TP1 et TP3) au lieu de trois | ce que coûtent les trois brackets à 20 € (minimum de 1 $ par ordre) |
| `temoin_journee_r` | l'ancien témoin (journée) des actions | l'effet de l'heure |
| `temoin_tirages` | le R de chaque tirage du témoin | la dispersion entre tirages |

`r_20e_1s` et `r_20e_2s` reprennent les sorties de la simulation à trois objectifs : chaque bracket sort comme la
part du milieu de sa tranche (`killmode._legs`). C'est le coût des ordres, pas une re-simulation avec un seul
objectif.

### Lecture du tableau

- **✅ seulement si la stratégie gagne aussi à 20 €** : `candidate` garde sa définition (avantage de la règle) ;
  `candidate_compte` exige en plus, en actions, une espérance `r_20e` positive. Sinon « ☑️ avantage, perd à 20 € ».
  Même chose pour le potentiel +10 et la ligne du labo de Killmode ; le brief transmet `rentable_20e` et la valeur.
- **Crypto en grappes par semaine ISO** (L5) : les positions de 24 h se chevauchent d'un jour à l'autre, des grappes
  par jour sous-estiment l'erreur. Le seuil en t se prend sur les semaines (environ 75 sur un an et demi : seuil
  3,56 au lieu de 3,43 sur ~527 jours, à 152 essais). Le se par jour et le facteur d'inflation sont affichés à côté. En actions,
  la même lecture en semaines est affichée, sans effet sur le verdict.
- **Effet de l'heure** (écart au témoin de la journée − écart au témoin de la même heure) et **dispersion entre
  tirages** (écart-type des moyennes par tirage) : affichés, sans effet.
- **Entrée de v1** : « limite ou rien » contre « ouverture ou rien », plan par plan (une branche non exécutée vaut 0).
  Sur les GO, avec `go_brut` ramené au risque prévu de v1, par écart ouverture / entrée, les ouvertures au-dessus du
  TP1 à part ; sur tous les plans, avec `r_ouverture`, par gap d'ouverture.
- Le se de l'espérance est exposé ; l'écart labo/direct de Killmode s'en sert (avant, un IC en t lu comme un IC
  normal grossissait le bruit du labo).
- En tête du tableau, la politique mesurée en une ligne (entrée, break-even, sortie au temps, taille).

### Contrôle de non-biais (test du simulateur)

Sur une marche aléatoire au cent près (aucun avantage par construction), l'écart apparié ordre stop − témoin des
biais du simulateur (simulé − vrai, le vrai rejoué transaction par transaction) : révision 2 entre −0,017 et
+0,001 R sur huit graines (tolérance 0,02 R, ~2,5 erreurs-types), révision 1 entre +0,025 et +0,079 R en faveur de
l'ordre stop. **Biais résiduel connu** : avec un stop serré (1 σ d'une bougie), un plus bas sous le stop dans la
bougie d'exécution compte −1 R alors qu'il a pu venir avant l'exécution : −0,04 à −0,06 R contre l'ordre stop. Il
peut cacher un avantage, pas en inventer un.

### Effet attendu

L2 et L2b gonflaient les stratégies à ordre stop face à leur témoin : leurs écarts au témoin vont baisser. Le
+0,16 R de la cassure de zone crypto n'est plus une piste avant re-mesure. L4 relève les achats à l'ouverture au-dessus
des objectifs (`go_brut` les jours de gap haussier). Le témoin à la même heure change l'écart dans un sens qu'on ne
connaît pas (saisonnalité de la journée). Les prix négociés relèvent surtout les coûts des petites valeurs avant un
regroupement d'actions.

### Coût

Rattrapage complet des deux marchés (`progress_job` : `strats:stock:d10`, `strats:crypto:d10`), bougies relues à la
source : plusieurs minutes par séance d'actions, hors séance seulement, et sans déborder sur l'heure du résumé du soir.
Un jour que la source refuse est retenté aux deux passages suivants, puis calculé sans les symboles en échec, ou
sauté si plus de la moitié de l'univers manque (ses lignes d'avant sont alors effacées). Les séances de la n° 9 sont
recalculées (marque `plus10.MESURE`, le tableau ne lit que les recalculées), et celles de Killmode complet aussi
(l'empreinte de coût comprend la révision du simulateur et change aussi avec les nouvelles variantes). Les jours du
direct en actions attendent leur journalier (relu au passage de la séance suivante), puis le rattrapage, qui traite
d'abord l'arriéré du rejeu. Jusqu'à la fin du rattrapage, le tableau, l'autopsie et le grade indicatif des GO
confirmés ne lisent que les lignes de la n° 10, avec la mention « Tableau partiel : N jours en rattrapage » (et
« rattrapage … en cours » dans `!autopsie` et `!grade`) : les lignes d'avant sont exclues, jamais mélangées.

### Limites

- Le témoin après exécution détient 5 à 30 min de moins (sortie à la clôture) : négligeable devant le biais d'avant.
- Une division survenue dans les deux séances d'historique de 5 min n'est pas corrigée (rare).
- `r_ouverture` se lit par gap d'ouverture : le prix d'ouverture n'est pas gardé dans la ligne de v1.
- Le journalier brut pour toute la base (tranches de prix et minimum de 5 $ des mesures de la n° 9) n'est pas fait ici.
- La comparaison du journal de Killmode au labo (`r_ibkr`, `killmode.settle`) rejoue encore en révision 1 : à aligner.

### Hypothèses pour la n° 11, non déclarées

Chacune demande d'abord sa donnée ; rien n'est mesuré ici sous forme d'essai.

| Hypothèse | Donnée à mesurer d'abord |
|---|---|
| Continuation après résultats ou guidance vérifiés | heure de publication fiable (part des actus « en retard », `visibilite_finnhub`) et calendrier archivé sur assez de séances |
| Horizons 15, 30 et 60 min contre témoin apparié (M2) | R à horizon fixe de chaque entrée et de son témoin à la même heure, part de +1 R avant −1 R |
| Une ou deux sorties au lieu de trois à 20 € | `r_20e_1s` et `r_20e_2s` contre `r_20e`, sur les lignes re-mesurées |
| RVOL à la même minute | volume cumulé à la minute du signal contre la moyenne des 20 séances à la même minute (bougies de 5 min relues) |
| Régime VIX / ADX (M1) | clôtures de la veille du VIX et de l'ADX 14 j de SPY, archivées par séance |
| H1 à H3 du journal des erreurs (rejet de catalyseur, bêta face au secteur, stop sous un plus bas évident) | séances de catalyseur rejetées, bêta et ETF de secteur, marge du stop en ATR journalier |
| L1 à L10 de Killmode (file d'attente du labo) | les trades `!prends … kill` et Killmode complet aux prix négociés, point par point |
| Ta règle de sortie (stop à l'entrée après TP2) | `r_be_tp2` contre `r_sans_be` sur les GO |
| Entrée de v1 à l'ouverture | `r_ouverture` contre « limite ou rien », par gap |

### Council

Position de l'analyste, sur les faits : la n° 10 devrait surtout retirer des candidates (biais connus en faveur des
stratégies, coûts réels plus hauts, grappes crypto plus larges) ; seul le témoin à la même heure peut jouer dans les
deux sens. Elle ne choisit rien après coup. **Sceptique** : changer le simulateur et le
témoin en même temps empêche d'attribuer la baisse à l'un ou l'autre ; accepté, `temoin_journee_r` et le contrôle de
non-biais séparent les deux. **Pragmatique** : aucun essai, une seule re-mesure de tout. **Critique** : le témoin à la
même heure n'existe que pour les actions, et un stop serré garde un biais contre la stratégie ; les deux sont écrits.
GO.

## Bloc scellé

Le rejeu du **6 avril au 5 octobre 2026** est calculé mais rangé à part (`trades_sealed`,
`strat_trades_sealed`, `app/lab/seal.py`). Aucune statistique, aucun brief, aucun grade, aucun fil
Claude ne le lit. Il sert une seule fois, pour confirmer une stratégie qui a passé tout le reste.
Limite assumée : les mouvements du jour et les fiches catalyseurs de ces dates restent visibles.

## Où voir les chiffres

`!strategies` sur Discord, `GET /api/lab/strategies` (clé du labo), ligne « Labo v2 » de `!labo`.
Les jours traités avant le labo v2 sont rattrapés par le worker hors séance (`app/lab/rattrapage.py`) :
bougies relues à la source et gardées en mémoire, rien n'est stocké.
