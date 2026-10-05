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
