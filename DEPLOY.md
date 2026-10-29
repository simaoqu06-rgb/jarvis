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

Le cockpit est à l'adresse `/cockpit` du serveur (ancien cockpit : `/cockpit/ancien` ; page du labo : `/cockpit/labo`),
qui n'est pas ouvert sur Internet. Tailscale (gratuit pour un usage personnel) crée un réseau privé entre tes
appareils : le cockpit n'est visible que par toi, sans ouvrir de port dans le firewall.

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
   Ouvre `https://<…>.ts.net:4443/cockpit` sur ton PC ou ton téléphone. Sur l'iPhone : Safari > Partager > « Sur
   l'écran d'accueil » ; le cockpit s'ouvre alors comme une appli, barre d'onglets en bas.

Le cockpit exige **HTTPS** (port 4443) : le jeton qui protège ses actions est un cookie sécurisé, que le navigateur
ne renvoie pas en HTTP. Il ne fonctionne donc ni en `http://<ip>:8000` ni sur le port 443, réservé au Funnel public
du labo (`/api/lab/*` seulement, voir plus bas).

**La page ne s'ouvre pas sur l'iPhone ?** Coupe le **Relais privé iCloud** (Réglages > ton nom > iCloud > Relais
privé, ou dans les réglages du réseau Wi-Fi utilisé) et vérifie que l'appli Tailscale est connectée : le relais peut
contourner le DNS de Tailscale, et l'adresse `.ts.net:4443` ne répond plus.

Après une mise à jour du cockpit, recharge la page une fois (les fichiers sont versionnés, le navigateur prend la
nouvelle version). Si une vue reste vide, le bouton « Télécharger le diagnostic » de la vue Santé (touche `8`)
produit un JSON sans aucune clé ni valeur de variable, à transmettre tel quel.

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
2. **Bot Discord** (commandes `!plan`, `!avis`, `!societe`, `!prends`, `!positions`, `!position`, `!scan`, `!bilan`,
   `!propositions`, `!oui`, `!non`, `!regle`, `!labo`, `!aide`, et propositions de réglages en privé ; détail dans le
   [README](README.md#commandes-discord)) :
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
   Il t'envoie aussi en privé une copie des alertes de tes trades suivis (`JARVIS_TRADE_DM=off` pour la couper) :
   le salon ne mentionne personne, le message privé sonne.

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

### Archive d'audit des mesures remplacées (2e tour d'avis)

Quand une déclaration change les lignes du labo v2 (n° 10 : simulateur révision 2, témoin à la même heure, prix
négociés ; n° 11 : colonnes et stratégies en plus), le labo réécrit ses anciennes lignes. Avant toute réécriture, le
worker les copie **automatiquement**, une seule fois, dans `data/archives/labo2-avant-n<déclaration>.db` (à côté de
`lab.db` ; `labo2-avant-n11.db` pour la première, la n° 11 étant en vigueur à la mise en service) : tables
`strat_trades`, `kill_complet` et `plus10_trades`, plus `archive_info` (heure, ce qui est remplacé, lignes par table).
Un fichier plus ancien du même dossier (la copie manuelle `labo2-avant-n10.db`, si elle a été faite) est gardé et
apparié à part dans l'audit. Si la copie échoue (disque plein…), le worker ne réécrit rien (rattrapage, Killmode
complet, n° 9 et recalcul du direct attendent) et le dit en tête de `!labo` et dans `/api/lab/status` (« ⚠️ Archive d'audit en échec depuis … (raison) »), avec le détail dans
`docker compose logs jarvis-lab`. Il réessaie à chaque passage : une fois la place libérée, l'alerte s'en va seule.
L'archive sert seulement à l'audit (`!strategies`, section « Audit », et `GET /api/lab/strategies/audit`) : rien
ne la lit pour décider. Le fichier fait à peu près la taille des lignes remplacées (pour la n° 11, presque toute la
table des stratégies : de l'ordre de 1 Go, quelques secondes à quelques minutes au premier passage) ; `data/` n'est jamais effacé
par le ménage. N° 13 : seules les lignes actions sont réécrites, donc archivées dans `labo2-avant-n13.db` ; la crypto
reste en n° 11 et garde `labo2-avant-n11.db`.

En secours (worker arrêté ou ancienne version du code), la même copie à la main, depuis le dossier de JARVIS
(`docker compose run` lance un conteneur à part sur la même image, le même `.env` et le même `data/`, que le worker
tourne ou non). Elle lit la déclaration en vigueur dans le code de l'image et ne crée une table que si elle manque
(l'image n'a pas l'outil `sqlite3`, d'où Python) :

```bash
docker compose run --rm --no-deps -T jarvis-lab python - <<'EOF'
import os, sqlite3
from app.lab import strategies as ST
d = ST.DECLARATION
db = os.getenv("JARVIS_LAB_DB", "data/lab.db")
out = os.path.join(os.path.dirname(db) or ".", "archives", f"labo2-avant-n{d}.db")
os.makedirs(os.path.dirname(out), exist_ok=True)
c = sqlite3.connect(db, timeout=300)
c.execute("ATTACH DATABASE ? AS a", (out,))
have = {r[0] for r in c.execute("SELECT name FROM a.sqlite_master WHERE type = 'table'")}
markets = "'stock', 'crypto'" if ST.CRYPTO_DECLARATION == d else "'stock'"
old = f"WHERE market IN ({markets}) AND declaration IS NOT {d}"
for t, where in (("strat_trades", old), ("kill_complet", ""), ("plus10_trades", "")):
    if t not in have:
        c.execute(f"CREATE TABLE a.{t} AS SELECT * FROM main.{t} {where}")
        c.commit()
    print(t, c.execute(f"SELECT COUNT(*) FROM a.{t}").fetchone()[0], "lignes archivées")
print("Archive :", out)
EOF
```

Le worker reconnaît cette copie : il ne recrée pas ses tables et n'y ajoute que les lignes qui manquent.

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
ssh root@2.31.19.106
cd /root/jarvis && git pull && docker compose up -d --build && docker image prune -f
```

Puis ouvre `https://jarvis.tail420262.ts.net:4443/cockpit` pour vérifier (la pastille « JARVIS » en haut passe au
vert dès le premier passage du suivi, une minute environ, et la vue Santé montre chaque boucle).

`docker image prune -f` efface les anciennes versions de l'image que chaque mise à jour laisse derrière elle
(images « sans nom », plus utilisées par aucun conteneur). Les données de `data/` ne sont pas touchées.

**Savoir s'il y a quelque chose à déployer** : la rubrique « À faire » du cockpit le dit. Le dossier `/root/jarvis`
est monté en lecture seule dans le conteneur `jarvis` (sous `/repo`, `docker-compose.yml`) : JARVIS compare le code qui
tourne à celui du dossier (`git pull` fait sans `--build` → « Code récupéré mais pas reconstruit »), puis le commit du
dossier à `main` sur GitHub, toutes les 30 min (« N PR fusionnées pas encore déployées », seulement si elles touchent
au code). Il ne lit jamais `.git/config` ni `.env` dans ce montage. Le montage prend effet au premier
`docker compose up -d --build` qui suit (le conteneur est recréé). Deux réglages facultatifs dans `.env` :
- `JARVIS_SSH_CIBLE=root@<adresse du serveur>` : les commandes `ssh` de la page arrivent toutes prêtes (sinon elles
  affichent `IP_DU_SERVEUR` à remplacer) ;
- `JARVIS_GITHUB_TOKEN` (jeton en **lecture seule**) : seulement si le dépôt redevient privé ; public, il ne sert pas.

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
