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

**Cockpit** : `/cockpit` (page web, rafraîchie toutes les 30 s). On y calcule un plan, on clique
« Je prends ce trade » pour le suivre, et on voit pour chaque trade le prix, les TP/SL touchés avec
l'heure, les délais estimés des TP restants, les signes de sortie et le journal des trades terminés.
Le même tableau est réécrit toutes les 2 minutes dans un message Discord (à épingler une fois).

| Route | Rôle |
|---|---|
| `POST /api/trading/plan` | Calcule un plan sans l'enregistrer (`?discord_post=true` pour l'envoyer sur Discord) |
| `POST /api/trading/trades` | Calcule le plan, l'enregistre et le suit (annonce sur Discord) |
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

**Suivi.** Toutes les 2 minutes, les bougies d'1 minute disent si un TP ou le stop a été touché ;
Discord reçoit l'alerte avec le prochain TP et son délai. Le stop reste en place après le TP1 et
remonte à l'entrée après le TP2. Un TP en retard sur ce que 3 cas passés sur 4 avaient fait, ou un
signe de retournement (divergence, anomalie de volume, bougie de retournement), déclenche une
alerte de sortie anticipée. Les trades sont gardés dans `data/trades.json` (`JARVIS_TRADES_PATH`).

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
