# JARVIS

Assistant personnel (usage non commercial). Ce dépôt contient le serveur Python (FastAPI)
qui récupère les données en temps réel pour l'agent JARVIS et ses futures notifications.

## Lancer

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env   # puis remplis les clés
.venv/bin/uvicorn app.main:app --reload
```

La liste des routes est sur http://localhost:8000/docs.

Pour le faire tourner 24 h/24 sur un serveur (Docker), voir [DEPLOY.md](DEPLOY.md).

## Format commun

Chaque route renvoie le même format, quelle que soit la source :

```json
{
  "need": "crypto_prices",
  "data": [...],
  "source": "Kraken",
  "attribution": "Données : Kraken",
  "updated_at": "2026-10-05T23:30:52+00:00",
  "stale": false,
  "message": null,
  "errors": []
}
```

- `attribution` est la mention de source à afficher à côté de la donnée.
- `updated_at` est l'heure (UTC) de la dernière mise à jour réussie.
- Si la source principale tombe, la source de secours prend le relais (`errors` dit pourquoi).
- Si les deux tombent, `stale` vaut `true`, `data` contient la dernière valeur connue
  (gardée dans `data/last_known.json`, même après un redémarrage) et `message` l'explique.

## Sources branchées

| Donnée | Route | Principale | Secours | Clé | Cache |
|---|---|---|---|---|---|
| Prix crypto en EUR | `/api/crypto/prices?symbols=BTC,ETH` | Kraken | CoinPaprika | non | 1 min |
| Classement crypto | `/api/crypto/ranking?limit=20` | CoinLore | CoinPaprika | non | 1 min |
| Taux de change | `/api/fx?base=EUR&symbols=USD,GBP` | Frankfurter (BCE) | Currency-api | non | 1 h (taux publiés 1 fois/jour) |
| Actions US | `/api/stocks/quotes?symbols=AAPL,MSFT` | Twelve Data | Finnhub | oui (les deux) | 1 min |
| Bourse NY ouverte ? | `/api/markets/us/status` | calcul local + jours de fermeture | | non | |
| Fermetures NYSE | `/api/markets/us/holidays` | the-calendar.net | Nager.Date (fériés fédéraux, approximatif) | non | 24 h |
| Jours fériés | `/api/holidays?country=FR` | Nager.Date | caldays | non | 24 h |
| Calendrier économique | `/api/calendar/economic` | Helious | aucun pour l'instant | non | 1 h |
| Actus marché | `/api/news/market` | Helious (retard 60 min) | Finnhub | Finnhub | 5 min |
| Actus par action | `/api/news/company/AAPL` | Finnhub | aucun pour l'instant | oui | 30 min |
| Actus tech | `/api/news/tech?q=nvidia` | Hacker News (Algolia) | Hacker News (officiel) | non | 5 min |
| Actus spatiales | `/api/news/space?q=Rocket Lab` | Spaceflight News | Launch Library 2 | non | 5 min |
| Météo | `/api/weather?city=Paris` | Open-Meteo | MET Norway | non | 10 min |
| Qualité de l'air | `/api/air-quality?city=Paris` | Open-Meteo | aucun pour l'instant | non | 10 min |
| Séismes | `/api/earthquakes?min_magnitude=4.5` | USGS | EMSC | non | 5 min |
| Ville → coordonnées | `/api/geocode?city=Lyon` | Open-Meteo Geocoding | Nominatim | non | 24 h |
| Adresse France | `/api/address/france?q=...` | IGN Géoplateforme | Nominatim | non | 24 h |
| Adresse monde | `/api/address/world?q=...` | Nominatim | Photon | non | 24 h |
| Wikipédia | `/api/wikipedia?topic=Nvidia` | Wikipédia FR | Wikidata | non | 24 h |
| Livres | `/api/books?q=...` | Open Library | aucun pour l'instant | non | 24 h |

Un module par source dans `app/sources/`, les associations principale/secours et les durées
de cache dans `app/needs.py`. Chaque appel a un délai maximum de 8 secondes.

## Plans de trade, suivi et Trading Cockpit

JARVIS propose des plans selon la méthode EmperorBTC ([docs/emperorbtc.md](docs/emperorbtc.md)) ;
c'est toi qui passes les ordres : aucun ordre n'est envoyé à un courtier.

**Cockpit** : `/cockpit` (page web). On y calcule un plan, on clique « Je prends ce trade » pour le
suivre, et on voit pour chaque trade le prix en direct (toutes les 10 s) avec un mini-graphe des
2 dernières heures et les niveaux SL/entrée/TP, les TP/SL touchés avec l'heure, un compte à rebours
vers chaque TP restant, les signes de sortie et le journal des trades terminés (résultats en R).
Les prix des actions viennent des bougies déjà lues par le suivi : la page ne coûte aucun crédit
Twelve Data. Ajoutée à l'écran d'accueil du téléphone, elle s'ouvre comme une appli.
Le même tableau est réécrit chaque minute dans un message Discord (à épingler une fois). Tout tourne sur ton serveur : aucun token Claude n'est consommé.

| Route | Rôle |
|---|---|
| `POST /api/trading/plan` | Calcule un plan sans l'enregistrer (`?discord_post=true` pour l'envoyer sur Discord) |
| `POST /api/trading/trades` | Calcule le plan, l'enregistre et le suit (annonce sur Discord) |
| `GET /api/trading/live` | Dernier prix et mini-graphe (2 h en 1 min) des trades ouverts, pour le cockpit |
| `GET /api/trading/trades?status=open` | Trades suivis, avec le dernier prix, la variation et le gain en R |
| `POST /api/trading/trades/{id}/close?price=…` | Clôture à la main |
| `DELETE /api/trading/trades/{id}` | Supprime un trade |

Corps des requêtes : `{"symbol": "NVDA", "market": "stock", "side": "long"}` (`market` : `crypto` ou
`stock`). Facultatifs : `entry` (vide = dernier cours), `stop` (vide = niveau d'invalidation),
`targets` (vide = prochaines résistances), `capital` et `risk_pct` pour la taille de position.

**Délais estimés.** Pour chaque TP, JARVIS rejoue le même plan (mêmes écarts en %) depuis chaque
bougie de l'historique de l'actif et mesure le temps qu'il a fallu pour toucher le TP avant le
stop : médiane, fourchette 25-75 %, heure estimée (Paris), part des cas où le TP a été atteint
avant le stop et espérance en R. Crypto : bougies d'1 h sur 30 jours (Kraken), horizon 24 h.
Actions : bougies de 15 min sur environ 190 séances (Twelve Data), horizon 2 séances, en ne
comptant que les heures de cotation.

**Suivi.** Chaque minute pour la crypto, et toutes les 2 minutes pendant la séance pour les actions (limite gratuite de Twelve Data), les bougies d'1 minute disent si un TP ou le stop a été touché ;
Discord reçoit l'alerte avec le prochain TP et son délai, sur le salon et en message privé (bot configuré). Le stop reste en place après le TP1 et
remonte à l'entrée après le TP2. Un TP en retard sur ce que 3 cas passés sur 4 avaient fait, ou un
signe de retournement (divergence, anomalie de volume, bougie de retournement), déclenche une
alerte de sortie anticipée. Les trades sont gardés dans `data/trades.json` (`JARVIS_TRADES_PATH`).

## Note composite et verdict GO / NO GO

Chaque plan reçoit une note sur 10, avec ses raisons ligne par ligne (`app/trading/score.py`) :

| Sous-note | Poids | Ce qui compte |
|---|---|---|
| Technique | 40 % | checklist EmperorBTC, RSI 14 jours dans la zone 45-75, R/R du plan (mesuré sur le TP2), bonus divergence et bougie |
| Catalyseur | 30 % | actus réelles des 7 derniers jours et leur tonalité, résultats trimestriels récents ou imminents, augmentation de capital (actions seulement, Finnhub) |
| Risque | 30 % | stop en ATR, mur proche (TP1 trop près), espérance rejouée sur l'historique |

Sans catalyseur évaluable (crypto), la note se calcule sur les deux autres. **GO** si la note
atteint 7 et qu'aucun veto ne tombe : résultats dans les 48 h, ou R/R sous 2. Le R/R n'est pas
suivi à la lettre : jusqu'à 0,2 en dessous, il passe si la note dépasse 7,5.

Tous les seuils vivent dans `app/trading/settings.py` (`GET /api/trading/settings`, `!regle`).
Ils évoluent par propositions : JARVIS propose, tu approuves ou refuses (voir plus bas).

**JARVIS n'est pas d'accord parce que c'est toi.** Si tu prends un plan noté NO GO (`!prends` ou le
cockpit), il le dit franchement avec ses raisons, suit le trade quand même et le marque « contre l'avis
de JARVIS ». `!desaccords` (ou `GET /api/trading/disagreements`) compare ces trades aux plans GO, avec
un tiers de la position par objectif : au bout de 10 trades terminés de chaque côté, il dit qui avait
raison, toi ou sa note. Le même principe s'applique aux Claude qui travaillent sur JARVIS
([CLAUDE.md](CLAUDE.md)).

## Scanner avant l'ouverture US

Chaque jour de Bourse à 9 h 10 heure de New York (15 h 10 à Paris), JARVIS examine les 100
actions les plus actives et les 50 plus fortes hausses (Alpaca), plus la liste suivie, et garde
celles qui sont en jeu et tradables : volume de la veille ≥ 1,5× la moyenne ou gap ≥ 2 %, prix ≥ 5 $,
ATR ≥ 2,5 %, au-dessus des MM 20 et 50 jours, RSI entre 45 et 75, pas d'augmentation de capital
ni de résultats dans les 48 h. Pas de minimum de liquidité ni de capitalisation. Les 5 meilleures
reçoivent un plan complet et leur note, envoyés sur Discord. `GET /api/trading/scan` le lance à la demande.

## Forward-test sur le compte fictif

Chaque valeur du scanner (GO comme NO GO) est passée en silence sur le compte fictif Alpaca :
trois ordres « bracket » (un tiers de la taille vers chaque TP, même stop), annulés si l'entrée
n'est pas touchée dans la séance, soldés après 2 séances. Aucun message trade par trade : seul un
bilan agrégé part le vendredi à 16 h 30 (New York), avec le résultat moyen en R par verdict,
par note, par RSI, par volume, par gap et par catalyseur, l'écart entre délais prévus et réels,
`GET /api/trading/paper/stats` ou `!bilan` à tout moment.

Quand un groupe a 15 exemples et un écart net, JARVIS en tire une **proposition de réglage**
numérotée (ex. « `rsi_max` 75 → 65 : RSI ≥ 65 à -0,40 R sur 18 plans »). Elle arrive en message
privé Discord avec des boutons Approuver / Refuser, et dans le cockpit. Rien ne change sans ton
accord ; une proposition refusée n'est pas reproposée avec la même valeur.

## Labo de performance

Un second service, `jarvis-lab` (`python -m app.lab.worker`), simule des centaines de trades par
jour avec **le même code que le direct** (`app/trading/plan.py`, `signals.py`, `score.py`, réglages
de `settings.py`, via `service.plan_from_bars`) et les journalise dans SQLite (`data/lab.db`,
`JARVIS_LAB_DB`). Il ne passe aucun ordre, ni réel ni fictif.

- **Univers du jour**, calculé avec les seules données de la veille : ~300 actions US (les 200 plus
  gros volumes en dollars, puis les plus fortes variations et les plus forts volumes relatifs) et les
  50 paires USDT d'OKX au plus gros volume. Aucun filtre de liquidité ni de capitalisation. Le scanner
  en direct part du screener Alpaca (pré-ouverture comprise), qui ne se rejoue pas : c'est l'approximation
  la plus proche sans biais. La liste des actions inclut les valeurs retirées de la cote qu'Alpaca connaît
  encore. **Limite connue (crypto)** : OKX ne liste que les paires cotées aujourd'hui, donc l'univers
  crypto rejoué exclut les paires retirées pendant la période (biais du survivant : les résultats crypto
  du rejeu sont probablement flattés). Ces limites figurent aussi dans `limitations` de `/api/lab/status`.
- **Plans** : actions à 9 h 10 (New York) sur des bougies de 15 min, crypto toutes les 4 h (0 h, 4 h…
  20 h UTC) sur des bougies d'1 h ; longs seulement pour l'instant (colonne `side` prête pour les
  shorts). Le plan ne voit que les bougies closes à l'heure de décision. Catalyseur non rejoué
  (`context.catalyst_replayed = false`) : les actus sont collectées par les fiches catalyseurs du
  Labo 2 mais n'entrent pas encore dans la note. Délais rejoués sur les 1 500 derniers départs.
- **Exécution simulée** sur les bougies de 5 min qui suivent : ordre limité à l'entrée (exécuté s'il
  est traversé d'un pas de cotation, ou à l'ouverture s'il est déjà au marché), sorties 1/3 au TP1,
  1/3 au TP2, 1/3 au TP3, stop remonté à l'entrée après le TP1 ; stop prioritaire sur une bougie
  ambiguë, gap au-delà du stop exécuté à l'ouverture. Actions soldées à la clôture de la séance, crypto
  à 24 h. Coûts estimés (demi-écart + glissement selon le prix ou le rang de volume, frais OKX),
  regroupés dans `app/lab/simulator.py`. Résultats en R bruts et nets, délais de chaque TP, MFE/MAE.
- **Rejeu** de `JARVIS_LAB_REPLAY_DAYS` séances (504 ≈ 2 ans) avant le premier lancement, par
  morceaux (surtout la nuit et le week-end), reprenable après une coupure ; puis chaque séance et
  chaque créneau crypto en direct (`origin = live`). Les réglages sont figés dans une version de
  méthode (`v1` au premier lancement), gardée sur chaque trade.
- **Données** : Alpaca (bougies SIP, clés existantes) pour les actions, API publique d'OKX (sans clé)
  pour la crypto. Le labo prend au plus 100 requêtes Alpaca par minute (`JARVIS_LAB_ALPACA_PER_MINUTE`)
  et ne touche pas aux actions pendant la séance US (le direct garde la limite) ; il ne demande que
  les heures de séance. Une division d'actions détectée (bougie déjà stockée qui change de plus de 1 %)
  recharge tout l'historique du symbole. Sans clés Alpaca, la crypto tourne seule.
- **Frais crypto** : OKX « maker » 0,08 % pour les ordres posés (entrée limitée, TP), « taker » 0,10 %
  pour l'entrée au marché, le stop et la sortie à l'horizon.

`GET /api/lab/status` ou `!labo` : avancement du rejeu, nombre de trades par marché et origine,
dernier jour en direct, taille de la base, étape en cours du worker et son dernier signe de vie
(« ⚠️ Worker muet » après 30 min sans battement), et les chiffres clés du Labo 2 (espérance en R avec son
intervalle de confiance, fiches catalyseurs, règles en attente et testées).
Mise en route et vérification des sources : [DEPLOY.md](DEPLOY.md#labo-de-performance).

### Labo 2 : mesures et validation

Tout tourne dans le worker, sans jeton d'IA ; rien ici ne change la méthode en direct.

- **Statisticien** (`app/lab/stats.py`) : sur les trades déclenchés, en R nets de coûts : espérance
  avec intervalle de confiance à 95 % (erreur type « en grappes par jour » : les trades d'un même jour
  se ressemblent), taux de déclenchement, réussite, TP1 avant le stop, TP2, TP3, délais tenus (part
  des TP touchés au plus tard au délai médian prévu par le plan, ~50 % si le plan est bien calibré),
  calibration de la note (une note de 8 bat-elle une note de 7 ?), pire série de pertes, plus forte
  baisse cumulée. Découpages par marché, setup, tranche de note, heure du plan, jour de la semaine,
  version de méthode, origine (rejeu / direct), **régime de marché** (SPY ou BTC au-dessus ou sous sa
  MM 50 jours, volatilité 20 jours en tiers, calculé avec la seule clôture de la veille) et période
  (20 derniers jours contre le reste). Une tranche de moins de 30 trades est marquée
  « échantillon faible » ; une tranche n'est dite « dégradée » qu'à z ≤ -3,2 contre le reste de son
  marché. **Capture du potentiel +10** : parmi les valeurs de l'univers du jour dont le plus haut a
  dépassé l'ouverture d'au moins 8 % (l'ouverture et non la clôture de la veille : un ordre limité posé
  avant l'ouverture ne capture jamais le gap), combien avaient un plan, un plan déclenché, un TP1.
  Une passe en flux sur la table (≈ 2,6 s pour 200 000 trades), résultat gardé en cache par le worker.
- **Résumé du soir** (`app/lab/summary.py`) : chaque jour après 22 h 30 à Paris (et 30 min après la
  clôture NYSE), un JSON de moins de 8 Ko et quelques lignes : chiffres du jour et cumulés, tranches
  qui se dégradent, mouvements +10 du jour avec leurs fiches catalyseurs, idées déjà rejetées.
- **Validateur** (`app/lab/validator.py`) : une règle de filtre en JSON, par exemple
  `{"nom": "…", "exclude": {"field": "context.rvol", "op": ">", "value": 5}, "markets": ["stock"]}`
  (`keep` au lieu d'`exclude`, conditions `and` / `or` / `not`, opérateurs `> >= < <= == != in not_in
  between is_null not_null`, `"value": "auto"` pour un seuil choisi sur la période d'ajustement). Les
  champs sont ceux connus à l'heure du plan (`plan.…`, `context.…`, `composite`, `regime`…) ; les
  résultats du trade sont refusés. Validation « walk-forward » sur 3 fenêtres chronologiques
  (ajustement sur le passé, test sur les mois qui suivent, 1 jour d'embargo). Critères, tous
  obligatoires : au moins 100 trades touchés en test, +0,05 R par trade en moyenne, meilleure dans 2
  fenêtres sur 3, capture du potentiel +10 préservée, et p < 0,05 / n où n est le numéro de l'essai
  (le seuil monte avec le nombre d'idées testées). Une règle déjà testée renvoie son verdict gardé.
  v1 : filtres seulement ; les règles qui demanderaient de régénérer les plans viendront plus tard.
  Les seuils sont dans la table `lab_config` (défauts dans le code) et ne changent que sur ton accord.
- **Fiches catalyseurs** (`app/lab/catalysts.py`) : chaque mouvement d'environ ±8 % de l'univers du
  jour (depuis l'ouverture ou la veille), avec l'amplitude réelle, le gap, l'heure de l'extrême, le
  « départ » (première bougie de 5 min qui franchit le tiers de l'amplitude), le volume relatif des 30
  premières minutes, le secteur, le mouvement du BTC pour la crypto ; et les actus Finnhub (~12 mois
  de profondeur) avec leur heure exacte et leur position : avant l'ouverture, avant le départ,
  pendant, après. Quelques contre-exemples (valeurs qui n'ont pas bougé) pour ne pas biaiser les
  statistiques. Le type de catalyseur est classé par la routine du soir (Labo 3). Crypto : pas encore de source
  d'actus.
- **Accès pour la routine du soir** : `GET /api/lab/summary`, `/api/lab/stats`, `/api/lab/catalysts`,
  `/api/lab/rules` et `POST /api/lab/rules` (soumission seulement, le worker valide), protégés par
  `JARVIS_LAB_API_KEY` (en-tête `X-Lab-Key`). Lecture seule sur la base.

### Labo 3 : le soir, le matin, et la méthode qui s'améliore

Le serveur n'écrit jamais dans `data/lab.db` : ce qu'il reçoit (règles, décisions, types de
catalyseurs) passe par la file `data/lab_queue/`, que le worker lit à chaque passage.

- **Le soir (23 h)** : une routine Claude planifiée (hors de ce dépôt) lit `GET /api/lab/brief` (tout
  en un appel, < 20 Ko : résumé, statistiques, fiches à classer des 2 derniers jours, règles récentes,
  méthode en place et challenger), fait débattre ses analystes, puis envoie au plus **3 règles par
  soirée** (`POST /api/lab/rules`, avec `"pourquoi"` et `"council"` gardés avec la règle ; au-delà :
  429) et classe les fiches (`POST /api/lab/catalysts/types`, types de `catalysts.TYPES`). Accès par
  Tailscale Funnel sur `/api/lab` seulement, clé `JARVIS_LAB_API_KEY` (voir [DEPLOY.md](DEPLOY.md#ouvrir-laccès-à-la-tâche-du-soir)).
- **La nuit** : le worker valide les règles (Labo 2).
- **Le matin (8 h - 9 h, heure de Paris)** : un seul message privé groupé (`app/lab/morning.py`) avec
  chaque règle validée, sa preuve chiffrée (« Les trades où rvol > 5 font −0,40 R en moyenne sur 212
  trades ; les exclure donne +0,15 R par trade sur 3 périodes de test ») et son « pourquoi », plus le
  nombre d'idées écartées depuis la veille. Boutons Approuver / Refuser par règle, ou `!oui N` /
  `!non N` (mêmes propositions que les réglages, même cockpit). Rien de validé : pas de message.
  Jamais hors de 8 h - 9 h ; manqué, il repart le lendemain ; jamais deux le même jour, même après un
  redémarrage (état dans `data/lab_morning.json`, marqué avant l'envoi). **Rodage** :
  `JARVIS_LAB_DM=off` (défaut) garde le message sans l'envoyer (`!labo`, `/cockpit/labo`,
  `GET /api/lab/matin`) ; à passer à `on` après ~5 séances.
- **Carnet des leçons** (`app/lab/lessons.py`) : en plus des règles, la routine du soir écrit ce
  qu'elle a compris (`POST /api/lab/lessons`, au plus ~3 par soir, carnet de 50 leçons actives au plus).
  Une leçon ne filtre rien : le worker la re-mesure chaque nuit sur les trades du labo (espérance et
  TP1 des trades concernés contre l'ensemble, z en grappes par jour, même sens sur les deux moitiés de
  l'historique) et la passe **confirmée** ou **démentie** tout seul. Les démenties restent au carnet
  (ne pas les retenter). Les confirmées (`data/lab_lessons.json`) ajustent la note des plans en direct
  de ±0,5 chacune (±1 au plus), avec une ligne « Leçons du labo » dans le plan ; le labo, lui, note
  ses plans sans elles. La routine relit le carnet (dans `GET /api/lab/brief`) avant d'analyser ;
  Simão le lit avec `!lecons`.
- **Approuvée** : la règle crée la version suivante (v2 = v1 + ses filtres), d'abord **challenger**.
  Les trades restent journalisés une seule fois (version de génération v1) ; champion et challenger
  en sont des sous-ensembles calculés avec le même SQL que le validateur. Quand le challenger a au moins
  100 trades déclenchés **en direct** depuis sa création et un R net moyen par trade meilleur que le
  champion sur la même période, sans perdre de capture du potentiel +10 (même tolérance que le
  validateur), il devient la méthode en place et tu reçois « Méthode v2 en place : … ». Après 300
  trades sans faire mieux, il est retiré (message aussi). Seuils `challenger_min_trades` et
  `challenger_max_trades` dans `lab_config`. Des règles approuvées pendant un essai attendent sa fin
  (sauf le même matin : elles rejoignent le challenger tant qu'il n'a aucun trade).
- **Refusée** : rangée dans la mémoire des échecs (« refusée par Simão »), jamais reproposée.
- **En direct** : le worker écrit la méthode en place dans `data/lab_champion.json`. Le scanner
  pré-ouverture applique ses filtres avec le même code que le labo (`method.evaluate_rows`) : un plan
  écarté n'est ni proposé ni passé sur le compte fictif, il reste listé (« Écartées par la méthode v2
  du labo : … ») et dans `data/scans.jsonl`. Contexte du direct reconstruit comme au labo
  (`app/lab/live.py` : note sans la part catalyseur, volume relatif et variation de la veille, régime
  de SPY) ; champs absents traités comme « non vérifiés ». Fichier absent ou illisible : aucun filtre,
  une ligne de journal, le direct continue. `!plan` et le cockpit ne sont pas filtrés (demandes à toi).
- **Cockpit** : page `/cockpit/labo` (lecture seule) : grade, avancement, chiffres clés, méthode en
  place et challenger, dernières propositions, règles testées, dernier message du matin.
- **Grade de JARVIS** (`app/lab/grade.py`, barème validé par Simão le 2026-10-07) : de F- à S+, la
  preuve mesurée par le worker (sans jeton) que la méthode en place gagne, par marché.
  - Lettre = espérance nette en R sur les 60 derniers jours de bourse de la méthode en place (hors
    échantillon) : S ≥ +0,40 R, A ≥ +0,25, B ≥ +0,15, C ≥ +0,05 (borne basse de l'IC95 > 0 dès C),
    D positive mais pas prouvée, E entre −0,10 et 0, F < −0,10. Moins de 100 trades sur 10 jours :
    « non noté ».
  - − / + : stabilité (pire série de pertes ≤ 1,25 × celle attendue, écart ≤ 0,15 R entre les deux
    moitiés de la fenêtre) et délais calibrés (40 à 65 % tenus). Réussite et % de TP sont affichés,
    sans effet sur le grade (un taux de réussite se gonfle en rapprochant le TP1).
  - Risque proposé par plan (`!plan`, cockpit) : F, E, non noté 0 % (plan en observation), D 0,25 %,
    puis ¼ de Kelly (borne basse de l'espérance / variance des R) plafonné à C 1 %, B 1,5 %, A 3 %,
    S 5 %. Plus un budget : risque ouvert sur les trades pris ≤ 4 %, arrêt de la journée à −3 R. Un
    risque demandé explicitement (`risk_pct`) est gardé.
  - Changements : baisse immédiate ; hausse après au moins 150 nouveaux trades sur 5 nouveaux jours,
    et une hausse de **lettre** attend ton accord (proposition Approuver / Refuser en privé, `!oui N`) ;
    à partir de C, une baisse de 20 R depuis le sommet ramène tout de suite en D (coupe-circuit) ; une
    nouvelle méthode repart de « non noté ». IC95 entièrement sous zéro sur 150 trades : méthode
    déclarée morte (message). E, F ou morte : mode survie de la routine du soir. Message privé à
    chaque changement de lettre ; un − / + dans la même lettre (risque inchangé) reste dans `!grade`.
  - Le council du soir contrôle le grade (`POST /api/lab/grade/avis`, un avis par marché et par soir,
    un cran au plus : une baisse s'applique, une hausse de lettre passe par ton accord).
  - `!grade`, `GET /api/lab/grade` (historique), page `/cockpit/labo`, ligne 🏅 sous chaque plan.

### Labo v2 : stratégies à déclencheur et bloc scellé

v1 achète tout l'univers à heure fixe sans attendre de signal. Le labo v2 mesure, à côté d'elle et sur
les mêmes jours, des stratégies qui attendent leur déclencheur : pullback en tendance + bougie de
confirmation, cassure du range d'ouverture (15 et 30 min), cassure du plus haut de la veille puis retest,
et, depuis les déclarations n° 2 et 3, les supports et résistances de la formation EmperorBTC (zones
tracées sur les creux et sommets de réaction, contacts distincts, veille) : cassure d'une zone puis retest,
rebond confirmé par la cassure du dernier sommet de réaction, et trois sorties EmperorBTC (prochaines
résistances seules, plus sortie sur alerte de retournement, ou sur alerte confirmée par le prix).
Liste, seuils et sorties sont déclarés d'avance : [docs/labo-strategies.md](docs/labo-strategies.md).

- Chaque trade est mesuré avec sept sorties (gestion v1, sans break-even, 2 R, stop suiveur, trois
  EmperorBTC), plus trois en actions (déclaration n° 6, règles de Gemini : clôture sous le VWAP après
  10 h 30, time stop à 14 h 30, avalement baissier de SPY),
  trois tests de robustesse (glissement ×2 et ×3, entrée retardée, ordres limités à peine
  touchés) et un **témoin aléatoire** (même stop, mêmes objectifs, entrée au hasard, moyenne de 5
  tirages) : une stratégie qui ne bat pas son témoin n'a pas d'avantage d'entrée.
- Classement à l'espérance nette en R (intervalle en grappes par jour), jamais au taux de réussite ; en
  actions, après la commission IBKR Fixe estimée (0,005 $ par action), avec les frais par tranche.
- Diagnostic de v1 : espérance par gap d'ouverture, mode d'entrée, raison de sortie, brut contre net.
- **Bloc scellé** : le rejeu du 6 avril au 5 octobre 2026 est calculé mais rangé à part, invisible des
  statistiques, du brief, du grade et du validateur (`app/lab/seal.py`). Il sert une seule fois.
- Les jours traités avant le labo v2 sont rattrapés hors séance (bougies relues à la source, rien stocké).
- `!strategies`, `GET /api/lab/strategies`, ligne « Labo v2 » et ligne 🔒 de `!labo`.

## Avis sur un actif et conseil avant la clôture

`!avis NVDA` (ou `GET /api/trading/opinion?symbol=NVDA`) calcule le plan long et le plan short, prend
le côté GO (la meilleure note s'il y en a deux) et le ramène à « attendre » si aucun objectif n'a une
espérance positive une fois rejoué sur l'historique de l'actif. L'avis affiche ce que le labo a mesuré
(plans v1 rejoués, hors bloc scellé) : tant que le GO ne fait pas nettement mieux que le NO GO, il le dit.

30 min avant la clôture US (`JARVIS_CLOSE_ADVICE_MINUTES`), chaque action suivie encore ouverte reçoit
un conseil (aussi `!positions`, bouton « Que faire ? » du cockpit, `GET /api/trading/advice`) :

1. résultats trimestriels avant la fin de la prochaine séance → **SORTIR** ;
2. garder jusqu'au prochain objectif perd en moyenne (rejoué depuis le prix actuel avec le stop suivi) → **SORTIR** ;
3. objectif en retard et position qui ne gagne pas → **SORTIR** ;
4. signe de retournement sur une position gagnante → **ALLÉGER** (moitié dehors, stop à l'entrée) ;
5. le gap de nuit a sauté le stop dans au moins 10 % des 250 dernières nuits (`JARVIS_GAP_LIMIT`) → **ALLÉGER** ;
6. sinon **GARDER**, avec le stop à laisser.

Règles fixes, pas encore validées : chaque conseil est gardé sur le trade (un par jour) et, une fois le
trade terminé, comparé à une sortie au moment du conseil (ligne « Bilan des conseils »).

## Commandes Discord

| Commande | Effet |
|---|---|
| `!plan NVDA` | plan complet en temps réel (ajoute `short`, `crypto` ou `action` si besoin) |
| `!avis NVDA` | avis de JARVIS : entrer long, entrer short ou attendre, pourquoi, ce qui invaliderait, et ce que le labo a mesuré |
| `!prends NVDA` | calcule le plan et suit le trade (TP/SL annoncés, cockpit) ; `!prends NVDA 132.5 stop 128` = ton entrée et ton stop réels |
| `!positions` | trades suivis : sortir, alléger ou garder, avec les faits (aussi envoyé seul 30 min avant la clôture US) |
| `!position NVDA 132.5` | même conseil pour une position prise hors de JARVIS (`short`, `stop 128` en plus) |
| `!scan` | scanner tout de suite |
| `!bilan` | bilan du forward-test |
| `!desaccords` | trades pris contre l'avis de JARVIS (NO GO) comparés aux plans GO : qui avait raison |
| `!propositions` | réglages proposés qui attendent ta réponse |
| `!oui 3` / `!non 3` | approuver ou refuser la proposition 3 (ou les boutons) |
| `!regle` | réglages actuels |
| `!labo` | état du labo (rejeu, espérance, règles, méthode en place, challenger, propositions du labo) |
| `!lecons` | carnet des leçons du labo : confirmées, à confirmer, démenties, avec leur preuve chiffrée |
| `!grade` | grade de JARVIS (F- à S+) par marché, de quoi il est fait, risque permis, derniers changements |
| `!strategies` | labo v2 : stratégies à déclencheur contre v1, sorties, témoin aléatoire, diagnostic de v1 |
| `!aide` | liste des commandes |

Le bot ne répond qu'aux comptes de `JARVIS_DISCORD_OWNER_ID`. Mise en place : [DEPLOY.md](DEPLOY.md).

## Tests

```bash
.venv/bin/python -m pytest                 # tout
.venv/bin/python -m pytest -m live         # seulement les appels aux vraies API
.venv/bin/python -m pytest -m "not live"   # repli et horaires de la Bourse, hors ligne
```

Un test par source dans `tests/live/` appelle la vraie API et vérifie que les champs utilisés
existent toujours. Les tests des sources à clé sont ignorés tant que la clé est absente.

## Clés (fichier `.env`, jamais commité)

| Variable | Sert à | Inscription |
|---|---|---|
| `TWELVEDATA_API_KEY` | cours des actions (principale) | https://twelvedata.com/register |
| `FINNHUB_API_KEY` | cours en secours, actus générales et par action | https://finnhub.io/register |
| `DISCORD_WEBHOOK_URL` | notifications sur ton salon Discord | Paramètres du salon > Intégrations > Webhooks |
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | scanner et forward-test (compte fictif uniquement) | https://app.alpaca.markets |
| `DISCORD_BOT_TOKEN`, `JARVIS_DISCORD_OWNER_ID` | commandes Discord | voir [DEPLOY.md](DEPLOY.md) |

## Notifications Discord

Dès que `DISCORD_WEBHOOK_URL` est définie, le serveur envoie sur le salon :

| Notification | Quand | Réglage |
|---|---|---|
| Point du matin (Bourse de NY, agenda économique du jour, crypto, actions suivies, change, météo et air) | chaque jour à 9 h heure de New York, 30 min avant l'ouverture | `JARVIS_WATCHLIST_*`, `JARVIS_CITY` |
| Variation forte d'une valeur suivie | vérifié toutes les 5 min, une alerte par palier et par sens sur 24 h glissantes (cryptos : variation sur 24 h ; actions : sur la séance, pendant la séance) | `JARVIS_MOVE_ALERT_PCT` |
| Séisme fort dans le monde | vérifié toutes les 5 min, sans doublon si la source passe de l'USGS à l'EMSC | `JARVIS_QUAKE_MIN_MAGNITUDE` |
| Nouvelles actus de marché et des actions suivies | vérifié toutes les 15 min, 10 au plus par envoi | `JARVIS_WATCHLIST_STOCKS` (actus par action : clé Finnhub) |

Chaque message porte la mention des sources et signale une valeur qui n'est plus à jour.
Au premier lancement, les séismes et actus déjà publiés sont mémorisés sans être envoyés.

- `POST /api/notify/test` envoie un message de test.
- `POST /api/notify/brief` envoie le point du matin tout de suite.
- `GET /api/notify/brief/preview` montre le point du matin sans l'envoyer.
- `JARVIS_NOTIFICATIONS=off` coupe tous les envois sur le salon. Sans webhook ou avec `off`, le suivi des
  trades (TP/SL), le compte fictif et le scanner tournent quand même, sans rien envoyer sur le salon.

**En message privé** (bot Discord configuré), et seulement pour ça :

- copie des alertes des trades suivis : TP, stop, sortie anticipée, objectif en retard, conseil avant la
  clôture US (`JARVIS_TRADE_DM=off` pour la couper ; le salon ne mentionne personne, le privé sonne) ;
- changement de lettre du grade de JARVIS (un − / + dans la même lettre n'envoie rien), méthode déclarée
  morte, nouvelle méthode en place, challenger retiré ;
- réglages proposés avec boutons Approuver / Refuser (bilan du compte fictif, hausse de lettre du grade)
  et message du matin du labo (`JARVIS_LAB_DM=on`).
