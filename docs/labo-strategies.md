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

## Témoin aléatoire

Pour chaque trade déclenché : même jour, même fenêtre, même stop en %, mêmes objectifs en R, entrée au
marché à une bougie tirée au hasard (graine fixe, reproductible). L'écart stratégie − témoin, apparié
trade par trade, dit si l'**entrée** apporte quelque chose. Une stratégie qui ne bat pas son témoin n'a
pas d'avantage d'entrée, quelle que soit son espérance.

## Lecture des résultats

- Classement par **espérance nette en R** avec son intervalle de confiance à 95 % en grappes par jour.
  Le taux de réussite est affiché, jamais utilisé pour classer.
- « Candidate » (✅) = borne basse de l'intervalle au-dessus de zéro, au moins 30 trades, et écart au
  témoin avec z ≥ 2 (relevé au seuil corrigé du nombre d'essais par la déclaration n° 2). Ce n'est pas
  une promotion.
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

## Bloc scellé

Le rejeu du **6 avril au 5 octobre 2026** est calculé mais rangé à part (`trades_sealed`,
`strat_trades_sealed`, `app/lab/seal.py`). Aucune statistique, aucun brief, aucun grade, aucun fil
Claude ne le lit. Il sert une seule fois, pour confirmer une stratégie qui a passé tout le reste.
Limite assumée : les mouvements du jour et les fiches catalyseurs de ces dates restent visibles.

## Où voir les chiffres

`!strategies` sur Discord, `GET /api/lab/strategies` (clé du labo), ligne « Labo v2 » de `!labo`.
Les jours traités avant le labo v2 sont rattrapés par le worker hors séance (`app/lab/rattrapage.py`) :
bougies relues à la source et gardées en mémoire, rien n'est stocké.
