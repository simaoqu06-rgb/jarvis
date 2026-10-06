# Règles EmperorBTC appliquées par JARVIS

Source : *EmperorBTC Trading Manual* (traduction française), déposé par Simão le 6 octobre 2026.
Chaque règle du manuel que JARVIS peut calculer est listée ici avec l'endroit du code qui l'applique.
Ce qui demande un œil humain (tracer une ligne de tendance, juger une tête-épaules) reste à Simão.

## Avant d'entrer : le « Trident de trading »

> « N'ouvrez un trade QUE lorsque vous avez établi un Trident de trading clair :
> déclenchement de l'entrée, niveau d'invalidation (stop loss), signes de renversement (sortie). »

| Règle du manuel | Ce que fait JARVIS | Code |
|---|---|---|
| Stop = niveau d'invalidation, jamais un % fixe | Stop sous le dernier creux de swing (au-dessus du dernier sommet pour un short) | `plan._invalidation` |
| Stop « un peu en dessous » pour éviter la chasse aux stops | Marge de 0,25 ATR au-delà du niveau, stop borné entre 1 et 3 ATR | `STOP_BUFFER_ATR` |
| La cible est la prochaine résistance où le prix peut se retourner | TP sur les prochains sommets de swing, pivots, plus haut/bas de la veille, un peu avant le niveau ; niveaux proches fusionnés en zones | `plan._candidate_levels`, `_merge` |
| Ne pas entrer sans connaître le R:R | Chaque TP affiche son multiple de R | `service.build_plan` |
| Pas de R:R fixe : choisir un R:R qui correspond à son taux de gain | Taux de réussite mesuré par TP, R:R minimal pour être rentable et espérance en R | `plan.expectancy` |

## Déclencheur d'entrée (checklist)

| Règle du manuel | Contrôle | Code |
|---|---|---|
| « Ne tradez que dans le sens du marché » (MM 50 jours), « ne détenez rien sous la MM 200 jours » | Prix au-dessus des MM 50 et 200 jours (en dessous pour un short) | `signals.checklist` → `trend_daily` |
| EMA 200 en 4 h comme support | Prix du bon côté de l'EMA 200 4 h | `ema200_4h` |
| Stratégie de day trading : Stoch RSI + VWAP + points pivots, les trois, dans n'importe quel ordre | Croisement Stoch RSI (14, 14, 3, 3) sur les 3 dernières bougies, prix du bon côté du VWAP de séance, pivot tenu ou cassé | `stoch_rsi`, `vwap`, `pivot` |
| Le volume confirme la tendance (moyenne 14) | Dernière bougie close au-dessus du volume moyen 14, dans le sens du trade | `volume` |
| Divergences RSI (forte, sur les clôtures) | Bonus si divergence récente dans le sens du trade | `indicators.rsi_divergence` |
| Marteau (mèche > 2× le corps), engulfing | Bonus si bougie de retournement récente | `indicators.is_hammer`, `engulfing` |

Verdict : **Feu vert** quand la tendance journalière est bonne et que Stoch RSI, VWAP et pivot
sont tous validés ; **attendre** si le déclencheur est incomplet ; **contre la tendance** sinon.

## Gestion du risque

| Règle du manuel | Ce que fait JARVIS | Code |
|---|---|---|
| Risquer 3 % du capital par trade (5 % = agressif) | `JARVIS_RISK_PCT=3` par défaut | `service.sizing` |
| La taille découle du stop, le levier découle de la taille | Taille = risque ÷ distance au stop, levier = taille ÷ capital | `service.sizing` |
| « Ne soyez pas pressé de passer votre SL au seuil de rentabilité » | Stop inchangé après le TP1, remonté à l'entrée après le TP2 | `service.apply_bars` |

## Sortie anticipée (pendant un trade gagnant)

> « Un renversement n'est pas un signal pour prendre un trade opposé : prenez vos bénéfices. »

| Règle du manuel | Alerte Discord | Code |
|---|---|---|
| Divergence RSI contraire = sommet probable | Divergence baissière sur un long (haussière sur un short) | `signals.exit_signals` |
| Anomalie corps/volume de David Weis | Gros volume (≥ 2× la moyenne), petit corps, longue mèche contre le trade | `indicators.weis_anomaly` |
| « Si le mouvement après l'entrée se fait sous le volume moyen, CHERCHEZ UNE SORTIE » | 3 bougies dans le sens du trade sous le volume moyen | `low_volume` |
| Bougie de retournement confirmée par le volume | Engulfing ou étoile filante contre le trade avec du volume | `reversal_candle` |

Chaque signal n'est envoyé qu'une fois par trade, et seulement quand le trade est en gain.

## Pas encore automatisé

- Intérêt ouvert (open interest) des futures crypto : demande une source de données dérivées.
- Lignes de tendance, tête-épaules, cup and handle, triangles : à juger sur le graphique.
- Journal de trading avec statistiques (taux de réussite réel de Simão) : le journal existe dans le
  cockpit, les statistiques viendront ensuite.
