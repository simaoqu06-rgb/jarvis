# Carte de l'interface : le nouveau Trading Cockpit de JARVIS

Phase 1 de la refonte (prompt corrigé du 28/10/2026, `/mnt/project-files/cockpit/prompt-cockpit-corrige-2026-10-28.md`).
Ce document relie chaque écran du futur cockpit à une donnée réelle de JARVIS : fonction, fichier, route, fréquence.
Il est la référence des PR 2 et 3 : **une valeur affichée qui n'est pas ici n'a pas le droit d'exister**.
Depuis la PR 3, les huit vues sont en place ; « PR 3 ✓ » marque ce qu'elle a ajouté.

Statuts : **disponible** (lu tel quel), **à exposer** (existe dans JARVIS, la PR 1 l'expose sous `/api/cockpit/*`,
sauf mention « PR 2/3 »), **manque** (n'existe pas : état vide qui dit pourquoi).

Rappel de fond : **JARVIS ne passe aucun ordre réel.** Il propose des plans ; Simão passe ses ordres lui-même chez
IBKR (aucune API). Le seul compte que JARVIS touche est le compte fictif Alpaca du forward-test. Le cockpit est un
tableau de bord et un journal, pas un terminal d'ordres.

---

## 1. Inventaire

### 1.1 Processus et boucles

Deux conteneurs (`docker-compose.yml`) : `jarvis` (FastAPI + planificateur + bot Discord, `127.0.0.1:8000`) et
`jarvis-lab` (`python -m app.lab.worker`, sans port). Le cockpit vit dans `jarvis`, qui tient l'état en mémoire.

Boucles du planificateur (`app/scheduler.py`, `CORE_JOBS` tournent toujours, `JOBS` seulement avec
`DISCORD_WEBHOOK_URL` et `JARVIS_NOTIFICATIONS` ≠ off ; plus `tick_lab` et le bot) :

| Boucle (`name`) | Fonction | Période | Verrou global | Ce qu'elle fait |
|---|---|---|---|---|
| suivi des trades et cockpit Discord | `tick_trades` | 60 s | oui | rejoue les bougies 1 min des trades de `data/trades.json` (`service.check_trades`), TP/SL, signaux de sortie ; réécrit le message épinglé (`cockpit_text`) |
| scanner pré-ouverture | `tick_scan` | 60 s | pour écrire | à `scan_time_ny` (09:10 NY) les jours de séance : `scanner.scan`, message, GO enregistrés pour la confirmation, picks sur le compte fictif |
| conseil avant la clôture | `tick_closing` | 60 s | pour écrire | 30 min avant la clôture (`JARVIS_CLOSE_ADVICE_MINUTES`) : sortir / alléger / garder ; rappel de vente des KILL |
| forward-test sur le compte fictif | `tick_paper` | 120 s | oui | relit les ordres Alpaca fictifs ; bilan du vendredi et propositions de réglages |
| contrôle des GO après l'ouverture | `tick_confirm` | 60 s | pour écrire | de 09:45 à 11:30 NY, bougies 5 min Twelve Data : GO confirmé (MP avec l'ordre), annulé, expiré ; mesure à la clôture (`confirmations.jsonl`) ; **depuis la PR 1, note chaque GO confirmé au journal des recommandations** |
| potentiel +10 confirmé | `tick_plus10` | 60 s | pour envoyer | n° 9 : listes de 09:46 et 10:01 NY (mesurées, jamais des GO) |
| challenger Gemini | `tick_gemini` | 60 s | pour envoyer | passages A 09:36, B 12:36, C 15:31 NY ; solde le lendemain contre le témoin |
| visibilité des actus Finnhub | `tick_visibilite` | 60 s | non | audit anti-fuite, rien n'est envoyé |
| **journal des recommandations** (nouveau) | `tick_reco` | 60 s | pour écrire | reprend les KILL, fait avancer GO confirmés et KILL sur les bougies 5 min (`app/trading/reco.py`) ; rien n'est envoyé |
| labo : message du matin | `tick_lab` | 60 s | oui | message du matin du labo, annonces du worker, propositions de grade et de méthode |
| file d'envoi Discord | `tick_outbox` | 60 s | non | vide `data/outbox.json` |
| point du matin | `tick_brief` | 60 s | oui | 09:00 NY |
| mouvements de cours / séismes / actus / disque / veille des routines | `tick_moves`, `tick_quakes`, `tick_news`, `tick_disk`, `tick_routines` | 5, 5, 15, 30, 5 min | oui | alertes Discord |

**Dernière exécution** : n'existait pas. La PR 1 l'ajoute (`scheduler.last_runs[name] = {at, ok, every_s, duree_s |
erreur}`, posé par `_loop`). Péremption du suivi : 3 min sans passage (`LOOP_STALE_S`).

Worker du labo : battement `lab.heartbeat` dans le dossier de `JARVIS_LAB_DB` (`app/lab/heartbeat.py`,
`{le, etape}`), lu par `heartbeat.read` (`il_y_a_s`). Périmé au-delà de 30 min (même règle que le healthcheck de
`jarvis-lab`).

### 1.2 Marchés

- Actions US : calendrier NYSE calculé sans réseau (`app/trading/calendar.py` : fériés de la règle 7.2, demi-séances
  à 13 h, `SPECIAL_CLOSURES`, `JARVIS_NYSE_EXTRA_CLOSED`). Pré-marché 04:00–09:30, séance 09:30–16:00, après-bourse
  16:00–20:00 (17:00 une demi-séance), heure de New York. `cockpit_api.market_sessions` en tire l'état, la prochaine
  bascule et l'écart Paris − New York (5 h pendant les semaines de décalage : ouverture 14 h 30 à Paris du 25/10 au
  1/11/2026, testé).
- Crypto : 24/7, Kraken (bougies 1 min, cache 15 s ; 1 h, 4 h, 1 j).

### 1.3 Modules (entrées → sorties)

| Module | Fichier | Entrées | Sorties |
|---|---|---|---|
| Plan EmperorBTC | `trading/service.py` (`build_plan`, `plan_from_bars`), `plan.py`, `levels.py` | bougies (Kraken / Twelve Data, Alpaca en secours), catalyseur | entrée, stop (`stop_source`, plancher 1/3 ATR jour), TP1-3 avec `pct`, `r_multiple`, `hit_rate`, délais (`eta_text`, `eta_median`, `eta_latest`, `minutes_*`), checklist, note composite (`score`), grade, taille (`sizing` / `sizing_regle`), brackets IBKR (`ibkr`) |
| Note composite + catalyseur | `score.py`, `catalyst.py` | plan, journalier, actus Finnhub | `score.go` (GO / NO GO), `composite`, sous-notes, raisons ; « priorité, pas une probabilité » |
| Scanner pré-ouverture | `scanner.py` | Alpaca (screeners, bougies), réglages | picks (plan + `features`), `data/scans.jsonl` |
| Confirmation des GO | `confirm.py` | GO du jour, bougies 5 min | état (`attente`, `repli`, `confirmé`, `annulé`, `expiré`), déclencheur, MP d'ordre, `data/confirmations.json(l)` |
| **Journal des recommandations** (PR 1) | `reco.py` | GO confirmés (MP), KILL (`killmode_calls.json`), bougies 5 min | `data/reco.json` : état, entrée exécutée, objectifs touchés, R brut, $ / € nets à la taille du message |
| Killmode | `killmode.py` | `!kill`, données Alpaca, Finnhub | verdict KILL / PRÊT / non, journal des appels, bilan (`!kill bilan`) ; interrupteur `data/killmode.json` |
| Avis et conseils en prix | `advice.py` | trade ou symbole, bougies | `!avis` (entrer, attendre), conseils par trade (sortir / alléger / garder, ventes, stop suiveur, renforts) gardés dans `trade["conseils"]` |
| Fiche société | `company.py` | Finnhub, SEC | `!societe` |
| Désaccords | `service.disagreements` | trades suivis, pente de la note au labo | avec / contre l'avis de JARVIS, taille à la règle |
| Forward-test Alpaca | `paper.py` | picks du scanner (GO et NO GO) | ordres fictifs 3 brackets, `data/paper.json`, bilan et propositions |
| Challenger Gemini | `gemini.py` | Alpaca, scans A/B/C | plans, soldes j0/j1 contre témoin, `data/gemini_challenger.json` |
| Potentiel +10 | `plus10.py` | gappers | listes 09:46 / 10:01 NY |
| Grade | `lab/grade.py` | labo (`lab_grade.json`), trades pris | lettre par marché, risque proposé, risque ouvert, arrêt du jour |
| Labo | `app/lab/*` | rejeu, direct, déclarations | statut, déclarations (n° 12 « ouverture »), council, `!veto`, leçons, autopsie, ratés +10, 8-K, Polymarket Fed |
| Propositions | `trading/proposals.py` | forward-test, labo, grade | `data/trading_proposals.json`, Approuver / Refuser |
| Recherche avant l'ouverture | `trading/research.py` | routine Claude | verdicts soutient / neutre / contre |

### 1.4 SL et TP d'un trade suivi (`data/trades.json`, `Book.add`)

Champs exacts : `entry`, `stop` (initial, sur la structure), `stop_current` (suivi : remonté à l'entrée **après TP2**,
**après TP1 pour un KILL**, ou par un conseil `advice.record` quand il dit de remonter), `stop_moved_at`, `stop_pct`,
`targets[]` (`name`, `price`, `pct`, `r_multiple`, `hit_rate`, `eta_text`, `eta_median`, `eta_latest`,
`minutes_median`, …), `targets_hit`, `sortie_unique` (indice de l'objectif de sortie : 1 = TP2 par défaut depuis le
25/10 ; `null` avec `JARVIS_IBKR_BRACKETS=3`, pour la crypto, un plan sans 3 objectifs ou un KILL), `events[]`
(`tp1`…, `stop` au pire du stop et de l'ouverture sur un gap, `close` avec `reason`, `corrige` après `!sortie`),
`exit_signals`, `late_warned`, `last_price`, `last_check`, `killmode`, `policy` (`go_confirme`), `side` (`short`
possible : SL au-dessus, TP en dessous). Objectif touché = franchi d'un pas de cotation. Aucun stop suiveur n'est un
ordre : seulement des conseils en prix (`conseils`).

### 1.5 Comptabilité

- **Réel (Mes trades)** : `service.realized_r` (R brut, sortie unique ou un tiers par objectif), `realized_r_net`
  (frais IBKR estimés, `fees.py`, barème `JARVIS_IBKR_PLAN`), € = R net × `sizing.risk_amount_eur` (inconnu sans
  taille). Capital : `JARVIS_CAPITAL`, **déclaré**, en euros. **Manque** : équité, trésorerie et exécutions IBKR
  (aucune connexion) → jamais affichées.
- **Papier (recommandations)** : `reco.result` : R brut du moteur, puis $ brut, commissions IBKR et $ / € nets à la
  quantité envoyée ; R net = $ net / (quantité × risque). « Si j'avais tout suivi » = capital déclaré + somme des €
  nets (`reco.bilan`).
- **Portefeuille fictif (Papier)** (choix de Simão du 29/10) : `reco.portfolio`, départ 1 000 €, chaque recommandation
  close rejouée à **20 € de risque fixe** (`K.shares(|entrée − stop|, eurusd du message)`), frais IBKR déduits, même
  quand le message n'a pas de taille (grade F). Indépendant de la taille du message et du grade.
- **Forward-test Alpaca** (labo) : `paper.stats`, en R, 3 brackets, sans frais. Ce n'est **pas** l'espace Papier.

### 1.6 Risque

`lab_grade.risk_now(market, trades)` : grade (lettre, `risque_grade`, `risque_propose`, `raison`, `perime`),
risque ouvert (`open_risk`, ≤ 3 % `RISQUE_OUVERT_MAX`), R du jour net (`day_r`, arrêt à −3 R `ARRET_JOUR_R`),
fraîcheur (`age_h`, périmé au-delà de 6 h `JARVIS_GRADE_MAX_AGE_H`). Taille : min(capital × % du grade, 20 €
`JARVIS_RISK_EUR`) (`service.recommended_eur`), actions entières (`euro_sizing`). Grade F partout aujourd'hui → 0 %.

### 1.7 Journal

Trades terminés (`book.trades`, statut `closed`), désaccords (`service.disagreements`), confirmations
(`confirm.read_log`), forward-test (`paper.stats`), propositions décidées (`proposals.items`), conseils mesurés
(`advice.advice_stats`), recommandations closes (`reco.json`).

### 1.8 Intégrations, quotas, preuve de bon fonctionnement

| Source | Usage | Quota / cache | Preuve |
|---|---|---|---|
| Twelve Data | bougies actions temps réel (suivi, confirmation, plans) | 8 crédits/min (`_spend`), 800/jour en gratuit ; **compteur du jour ajouté** (`twelvedata.credits_today`, ce processus seulement) | `sources_health` (nouveau) |
| Alpaca données | scanner, secours, Killmode, cockpit hors suivi | SIP 16 min de retard (IEX en secours), gratuit | idem |
| Alpaca compte fictif | forward-test | — | idem |
| Kraken | crypto | cache 15 s (1 min) | idem |
| Finnhub, SEC EDGAR, Frankfurter (BCE), CoinLore / CoinPaprika, Nager.Date, Polymarket | catalyseur, 8-K, change, crypto, fériés, Fed | caches `fallback.store` | idem |

**Écart majeur** : `GET /api/health` existe mais renvoie seulement `{"status": "ok"}`. La santé des sources n'existait
pas : la PR 1 ajoute `app/core/sources_health.py`, alimenté par `core/http._get` et `alpaca._request` (dernier
succès, dernière erreur en texte court, latence, compteurs ; ni URL ni paramètre gardés). Le worker du labo n'y est
pas visible (autre processus).

### 1.9 Stockage

Tout sous `data/` (écrit par `jarvis` sous `scheduler._lock`) : `trades.json`, `reco.json` (nouveau), `paper.json`,
`trading_proposals.json`, `trading_settings.json`, `confirmations.json(l)`, `scans.jsonl`, `killmode*.json`,
`killmode_bars/`, `killmode_ticks/`, `gemini_challenger.json`, `plus10_*`, `research.json`, `outbox.json`,
`alerts_state.json`, `last_known.json`, `lab_morning.json`. Labo : `data/lab.db` (≈ 4,7 Go, écrit **seulement** par le
worker ; lu par `lab.db.open_readonly`), `lab_grade.json`, `lab.heartbeat`.

### 1.10 Configuration (noms seulement)

Clés : `TWELVEDATA_API_KEY`, `FINNHUB_API_KEY`, `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPACA_PAPER`,
`DISCORD_WEBHOOK_URL`, `DISCORD_BOT_TOKEN`, `JARVIS_DISCORD_OWNER_ID`, `JARVIS_LAB_API_KEY`. Réglages :
`JARVIS_CAPITAL`, `JARVIS_RISK_EUR`, `JARVIS_RISK_PCT`, `JARVIS_IBKR_PLAN`, `JARVIS_IBKR_BRACKETS`,
`JARVIS_WATCHLIST_STOCKS`, `JARVIS_WATCHLIST_CRYPTO`, `JARVIS_STOCK_CHECK_SECONDS`, `JARVIS_GRADE_MAX_AGE_H`,
`JARVIS_MP_TAILLE_APRES_ARRET`, `JARVIS_NOTIFICATIONS`, `JARVIS_TRADE_DM`, `JARVIS_DISCORD_COCKPIT`,
`JARVIS_NYSE_EXTRA_CLOSED`, `JARVIS_ALLOWED_ORIGINS`, `JARVIS_LAB_*`, chemins `JARVIS_*_PATH` (dont
`JARVIS_RECO_PATH`, nouveau), `TWELVEDATA_CREDITS_PER_MINUTE`, `TWELVEDATA_CREDITS_PER_DAY` (nouveau, 800).
Réglages de la note et du scanner : `data/trading_settings.json` (`settings`, `GET /api/trading/settings`).

---

## 2. Mes trades / Papier

| | **Mes trades** (braise) | **Papier** (lavande) |
|---|---|---|
| Ce que c'est | trades IBKR que Simão **déclare** (`!prends`, « Je prends ce trade ») | chaque recommandation **actionnable** envoyée sur Discord, prise ou non, suivie **telle qu'envoyée** |
| Source | `data/trades.json` (`scheduler.book`) | `data/reco.json` (`scheduler.reco_book`, PR 1) |
| Entrée | prix déclaré | déclenchement de l'ordre stop du message (pire de l'ordre et de l'ouverture) |
| Gestion | stop fixe, à l'entrée après TP2 (TP1 pour un KILL), sortie unique TP2 par défaut, pas de sortie au temps, `!sortie` pour le vrai prix | celle du message : GO confirmé = sortie unique TP2, stop à l'entrée après TP2, pas de sortie au temps ; KILL = un bracket par objectif, stop à l'entrée dès TP1, vendu à la clôture |
| Taille | `sizing` / `sizing_regle` du plan | la quantité du message (sans taille dans le message : R seulement, € inconnus) |
| Résultat | R brut, R net de frais estimés, € | R brut, $ et € nets de commissions IBKR, R net |
| Contenu exclu | — | NO GO, GO annulés / expirés, ordres non déclenchés (comptés « pas déclenché »), `!avis`, `!plan`, témoins Killmode, Gemini, potentiel +10 |
| Lien | — | `reco.linked_trade` : trade de Mes trades du même jour pris via `!prends … go` / `kill` → l'écart mesure l'exécution |

**Commun aux deux** : signaux et plans du jour (scanner, GO / NO GO, confirmations), séances, agenda, labo, risque
(grade, calculé sur Mes trades), santé, propositions.

**Pas de « promotion papier → réel » dans JARVIS.** Le chemin réel d'une méthode : déclaration au labo (une par fil,
pré-enregistrée) → essais contre témoin aléatoire → council autonome (déclare, approuve, promeut ; `!veto` de Simão)
→ grade mesuré → risque proposé (les **hausses** de lettre restent à Simão, en proposition). Le cockpit le montre
dans la vue Labo, pas comme un bouton.

**Verrou de Mes trades** (choix de Simão du 28/10, contre l'avis de JARVIS, pari noté) : ouverture sur Papier, appui
maintenu 1,1 s pour entrer dans Mes trades ; le serveur exige `"space": "trades"` et `"confirm": "MES_TRADES"` sur
les actions qui écrivent dans cet espace (PR 2 ✓, `POST /api/cockpit/actions`, §4.10). Depuis le Papier, « Je prends ce
trade… » d'un plan demande le même appui maintenu, puis ouvre Mes trades et y déclare le trade.

---

## 3. Table écran ↔ données

Fréquence : `state` toutes les 5 s (sans appel réseau) ; vues lourdes toutes les 10 s quand elles sont affichées.

### 3.1 Barre du haut et rail

| Élément | Source | Champs | Statut |
|---|---|---|---|
| Pastille « JARVIS · suivi il y a 40 s » | `state.jarvis.suivi` ← `scheduler.last_runs["suivi des trades et cockpit Discord"]` | `at`, `age_s`, `stale` | à exposer (PR 1 ✓) |
| Pastille du labo | `state.lab` ← `heartbeat.read` | `etape`, `age_s`, `stale` (> 30 min) | à exposer (PR 1 ✓) |
| Horloge Paris | horloge du navigateur | — | disponible |
| Bascule d'espace | `?space=trades|paper` | — | PR 2 ✓ (appui maintenu vers Mes trades, touche M) |
| Bannière API muette | échec de `GET /api/cockpit/state` (délai 8 s) | message d'erreur, âge des dernières données | PR 2 ✓ |

### 3.2 Cockpit (1)

| Carte | Source | Champs | Statut |
|---|---|---|---|
| Résumé · Mes trades | `state.portfolio` (space=trades) ← `realized_r`, `realized_r_net`, `sizing.risk_amount_eur`, positions | `latent{r,eur}`, `jour{r,r_net,eur,fees_eur}`, `semaine{…}`, `positions_ouvertes`, `capital.eur` « déclaré » | à exposer (PR 1 ✓) |
| Résumé · Papier | `state.portfolio` (space=paper) ← `reco.bilan` | `si_tout_suivi{equite_eur, eur_net, r_net, taux_reussite, par_etat}`, `recos_du_jour`, `go_en_attente_de_confirmation` | à exposer (PR 1 ✓) |
| Portefeuille fictif · Papier | `state.portfolio.portefeuille` ← `reco.portfolio` | `depart_eur`, `risque_par_trade_eur`, `capital_eur`, `resultat_eur`, `rendement_pct`, `closes`, `gagnantes`, `sans_resultat`, `r_net`, `ouvertes`, `latent_eur`, `points[[clos_at, €]]` (courbe de la carte Papier) | exposé |
| Courbe du P&L cumulé | `state.portfolio.courbe` ← `_closed_rows` | Mes trades : R nets cumulés des clôtures du jour ; Papier : € nets (R sans taille) depuis la mise en service | à exposer (PR 2 ✓, calculée par le serveur) |
| Recommandations en attente du déclenchement (Papier) | `state.portfolio.en_attente` ← recos `attente` | `symbol`, `type_fr`, `entry`, `stop`, `qty`, `valid_until` | à exposer (PR 2 ✓) |
| Séances (cadran) | `state.market_sessions` ← `calendar.py` | `etat`, `aujourd_hui{premarche, ouverture, cloture, fin_apres_bourse}`, `prochain_changement`, `prochaine_ouverture`, `raison_fermeture`, `demi_seance`, `ecart_paris_new_york_h` | à exposer (PR 1 ✓) |
| Trades ouverts | `state.positions_open` (bloc §4.9) | piste stop → objectif, prochain TP + délai, P&L latent | à exposer (PR 1 ✓) |
| Risque (3 jauges) | `state.risk` ← `lab_grade` | `risque_ouvert_pct` / `risque_ouvert_max_pct`, `r_du_jour_net` / `arret_jour_r`, `grades.{stock,crypto}` | à exposer (PR 1 ✓) |
| Plans du jour | `state.plans_today` ← `research.last_scan`, `scheduler.confirm_state` | picks GO / NO GO, `composite`, `raisons`, `confirmation`, `declencheur` | à exposer (PR 1 ✓) |
| À venir aujourd'hui | `state.agenda_today` ← horaires internes de JARVIS | `at`, `titre`, `categorie`, `source`, `passe` | à exposer (PR 1 ✓) ; calendrier économique, résultats et 8-K : vue Agenda (PR 3 ✓) |
| Propositions en attente | `state.proposals_pending` ← `PR.proposals.pending()` | `id`, `kind`, `key`, `value`, `current`, `why` | disponible |

### 3.3 Marchés (2)

| Carte | Source | Statut |
|---|---|---|
| Graphique bougies + volume | `GET /api/cockpit/instrument/{symbol}?tf=` ← crypto Kraken ; action : cache du suivi, sinon Alpaca (16 min de retard). **Jamais Twelve Data** | à exposer (PR 1 ✓) |
| Lignes SL / TP / entrée, zones, délais | `instrument.positions[]` (bloc §4.9) | à exposer (PR 1 ✓) |
| Bandeau « En position » | `state.positions_open` | à exposer (PR 1 ✓) |
| Watchlist | `GET /api/cockpit/instruments` ← `JARVIS_WATCHLIST_*` + symboles en position | à exposer (PR 1 ✓) |
| Avis JARVIS (touche A), Fiche société, Nouveau plan (`+`) | actions `opinion`, `company`, `plan` (mêmes fonctions que `!avis`, `!societe`, `POST /api/trading/plan`) : **seules dépenses Twelve Data venues de la page, au clic, et l'écran le dit** | PR 2 ✓ |
| Conseil « stop à … » (ligne info) | `position.conseil` (dernier conseil gardé) | à exposer (PR 1 ✓) |

### 3.4 Positions (3)

| Carte | Source | Statut |
|---|---|---|
| Trades sur le graphique | `GET /api/cockpit/positions/charts?tf=15m` (70 bougies) | à exposer (PR 1 ✓) |
| Tableau des ouverts, ordres IBKR en prix | `positions_open[].ibkr_orders` (plan enregistré / message) | à exposer (PR 1 ✓) |
| Clôturés récemment | `GET /api/cockpit/journal?space=` `clotures` | à exposer (PR 1 ✓) |
| Conseils en prix, « J'ai vendu à… », « Rouvrir un trade arrêté » | actions `advice`, `exit`, `reopen` (Mes trades seulement) | PR 2 ✓ |
| Clôturer, Supprimer (par trade) | actions `close`, `delete` (appui maintenu) | PR 2 ✓ |

### 3.5 Risque (4)

| Carte | Source | Statut |
|---|---|---|
| Grade par marché | `state.risk.grades.{stock,crypto}` ← `lab_grade.risk_now` : lettre, `risque_propose`, `risque_grade`, plafond `risque_max`, `raison`, `observation`, `perime` ; « jamais calculé par le labo » si `grade_age_h` est nul ; puce « non noté » si `non_note` (échantillon trop petit) | PR 3 ✓ |
| Risque ouvert et arrêt du jour | `state.risk` : `risque_ouvert_pct` / `risque_ouvert_max_pct`, `r_du_jour_net` / `arret_jour_r`, budget libre calculé par la page (`risque_ouvert_max_pct` − `risque_ouvert_pct`), `risque_max_par_trade_eur`, `capital_declare_eur` (« déclaré ») ; rappel `risk.note` (risque au stop ≠ perte maximale) | PR 3 ✓ |
| Dernier calcul de taille | `sizing` et `grade` du dernier plan calculé **dans ce navigateur** (action `plan`, gardé en `localStorage` `cockpit.lastPlan`) : capital × % du grade, plafond de 20 €, taille ; sinon état vide « Aucun plan calculé » | PR 3 ✓ |
| Frais IBKR | `state.risk.ibkr` ← `killmode.IBKR_PLANS`, `JARVIS_IBKR_PLAN`, `ibkr.single_exit()` : barème du compte, par action, minimum, plafond, frais de place, plancher d'un aller-retour en $ et en R à 20 € ; frais du dernier plan (`plan.ibkr`) | PR 3 ✓ |
| Grade détaillé, « Ta mise de 20 € chez IBKR » | `GET /api/cockpit/texte/grade` et `/texte/frais` (mêmes fonctions que `!grade`, `!frais`) | PR 3 ✓ |

### 3.6 Labo (5)

`GET /api/cockpit/lab` (cache 60 s) : `statut` (`lab_status.status` : rejeu, direct, méthode, déclaration en cours,
battement), `declarations` (`council.load`), `regles` (`validator.counts`), `grade` (`payload` + historique),
`lecons` (`lessons.counts`), `gemini` (`summary` j0 / j1), `killmode` (actif, KILL et témoins, derniers r_20e),
`forward_test` (`paper.stats`), `propositions` (labo, grade, promotion), `veto` ; `base` = `{"erreur"}` seulement quand
`lab.db` manque (sa taille est `statut.db_size_mb`).

| Carte | Source | Statut |
|---|---|---|
| Labo (méthode, rejeu, règles, leçons) + texte de `!labo` | `lab.statut`, `lab.regles`, `lab.lecons`, `lab.base` ; `GET /api/cockpit/texte/labo` | PR 3 ✓ |
| Killmode (interrupteur, derniers KILL, journal complet) | `lab.killmode` ; interrupteur : actions `killmode_on/off` (appui maintenu) ; journal : `/texte/killmode` (`killmode.bilan_text`, **sans solder** : `!kill bilan` solde avec les bougies de la séance) | PR 3 ✓ |
| Modules du labo (onglets) | `/texte/{strategies, ouverture, gemini, lecons, autopsie, rates, etude8k, sec8k, polyfed, potentiel}` = le texte exact de `!strategies`, `!ouverture`, `!gemini`, `!lecons`, `!autopsie`, `!rates`, `!etude8k`, `!8k`, `!fed`, `!potentiel` ; onglet gardé en `localStorage` | PR 3 ✓ |
| Council et veto | `lab.propositions` (labo, grade, promotion ; statut) ; `!veto` : action `veto` (appui maintenu) | PR 3 ✓ |
| Déclarations du council | `lab.declarations` (les plus récentes : nom, marché, soirée) | PR 3 ✓ |

La page `/cockpit/labo` reste servie (lien « page Labo actuelle ») jusqu'à validation par Simão.

### 3.7 Agenda (6)

| Carte | Source | Statut |
|---|---|---|
| Aujourd'hui (frise) | `agenda.aujourd_hui` (horaires internes de JARVIS, = `state.agenda_today`) + événements économiques du jour, fusionnés par heure de Paris ; passé atténué, ligne « maintenant » | PR 3 ✓ |
| Résultats du jour | `agenda.resultats` ← `finnhub.earnings_calendar` (clé `FINNHUB_API_KEY`) : valeurs suivies (BPA estimé / réel, avant ouverture / après clôture) et total de la cote par moment | PR 3 ✓ |
| 8-K des trois derniers jours | `agenda.sec8k` ← `edgar.tickers` + `edgar.submissions` (`SEC_USER_AGENT`) : formulaire, items traduits, heure d'acceptation (« douteuse » si EDGAR l'a mal datée), lien EDGAR ; « aucun 8-K » compté sur les valeurs **lues** seulement, les autres listées comme « non vérifiées » (erreur, SEC trop lente, sans CIK, au-delà du plafond) | PR 3 ✓ |
| Cette semaine | `agenda.economique` ← `needs.economic_calendar` (Helious, impact fort ou moyen, US / zone euro / France / Allemagne, comme le point du matin) + résultats suivis par jour ; un jour au-delà de `couvre_jusqu_au`, un calendrier en panne ou des résultats sans clé s'affichent « inconnus », jamais « rien de notable » | PR 3 ✓ |

Valeurs suivies (`agenda.valeurs_suivies`), les plus importantes d'abord : actions en position (deux espaces), plans du
scan du jour (pas ceux d'une séance passée), `JARVIS_WATCHLIST_STOCKS` ; 20 au plus pour la SEC, le plafond coupe la
watchlist en premier.

### 3.8 Journal (7)

`GET /api/cockpit/journal?space=` : `clotures` (Mes trades : raison, R, R net, € ; Papier : recommandations closes
et pas déclenchées), `desaccords`, `confirmations` (60 dernières lignes, état du jour), `forward_test`, `decisions`.
Chaque carte ouvre aussi le texte complet de la commande Discord : `/texte/desaccords` (`!desaccords`),
`/texte/confirmations` (`!confirmations`), `/texte/bilan` (`!bilan`). PR 3 ✓.

### 3.9 Santé (8)

`GET /api/cockpit/health` : `sources.lectures` (statut prouvé par la dernière lecture réussie, latence),
`sources.twelve_data` (crédits du jour), `planificateur` (chaque boucle, âge, péremption), `labo` (battement),
`discord` (file d'attente), `disque`, `erreurs_recentes` (50 derniers WARNING+, clés masquées). Bouton « Télécharger
le diagnostic » : `GET /api/cockpit/diagnostics`. PR 3 ✓ (grille des sources, crédits Twelve Data du jour, Alpaca,
JARVIS et labo, boucles, erreurs récentes).

### 3.10 Inspecteur

Prix, variation, état de séance : `instruments` ; mini-graphique avec SL / TP : `instrument/{symbol}` ; dernier plan :
`instrument.plan_du_jour` ; avis : action `opinion` à la demande (PR 2 ✓).

---

## 4. Contrat d'API (`/api/cockpit/*`, hors OpenAPI)

Unités : prix en **USD**, risque et résultats de règle en **EUR**, résultats en **R**, pourcentages en **%**,
dates en **ISO-8601 UTC**. Valeur inconnue = `null` (jamais 0). En-têtes de sécurité (§9.1 du prompt) et
`Cache-Control: no-store` sur toutes ces routes. `space` vaut `trades` ou `paper` (défaut `paper`), autre valeur →
**400**. Symbole : `^[A-Z0-9.\-]{1,12}$` après mise en majuscules, sinon 400 ; `tf` ∈ `1m 5m 15m 1h 1j`, sinon 400.

### 4.1 `GET /api/cockpit/state?space=` (5 s, aucun appel réseau)
```json
{"generated_at": "…", "space": "paper",
 "jarvis": {"boucles": {"<nom>": {"at": "…", "ok": true, "every_s": 60, "duree_s": 0.4, "age_s": 12, "stale": false}},
            "suivi": {"at": "…", "age_s": 12, "stale": false}},
 "lab": {"etape": "direct stock", "le": "…", "age_s": 40, "stale": false},
 "portfolio": {"positions_ouvertes": 1, "latent": {"r": 0.6, "eur": 12.1, "inconnus": 0},
               "jour": {"n": 1, "r": 1.5, "r_net": 1.41, "eur": 28.07, "fees_eur": 1.72, "eur_inconnus": 0},
               "semaine": {"…": "…"},
               "capital": {"eur": 1000, "etiquette": "déclaré (JARVIS_CAPITAL)"},          // trades
               "courbe": {"unite": "R|€", "titre": "…", "points": [["<iso>", 1.41]]},       // cumul des clôtures
               "en_attente": [{"id", "symbol", "market", "type_fr", "entry", "stop", "qty", "sent_at", "valid_until"}],  // paper
               "si_tout_suivi": {"depuis": "…", "equite_eur": 1028.07, "eur_net": 28.07, "r_net": 1.413,
                                 "taux_reussite": 1.0, "par_etat": {"attente": 0, "ouvert": 1, "clos": 1, "pas_declenche": 0},
                                 "par_type": {"go_confirme": 2, "kill": 0}, "latent_r": 0.6, "latent_eur": 12.1},  // paper
               "recos_du_jour": {"…": 0}, "go_en_attente_de_confirmation": 0},                // paper
 "positions_open": ["<bloc position §4.9>"],
 "risk": {"grades": {"stock": {"grade": "F", "risque_propose": 0.0, "raison": "…", "risque_propose_eur": null}, "crypto": {}},
          "grade_age_h": 1.2, "grade_max_age_h": 6, "risque_ouvert_pct": 0.0, "risque_ouvert_max_pct": 3.0,
          "r_du_jour_net": 0.0, "arret_jour_r": -3.0, "risque_max_par_trade_eur": 20, "capital_declare_eur": 1000,
          "note": "risque au stop ≠ perte maximale : …"},
 "proposals_pending": [{"id": 3, "kind": "labo", "key": "…", "value": "…", "current": "…", "why": "…"}],
 "market_sessions": {"nyse": {"etat": "premarche|seance|apres_bourse|ferme", "prochain_changement": "…",
                              "prochaine_ouverture": "…", "aujourd_hui": {"premarche": "…", "ouverture": "…", "cloture": "…",
                              "fin_apres_bourse": "…", "demi_seance": false}, "raison_fermeture": null,
                              "ecart_paris_new_york_h": 5.0, "decalage_inhabituel": true},
                     "crypto": {"etat": "ouvert", "label": "24/7"}},
 "sources": {"lectures": {"Twelve Data": {"statut": "ok", "dernier_succes": "…", "latence_ms": 230, "ok": 41, "erreurs": 0}},
             "twelve_data": {"jour_new_york": "2026-10-28", "depenses": 212, "par_jour": 800, "par_minute": 8},
             "alpaca_configure": true, "twelve_data_configure": true},
 "agenda_today": [{"at": "…", "titre": "Ouverture NYSE", "categorie": "marche", "source": "calendar.py", "importance": "haute", "passe": true}],
 "plans_today": {"du_jour": true, "scan_at": "…", "picks": [{"symbol": "…", "verdict": "GO", "composite": 7.1,
                 "raisons": [], "entry": 0, "stop": 0, "targets": [], "confirmation": "confirmé", "declencheur": 0}],
                 "note_composite": "priorité, pas une probabilité"},
 "killmode": {"actif": false, "depuis": "…"},
 "attention": {"total": {"action": 1, "probleme": 0, "info": 2}, "commun": {"…": 0}, "trades": {"…": 0}, "paper": {"…": 0}},
 "attention_top": [{"id": 12, "key": "deploy:pending", "kind": "action", "space": "commun", "title": "…", "last_seen": "…"}]}
```
`attention` compte les constats ouverts et non rangés de la page À faire (§8), tous espaces ; `attention_top` en donne
les trois premiers dans l'ordre de la page.

### 4.2 `GET /api/cockpit/instruments?space=`
`{"instruments": [{"symbol", "market": "stock|crypto", "price" (USD), "change_pct" (% sur la clôture de la veille),
"source", "as_of", "freshness": {"age_s", "stale", "etat": "frais|perime|cloture"}, "spark": [clôtures 1 min],
"en_position": bool}], "manque": null | "…"}`. Crypto : Kraken (cache 15 s) ; action : cache du suivi, sinon
snapshots Alpaca IEX groupés (un appel par minute au plus).

### 4.3 `GET /api/cockpit/instrument/{symbol}?space=&tf=&market=`
`{"symbol", "market", "tf", "source", "manque", "bars": [[t (s UTC), o, h, l, c, v]] (130 avec un trade, 300 sinon),
"as_of", "positions": [bloc §4.9], "plan_du_jour": pick | null}`.

### 4.4 `GET /api/cockpit/positions/charts?space=&tf=15m`
`{"charts": [{"position": bloc §4.9, "source", "manque", "bars": [70 bougies]}]}`.

### 4.5 `GET /api/cockpit/reco?days=30`
`{"recos": [{"id", "type": "go_confirme|kill", "type_fr", "symbol", "day", "sent_at", "message": {"entry", "stop",
"targets", "qty", "risk_eur", "eurusd", "size_note", "sortie_unique", "valid_until", "brackets"?}, "status":
"attente|ouvert|clos|pas_declenche", "status_fr", "fill": {"price", "at"}, "targets_hit", "stop_current",
"last_price", "events", "result": {"r", "r_net", "usd_brut", "fees_usd", "usd", "eur", "raison", "closed_at",
"taille"}, "unrealized": {"r", "usd", "eur"}, "mes_trades": {"id", "entry", "status", "r_net"} | null}],
"bilan": reco.bilan, "gestion": {…}}`.

### 4.6 `GET /api/cockpit/lab` (cache 60 s)
`{"statut", "declarations", "regles", "grade", "lecons", "gemini", "killmode", "forward_test", "propositions",
"veto", "base"?, "modules": {"<nom>": "/api/cockpit/texte/<nom>"}}` (`base` seulement si `lab.db` manque) ; chaque partie
peut valoir `{"erreur": "…"}` sans
casser les autres.

### 4.7 `GET /api/cockpit/journal?space=&limit=100`
`{"clotures": [Mes trades : {"id", "symbol", "side", "entry", "exit_price", "raison", "corrige", "r", "r_net",
"eur", "verdict", "against_jarvis", "closed_at"} | Papier : vue reco §4.5], "desaccords", "confirmations":
{"lignes", "total", "aujourd_hui", "jour"}, "forward_test", "decisions"}`.

### 4.8 `GET /api/cockpit/health` et `GET /api/cockpit/diagnostics`
Santé : voir §3.9. Diagnostic : pièce jointe JSON (`Content-Disposition: attachment`) : boucles, labo, sources,
compteurs, `cles_presentes` (booléens), `variables_jarvis_definies` (**noms seulement**), erreurs récentes. Aucun
secret, aucune valeur de variable (testé). Depuis la rubrique À faire (§8), les deux portent aussi `bot` (connecté,
depuis quand, dernière erreur expurgée), `version_deployee` (commit du disque, empreintes du code, comparaison avec
`main`) et `page_a_faire` (dernière vérification ; dans le diagnostic, les constats ouverts : clé, type, portée, dates,
jamais leurs preuves).

### 4.9 Bloc position (commun)
```json
{"id": "t1", "symbol": "NVDA", "market": "stock", "side": "long", "qty": 4, "entry": 176.9,
 "created_at": "…", "verdict": "GO|NO GO|KILL|null", "against_jarvis": false,
 "stop": {"price": 171.6, "initial": 171.6, "kind": "fixed|breakeven", "source": "IBKR, posé par toi · suivi par JARVIS"},
 "targets": [{"name": "TP2", "price": 189.0, "pct": 6.84, "r_multiple": 2.28, "role": "exit|repere", "hit_at": null,
              "eta_text": "…", "eta_median": "…", "eta_latest": "…", "late": false}],
 "sortie_unique": 1, "targets_hit": 0, "last_price": 180.0, "as_of": "…", "price_source": "Twelve Data", "eurusd": 1.16,
 "freshness": {"age_s": 30, "stale": false, "etat": "frais"}, "move_pct": 1.75, "r_now": 0.58,
 "unrealized": {"r": 0.58, "usd": 12.4, "eur": 10.69},
 "fees_est": {"usd": 2.0, "r": 0.09, "bareme": "degressif"},
 "exit_signals": [], "ibkr_orders": [{"quantity": 4, "entry": 176.9, "stop": 171.6, "name": "TP2", "take_profit": 189.0}],
 "conseil": {"decision": "GARDER", "…": "…"}, "events": []}
```
`unrealized` vaut `null` quand la cotation est périmée ; `usd` / `eur` `null` sans quantité. En Papier : `fees_est`
null (les commissions sont dans le résultat), `stop.source` « message Discord · suivi par JARVIS », `reco_id`.
Les SL et TP viennent du trade enregistré (ou du message), jamais d'un calcul de l'interface.

### 4.10 Actions (PR 2 ✓, `app/web/cockpit_actions.py`)
`POST /api/cockpit/actions` avec `{"action", "space": "paper|trades", "confirm": "MES_TRADES"?, "args": {…}}`.

Gardes, dans l'ordre : `reject_cross_site` (Origin / Sec-Fetch-Site) ; jeton CSRF (cookie `jarvis_csrf` posé par
`GET /cockpit`, `SameSite=Strict`, `Secure`, lisible par la page, renvoyé dans `X-CSRF-Token`, comparé à temps
constant) sinon **403** ; action hors liste → **400** ; `space` invalide → **400** ; action qui écrit dans Mes trades
sans `space: trades` **et** `confirm: MES_TRADES` → **403**. Arguments contrôlés (symbole, nombres > 0 avec virgule
acceptée, `id` `^[A-Za-z0-9_.:-]{1,64}$`, 5 objectifs au plus, note de 300 caractères) → **400**. Erreur inattendue →
**500** « Action X en échec : Type » (sans détail). Réponse : `{"ok": true, "action", "at", "text", …}` = le résultat
réel de la fonction, jamais « en attente ».

| Action | Mes trades | Appui maintenu | Même fonction que |
|---|---|---|---|
| `plan` | — | — | `POST /api/trading/plan` (`main.trading_plan`, sans envoi Discord) |
| `take` | ✓ | — (verrou d'espace) | `POST /api/trading/trades` (`main.trading_open` → `book.add`) |
| `exit` | ✓ | ✓ | `!sortie` (`service.record_exit` sous `scheduler._lock`, `exit_text`) |
| `close` | ✓ | ✓ | `POST /api/trading/trades/{id}/close` (`book.close`) |
| `delete` | ✓ | ✓ | `DELETE /api/trading/trades/{id}` (`book.remove`) |
| `reopen` | ✓ | ✓ | `!rouvre` (`build_plan` puis `service.reopen` sous le verrou) |
| `advice` | ✓ | — | `GET /api/trading/advice` (garde le conseil sur le trade) |
| `scan_now` | — | — | `GET /api/trading/scan` (rien enregistré, aucun ordre fictif) |
| `proposal_approve` / `proposal_refuse` | — | ✓ / — | `POST /api/trading/proposals/{id}/{choice}` (`proposals.decide`) |
| `veto` | — | ✓ | `!veto` (`lab.method.enqueue_veto`) |
| `killmode_on` / `killmode_off` | — | ✓ | `!kill on` / `!kill off` (`killmode.set_active`) ; l'interface offre « Annuler » 6 s |
| `opinion`, `company` | — | — | `!avis` (`main.trading_opinion`), `!societe` (`main.trading_company`) |

Journal : une ligne JSON par appel dans `data/cockpit_actions.jsonl` (`JARVIS_COCKPIT_ACTIONS_PATH`) : heure, action,
espace, arguments (textes coupés à 120 caractères), `ok`, statut HTTP, résultat (300 caractères). Aucune action ne
passe d'ordre (ni IBKR ni Alpaca) ni ne touche une clé.

### 4.11 `GET /api/cockpit/texte/{module}` (PR 3 ✓, cache 2 min)
Le texte exact d'une commande Discord, par la même fonction que le bot, en lecture seule. Liste blanche
`TEXT_MODULES` (module inconnu → **400**) : `labo` (`!labo`), `strategies`, `ouverture`, `gemini`, `killmode`
(`!kill bilan`, lu sans solder), `lecons`, `grade`, `frais`, `autopsie`, `rates`, `sec8k` (`!8k`), `etude8k`,
`polyfed` (`!fed`), `potentiel`, `bilan`, `confirmations`, `desaccords`. Réponse : `{"module", "titre", "commande",
"texte", "erreur", "at", "note"}` ; une base illisible rend `texte: null` et `erreur` (200, gardé 15 s), sans casser
la vue. La page rend le Markdown de Discord (gras, `code`, blocs, citations) sans `innerHTML`.

### 4.12 `GET /api/cockpit/agenda` (PR 3 ✓)
`{"generated_at", "aujourd_hui", "seances", "valeurs_suivies", "economique": {"evenements": [{"at", "titre", "pays",
"impact", "prevision", "precedent", "reel", "statut"}], "source", "attribution", "maj", "stale", "message", "filtre"},
"resultats": {"suivies": [{"symbol", "date", "heure", "trimestre", "bpa_estime", "bpa_reel", "ca_estime",
"ca_reel"}], "jours": {"<date>": {"total", "avant_ouverture", "apres_cloture", "pendant", "heure_inconnue"}},
"source"} | {"manque"}, "sec8k": {"depots": [{"symbol", "form", "date", "accepte", "heure_douteuse", "items":
[{"code", "label"}], "lien"}], "valeurs", "lues", "sans_cik", "erreurs", "trop_lentes", "hors_plafond", "depuis",
"source", "note"} | {"manque"}}` ; `economique` porte aussi `couvre_jusqu_au` (dernier jour, heure de Paris, que la
source publie).
Sources gratuites en cache côté serveur : calendrier économique 1 h (`needs.resolve`), résultats 1 h par jour de New
York, liste des CIK 24 h, dépôts 15 min par société, erreur retentée après 1 min ; une lecture en cours est partagée
entre les requêtes et va au bout même si la vue n'attend plus. Les trois parties sont lues en parallèle ; les dépôts
SEC par 4 au plus, 12 s en tout (le reste est « trop lent » et sera en cache au passage suivant). Une partie en panne
ou un calendrier économique sans aucune valeur connue rend `{"erreur"}` sans effacer les autres. **Jamais Twelve
Data** (testé).

---

## 5. Écarts prompt ↔ JARVIS et décisions

| Le prompt dit | JARVIS | Décision |
|---|---|---|
| « Santé des sources : `GET /api/health` (existe déjà) » | renvoie `{"status": "ok"}` seulement | **Ajouter** `core/sources_health.py` (dernier succès / erreur par source) ; `/api/health` inchangé |
| « quota Twelve Data du jour » | seul un plafond par minute existait | **Ajouter** un compteur par jour de New York (ce processus) |
| « dernier passage de chaque boucle » | absent | **Ajouter** `scheduler.last_runs` (posé par `_loop`) |
| §9.3 : Papier = « équité, trésorerie, P&L du compte Alpaca fictif » | contredit §1 (Papier = recommandations ; Alpaca = labo) | **Papier = journal des recommandations** ; le compte Alpaca va dans la vue Labo (`lab.forward_test`) ; son équité n'est pas lue (appel réseau) |
| Suivi « en direct » des recommandations | seulement à la clôture (`confirmations.jsonl`, `settle` Killmode) et un bracket par objectif | **Ajouté** : `reco.py` + `tick_reco`, gestion du message (sortie unique TP2 pour les GO) |
| Suivi des recos par bougies | bougies 1 min du suivi ou 5 min | **Bougies 5 min**, une lecture Twelve Data par bougie close et par action suivie (≈ 78 crédits par action et par séance), Alpaca en secours. Côté serveur seulement : la page n'en déclenche aucune |
| Entrée Papier « exactement telle qu'envoyée » | une exécution peut être plus haute que l'ordre (gap) | entrée = pire de l'ordre et de l'ouverture de la bougie ; le prix du message reste dans `message.entry` |
| GO confirmé « 3 brackets si `JARVIS_IBKR_BRACKETS=3` » | `ibkr.single_target` | identique, figé à la confirmation (`message.sortie_unique`) |
| Taille du KILL | le MP de `!kill` affiche les brackets au change de l'analyse, que le journal ne garde pas | change du jour lu à la reprise (BCE, 1,15 si muette) ; écart possible d'une action, noté ici |
| Mise en service du journal | — | `depuis` = premier passage de `tick_reco` ; aucun KILL ni GO d'avant n'est repris (pas de reconstitution de tailles non gardées) |
| Délais estimés des TP Papier | le MP de confirmation n'en donne pas | repris du plan du scanner (minutes de cotation, gardées par `confirm.register`) et repartis de l'heure du déclenchement ; KILL : « délai non estimé » |
| CSP sur `/cockpit*` | l'ancien cockpit a des scripts et styles en ligne | PR 2 ✓ : en-têtes sur `/cockpit`, `/cockpit/app.*`, `/cockpit-static/*` et `/api/cockpit/*` ; jamais sur `/cockpit/ancien`, `/cockpit/labo`, `/docs`. Lightweight Charts insère une balise `<style>` pour son logo : la page lui rend un gabarit inerte et ses règles sont recopiées dans `app.css` (zéro violation CSP vérifiée dans Chromium) |
| Actions de §9.4 | l'avis (touche A, inspecteur) et la fiche société n'y figurent pas | **ajoutées** (`opinion`, `company`) : mêmes fonctions que `!avis` et `!societe`, sans écriture |
| « la page ne déclenche jamais Twelve Data » | un plan, une prise, une reprise, un avis, une fiche et les conseils en prix lisent l'historique (le scanner lit Alpaca) | la **navigation** n'en déclenche aucun (testé) ; ces actions, au clic seulement, en dépensent comme sur Discord, et l'écran le dit à côté du bouton |
| Vue Marchés en PR 3 (§15) | le graphique porte les SL / TP des positions, cœur de la PR 2 | **livrée en PR 2** avec Cockpit et Positions ; les cinq autres vues ont un état vide qui renvoie vers la page Labo, l'ancien cockpit ou le diagnostic |
| Agenda : résultats, 8-K, « Cette semaine » | sources existent, pas dans le cockpit | **PR 3 ✓** : `GET /api/cockpit/agenda` (Helious, Finnhub, SEC EDGAR, en cache) |
| Labo : « déclarations, n° 12, Gemini, leçons, autopsie, ratés, 8-K, Polymarket » en cartes | chaque module a déjà sa commande Discord, qui est la référence de Simão | **PR 3 ✓** : le cockpit montre le texte exact de la commande (`/texte/{module}`, même fonction) plutôt qu'une seconde mise en forme qui pourrait diverger ; chiffres clés en tuiles à côté |
| Risque : « dernier calcul de taille en étapes » | aucun plan n'est gardé côté serveur sans prise | **PR 3 ✓** : dernier plan calculé dans ce navigateur (`localStorage`) ; sinon état vide qui l'explique |
| Killmode : journal | `!kill bilan` solde les appels avec les bougies de la séance (Twelve Data) | la page lit le journal **sans solder** et le dit ; le solde reste sur Discord |
| Challenger Gemini et potentiel +10 envoient aussi des plans sur Discord | choix de Simão : Papier = GO confirmés + KILL | **exclus** du Papier, montrés au Labo ; à revoir si Simão veut les compter |
| Graphique : bougies 1 m à 1 j de toute action | actions hors suivi : Alpaca 16 min de retard | dit dans `source` (« Alpaca (16 min de retard) ») ; sans clés Alpaca : état vide |
| Équité / trésorerie IBKR | aucune connexion IBKR | **jamais affichées** ; capital `JARVIS_CAPITAL` étiqueté « déclaré » |
| `TrustedHostMiddleware` | absent | non ajouté (casserait le healthcheck et le Funnel) |

Modules de JARVIS que le prompt ne montrait pas : recherche avant l'ouverture (verdicts) → Journal / Labo ;
visibilité Finnhub (audit anti-fuite) → Labo ; veille des routines Claude → Santé (alertes) ; conseils mesurés
(`advice_stats`) → Journal ; propositions de grade et de méthode → Cockpit et Labo.

---

## 6. Parité avec le cockpit actuel (`app/web/cockpit.html`)

| Fonction actuelle | Route utilisée aujourd'hui | Place dans le nouveau | PR |
|---|---|---|---|
| KPI : trades ouverts, latent, prochain objectif, journal | `/api/trading/trades`, `/api/trading/live` | Cockpit · Résumé et Trades ouverts (`state`) | 2 ✓ |
| Planificateur de plan (crypto/action, long/short, entrée, stop, TP) | `POST /api/trading/plan` | formulaire « Nouveau plan » (`+`) → action `plan` | 2 ✓ |
| « Je prends ce trade » (avec note) | `POST /api/trading/trades` | action `take` | 2 ✓ |
| Propositions Approuver / Refuser | `/api/trading/proposals` | Cockpit · Propositions → actions `proposal_*` | 2 ✓ |
| Conseils en prix | `GET /api/trading/advice` | Positions · Conseils → action `advice` | 2 ✓ |
| Clôture avec prix | `POST /api/trading/trades/{id}/close` | action `close` (appui maintenu) | 2 ✓ |
| Suppression | `DELETE /api/trading/trades/{id}` | action `delete` (appui maintenu) | 2 ✓ |
| Journal des trades clôturés | `/api/trading/trades` | Journal (`journal?space=trades`) | 3 ✓ |
| Page du labo (lecture seule) | `/cockpit/labo`, `/api/lab/*` | vue Labo (`lab`, `texte/{module}`) ; `/cockpit/labo` reste servie | 3 ✓ |
| Mini-graphes 10 s | `/api/trading/live` | Positions · Trades sur le graphique (`positions/charts`) | 2 ✓ |
| Chip NYSE | `/api/markets/us/status` | cadran de séance (`state.market_sessions`) | 2 ✓ |
| Échelle des TP, alertes de retard, signes de sortie | trades | lignes de trade, piste, puces « en retard » | 2 ✓ |
| Toasts, vibration, pause quand l'onglet est masqué | — | toasts, scrutation arrêtée onglet masqué | 2 ✓ |

Depuis la PR 2, `/cockpit` sert le nouveau cockpit et l'ancien reste sous `/cockpit/ancien` (lien dans le rail et
la palette) jusqu'à validation par Simão. Le message Discord épinglé (`cockpit_text`) et les commandes `!…` ne
changent pas.

---

## 7. Garde-fous vérifiés par les tests (PR 1, 2 et 3)

`tests/test_cockpit_api.py` : toutes les routes répondent sur `data/` vide ; en-têtes de sécurité sur `/api/cockpit/*`
et `/cockpit` (pas sur `/api/health`, `/cockpit/ancien`, `/cockpit/labo`, `/docs`) ; `space`, symbole, `tf`, `market` invalides → 400 ;
diagnostic sans secret ni valeur de variable ; erreurs récentes sans clé ; blocs position long en sortie unique et
short au point mort ; cotation périmée → pas de latent ; espaces séparés ; **aucun appel Twelve Data** depuis les
routes du cockpit ; cadran un jour férié, un week-end, une demi-séance et pendant la semaine de décalage (ouverture
14 h 30 à Paris). `tests/test_reco.py` : GO confirmé tel qu'envoyé (sortie unique, € au centime, commissions),
ordre au-dessus de l'ouverture, stop sur gap, non déclenché à 11 h 30, sans taille, KILL (entrée dès TP1, vente à la
clôture), reprise des KILL depuis la mise en service, une lecture par bougie close, lien avec Mes trades, taille du MP
reprise par `tick_confirm`, bilan.

`tests/test_cockpit_actions.py` (PR 2) : `GET /cockpit` pose le cookie CSRF (`SameSite=Strict`, `Secure`) et sert des
fichiers versionnés ; polices, Lightweight Charts, `app.js` et `app.css` servis en local avec la CSP ; aucune ressource
externe, aucun style ni script en ligne, ni `innerHTML` ni `eval` dans la page ; CSRF absent, faux ou sans cookie →
403 ; autre site → 403 même avec le jeton ; action ou espace inconnus → 400 ; les six actions qui écrivent dans Mes
trades refusées en Papier ou sans `MES_TRADES` (403, rien d'écrit) ; liste des actions protégées ; clôture, suppression,
sortie réelle, reprise, propositions, veto et Killmode passent par les mêmes fonctions que les routes et `!…` ; erreur
inattendue sans détail ; journal des actions. Contrôle à la main dans Chromium (1440, 1100 et 390 px, deux espaces) :
zéro erreur JavaScript, zéro violation CSP, aucun débordement horizontal, appui maintenu au doigt et au clavier.

PR 3 (`tests/test_cockpit_api.py`) : `state.risk.ibkr` porte les barèmes de Killmode et le plancher en R ; `/texte` :
module hors liste → 400, même texte que la fonction du bot, cache de 2 min, base illisible → 200 avec `erreur`, chaque
module répond sur `data/` vide ; `/agenda` : filtres (impact, pays, 7 jours ; 8-K seulement, trois jours), ordre des
valeurs suivies (positions, plans du jour, watchlist ; pas les plans d'une séance passée), items traduits, lien EDGAR,
cache par société, valeurs lues séparées des valeurs non vérifiées (erreur, sans CIK, au-delà du plafond, SEC trop
lente), calendrier sans aucune valeur connue → `erreur`, `couvre_jusqu_au`, aucune clé dans la réponse, clés absentes
→ `manque`, source en panne → `erreur` sans effacer le reste, et toujours aucun appel Twelve Data (garde du `client`) ;
`/journal` : GO d'une séance passée marqués `du_jour: false`. Contrôle à la main
des huit vues dans Chromium à 1440, 1100 et 390 px, dans les deux espaces : zéro erreur JavaScript, zéro violation
CSP, zéro requête en échec, aucun débordement horizontal.

---

## 8. Rubrique « À faire » (prompt corrigé du 29/10/2026)

Source : `/mnt/project-files/cockpit/a-faire/prompt-a-faire-corrige-2026-10-29.md` (revue du prompt de Meme Radar,
verdict Améliorer). Ce que Simão doit faire de son côté, les problèmes que JARVIS voit dans son propre fonctionnement,
et ce qui est bon à savoir. **PR « À faire » 1 ✓** : moteur, stockage, routes et instrumentations
(`app/web/attention.py`) ; **PR « À faire » 2** : la vue, le badge du rail et la bannière du Cockpit.

### 8.1 Modèle

Un constat = `{id, key, kind, space, title, detail, steps, evidence, first_seen, last_seen, resolved_at, ack_at}`.
- `kind` : `action` (Simão doit faire quelque chose), `probleme` (défaut ; JARVIS continue si possible), `info`.
- `space` : `commun`, `trades` (Mes trades), `paper` (Papier). Ordre de la page : action, problème, info ; à rang égal
  Mes trades, commun, Papier ; le plus récent d'abord ; rangés puis réglés en bas.
- `steps` : texte, plus au plus **une** commande à copier (`cmd`), **une** action (`action` ∈ `proposal_approve`,
  `proposal_refuse`, `veto`, les mêmes que §4.10) ou **une** vue (`go`). Validé à la création (`item`) : un constat
  ne peut pas proposer une autre action ni une vue inconnue. Aucun champ de saisie, jamais de clé à coller.
- `evidence` : la donnée qui a déclenché le constat, expurgée (`key=…`, `token=…`, `Bearer …` masqués).
- Cycle : clé nouvelle → nouvelle ligne ; clé encore vue → mise à jour (`last_seen`) ; clé disparue → `resolved_at`.
  Une récidive ouvre une nouvelle ligne. « C'est fait » / « Masquer » posent `ack_at` et **ne ferment rien** : un
  constat rangé encore vu plus de 2 min après porte `toujours_constate`. Réglés renvoyés 72 h ; lignes gardées 30 jours,
  2000 au plus (`data/attention.json`, écrit de façon atomique ; illisible → mis de côté en `attention.illisible`).

### 8.2 Moteur

`scheduler.tick_attention`, toutes les minutes, **sans le verrou global** (`locked=None` : ni verrou ni sauvegarde de
l'état des alertes), pour ne jamais attendre derrière un job bloqué. Chaque règle est une fonction
`(ctx, now) -> list | None` qui lit l'état en mémoire ou sur disque : **aucun appel réseau** (jamais Twelve Data),
**rien envoyé sur Discord**, rien ne change la stratégie, la taille, le grade, un filtre ni le Killmode. `None` = pas
d'avis (délai de grâce de 3 min après le démarrage, comparaison GitHub pas encore faite) : ses constats ouverts restent
tels quels. Une règle qui plante devient le constat `rule:<nom>` et garde ses constats. La base du labo (4,7 Go) est
lue en lecture seule dans un fil à part (`lab_snapshot` : garde du Labo 1 et `lab_events` des 72 dernières heures).
Calculés à la lecture : `attention:stale` (aucun passage depuis 3 min) et `attention:file` (historique illisible au
démarrage). Un JARVIS entièrement arrêté ne sert plus la page : c'est la bannière « API muette » qui le dit.

### 8.3 Catalogue

| Clé | Type | Portée | Déclencheur (donnée) | Se ferme quand |
|---|---|---|---|---|
| `lab:down` | action | commun | `lab_heartbeat` absent ou > 30 min | le worker rebat |
| `lab:base` | problème | commun | base du labo illisible, ou absente alors que le worker bat | base relue |
| `lab:v1_pause` | problème | commun | `progress.garde_v1` ≠ ok (Labo 1 en pause) | garde repassée |
| `council:decision:<id>` | info | commun | `lab_events` (bascule, retrait, council, veto, reprise du Labo 1) des 72 h ; bouton `veto` sur la dernière décision vetable seulement | 72 h passées |
| `loop:stuck:<boucle>` | problème (action pour le suivi s'il y a un trade crypto ouvert, ou un trade action en séance) | trades pour le suivi, paper pour le journal des recommandations, commun sinon | `last_runs` plus vieux que max(180 s, 3 × période) ; boucle jamais passée après ce délai | la boucle repasse |
| `loop:failing:<boucle>` | problème | idem | `last_runs[].echecs_suite` ≥ 3 | un passage réussi |
| `errors:<module>` | problème | commun | ≥ 3 ERROR/CRITICAL du même module en 30 min (`recent_errors`, hors planificateur et page À faire) | fenêtre passée |
| `disk` | problème ≥ `JARVIS_DISK_ALERT_PCT`, action ≥ 90 % | commun | `shutil.disk_usage` de `data/` | place libérée |
| `source:<nom>` | problème | commun | ≥ 3 échecs d'affilée, premier échec il y a plus de 5 min, dernière tentative de moins d'1 h | un succès |
| `internet` | problème | commun | ≥ 3 sources en panne ensemble (remplace leurs constats) | idem |
| `auth:<clé>` | action | commun | HTTP 401 (≥ 2 d'affilée) d'une source à clé : Twelve Data (y compris dans une réponse 200), Finnhub, Alpaca | un succès |
| `quota:twelvedata` | info ≥ 80 %, problème à 100 % ou après 3 refus 429 | commun | `twelvedata.credits_today()` (processus web seulement) | minuit à New York |
| `key:missing:<nom>` | action (info pour `SEC_USER_AGENT`) | commun | `config.secret` vide | clé ajoutée |
| `notifications:off`, `discord:bot:off` | info | commun | `scheduler.enabled()` / `bot.enabled()` faux | réglage remis |
| `discord:outbox` | problème | commun | message le plus ancien de la file en attente depuis > 10 min | file vidée |
| `auth:discord`, `discord:intent`, `discord:bot` | action, action, problème | commun | `bot.status` déconnecté depuis > 5 min (`LoginFailure`, `PrivilegedIntentsRequired`, autre) | reconnexion |
| `routine:soir` | action | commun | `routines_watch.soir_alert` (sans repère : pas d'avis) | dépôt du council |
| `routine:recherche` | action | commun | `routines_watch.recherche_alert` | verdicts déposés |
| `deploy:rebuild` | action | commun | empreinte du code de l'image ≠ celle du dépôt monté (`git pull` sans `--build`) | reconstruction |
| `deploy:pending` | action | commun | GitHub : `main` en avance sur le commit du disque, avec des fichiers de `app/`, `requirements.txt`, `Dockerfile` ou `docker-compose.yml` | déploiement |
| `deploy:unknown`, `deploy:ahead` | info | commun | dépôt non monté, GitHub injoignable ou limité ; commit absent de `main` | lecture rétablie |
| `exit:undeclared:<id>` | action | trades | trade fermé par le suivi (stop, TP) sans `corrige`, depuis moins de 7 jours | `!sortie` déclarée |
| `kill:open_after_close:<id>` | action | trades | KILL encore ouvert 5 min après la clôture de sa séance | vente déclarée |
| `advice:<id>` | action | trades | conseil du jour SORTIR ou ALLÉGER gardé dans le trade (`conseils`) | jour suivant ou trade fermé |
| `exit:signals:<id>` | action | trades | `exit_signals` du trade ouvert | signes disparus |
| `quote:stale:trades` | problème | trades | cotation périmée au double du seuil du cockpit (crypto 2 min, action 8 min **en séance**) | cotation revenue |
| `tp:late:<id>:<tp>` | info | trades | objectif au-delà de `eta_latest`, pas atteint | atteint ou trade fermé |
| `risk:day_stop`, `risk:open` | info | trades | `risk()` : R net du jour ≤ −3 R ; risque ouvert > budget | lendemain ; risque redescendu |
| `grade:stale` | info (pas encore calculé) ou problème (périmé) | commun | `risk().grade_age_h` > `grade_max_age_h` ; muet si `lab:down` | grade réécrit |
| `proposal:<id>`, `proposals:more` | action | commun | `PR.proposals.pending()` (8 au plus, le reste résumé) ; boutons `proposal_approve` / `proposal_refuse` | décision |
| `rule:<nom>`, `attention:stale`, `attention:file` | problème | commun | §8.2 | règle réparée, moteur reparti |

Commandes : `ssh` (cible `JARVIS_SSH_CIBLE` si posée, sinon `ssh root@IP_DU_SERVEUR` à compléter : aucune adresse
dans le code), `cd /root/jarvis`, puis la commande du constat. Changer une clé : `nano .env` (seulement la ligne
nommée), puis `docker compose up -d --force-recreate` (un `restart` ne relit pas `.env`).

### 8.4 Contrat d'API

- `GET /api/cockpit/attention?space=all|commun|trades|paper` (défaut `all`, autre valeur → 400) :
  `{"generated_at", "space", "items": [constat + {"live", "toujours_constate"}], "counts": {"total"|espace: {"action",
  "probleme", "info"}}, "last_check_at", "tronque", "note"}`. Au plus 200 lignes, calculés à la lecture en tête
  (`id: null`, pas rangeables).
- `POST /api/cockpit/attention/{id}/ack` et `/unack` : jeton CSRF (cookie + `X-CSRF-Token`, sinon 403), même garde
  « autre site » que les actions ; constat inconnu, réglé ou déjà dans cet état → 404. Chaque appel est noté dans le
  journal des actions du cockpit (`attention_ack`, `attention_unack`).
- `state` : `attention`, `attention_top` (§4.1).

### 8.5 Instrumentations ajoutées

- `core/sources_health.py` : `dernier_resultat` (le statut ne compare plus deux heures à la seconde), `echecs_suite`,
  `premier_echec`, `derniere_tentative`, `code` (HTTP) ; texte d'erreur expurgé (`scrub`).
- `core/http.get_json(refusal=…)` et `twelvedata.refusal` : un refus de Twelve Data dans une réponse HTTP 200 (clé
  refusée 401, crédits 429, panne 5xx) est noté comme un échec de la source ; une erreur de symbole (400, 404) non.
- `scheduler._loop` : `echecs_suite` dans `last_runs` ; `_run(locked=None)`.
- `notify/bot.status` : connecté, depuis quand, dernière erreur (type et texte expurgé).
- `core/outbox.py` : `derniere_erreur` de chaque message (adresse du webhook masquée), `oldest()`.
- `core/version.py` : empreinte (SHA-256 de `app/` et `requirements.txt`, sans `__pycache__`) de l'image au
  démarrage contre celle du dépôt monté en lecture seule sous `/repo` (`docker-compose.yml`) ; `HEAD` lu sans git et
  sans jamais lire `.git/config` ; comparaison `GET /repos/{dépôt}/compare/{commit}...main` (API publique, jeton
  `JARVIS_GITHUB_TOKEN` en lecture seule seulement si le dépôt redevient privé) toutes les 30 min ou dès que le commit
  du disque change.

### 8.6 Écarts avec le prompt corrigé

| Prompt corrigé | Fait | Pourquoi |
|---|---|---|
| `lab:error` (erreur de `lab_status.status()`) | `lab:base` et `lab:v1_pause` | `status()` est trop lourd pour tourner chaque minute ; la lecture légère dit la même chose, et la pause du Labo 1 méritait son constat |
| `errors:<module>` : WARNING et plus | ERROR et CRITICAL | les WARNING des bascules de source (Twelve Data → Alpaca) reviennent chaque minute : du bruit |
| `auth` sur 401 **ou 403** | 401 seulement | un 403 d'Alpaca ou de Twelve Data dit « pas dans ton offre » ou « symbole non couvert », pas « clé morte » |
| `discord:bot` | quatre constats | jeton refusé, intention « Message Content » coupée, déconnexion simple et bot non configuré n'ont pas les mêmes étapes |
| `routine:soir:<date>`, `routine:recherche:<date>` | `routine:soir`, `routine:recherche` | une seule ligne par routine, fermée au dépôt suivant |
| `deploy:rebuild` : `HEAD` du disque ≠ version démarrée | empreinte du code de l'image ≠ empreinte du disque | un conteneur relancé (`restart`, `--force-recreate`) après un `git pull` relit le nouveau `HEAD` alors qu'il tourne sur l'ancienne image : faux « à jour » |
| `deploy:pending` pour tout commit de `main` | seulement si un fichier de `app/`, `requirements.txt`, `Dockerfile` ou `docker-compose.yml` change | une PR de docs ou de tests n'a rien à déployer |
| `exit:undeclared` : `close` sans raison déclarée | `stop` et `tp…` | un `close` ne vient que d'une clôture faite par Simão (route ou `!sortie`) : déjà déclarée |
| `earnings:<symbole>`, `sec8k:<symbole>` | `advice:<id>` | le conseil avant la clôture (`advice.py`) dit déjà SORTIR avant des résultats, en prix ; le cache de l'agenda n'est rempli que si la vue Agenda a été ouverte, et le moteur ne doit déclencher aucun appel |
| `reco:stalled` | `loop:stuck` / `loop:failing` du journal des recommandations (portée Papier) | même cause, même étape |
| cotation périmée en Papier | non | les recommandations se lisent en bougies de 5 min : une alerte à 2 min serait du bruit |

Manques assumés : le worker du labo a ses propres appels et crédits Twelve Data, invisibles ici ; le prix
d'exécution réel chez IBKR n'est connu que par `!sortie`.

### 8.7 Garde-fous vérifiés par les tests (`tests/test_attention.py`)

Base saine → aucun constat ; un constat ne peut proposer qu'une action de la liste blanche ou une vue connue, et sa
preuve ne garde aucune clé ; commande `ssh` sans adresse dans le code ; worker arrêté ou bloqué, base absente, garde
du Labo 1, décisions du council (veto sur la dernière seulement, plus après un veto) ; boucle du suivi bloquée (action
avec un trade ouvert), boucle qui échoue, délai de grâce qui garde les constats ; erreurs répétées hors planificateur ;
disque ; source seule en panne, grâce de 5 min, source plus appelée, trois sources → un seul constat réseau, 403 de la
SEC ou d'Alpaca ≠ clé, clé Twelve Data refusée dans une réponse 200 (sans la clé dans la page), quota, clés absentes ;
notifications coupées, file Discord bloquée, bot déconnecté (jeton refusé) ; routines ; déploiement (en attente, docs
seulement, pull sans reconstruction, inconnu, pas encore comparé) ; `HEAD` lu sans git (refs, packed-refs, référence
piégée), empreinte du code, comparaison GitHub par une fausse API (PR fusionnées et écrasées, sans jeton, 404) ; sortie
à déclarer fermée par `!sortie`, KILL après la clôture, aucune cotation d'action hors séance, cotation périmée en séance,
conseil et signes de sortie, arrêt du jour et risque ouvert, grade périmé, propositions ; règle qui plante, cycle de
vie (rangé, toujours constaté, récidive, purge à 30 jours), ordre et compteurs, moteur arrêté, fichier illisible,
relecture au redémarrage ; **le moteur n'envoie rien** (file Discord et MP vides) et **ne prend pas le verrou** ;
routes (CSRF, autre site, 404, 400, journal) ; `state`, santé et diagnostic sans jeton.
