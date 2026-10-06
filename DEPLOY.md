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
Les routes ne sont ouvertes que sur le serveur lui-même (127.0.0.1), pas sur Internet.

## Ouvrir le Trading Cockpit sur ton PC et ton téléphone (Tailscale)

Le cockpit est à l'adresse `/cockpit` du serveur, qui n'est pas ouvert sur Internet. Tailscale
(gratuit pour un usage personnel) crée un réseau privé entre tes appareils : le cockpit n'est
visible que par toi, sans ouvrir de port dans le firewall.

1. Crée un compte sur https://login.tailscale.com/start et installe l'application Tailscale sur
   ton PC et ton téléphone (connecte-les au même compte).
2. Sur le serveur :

   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   tailscale up                      # ouvre le lien affiché pour relier le serveur à ton compte
   tailscale serve --bg 8000         # publie JARVIS dans ton réseau privé, en HTTPS
   ```

   Si la commande `serve` affiche un lien pour activer HTTPS sur ton réseau Tailscale, ouvre-le et
   accepte, puis relance la commande.
3. `tailscale serve status` affiche l'adresse, du type `https://<nom-du-serveur>.<ton-réseau>.ts.net`.
   Ouvre `https://<…>.ts.net/cockpit` sur ton PC ou ton téléphone (ajoute-la à l'écran d'accueil).

Le message « Trading Cockpit » apparaît aussi sur Discord dès le premier trade suivi : épingle-le
une fois (clic droit > Épingler), JARVIS le réécrit chaque minute.

## Scanner, forward-test et commandes Discord

Trois ajouts dans `.env`, puis `docker compose up -d` :

1. **Alpaca** (scanner et compte fictif) : sur https://app.alpaca.markets, passe sur le compte
   *Paper*, section *API Keys*, génère une paire de clés et colle-les :

   ```
   ALPACA_API_KEY=...
   ALPACA_SECRET_KEY=...
   ALPACA_PAPER=true
   ```

   Génère une nouvelle paire plutôt que de reprendre celle d'OpenClaw, puis révoque l'ancienne.
2. **Bot Discord** (commandes `!plan`, `!prends`, `!scan`, `!bilan`, `!oui`, `!non`, et propositions de réglages en privé) :
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

## Mettre à jour

```bash
cd jarvis && git pull && docker compose up -d --build
```

## Arrêter les notifications sans tout couper

Mets `JARVIS_NOTIFICATIONS=off` dans `.env`, puis `docker compose up -d`.
