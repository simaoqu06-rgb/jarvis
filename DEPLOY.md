# Héberger JARVIS 24 h/24

Le serveur doit tourner sans interruption : c'est lui qui envoie le point du jour et les alertes
Discord. Il garde aussi deux petits fichiers dans `data/` (dernière valeur connue, alertes déjà
envoyées). Il faut donc une machine toujours allumée avec un disque qui persiste ; les offres
gratuites qui mettent le serveur en veille ne conviennent pas.

L'installation passe par Docker et marche pareil sur n'importe quel serveur Linux
(VPS Hetzner, Oracle Cloud, Raspberry Pi, PC).

## 1. Créer le serveur (exemple : Hetzner)

1. Crée un compte sur https://www.hetzner.com/cloud.
2. « Add Server » : image **Ubuntu 24.04**, le plus petit type (CX22 ou équivalent), une région
   en Europe (Falkenstein, Nuremberg ou Helsinki).
3. Ajoute ta clé SSH si tu en as une, sinon Hetzner t'envoie un mot de passe root par mail.
4. Note l'adresse IP du serveur.

## 2. Installer Docker et JARVIS

Connecte-toi (`ssh root@IP_DU_SERVEUR`) puis :

```bash
curl -fsSL https://get.docker.com | sh
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
