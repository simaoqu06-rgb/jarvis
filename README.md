# JARVIS

Assistant personnel (usage non commercial). Ce dépôt contient le serveur Python (FastAPI)
qui récupère les données en temps réel pour l'agent JARVIS et ses futures notifications.

## Lancer

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
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
Discord reçoit l'alerte avec le prochain TP et son délai. Le stop reste en place après le TP1 et
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

## Commandes Discord

| Commande | Effet |
|---|---|
| `!plan NVDA` | plan complet en temps réel (ajoute `short`, `crypto` ou `action` si besoin) |
| `!prends NVDA` | calcule le plan et suit le trade (TP/SL annoncés, cockpit) |
| `!scan` | scanner tout de suite |
| `!bilan` | bilan du forward-test |
| `!propositions` | réglages proposés qui attendent ta réponse |
| `!oui 3` / `!non 3` | approuver ou refuser la proposition 3 (ou les boutons) |
| `!regle` | réglages actuels |

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
| Variation forte d'une valeur suivie | vérifié toutes les 5 min, une alerte par palier et par jour (actions : pendant la séance) | `JARVIS_MOVE_ALERT_PCT` |
| Séisme fort dans le monde | vérifié toutes les 5 min | `JARVIS_QUAKE_MIN_MAGNITUDE` |
| Nouvelles actus de marché et des actions suivies | vérifié toutes les 15 min, 10 au plus par envoi | `JARVIS_WATCHLIST_STOCKS` (actus par action : clé Finnhub) |

Chaque message porte la mention des sources et signale une valeur qui n'est plus à jour.
Au premier lancement, les séismes et actus déjà publiés sont mémorisés sans être envoyés.

- `POST /api/notify/test` envoie un message de test.
- `POST /api/notify/brief` envoie le point du matin tout de suite.
- `GET /api/notify/brief/preview` montre le point du matin sans l'envoyer.
- `JARVIS_NOTIFICATIONS=off` coupe tous les envois.
