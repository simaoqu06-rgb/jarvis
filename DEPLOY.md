# Héberger JARVIS 24 h/24

Le serveur doit tourner sans interruption : c'est lui qui envoie le point du jour et les alertes
Discord. Il garde aussi deux petits fichiers dans `data/` (dernière valeur connue, alertes déjà
envoyées). Il faut donc une machine toujours allumée avec un disque qui persiste ; les offres
gratuites qui mettent le serveur en veille ne conviennent pas.

L'installation passe par Docker et marche pareil sur n'importe quel serveur Linux
(VPS Hetzner, Oracle Cloud, Raspberry Pi, PC).

## 1. Créer le serveur (exemple : Hetzner)

Crée un compte sur https://www.hetzner.com/cloud, puis « Add Server » avec :

| Option | Choix | Pourquoi |
|---|---|---|
| Type | Cost-Optimized, x86, **CX23** (2 vCPU, 4 Go, 40 Go) | JARVIS utilise ~45 Mo de mémoire. Si CX23 est indisponible : CAX11 (Arm, même image Docker) |
| Location | Nuremberg ou Falkenstein | les plus proches de Lyon, même prix |
| Image | **Ubuntu 26.04** | LTS supportée jusqu'en 2031, Docker la supporte |
| Networking | IPv4 **et** IPv6 cochées | GitHub et plusieurs API n'ont pas d'IPv6 |
| SSH key | ta clé publique | ne peut plus être ajoutée depuis la console après coup |
| Volumes, réseau privé, placement, labels | rien | inutile |
| Firewall | créer « jarvis » : entrant TCP 22 + ICMP seulement | aucun port de JARVIS n'est exposé |
| Backups | non (activables plus tard) | code sur GitHub, état reconstruit tout seul |
| Cloud config | le bloc ci-dessous | installe Docker au premier démarrage |
| Name | jarvis | |

```yaml
#cloud-config
package_update: true
package_upgrade: true
runcmd:
  - curl -fsSL https://get.docker.com | sh
```

Note l'adresse IPv4 du serveur une fois créé.

## 2. Installer Docker et JARVIS

Connecte-toi (`ssh root@IP_DU_SERVEUR`) puis :

```bash
docker --version   # sinon : curl -fsSL https://get.docker.com | sh
git clone https://github.com/simaoqu06-rgb/jarvis.git
cd jarvis
cp .env.example .env
nano .env    # colle DISCORD_WEBHOOK_URL, TWELVEDATA_API_KEY, FINNHUB_API_KEY ; Ctrl+O, Entrée, Ctrl+X
docker compose up -d --build
```

Le dépôt est privé : `git clone` demandera ton identifiant GitHub et un jeton
(https://github.com/settings/tokens, droit « Contents: read » sur ce dépôt) à la place du mot de passe.

## 3. Vérifier

```bash
docker compose logs -f                                  # Ctrl+C pour quitter
curl -X POST http://127.0.0.1:8000/api/notify/test      # message de test sur Discord
```

Le conteneur redémarre tout seul si le serveur redémarre (`restart: unless-stopped`).
`docker ps` indique aussi son état de santé (`healthy` / `unhealthy`), vérifié chaque minute sur `/api/health`.
Les routes ne sont ouvertes que sur le serveur lui-même (127.0.0.1), pas sur Internet.

## Ouvrir le Trading Cockpit sur ton PC et ton téléphone (Tailscale)

Le cockpit est à l'adresse `/cockpit` du serveur (page du labo : `/cockpit/labo`), qui n'est pas ouvert sur Internet. Tailscale
(gratuit pour un usage personnel) crée un réseau privé entre tes appareils : le cockpit n'est
visible que par toi, sans ouvrir de port dans le firewall.

1. Crée un compte sur https://login.tailscale.com/start et installe l'application Tailscale sur
   ton PC et ton téléphone (connecte-les au même compte).
2. Sur le serveur :

   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   tailscale up                      # ouvre le lien affiché pour relier le serveur à ton compte
   tailscale serve --bg --https=4443 http://127.0.0.1:8000   # JARVIS dans ton réseau privé, en HTTPS
   ```

   Si la commande `serve` affiche un lien pour activer HTTPS sur ton réseau Tailscale, ouvre-le et
   accepte, puis relance la commande.
3. `tailscale serve status` affiche l'adresse, du type `https://<nom-du-serveur>.<ton-réseau>.ts.net:4443`.
   Ouvre `https://<…>.ts.net:4443/cockpit` sur ton PC ou ton téléphone (ajoute-la à l'écran d'accueil).

Le message « Trading Cockpit » apparaît aussi sur Discord dès le premier trade suivi : épingle-le
une fois (clic droit > Épingler), JARVIS le réécrit chaque minute.

## Scanner, forward-test et commandes Discord

Deux ajouts dans `.env`, puis `docker compose up -d` :

1. **Alpaca** (scanner et compte fictif) : sur https://app.alpaca.markets, passe sur le compte
   *Paper*, section *API Keys*, génère une paire de clés et colle-les :

   ```
   ALPACA_API_KEY=...
   ALPACA_SECRET_KEY=...
   ALPACA_PAPER=true
   ```

   Génère une nouvelle paire plutôt que de reprendre celle d'OpenClaw, puis révoque l'ancienne.
2. **Bot Discord** (commandes `!plan`, `!avis`, `!prends`, `!positions`, `!scan`, `!bilan`, `!propositions`, `!oui`, `!non`,
   `!regle`, `!labo`, `!aide`, et propositions de réglages en privé ; détail dans le [README](README.md#commandes-discord)) :
   - https://discord.com/developers/applications > *New Application* > onglet *Bot* :
     *Reset Token* et copie-le, puis active *Message Content Intent*.
   - Onglet *OAuth2* > *URL Generator* : coche `bot`, puis les permissions *View Channels*,
     *Send Messages*, *Read Message History* ; ouvre le lien et ajoute le bot à ton serveur.
   - Ton identifiant Discord : *Paramètres > Avancés > Mode développeur*, puis clic droit sur
     ton nom > *Copier l'identifiant*.

   ```
   DISCORD_BOT_TOKEN=...
   JARVIS_DISCORD_OWNER_ID=...
   ```

   Le bot ne répond qu'à ce compte (plusieurs identifiants possibles, séparés par des virgules).

## Labo de performance

`docker compose up -d --build` démarre les deux services : `jarvis` (le serveur) et `jarvis-lab`
(le labo, un seul cœur, sans port). Le labo utilise les clés Alpaca déjà dans `.env` ; la crypto vient
de l'API publique d'OKX, sans clé. Réglages dans `.env` :

```
JARVIS_LAB_ENABLED=on        # off : le service reste en veille
JARVIS_LAB_DB=data/lab.db
JARVIS_LAB_REPLAY_DAYS=504   # séances rejouées (≈ 2 ans)
JARVIS_LAB_ALPACA_PER_MINUTE=100  # part de la limite Alpaca (200/min) laissée au labo
JARVIS_LAB_FINNHUB_PER_MINUTE=20  # actus des fiches catalyseurs (Finnhub gratuit : 60/min en tout)
JARVIS_LAB_API_KEY=              # clé des routes /api/lab/* de la routine du soir ; vide = routes désactivées
```

Les fiches catalyseurs utilisent la clé `FINNHUB_API_KEY` déjà présente ; sans elle, les fiches sont
créées sans actus. Le labo ne demande jamais d'actus pendant la séance US.

Le labo ne touche pas aux actions pendant la séance US (la crypto continue) et tourne surtout la
nuit et le week-end. Il est limité à 1 cœur et 1,5 Go de mémoire.

Avant le premier lancement, vérifie la profondeur des sources, la place libre et la taille estimée
de la base :

```bash
docker compose run --rm jarvis-lab python -m app.lab.check_sources
```

Ensuite : `docker compose logs -f jarvis-lab` pour suivre le rejeu, `!labo` sur Discord ou
`curl http://127.0.0.1:8000/api/lab/status` pour l'avancement. Le premier passage télécharge
l'historique (bougies journalières de toutes les actions, puis 5 min des valeurs de chaque jour) :
compter quelques heures avant la fin du rejeu, surtout la nuit et le week-end.

### Accès du labo pour la routine du soir (Labo 2)

Les routes `GET /api/lab/summary`, `/api/lab/stats`, `/api/lab/catalysts`, `/api/lab/rules` et
`POST /api/lab/rules` demandent la clé `JARVIS_LAB_API_KEY` dans l'en-tête `X-Lab-Key` (ou
`Authorization: Bearer …`). Sans clé dans `.env`, elles répondent 503. Génère une clé longue :

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

puis mets-la dans `.env` et relance `docker compose up -d`. Essai depuis le serveur :

```bash
curl -H "X-Lab-Key: $JARVIS_LAB_API_KEY" http://127.0.0.1:8000/api/lab/summary
curl -H "X-Lab-Key: $JARVIS_LAB_API_KEY" -H "Content-Type: application/json" \
     -d '{"nom": "essai", "exclude": {"field": "context.rvol", "op": ">", "value": 5}}' \
     http://127.0.0.1:8000/api/lab/rules
```

Une règle soumise est seulement mise en file (`data/lab_queue/`) ; le worker la valide la nuit et
rien ne change dans la méthode en direct. Le serveur n'ouvre `data/lab.db` qu'en lecture seule.
`/api/lab/status` reste sans clé depuis le serveur et ton réseau Tailscale ; depuis Internet (Funnel),
il demande la clé lui aussi.

### Labo 3 : message du matin et méthode en place

Deux réglages dans `.env` :

```
JARVIS_LAB_API_KEY=...   # la clé ci-dessus (la routine du soir s'en sert)
JARVIS_LAB_DM=off        # rodage : tout tourne, le message du matin est gardé sans être envoyé
```

- **Rodage** : laisse `JARVIS_LAB_DM=off` pendant ~5 séances. Le message du matin est préparé chaque
  jour entre 8 h et 9 h (heure de Paris) et gardé : `!labo`, `!propositions`, la page
  `https://<…>.ts.net:4443/cockpit/labo` et `GET /api/lab/matin` le montrent. Les propositions peuvent
  déjà être approuvées (`!oui N`, cockpit).
- **En service** : `JARVIS_LAB_DM=on` puis `docker compose up -d`. Un seul message privé par jour,
  entre 8 h et 9 h, seulement s'il y a des règles validées (boutons Approuver / Refuser par règle).
  Il faut le bot Discord (`DISCORD_BOT_TOKEN`, `JARVIS_DISCORD_OWNER_ID`).
- Une règle approuvée part en essai au labo (challenger, « v2 ») ; elle ne s'applique au scanner
  en direct qu'après 100 trades en direct meilleurs que la méthode en place (message « Méthode v2 en
  place »). Le worker écrit la méthode en place dans `data/lab_champion.json`, que JARVIS relit ; sans
  ce fichier, JARVIS tourne sans filtre.

### Ouvrir l'accès à la tâche du soir

La routine du soir (tâche Claude planifiée à 22 h 52, du lundi au vendredi) tourne hors de ton réseau : elle passe par
**Tailscale Funnel**, qui publie sur Internet un seul chemin, `/api/lab`, sur le port 443 (le seul
que l'environnement cloud des routines laisse sortir : 8443 et 10000 y sont coupés).
Le cockpit reste privé (`tailscale serve`, port 4443, réseau Tailscale seulement).

1. Active Funnel pour ton réseau la première fois : la commande ci-dessous affiche un lien si besoin
   (console Tailscale > *Access controls* : attribut `funnel` pour le serveur) ; ouvre-le et accepte.
2. Sur le serveur :

   ```bash
   sudo tailscale serve --https=443 off     # si le cockpit était encore servi sur 443
   sudo tailscale serve --bg --https=4443 http://127.0.0.1:8000
   sudo tailscale funnel --bg --https=443 --set-path=/api/lab http://127.0.0.1:8000/api/lab
   tailscale funnel status
   ```

   `status` doit montrer **seulement** `https://<…>.ts.net/api/lab` publié sur Internet
   (*Funnel on*), et `https://<…>.ts.net:4443/` (le cockpit) en réseau privé (*tailnet only*).
   Les premières minutes, le temps que Tailscale prépare le certificat, des connexions échouent.
3. Vérifie depuis un téléphone **hors Wi-Fi et sans Tailscale** :
   - `https://<…>.ts.net/api/lab/brief` → 401 sans clé (normal) ;
   - `https://<…>.ts.net/cockpit` → 404, et `https://<…>.ts.net:4443/cockpit` ne répond pas.
4. Donne à la routine l'adresse `https://<…>.ts.net/api/lab` et la clé (en-tête `X-Lab-Key`).
   Elle utilise :
   - `GET /api/lab/brief` : tout en un appel (résumé du soir, statistiques, fiches à classer,
     règles récentes, méthode en place et challenger, place restante ce soir) ;
   - `POST /api/lab/rules` : une règle, avec `"pourquoi"` (≤ 1 000 caractères) et `"council"`
     (≤ 2 000) ; au plus 3 nouvelles règles par soirée (au-delà : 429) ;
   - `POST /api/lab/catalysts/types` : `[{"id": 12, "type": "resultats"}, …]` (types : `types_permis`
     du brief) ;
   - `POST /api/lab/lessons` : une leçon (`texte`, `quand`, `sens`, `markets`, `pourquoi`) ou un
     retrait (`{"retirer": 12, "raison": …}`), et `GET /api/lab/lessons` (carnet des leçons) ;
   - `POST /api/lab/grade/avis` : avis du council sur le grade (`marche`, `ajustement` −1, 0 ou 1,
     `motifs`, `voix`), un par marché et par soir ; `GET /api/lab/grade` (grade et historique) ;
   - au besoin `GET /api/lab/summary`, `/stats`, `/catalysts`, `/rules`.

**Ne remets jamais** le cockpit (`tailscale serve`) sur le port 443, et ne lance jamais
`tailscale funnel` sans `--set-path` : Funnel s'active pour tout un port, et publierait aussi le
cockpit. Pour tout couper :

```bash
sudo tailscale funnel --https=443 --set-path=/api/lab off
```

Change la clé (`JARVIS_LAB_API_KEY`, puis `docker compose up -d`) si elle a pu fuiter.

## Mettre à jour

```bash
cd jarvis && git pull && docker compose up -d --build && docker image prune -f
```

`docker image prune -f` efface les anciennes versions de l'image que chaque mise à jour laisse derrière elle
(images « sans nom », plus utilisées par aucun conteneur). Les données de `data/` ne sont pas touchées.

## Ménage automatique

JARVIS vérifie le disque toutes les 30 min et prévient sur Discord au-dessus de 75 %
(`JARVIS_DISK_ALERT_PCT`), puis encore au-dessus de 90 %. Il n'efface rien lui-même.

Le ménage Docker se programme une fois sur le serveur (chaque dimanche à 4 h : anciennes images
et cache de build de plus d'une semaine ; `data/` n'est jamais touché) :

```bash
( crontab -l 2>/dev/null | grep -v jarvis-menage; echo '0 4 * * 0 docker image prune -f >/dev/null && docker builder prune -f --filter until=168h >/dev/null # jarvis-menage' ) | crontab -
```

`crontab -l` montre la ligne ; pour l'enlever : `crontab -l | grep -v jarvis-menage | crontab -`.

## Arrêter les notifications sans tout couper

Mets `JARVIS_NOTIFICATIONS=off` dans `.env`, puis `docker compose up -d`.
