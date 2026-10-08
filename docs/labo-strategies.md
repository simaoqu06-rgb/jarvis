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
« setup gagnant » ; la note composite de v1, qui compte déjà le catalyseur, n'a rien prédit (GO ≈ NO GO ≈
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
    par jour, par semaine et avec 3 c de glissement par côté ; si l'alerte du 30e a été écartée, le pari se
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
  - le R aux frais réels compte le gain bracket par bracket en actions entières, comme les frais ;
  - « avec 3 c de glissement par côté » se calcule à la taille de Simão (mêmes brackets et minimums IBKR) ;
  - l'écart au labo (`r_ibkr`) rejoue l'appel comme le labo : bougies de 5 min dès la suivante, résolution du
    labo (`simulator.simulate`, `resolution="v1"`, inchangée) ;
  - l'écart KILL − témoin propre mesure la valeur prédictive du filtre catalyseur à règle de prix égale, pas
    l'effet causal d'une actu.

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

## Bloc scellé

Le rejeu du **6 avril au 5 octobre 2026** est calculé mais rangé à part (`trades_sealed`,
`strat_trades_sealed`, `app/lab/seal.py`). Aucune statistique, aucun brief, aucun grade, aucun fil
Claude ne le lit. Il sert une seule fois, pour confirmer une stratégie qui a passé tout le reste.
Limite assumée : les mouvements du jour et les fiches catalyseurs de ces dates restent visibles.

## Où voir les chiffres

`!strategies` sur Discord, `GET /api/lab/strategies` (clé du labo), ligne « Labo v2 » de `!labo`.
Les jours traités avant le labo v2 sont rattrapés par le worker hors séance (`app/lab/rattrapage.py`) :
bougies relues à la source et gardées en mémoire, rien n'est stocké.
