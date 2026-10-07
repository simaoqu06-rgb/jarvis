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
  témoin avec z ≥ 2. Ce n'est pas une promotion.
- Aucune promotion avant : au moins un an de rejeu couvrant au moins deux régimes de marché, puis une
  seule ouverture du bloc scellé, puis le forward-test.

## Bloc scellé

Le rejeu du **6 avril au 5 octobre 2026** est calculé mais rangé à part (`trades_sealed`,
`strat_trades_sealed`, `app/lab/seal.py`). Aucune statistique, aucun brief, aucun grade, aucun fil
Claude ne le lit. Il sert une seule fois, pour confirmer une stratégie qui a passé tout le reste.
Limite assumée : les mouvements du jour et les fiches catalyseurs de ces dates restent visibles.

## Où voir les chiffres

`!strategies` sur Discord, `GET /api/lab/strategies` (clé du labo), ligne « Labo v2 » de `!labo`.
Les jours traités avant le labo v2 sont rattrapés par le worker hors séance (`app/lab/rattrapage.py`) :
bougies relues à la source et gardées en mémoire, rien n'est stocké.
