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

## Mettre à jour

```bash
cd jarvis && git pull && docker compose up -d --build
```

## Arrêter les notifications sans tout couper

Mets `JARVIS_NOTIFICATIONS=off` dans `.env`, puis `docker compose up -d`.
