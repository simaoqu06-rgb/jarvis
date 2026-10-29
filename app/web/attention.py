"""Rubrique « À faire » du Trading Cockpit (prompt corrigé du 29/10/2026, docs/CARTE-INTERFACE.md §8).

Ce que Simão doit faire de son côté, les problèmes que JARVIS voit dans son propre fonctionnement, et ce qui est bon
à savoir. Chaque constat dit ce qui se passe, sur quelle preuve (`evidence`), et quoi faire exactement ; il se ferme
tout seul quand sa cause disparaît.

- Une règle = une fonction pure `(ctx, now) -> list[constat] | None`, qui lit l'état déjà en mémoire ou sur disque :
  **aucun appel réseau** (jamais Twelve Data), rien n'est envoyé sur Discord, rien ne change la stratégie, la taille,
  le grade, un filtre ou le Killmode. `None` = pas d'avis (délai de grâce au démarrage) : ses constats restent tels
  quels. Chaque règle tourne dans son propre `try` : une règle qui plante devient le constat `rule:<nom>` et ses
  constats ouverts ne sont pas fermés à tort.
- Le moteur (`run`) tourne toutes les minutes dans le planificateur, sans le verrou global (il ne fait que lire), et
  synchronise data/attention.json : clé nouvelle → nouvelle ligne ; clé ouverte → mise à jour ; clé disparue →
  fermée (`resolved_at`). Une récidive ouvre une nouvelle ligne. « Ranger » (`ack_at`) ne ferme rien.
- Calculé à la lecture : `attention:stale` (le moteur ne tourne plus). Un JARVIS entièrement arrêté ne sert plus la
  page : c'est la bannière « API muette » du cockpit qui le dit.
- Portées : `commun`, `trades` (Mes trades), `paper` (Papier).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from app import alerts, config, scheduler
from app.core import sources_health, version
from app.lab import db as lab_db
from app.lab.method import COUNCIL
from app.lab import routines_watch as RW
from app.notify import bot
from app.sources import twelvedata
from app.trading import calendar as CAL
from app.trading import proposals as PR
from app.trading import research
from app.trading import service as T
from app.web import cockpit_api as API
from app.web.cockpit_actions import _journal, csrf_ok

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/cockpit", include_in_schema=False)

PATH = os.getenv("JARVIS_ATTENTION_PATH", "data/attention.json")
PARIS, NEW_YORK = T.PARIS, CAL.NEW_YORK
STARTED = datetime.now(timezone.utc)  # démarrage du processus : délais de grâce

KINDS = ("action", "probleme", "info")
SPACES = ("commun", "trades", "paper")
SPACE_ORDER = {"trades": 0, "commun": 1, "paper": 2}  # Mes trades d'abord (ton argent), puis commun, puis Papier
GO_VIEWS = {"cockpit", "afaire", "marches", "positions", "risque", "labo", "agenda", "journal", "sante"}
STEP_ACTIONS = {"proposal_approve", "proposal_refuse", "veto"}  # seules actions qu'un constat peut proposer

# Seuils (constantes nommées : rien n'est caché dans le code des règles)
GRACE_S = 180                # après le démarrage : ni boucles, ni sources, ni bot (last_runs et la santé sont vides)
LOOP_BUSY_MAX_S = 900        # un passage en cours depuis plus de 15 min : boucle bloquée
LOOP_MIN_FAILS = 3
STALE_ENGINE_S = 180         # moteur sans passage : constat calculé à la lecture
HISTORY_H = 72               # constats réglés renvoyés pendant 72 h
KEEP_DAYS, MAX_ROWS = 30, 2000
ACK_STILL_S = 120            # rangé mais encore constaté au-delà : puce « toujours constaté »
SOURCE_MIN_FAILS, SOURCE_DOWN_S, SOURCE_IDLE_S = 3, 300, 3600
INTERNET_MIN = 3             # sources en panne en même temps : réseau du serveur
AUTH_MIN_FAILS = 2
ERRORS_MIN, ERRORS_WINDOW_S = 3, 1800
OUTBOX_LATE_S = 600
BOT_DOWN_S = 300
QUOTA_INFO_PCT = 80
UNDECLARED_DAYS = 7
KILL_GRACE_S = 300           # après la clôture de sa séance, un KILL encore ouvert
QUOTE_STALE_FACTOR = 2       # cotation périmée au double du seuil du cockpit (pas d'alerte sur un retard de bougie)
QUOTE_OPEN_GRACE_S = 300     # à l'ouverture, la cotation d'une action date de la veille jusqu'à la première bougie
COUNCIL_RECENT_H = 72
REBUILD_GRACE_S = 600        # git pull puis --build : l'ancien conteneur voit le nouveau code pendant la construction
PROPOSALS_MAX = 8
MAX_ITEMS = 200

SSH_RE = re.compile(r"^[A-Za-z0-9._@:\-]{1,100}$")


# --- Formes ------------------------------------------------------------------------------------------

def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _paris(d: datetime | None, now: datetime | None = None) -> str:
    if d is None:
        return "inconnu"
    local = d.astimezone(PARIS)
    if now is not None and local.date() == now.astimezone(PARIS).date():
        return f"{local:%H:%M}"
    return f"{local:%d/%m} à {local:%H:%M}"


def _ago(seconds: float | None) -> str:
    if seconds is None:
        return "inconnu"
    s = max(0, int(seconds))
    if s < 90:
        return f"{s} s"
    if s < 48 * 3600:
        return T.duration_fr(s / 60)
    return f"{s // 86400} j"


def _price(x) -> str:
    return "inconnu" if x is None else T._num(float(x)).replace(".", ",")


def _n(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def slug(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")[:60] or "x"


def _scalar(v):
    if isinstance(v, str):
        return sources_health.scrub(v)[:300]
    if v is None or isinstance(v, (bool, int, float)):
        return v
    if isinstance(v, (list, tuple)):
        return [_scalar(x) for x in v][:20]
    return sources_health.scrub(str(v))[:300]


def item(key: str, kind: str, space: str, title: str, detail: str, steps: list[dict] | None = None,
         evidence: dict | None = None) -> dict:
    """Un constat validé : type et portée connus, étapes de la liste fermée, preuve expurgée (aucune clé)."""
    if kind not in KINDS or space not in SPACES:
        raise ValueError(f"constat {key} : type {kind} ou portée {space} inconnus")
    steps = list(steps or [])
    for s in steps:
        if not isinstance(s.get("text"), str) or not s["text"]:
            raise ValueError(f"constat {key} : étape sans texte")
        if sum(k in s for k in ("cmd", "action", "go")) > 1:
            raise ValueError(f"constat {key} : une étape porte au plus une commande, une action ou une vue")
        if "action" in s and s["action"] not in STEP_ACTIONS:
            raise ValueError(f"constat {key} : action {s['action']} hors de la liste blanche des constats")
        if "go" in s and s["go"] not in GO_VIEWS:
            raise ValueError(f"constat {key} : vue {s['go']} inconnue")
    if not steps:
        steps = [{"text": "Rien à faire de ton côté : JARVIS reprend tout seul."}]
    ev = {str(k): _scalar(v) for k, v in (evidence or {}).items()}
    return {"key": key, "kind": kind, "space": space, "title": sources_health.scrub(title)[:160],
            "detail": sources_health.scrub(detail)[:900], "steps": steps, "evidence": ev}


def say(text: str) -> dict:
    return {"text": text}


def cmd(text: str, command: str) -> dict:
    return {"text": text, "cmd": command}


def go(label: str, view: str, symbol: str | None = None) -> dict:
    return {"text": label, "go": view} | ({"symbol": symbol} if symbol else {})


def act(label: str, action: str, args: dict | None = None) -> dict:
    return {"text": label, "action": action, "args": args or {}, "label": label}


def ssh_steps() -> list[dict]:
    target = os.getenv("JARVIS_SSH_CIBLE", "").strip()
    if target and SSH_RE.match(target):
        first = cmd("Depuis PowerShell sur ton PC, connecte-toi au serveur :", f"ssh {target}")
    else:
        first = cmd("Depuis PowerShell sur ton PC, connecte-toi au serveur (remplace IP_DU_SERVEUR par son adresse ; "
                    "avec JARVIS_SSH_CIBLE dans .env, la commande arrive ici toute prête) :", "ssh root@IP_DU_SERVEUR")
    return [first, cmd("Place-toi dans le dossier de JARVIS :", "cd /root/jarvis")]


def env_steps(*names: str) -> list[dict]:
    lines = (f"la ligne {names[0]}=… (ajoute-la si elle manque)" if len(names) == 1 else
             "les lignes " + " et ".join(f"{n}=…" for n in names) + " (ajoute-les si elles manquent)")
    return ssh_steps() + [
        cmd("Ouvre le fichier des réglages :", "nano .env"),
        say(f"Change seulement {lines}. Enregistre avec Ctrl O puis Entrée, quitte avec Ctrl X. Ne colle jamais une "
            "clé dans le cockpit ni dans un chat."),
        cmd("Relance les conteneurs pour qu'ils relisent .env (un simple restart ne le relit pas) :",
            "docker compose up -d --force-recreate"),
    ]


def diag_steps() -> list[dict]:
    return [say("Si ça dure, télécharge le diagnostic (vue Santé → « Télécharger le diagnostic ») et envoie le fichier "
                "à Claude dans le projet JARVIS."), go("Ouvrir Santé", "sante")]


def _uptime(ctx: dict) -> float:
    return ctx.get("uptime_s", 0.0)


# --- Règles : JARVIS, le labo, le serveur ----------------------------------------------------------------

def r_lab_worker(ctx: dict, now: datetime) -> list[dict]:
    hb = API.lab_heartbeat(now)
    if not hb["stale"]:
        return []
    age = hb.get("age_s")
    if age is None:
        title, detail = ("Le worker du labo ne tourne pas",
                         "Aucun battement du worker du labo (service jarvis-lab) : il n'a jamais démarré, ou il "
                         "n'écrit pas dans le même dossier data/ que JARVIS.")
    else:
        title, detail = ("Le worker du labo ne tourne plus",
                         f"Dernier battement il y a {_ago(age)} (étape : {hb.get('etape') or 'inconnue'}).")
    detail += (" Le rejeu, les déclarations, le grade et les leçons sont figés ; le suivi de tes trades, le scanner et "
               "les alertes continuent.")
    steps = ssh_steps() + [
        cmd("Regarde son état (healthy, unhealthy, arrêté) :", "docker compose ps"),
        cmd("Copie ses derniers journaux, utiles pour le diagnostic :", "docker compose logs --tail 80 jarvis-lab"),
        cmd("Relance-le :", "docker compose up -d jarvis-lab"),
    ] + diag_steps()
    return [item("lab:down", "action", "commun", title, detail, steps,
                 {"dernier battement": hb.get("le"), "âge (s)": age, "étape": hb.get("etape"),
                  "seuil (s)": API.LAB_STALE_S})]


def r_lab_base(ctx: dict, now: datetime) -> list[dict]:
    lab = ctx["lab"]
    if isinstance(lab, Exception):
        raise lab
    if lab.get("erreur"):
        return [item("lab:base", "probleme", "commun", "La base du labo est illisible",
                     f"La lecture de la base du labo échoue : {lab['erreur']}. Les vues Labo et Risque peuvent être "
                     "incomplètes.", diag_steps(), {"erreur": lab["erreur"], "base": lab.get("chemin")})]
    if lab.get("absente") and not API.lab_heartbeat(now)["stale"]:
        return [item("lab:base", "probleme", "commun", "Base du labo introuvable",
                     "Le worker du labo bat, mais JARVIS ne trouve pas sa base : JARVIS_LAB_DB ne désigne pas le même "
                     "fichier dans les deux conteneurs ?", diag_steps(), {"base": lab.get("chemin")})]
    return []


def r_lab_v1(ctx: dict, now: datetime) -> list[dict]:
    lab = ctx["lab"]
    if isinstance(lab, Exception):
        raise lab
    g = lab.get("garde_v1")
    if not g or g.get("etat") in (None, "ok"):
        return []
    if g.get("etat") == "change":
        title = "Labo 1 en pause : le comportement v1 a changé"
        why = (f"L'empreinte du code v1 qui tourne ({g.get('calculee')}) n'est pas celle attendue "
               f"({g.get('attendue')}).")
    else:
        title = "Labo 1 en pause : empreinte v1 incalculable"
        why = f"Le calcul de l'empreinte v1 échoue ({g.get('erreur') or 'erreur inconnue'})."
    detail = (why + " Le rejeu et le direct du Labo 1 n'écrivent plus rien (aucune ligne d'un comportement non "
                    "vérifié) ; le Labo 2, le rattrapage et le grade continuent. La garde est retentée à chaque passage.")
    steps = [say("Rien à faire si c'est passager : la garde se relance seule à chaque passage du labo."),
             say("Si ça dure, c'est un changement de code à trancher (revenir en arrière, ou ouvrir une nouvelle "
                 "version de méthode avec sa nouvelle empreinte) : envoie le diagnostic à Claude dans le projet JARVIS.")]
    return [item("lab:v1_pause", "probleme", "commun", title, detail, steps + [go("Ouvrir Labo", "labo")],
                 {"état": g.get("etat"), "empreinte attendue": g.get("attendue"), "empreinte calculée": g.get("calculee"),
                  "erreur": g.get("erreur"), "vérifié le": g.get("le")})]


EVENT_TITLES = {"bascule": "Méthode {v} en place (promotion que tu as approuvée)",
                "retrait": "Méthode {v} retirée par le labo", "council": "Une règle part en essai (décision du council)",
                "veto": "Ton veto a été appliqué", "labo1_reprise": "Labo 1 repris"}
COUNCIL_BASCULE = "Le council a mis en place la méthode {v}"
DECISIONS = ("bascule", "council", "retrait", "veto")  # ce sur quoi porte `!veto` (method.apply_veto)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("**", "").replace("`", "")).strip()


def r_council(ctx: dict, now: datetime) -> list[dict]:
    lab = ctx["lab"]
    if isinstance(lab, Exception):
        raise lab
    events = [e for e in lab.get("evenements") or [] if e.get("genre") in EVENT_TITLES]
    # Le veto annule la dernière décision du council : bouton seulement si la décision la plus récente sur la méthode
    # (promotion, mise en essai, retrait, veto) est une décision du council
    last = max((e for e in events if e["genre"] in DECISIONS), key=lambda e: e["id"], default=None)
    target = last["id"] if last and last.get("par") == COUNCIL else None
    out = []
    for e in events:
        vetoable = e["id"] == target
        steps = ([say("C'est sa dernière décision : un veto l'annule au prochain passage du labo (une méthode promue "
                      "par le council redevient l'ancienne, sinon le challenger en essai est retiré)."),
                  act("Mettre mon veto", "veto")] if vetoable else
                 [say("Rien à faire de ton côté.")])
        council_bascule = e["genre"] == "bascule" and e.get("par") == COUNCIL
        title = COUNCIL_BASCULE if council_bascule else EVENT_TITLES[e["genre"]]
        out.append(item(f"council:decision:{e['id']}", "info", "commun",
                        title.format(v=e.get("version") or "?"), _clean(e.get("texte") or ""),
                        steps + [go("Ouvrir Labo", "labo")],
                        {"événement": e["id"], "genre": e["genre"], "version": e.get("version"),
                         "décidé par": e.get("par"), "le": e.get("le")}))
    return out


LOOP_SPACE = {API.TRACKING_LOOP: "trades", "journal des recommandations": "paper"}
LOOP_IMPACT = {
    API.TRACKING_LOOP: "Tes TP et stops ne sont plus vérifiés ni annoncés sur Discord, et le message épinglé n'est "
                       "plus réécrit.",
    "journal des recommandations": "Le Papier (si j'avais suivi JARVIS) n'avance plus.",
    "scanner pré-ouverture": "Le scan de 9 h 10 (New York) et ses GO ne partent pas.",
    "contrôle des GO après l'ouverture": "Les GO ne sont plus confirmés ni annulés après l'ouverture.",
    "conseil avant la clôture": "Le conseil sortir / alléger / garder avant la clôture ne part pas.",
    "forward-test sur le compte fictif": "Le forward-test du labo sur le compte fictif n'avance plus.",
    "file d'envoi Discord": "Les messages Discord restent en file.",
    "labo : message du matin": "Le message du matin du labo et ses annonces ne partent pas.",
    "veille des routines Claude": "La veille des routines Claude ne prévient plus sur Discord.",
}


def expected_loops() -> list[tuple[str, int]]:
    loops = [("labo : message du matin", scheduler.MINUTE)]
    loops += [(n, every) for n, _, every, _ in scheduler.CORE_JOBS]
    if scheduler.enabled():
        loops += [(n, every) for n, _, every, _ in scheduler.JOBS]
    return [(n, e) for n, e in loops if n != scheduler.ATTENTION_LOOP]


def _tracking_matters(now: datetime) -> bool:
    opened = scheduler.book.open_trades()
    return any(t.get("market") == "crypto" for t in opened) or (
        CAL.session_open(now) and any(t.get("market", "stock") == "stock" for t in opened))


def r_loops(ctx: dict, now: datetime) -> list[dict] | None:
    """Une boucle est bloquée quand elle n'a ni fini de passage depuis max(180 s, 3 × sa période), ni de passage en
    cours depuis moins de 15 min : un passage long (scanner du matin) n'est pas un blocage."""
    if _uptime(ctx) < GRACE_S:
        return None
    out = []
    for name, every in expected_loops():
        run = scheduler.last_runs.get(name)
        limit = max(API.LOOP_STALE_S, 3 * every)
        space = LOOP_SPACE.get(name, "commun")
        impact = LOOP_IMPACT.get(name, "")
        busy = _parse(scheduler.running.get(name))
        busy_s = (now - busy).total_seconds() if busy else None
        if busy_s is not None and busy_s < LOOP_BUSY_MAX_S:
            continue
        ended = _parse(run["at"]) + timedelta(seconds=run.get("duree_s") or 0) if run else None
        idle_s = (now - ended).total_seconds() if ended else _uptime(ctx)  # sans passage : depuis le démarrage
        if busy_s is None and idle_s <= limit:  # passage fini récemment (ou premier passage pas encore fini)
            if run and not run.get("ok") and run.get("echecs_suite", 0) >= LOOP_MIN_FAILS:
                out.append(item(
                    f"loop:failing:{slug(name)}", "probleme", space, f"La boucle « {name} » échoue à chaque passage",
                    f"{run['echecs_suite']} passages d'affilée en échec ({run.get('erreur') or 'erreur inconnue'}). "
                    f"JARVIS continue et la relance tout seul, mais c'est à corriger. {impact}".strip(),
                    diag_steps(), {"boucle": name, "échecs d'affilée": run["echecs_suite"],
                                   "erreur": run.get("erreur"), "dernier passage": run["at"]}))
            continue
        kind = "action" if name == API.TRACKING_LOOP and _tracking_matters(now) else "probleme"
        if busy_s is not None:
            since = f"un passage dure depuis {_ago(busy_s)} : la boucle est sans doute bloquée"
        elif run is None:
            since = f"aucun passage terminé depuis le démarrage de JARVIS il y a {_ago(_uptime(ctx))}"
        else:
            since = f"aucun passage depuis {_ago(idle_s)}"
        out.append(item(
            f"loop:stuck:{slug(name)}", kind, space, f"La boucle « {name} » ne tourne plus",
            f"{since[0].upper()}{since[1:]}. {impact}".strip(),
            [say("Rien à faire si elle repart seule au passage suivant.")] + ssh_steps()
            + [cmd("Sinon, redémarre JARVIS (le suivi reprend où il en était ; la page se recharge seule) :",
                   "docker compose restart jarvis")] + diag_steps(),
            {"boucle": name, "dernier passage": run["at"] if run else None, "durée (s)": (run or {}).get("duree_s"),
             "passage en cours depuis": scheduler.running.get(name), "période (s)": every, "seuil (s)": limit}))
    return out


EXCLUDED_LOGGERS = ("app.scheduler", "app.web.attention")  # boucles et bot : leurs propres constats


def r_errors(ctx: dict, now: datetime) -> list[dict]:
    since = now - timedelta(seconds=ERRORS_WINDOW_S)
    groups: dict[str, list[dict]] = {}
    for e in list(API.recent_errors.items):
        at = _parse(e.get("at"))
        if e.get("niveau") not in ("ERROR", "CRITICAL") or at is None or at < since:
            continue
        if any(e.get("module", "").startswith(x) for x in EXCLUDED_LOGGERS):
            continue
        groups.setdefault(e.get("module") or "inconnu", []).append(e)
    out = []
    for module, rows in sorted(groups.items()):
        if len(rows) < ERRORS_MIN:
            continue
        last = rows[-1]
        out.append(item(f"errors:{slug(module)}", "probleme", "commun", f"Erreurs répétées dans « {module} »",
                        f"{len(rows)} erreurs en {ERRORS_WINDOW_S // 60} min ; la dernière à "
                        f"{_paris(_parse(last['at']), now)} : {last.get('message')}. JARVIS continue, mais c'est à "
                        "corriger.", diag_steps(),
                        {"module": module, "erreurs": len(rows), "dernière": last.get("at"),
                         "message": last.get("message")}))
    return out


def r_disk(ctx: dict, now: datetime) -> list[dict]:
    import shutil

    folder = config.DATA_DIR if Path(config.DATA_DIR).exists() else "."
    total, used, free = shutil.disk_usage(folder)
    pct = used / total * 100 if total else 0.0
    if pct < config.DISK_ALERT_PCT:
        return []
    critical = pct >= alerts.DISK_CRITICAL_PCT
    files = ", ".join(f"{n} {s / 1e9:.1f} Go" for n, s in alerts._biggest_files(Path(folder)))
    steps = ssh_steps() + [
        cmd("Libère les images et le cache de construction Docker inutiles (sans risque) :",
            "docker image prune -a -f && docker builder prune -f"),
        cmd("Vérifie la place libre :", "df -h /"),
        say("Ne supprime jamais le dossier data/ ni lab.db, et jamais « docker compose down -v » : ce sont les données "
            "de JARVIS et du labo."),
    ] + diag_steps()
    return [item("disk", "action" if critical else "probleme", "commun",
                 f"Disque du serveur {'presque plein' if critical else 'bien rempli'} ({pct:.0f} %)",
                 f"{used / 1e9:.1f} Go occupés sur {total / 1e9:.1f} Go. Au-delà, la base du labo et les fichiers de "
                 "JARVIS risquent de ne plus s'écrire." + (f" Plus gros fichiers de data/ : {files}." if files else ""),
                 steps, {"occupé (%)": round(pct, 1), "libre (Go)": round(free / 1e9, 1),
                         "seuil (%)": config.DISK_ALERT_PCT, "critique (%)": alerts.DISK_CRITICAL_PCT})]


# --- Règles : sources et accès ----------------------------------------------------------------------------

SOURCE_ROLES = {
    "Twelve Data": ("bougies des actions en temps réel", "le suivi de tes trades actions, la confirmation des GO et "
                    "les plans passent sur Alpaca (16 min de retard) quand c'est possible, sinon ils s'arrêtent"),
    "Kraken": ("bougies de la crypto", "le suivi des trades crypto et les plans crypto s'arrêtent"),
    "Alpaca (données)": ("scanner pré-ouverture, Killmode et secours des bougies actions",
                         "le scan du matin, le Killmode et le secours de Twelve Data ne marchent plus"),
    "Alpaca (compte fictif)": ("forward-test du labo sur le compte fictif (aucun argent réel)",
                               "le forward-test n'avance plus"),
    "Finnhub": ("catalyseurs des plans (actus, résultats), agenda des résultats, fiche société",
                "la note catalyseur des plans et l'agenda des résultats sont incomplets"),
    "SEC EDGAR": ("dépôts 8-K (agenda, fiche société)", "les 8-K de l'agenda ne sont plus vérifiés"),
    "Frankfurter (BCE)": ("change EUR/USD des tailles en euros", "les tailles en euros prennent le dernier change connu"),
}
KEYED = {"Twelve Data": "TWELVEDATA_API_KEY", "Finnhub": "FINNHUB_API_KEY",
         "Alpaca (données)": "ALPACA_API_KEY et ALPACA_SECRET_KEY",
         "Alpaca (compte fictif)": "ALPACA_API_KEY et ALPACA_SECRET_KEY"}
RENEW_WHERE = {
    "TWELVEDATA_API_KEY": "sur twelvedata.com (connecté, page « API Keys »)",
    "FINNHUB_API_KEY": "sur finnhub.io (connecté, « Dashboard »)",
    "ALPACA_API_KEY et ALPACA_SECRET_KEY": "sur app.alpaca.markets, dans le compte **Paper** (« API Keys » : les clés du "
                                           "compte fictif, jamais celles d'un compte réel)",
}


def _down(s: dict, now: datetime) -> bool:
    first, last = _parse(s.get("premier_echec")), _parse(s.get("derniere_tentative"))
    return (s.get("statut") == "erreur" and s.get("echecs_suite", 0) >= SOURCE_MIN_FAILS and first is not None
            and (now - first).total_seconds() >= SOURCE_DOWN_S and last is not None
            and (now - last).total_seconds() <= SOURCE_IDLE_S)


def _source_evidence(name: str, s: dict) -> dict:
    return {"source": name, "dernière lecture réussie": s.get("dernier_succes"), "premier échec": s.get("premier_echec"),
            "échecs d'affilée": s.get("echecs_suite"), "dernière erreur": s.get("erreur"), "code": s.get("code"),
            "dernière tentative": s.get("derniere_tentative")}


def r_sources(ctx: dict, now: datetime) -> list[dict] | None:
    if _uptime(ctx) < GRACE_S:
        return None
    snap = sources_health.snapshot()
    down, auth = {}, {}
    for name, s in snap.items():
        if s.get("statut") != "erreur":
            continue
        if name in KEYED and s.get("code") == 401 and s.get("echecs_suite", 0) >= AUTH_MIN_FAILS:
            auth.setdefault(KEYED[name], []).append((name, s))
        elif not (name == "Twelve Data" and s.get("code") == 429) and _down(s, now):
            down[name] = s
    out = []
    for key_name, hits in auth.items():
        names = ", ".join(n for n, _ in hits)
        s = hits[0][1]
        out.append(item(f"auth:{slug(key_name)}", "action", "commun", f"Accès refusé par {names}",
                        f"{names} refuse la clé ({s.get('erreur')}) depuis {_paris(_parse(s.get('premier_echec')), now)}. "
                        f"Renouvelle {key_name} {RENEW_WHERE[key_name]}.", env_steps(*key_name.split(" et ")),
                        _source_evidence(names, s) | {"clé": key_name}))
    if len(down) >= INTERNET_MIN:
        names = sorted(down)
        out.append(item("internet", "probleme", "commun", "Plus aucune source ne répond",
                        f"{len(down)} sources en panne en même temps ({', '.join(names)}) : le réseau du serveur est "
                        "coupé, ou un service commun (DNS) ne répond plus.",
                        [say("Rien à faire si ça revient seul.")] + ssh_steps()
                        + [cmd("Vérifie que les conteneurs tournent :", "docker compose ps")] + diag_steps(),
                        {"sources": names, "premier échec": min(down[n].get("premier_echec") or "" for n in names)}))
        return out
    for name, s in sorted(down.items()):
        role, impact = SOURCE_ROLES.get(name, ("source de données de JARVIS", "les vues qui l'utilisent sont "
                                                                                "incomplètes"))
        out.append(item(f"source:{slug(name)}", "probleme", "commun", f"{name} ne répond plus",
                        f"Rôle : {role}. Sans elle, {impact}. Dernière lecture réussie : "
                        f"{_paris(_parse(s.get('dernier_succes')), now)} ; dernière erreur : {s.get('erreur')}.",
                        [say("Rien à faire si ça revient seul.")] + diag_steps(), _source_evidence(name, s)))
    return out


def r_quota(ctx: dict, now: datetime) -> list[dict]:
    c = twelvedata.credits_today(now)
    spent, per_day = c["depenses"], c["par_jour"] or 0
    pct = spent / per_day * 100 if per_day else 0.0
    s = sources_health.snapshot().get("Twelve Data") or {}
    refused = s.get("statut") == "erreur" and s.get("code") == 429 and s.get("echecs_suite", 0) >= SOURCE_MIN_FAILS
    if pct < QUOTA_INFO_PCT and not refused:
        return []
    ny = now.astimezone(NEW_YORK)
    reset = datetime.combine(ny.date() + timedelta(days=1), datetime.min.time(), NEW_YORK)
    detail = (f"{spent} crédits dépensés sur {per_day} aujourd'hui (jour de New York ; ce processus seulement, le "
              f"worker du labo compte à part). Remise à zéro à minuit à New York, {_paris(reset)} à Paris. ")
    detail += ("Twelve Data refuse les appels (crédits épuisés) : les bougies des actions passent sur Alpaca, avec "
               "16 min de retard." if refused or pct >= 100 else
               "Au-delà, les bougies des actions passeront sur Alpaca, avec 16 min de retard.")
    kind = "probleme" if refused or pct >= 100 else "info"
    steps = [say("Rien à faire de ton côté : JARVIS bascule tout seul, et revient sur Twelve Data à la remise à zéro.")]
    return [item("quota:twelvedata", kind, "commun", f"Crédits Twelve Data du jour : {pct:.0f} %", detail, steps,
                 {"dépensés": spent, "par jour": per_day, "par minute": c.get("par_minute"),
                  "refus (429)": refused, "jour de New York": c.get("jour_new_york")})]


KEYS = [
    ("TWELVEDATA_API_KEY", "action", "le suivi des actions passe sur Alpaca (16 min de retard) et la confirmation des "
                                     "GO perd le temps réel",
     "sur twelvedata.com (offre gratuite)"),
    ("FINNHUB_API_KEY", "action", "plus de catalyseur dans les plans, ni d'agenda des résultats",
     "sur finnhub.io (offre gratuite)"),
    ("ALPACA_API_KEY", "action", "plus de scanner du matin, de Killmode ni de forward-test",
     "sur app.alpaca.markets, compte Paper"),
    ("JARVIS_LAB_API_KEY", "action", "les routines Claude (équipe du soir, recherche avant l'ouverture) ne peuvent ni "
                                     "lire le labo ni déposer leurs avis",
     "c'est la même clé que celle de l'environnement des routines Claude"),
    ("SEC_USER_AGENT", "info", "plus de 8-K dans l'agenda ni dans la fiche société, ni de collecte 8-K au labo",
     "c'est ton nom et ton e-mail, exigés par la SEC (ex. « Prénom Nom adresse@exemple.fr »)"),
]


def r_keys(ctx: dict, now: datetime) -> list[dict]:
    out = []
    for name, kind, impact, where in KEYS:
        present = bool(config.secret(name)) and (name != "ALPACA_API_KEY" or bool(config.secret("ALPACA_SECRET_KEY")))
        if present:
            continue
        label = "ALPACA_API_KEY et ALPACA_SECRET_KEY" if name == "ALPACA_API_KEY" else name
        out.append(item(f"key:missing:{slug(name)}", kind, "commun",
                        f"{label} {'absentes' if name == 'ALPACA_API_KEY' else 'absente'}",
                        f"Sans {'elles' if ' et ' in label else 'elle'}, {impact}. Où l'obtenir : {where}.",
                        env_steps(*label.split(" et ")), {"variable": label}))
    return out


def r_discord(ctx: dict, now: datetime) -> list[dict] | None:
    out = []
    if not scheduler.enabled():
        chosen = bool(config.secret("DISCORD_WEBHOOK_URL"))  # JARVIS_NOTIFICATIONS=off : ton choix
        why = "JARVIS_NOTIFICATIONS=off" if chosen else "DISCORD_WEBHOOK_URL absente"
        out.append(item("notifications:off", "info", "commun", "Alertes du salon Discord coupées",
                        f"{why} : le point du matin, les alertes de cours, d'actus, de disque et la veille des routines "
                        "ne partent pas sur le salon. Le suivi de tes trades tourne quand même, et ses alertes "
                        "partent en MP si le bot est actif.",
                        env_steps("JARVIS_NOTIFICATIONS" if chosen else "DISCORD_WEBHOOK_URL"), {"raison": why}))
    else:
        old = scheduler.outbox.oldest()
        created = _parse((old or {}).get("created_at"))
        if old and created and (now - created).total_seconds() >= OUTBOX_LATE_S:
            n = len(scheduler.outbox.items)
            err = old.get("derniere_erreur") or "inconnue"
            out.append(item("discord:outbox", "probleme", "commun", f"{_n(n, 'message', 'messages')} Discord en attente",
                            f"Le plus ancien attend depuis {_ago((now - created).total_seconds())} "
                            f"({old.get('attempts', 0)} tentatives ; dernière erreur : {err}). Tes alertes Discord ne "
                            "partent plus ; elles sont gardées et partiront au retour (un message est abandonné après environ 2 h "
                            "d'échecs).",
                            [say("Rien à faire si Discord revient seul.")] + diag_steps(),
                            {"en attente": n, "plus ancien": old.get("created_at"), "tentatives": old.get("attempts"),
                             "dernière erreur": err}))
        refused = scheduler.outbox.last_refusal
        if refused and refused.get("status") in (401, 403, 404) and refused["at"] > (scheduler.outbox.last_ok_at or ""):
            out.append(item("discord:webhook", "action", "commun", "Discord refuse le webhook du salon",
                            f"Dernier refus à {_paris(_parse(refused['at']), now)} ({refused.get('erreur')}) : le "
                            "webhook a été supprimé ou son adresse a changé. Les messages refusés ne sont pas gardés : "
                            "les alertes du salon sont perdues tant qu'il n'est pas remplacé. Nouveau webhook : "
                            "Paramètres du salon → Intégrations → Webhooks → Nouveau webhook → « Copier l'URL ».",
                            env_steps("DISCORD_WEBHOOK_URL"),
                            {"refus": refused["at"], "code": refused.get("status"), "erreur": refused.get("erreur"),
                             "dernier envoi réussi": scheduler.outbox.last_ok_at}))
    if not bot.enabled():
        out.append(item("discord:bot:off", "info", "commun", "Commandes Discord désactivées",
                        "DISCORD_BOT_TOKEN ou JARVIS_DISCORD_OWNER_ID absent : ni `!…`, ni MP de trades, ni boutons "
                        "des propositions (elles restent dans le cockpit).",
                        env_steps("DISCORD_BOT_TOKEN", "JARVIS_DISCORD_OWNER_ID")))
        return out
    if _uptime(ctx) < GRACE_S:
        return None
    st = bot.status
    since = _parse(st.get("depuis")) or STARTED
    if st.get("connecte") or (now - since).total_seconds() < BOT_DOWN_S:
        return out
    ev = {"connecté": False, "depuis": st.get("depuis"), "dernière erreur": st.get("derniere_erreur"),
          "erreur le": st.get("erreur_le")}
    if st.get("erreur_type") == "LoginFailure":
        out.append(item("auth:discord", "action", "commun", "Discord refuse le jeton du bot",
                        "Le bot ne peut plus se connecter : jeton refusé (réinitialisé ou révoqué). Ni `!…`, ni MP de "
                        "trades, ni boutons des propositions. Nouveau jeton : portail développeur Discord → "
                        "Applications → ton bot → Bot → « Reset Token ».", env_steps("DISCORD_BOT_TOKEN"), ev))
    elif st.get("erreur_type") == "PrivilegedIntentsRequired":
        out.append(item("discord:intent", "action", "commun", "Discord bloque la lecture des messages du bot",
                        "L'intention « Message Content » du bot est désactivée : il ne lit plus tes commandes. Portail "
                        "développeur Discord → Applications → ton bot → Bot → active « Message Content Intent ».",
                        [say("JARVIS se reconnecte tout seul chaque minute : rien à relancer.")], ev))
    else:
        out.append(item("discord:bot", "probleme", "commun", "Le bot Discord est déconnecté",
                        f"Hors ligne depuis {_ago((now - since).total_seconds())} : ni `!…`, ni MP de trades, ni boutons "
                        f"des propositions. Dernière erreur : {st.get('derniere_erreur') or 'aucune'}. JARVIS retente "
                        "chaque minute.", [say("Rien à faire si Discord revient seul.")] + diag_steps(), ev))
    return out


# --- Règles : routines Claude et déploiement --------------------------------------------------------------

ROUTINE_STEPS = [
    say("Ouvre son fil dans le projet JARVIS (appli Claude) : s'il est marqué résolu, rouvre-le. Une routine dont le "
        "fil est résolu ne tourne plus."),
    say("Ne la lance pas à la main : une session lancée à la main n'a ni le dossier partagé ni le dépôt (« consignes "
        "introuvables »)."),
    say("Ce constat se ferme tout seul au prochain dépôt."),
]


def r_routines(ctx: dict, now: datetime) -> list[dict]:
    seen = scheduler.state.data.get("routines") or {}
    out = []
    # Sans repère (mise en service), la veille des routines le pose à son premier passage : pas d'avis d'ici là
    hit = RW.soir_alert(now, seen.get("soir"), None) if "soir" in seen else None
    if hit:
        want = hit[0].removeprefix("soir:")
        out.append(item("routine:soir", "action", "commun", "L'équipe du soir n'a rien déposé",
                        f"Aucun avis du council reçu pour la soirée du {_day(want)} (dernier dépôt vu : "
                        f"{_day(seen.get('soir'))}). Le labo n'a pas eu sa relecture du soir.",
                        [say("Routine « Équipe du soir du labo » (lun-ven, 22 h 52 Paris).")] + ROUTINE_STEPS,
                        {"soirée attendue": want, "dernier dépôt vu": seen.get("soir"),
                         "vérifié à partir de": f"{RW.SOIR_LIMITE_PARIS:%H:%M} (Paris)"}))
    view = research.scan_view(research.last_scan(), now)
    scan = view.get("scan") or {}
    picks = len(scan.get("picks") or [])
    verdicts = len(research.ResearchBook().day(view["jour"]))
    hit = RW.recherche_alert(now, scan.get("jour"), picks, verdicts, None)
    if hit:
        out.append(item("routine:recherche", "action", "commun", "La recherche avant l'ouverture n'a rien rendu",
                        f"Aucun verdict sur les {picks} valeurs du scan d'aujourd'hui.",
                        [say("Routine « Recherche avant l'ouverture » (lun-ven, 9 h 12 New York). Elle part parfois "
                             "en retard : si son fil montre qu'elle a démarré, attends son dépôt.")] + ROUTINE_STEPS,
                        {"jour": view["jour"], "valeurs du scan": picks, "verdicts": verdicts,
                         "vérifié à partir de": f"{RW.RECHERCHE_LIMITE_NY:%H:%M} (New York)"}))
    return out


def _day(iso_day: str | None) -> str:
    if not iso_day:
        return "jamais"
    try:
        return f"{datetime.fromisoformat(iso_day):%d/%m}"
    except ValueError:
        return iso_day


_rebuild = {"since": None}  # premier passage où le code du disque diffère de celui qui tourne


def r_deploy(ctx: dict, now: datetime) -> list[dict] | None:
    v = version.snapshot()
    disk = v["sur_le_disque"]
    if v["code"] != "different":
        _rebuild["since"] = None
    ev = {"commit du disque": (disk or "")[:7] or None, "branche": v["branche"], "code": v["code"],
          "empreinte de l'image": v["empreinte_image"], "empreinte du disque": v["empreinte_disque"],
          "vérifié le": v["verifie_le"], "état GitHub": v["statut"]}
    if not disk or v["code"] is None:
        return [item("deploy:unknown", "info", "commun", "Version qui tourne inconnue",
                     f"JARVIS ne lit pas le dépôt du serveur ({v['erreur_disque'] or 'code illisible'}) : il ne peut "
                     "pas dire quelles PR fusionnées ne sont pas encore déployées. Le montage en lecture seule est dans "
                     "docker-compose.yml : il prend effet au prochain déploiement.",
                     [say("Rien d'autre à faire : déploie comme d'habitude.")], ev)]
    if v["code"] == "different":
        _rebuild["since"] = _rebuild["since"] or now
        if (now - _rebuild["since"]).total_seconds() < REBUILD_GRACE_S:
            return None  # sans doute une construction en cours (git pull && docker compose up -d --build)
        return [item("deploy:rebuild", "action", "commun", "Code récupéré mais pas reconstruit",
                     f"Le dossier du serveur est à {disk[:7]}, mais le code qui tourne n'est pas celui-là : un « git "
                     "pull » a été fait sans reconstruire l'image (un redémarrage ne suffit pas).",
                     ssh_steps() + [cmd("Reconstruis et relance :", "docker compose up -d --build")], ev)]
    if v["verifie_le"] is None or v["base_verifiee"] != disk:
        return None  # comparaison avec GitHub pas encore faite pour ce commit (au plus une minute)
    if v["erreur"]:
        return [item("deploy:unknown", "info", "commun", "Déploiements en attente : inconnu",
                     f"La comparaison avec GitHub échoue ({v['erreur']}). Elle est retentée toutes les 30 min.",
                     [say("Rien à faire de ton côté.")], ev | {"erreur": v["erreur"]})]
    prs, behind = v["pr_en_retard"], v["commits_en_retard"] or 0
    if v["statut"] in ("behind", "diverged"):
        return [item("deploy:ahead", "info", "commun", "JARVIS tourne sur une version absente de main",
                     f"Le commit qui tourne ({disk[:7]}, branche {v['branche'] or 'détachée'}) n'est pas dans "
                     f"l'historique de main (état GitHub : {v['statut']}) : une branche de test, ou main réécrite.",
                     [say("Rien à faire si c'est voulu.")], ev)]
    if not behind or not v["code_touche"]:
        return []  # à jour, ou seulement des docs et des tests : rien à reconstruire
    listed = (" ; ".join(f"#{p['numero']} {p['titre']}" for p in prs[:10])
              or _n(behind, "commit sans PR", "commits sans PR"))
    title = (_n(len(prs), "PR fusionnée pas encore déployée", "PR fusionnées pas encore déployées") if prs else
             _n(behind, "commit de main pas encore déployé", "commits de main pas encore déployés"))
    return [item("deploy:pending", "action", "commun", title,
                 f"{listed}. Si une PR précise une condition de déploiement (attendre la fin d'un rattrapage du labo, "
                 "par exemple), respecte-la : cette page ne la connaît pas.",
                 ssh_steps() + [cmd("Récupère le code et reconstruis (git pull te demande ton jeton GitHub en lecture "
                                    "seule) :", "git pull && docker compose up -d --build"),
                                say("Le constat se ferme au redémarrage sur la nouvelle version.")],
                 ev | {"commits en retard": behind, "PR": [f"#{p['numero']}" for p in prs]})]


# --- Règles : Mes trades -------------------------------------------------------------------------------

def _exit_name(kind: str) -> str:
    return "au stop" if kind == "stop" else f"à {kind.upper()}"


def r_undeclared(ctx: dict, now: datetime) -> list[dict]:
    """Trade de Mes trades fermé par le suivi (stop, objectif) sans `!sortie` : le journal compte un prix supposé."""
    out = []
    since = now - timedelta(days=UNDECLARED_DAYS)
    for t in scheduler.book.trades:
        if t.get("status") != "closed" or not t.get("events"):
            continue
        last = t["events"][-1]
        kind = last.get("kind") or ""
        if not (kind == "stop" or kind.startswith("tp")) or last.get("corrige"):
            continue
        at = _parse(last.get("at"))
        if at is None or at < since:
            continue
        sym = t["symbol"]
        several = t.get("sortie_unique") is None and kind != "stop" or (kind == "stop" and t.get("targets_hit"))
        detail = (f"Le suivi de JARVIS a fermé {sym} {_exit_name(kind)} le {_paris(at)} (prix supposé "
                  f"{_price(last.get('price'))}). Ton vrai prix d'exécution chez IBKR n'est pas connu : ton journal, "
                  "tes résultats et le grade comptent ce prix supposé.")
        if several:
            detail += " Si tu as vendu en plusieurs fois, donne le prix de la dernière sortie."
        steps = [say("Regarde ton prix d'exécution dans l'application IBKR."),
                 cmd("Déclare-le sur Discord (remplace le prix par le tien s'il diffère ; même prix : déclare-le quand "
                     "même, le constat se ferme) :", f"!sortie {sym} {last.get('price')}"),
                 go("Ouvrir le journal", "journal")]
        out.append(item(f"exit:undeclared:{t['id']}", "action", "trades", f"Sortie à déclarer : {sym}", detail, steps,
                        {"trade": t["id"], "sortie": kind, "prix du suivi": last.get("price"), "fermé le": last.get("at"),
                         "délai de déclaration (j)": UNDECLARED_DAYS}))
    return out


def r_kill_overnight(ctx: dict, now: datetime) -> list[dict]:
    out = []
    for t in scheduler.book.open_trades():
        if not t.get("killmode") or t.get("market", "stock") != "stock":
            continue
        created = _parse(t.get("created_at"))
        bounds = CAL.session_bounds(created.astimezone(NEW_YORK).date()) if created else None
        if not bounds or (now - bounds[1]).total_seconds() < KILL_GRACE_S:
            continue
        sym = t["symbol"]
        out.append(item(f"kill:open_after_close:{t['id']}", "action", "trades",
                        f"KILL encore ouvert après la clôture : {sym}",
                        f"Séance close depuis {_ago((now - bounds[1]).total_seconds())}. Un KILL ne passe pas la nuit : "
                        "c'est la règle que mesure son journal (vendu à la dernière minute de la séance). Le suivi le "
                        "garde ouvert tant que tu n'as pas déclaré ta vente.",
                        [say("Vérifie dans IBKR que la position est bien vendue."),
                         cmd("Déclare ta vente (remplace le prix par le tien) :",
                             f"!sortie {sym} {t.get('last_price')}"),
                         go("Ouvrir le trade", "positions", sym)],
                        {"trade": t["id"], "pris le": t.get("created_at"), "clôture de la séance": _iso(bounds[1]),
                         "dernier prix": t.get("last_price")}))
    return out


SIGNAL_FR = {"divergence": "divergence RSI", "weis": "anomalie corps/volume", "low_volume": "avance sans volume",
             "reversal_candle": "bougie de retournement avec du volume"}


def r_positions(ctx: dict, now: datetime) -> list[dict] | None:
    """Conseil de sortie du jour, signes de sortie, cotation périmée, objectif en retard (Mes trades)."""
    positions = API.open_positions("trades", now)
    today = now.astimezone(NEW_YORK).date().isoformat()
    by_id = {t["id"]: t for t in scheduler.book.open_trades()}
    out, stale = [], []
    bounds = CAL.session_bounds(now.astimezone(NEW_YORK).date())
    opening = bool(bounds) and 0 <= (now - bounds[0]).total_seconds() < QUOTE_OPEN_GRACE_S
    for p in positions:
        t = by_id.get(p["id"]) or {}
        sym = p["symbol"]
        c = (t.get("conseils") or [None])[-1]
        if c and c.get("day") == today and c.get("decision") in ("SORTIR", "ALLÉGER"):
            out.append(item(f"advice:{p['id']}", "action", "trades", f"JARVIS conseille de {c['decision'].lower()} {sym}",
                            f"Conseil de {_paris(_parse(c.get('at')), now)} vers {_price(c.get('price'))} : "
                            f"{_clean(c.get('why') or '')}. C'est à toi de décider : tu passes tes ordres chez IBKR.",
                            [cmd("Les ordres en prix (ventes, stop) :", "!positions"),
                             cmd("Une fois vendu, déclare ton prix :", f"!sortie {sym} {c.get('price')}"),
                             go("Ouvrir le trade", "positions", sym)],
                            {"trade": p["id"], "décision": c["decision"], "prix": c.get("price"), "le": c.get("at"),
                             "R à la sortie": c.get("exit_r")}))
        if p.get("exit_signals"):
            names = ", ".join(SIGNAL_FR.get(k, k) for k in p["exit_signals"])
            out.append(item(f"exit:signals:{p['id']}", "action", "trades", f"Signes de sortie sur {sym}",
                            f"Sur ce trade gagnant, JARVIS voit : {names}. EmperorBTC : on prend ses bénéfices sur un "
                            "retournement. C'est à toi de décider : tu passes tes ordres.",
                            [cmd("Pour un conseil en prix (vendre, alléger, remonter le stop) :", "!positions"),
                             go("Ouvrir le trade", "positions", sym)],
                            {"trade": p["id"], "signes": list(p["exit_signals"]), "dernier prix": p.get("last_price")}))
        fresh = p.get("freshness") or {}
        limit = QUOTE_STALE_FACTOR * API.STALE_S.get(p.get("market", "stock"), 240)
        if p.get("market", "stock") == "stock" and opening:
            pass  # première bougie de la séance pas encore lue
        elif fresh.get("stale") and (fresh.get("age_s") is None or fresh["age_s"] > limit):
            stale.append((sym, fresh.get("age_s"), p.get("price_source")))
        late = next((x for x in p.get("targets") or [] if x.get("late")), None)
        if late:
            out.append(item(f"tp:late:{p['id']}:{late['name'].lower()}", "info", "trades",
                            f"{sym} : {late['name']} en retard sur son délai estimé",
                            f"{late['name']} ({_price(late.get('price'))}) était prévu {late.get('eta_text') or ''} "
                            f"(au plus tard {_paris(_parse(late.get('eta_latest')), now)}), pas encore atteint. "
                            "La règle : pas de sortie au temps ; le délai est un ordre de grandeur, pas une échéance.",
                            [say("Rien à faire de ton côté : le stop et l'objectif restent en place.")],
                            {"trade": p["id"], "objectif": late["name"], "au plus tard": late.get("eta_latest")}))
    if stale:
        if _uptime(ctx) < GRACE_S:
            return None  # cache des bougies encore vide au démarrage
        out.append(item("quote:stale:trades", "probleme", "trades",
                        f"Cotation périmée : {', '.join(s for s, _, _ in stale)}",
                        ("Ses objectifs et son stop ne sont" if len(stale) == 1 else
                         "Leurs objectifs et leurs stops ne sont")
                        + " plus vérifiés tant que la cotation ne revient pas ("
                        + " ; ".join(f"{s} : dernière il y a {_ago(a)}" for s, a, _ in stale) + ").",
                        [say("Rien à faire si la source revient seule (voir les constats de source).")] + diag_steps(),
                        {"valeurs": [s for s, _, _ in stale], "âges (s)": [a for _, a, _ in stale],
                         "sources": [src for _, _, src in stale]}))
    return out


def r_risk(ctx: dict, now: datetime) -> list[dict]:
    from app.trading import confirm

    rk = API.risk(now)
    out = []
    day, limit = rk.get("r_du_jour_net"), rk.get("arret_jour_r")
    if day is not None and limit is not None and day <= limit:
        mp = ("les MP de GO confirmés gardent la taille de ta règle (JARVIS_MP_TAILLE_APRES_ARRET)"
              if confirm.SIZE_AFTER_STOP else "les MP de GO confirmés partent sans taille jusqu'à demain")
        out.append(item("risk:day_stop", "info", "trades", f"Arrêt du jour atteint ({day:+g} R)",
                        f"R net du jour sur tes trades : {day:+g} R (arrêt à {limit:g} R, jour de Paris). JARVIS ne "
                        f"propose plus de risque aujourd'hui ; {mp}.",
                        [say("Rien à faire de ton côté : tout repart demain.")],
                        {"R net du jour": day, "arrêt (R)": limit}))
    opened, cap = rk.get("risque_ouvert_pct"), rk.get("risque_ouvert_max_pct")
    if opened is not None and cap is not None and opened > cap:
        out.append(item("risk:open", "info", "trades", f"Risque ouvert au-delà du budget ({opened:g} %)",
                        f"Risque encore en jeu sur tes trades ouverts : {opened:g} % du capital déclaré (budget "
                        f"{cap:g} %). Les prochains plans proposent 0 % tant qu'il ne redescend pas.",
                        [say("Rien à faire de ton côté : c'est ton choix, gardé tel quel.")],
                        {"risque ouvert (%)": opened, "budget (%)": cap}))
    return out


# --- Règles : labo, grade, propositions --------------------------------------------------------------------

def r_grade(ctx: dict, now: datetime) -> list[dict]:
    rk = API.risk(now)
    age, limit = rk.get("grade_age_h"), rk.get("grade_max_age_h")
    if API.lab_heartbeat(now)["stale"]:
        return []  # lab:down le dit déjà, avec la cause
    if age is None:
        return [item("grade:stale", "info", "commun", "Grade pas encore calculé",
                     "Le labo n'a pas encore écrit de grade (lab_grade.json) : le risque proposé reste à 0 %.",
                     [say("Rien à faire de ton côté : il arrive au premier passage complet du labo.")],
                     {"âge (h)": None, "péremption (h)": limit})]
    if limit is not None and age > limit:
        return [item("grade:stale", "probleme", "commun", "Le grade n'est plus rafraîchi",
                     f"Dernier passage du grade il y a {age:.0f} h (périmé au-delà de {limit:g} h) : risque proposé à "
                     "0 % sur tous les plans. Le worker du labo bat, mais il n'écrit plus le grade.",
                     diag_steps() + [go("Ouvrir Risque", "risque")], {"âge (h)": round(age, 1), "péremption (h)": limit})]
    return []


def _proposal_title(p: dict) -> str:
    if p.get("kind") == "grade":
        return f"Proposition {p['id']} : grade {p.get('current')} → {p.get('value')}"
    if p.get("kind") == "labo":
        return f"Proposition {p['id']} : essayer la règle « {p.get('nom')} »"
    if p.get("kind") == "promotion":
        return f"Proposition {p['id']} : mettre en place la méthode {p.get('value')}"
    return f"Proposition {p['id']} : {p.get('key')} {p.get('current')} → {p.get('value')}"


def r_proposals(ctx: dict, now: datetime) -> list[dict]:
    pending = PR.proposals.pending()
    out = []
    for p in pending[:PROPOSALS_MAX]:
        detail = _clean(PR.text(p)) + ". Rien ne change tant que tu n'as pas décidé."
        if p.get("kind") == "grade":
            detail += " Une hausse du grade augmente le risque proposé par trade : c'est ta décision."
        out.append(item(f"proposal:{p['id']}", "action", "commun", _proposal_title(p), detail,
                        [act("Approuver", "proposal_approve", {"id": p["id"]}),
                         act("Refuser", "proposal_refuse", {"id": p["id"]}),
                         go("Ouvrir le Cockpit (carte Propositions)", "cockpit")],
                        {"proposition": p["id"], "genre": p.get("kind") or "réglage", "créée le": p.get("created_at")}))
    if len(pending) > PROPOSALS_MAX:
        rest = pending[PROPOSALS_MAX:]
        out.append(item("proposals:more", "action", "commun", f"{len(rest)} autres propositions en attente",
                        "Numéros " + ", ".join(str(p["id"]) for p in rest[:30]) + " : à décider dans la carte "
                        "Propositions du Cockpit, ou sur Discord (`!propositions`, puis `!oui N` / `!non N`).",
                        [go("Ouvrir le Cockpit (carte Propositions)", "cockpit")], {"en attente": len(pending)}))
    return out


RULES = [
    ("worker du labo", r_lab_worker), ("base du labo", r_lab_base), ("garde du Labo 1", r_lab_v1),
    ("décisions du council", r_council), ("boucles", r_loops), ("erreurs", r_errors), ("disque", r_disk),
    ("sources", r_sources), ("crédits Twelve Data", r_quota), ("clés", r_keys), ("Discord", r_discord),
    ("routines Claude", r_routines), ("déploiement", r_deploy), ("sorties à déclarer", r_undeclared),
    ("KILL après la clôture", r_kill_overnight), ("positions", r_positions), ("risque", r_risk),
    ("grade", r_grade), ("propositions", r_proposals),
]


# --- Lecture du labo (fil à part : la base fait 4,7 Go) ---------------------------------------------------

def lab_snapshot(now: datetime) -> dict:
    path = lab_db.db_path()
    conn = lab_db.open_readonly(path)
    if conn is None:
        return {"absente": True, "chemin": path}
    try:
        row = conn.execute("SELECT value FROM progress WHERE job = 'lab' AND key = 'garde_v1'").fetchone()
        garde = json.loads(row[0]) if row and row[0] else None
        since = int((now - timedelta(hours=COUNCIL_RECENT_H)).timestamp())
        try:
            events = [{"id": i, "le": c, "genre": k, "version": v, "texte": t} for i, c, k, v, t in conn.execute(
                "SELECT id, created_at, kind, version, text FROM lab_events WHERE ts >= ? ORDER BY id", (since,))]
        except sqlite3.OperationalError:  # base d'avant les événements
            events = []
        for e in events:  # qui a décidé : le council (veto possible) ou Simão (promotion approuvée)
            e["par"] = None
            if e["genre"] == COUNCIL:
                e["par"] = COUNCIL
            elif e["genre"] == "bascule":
                e["par"] = _promoted_by(conn, e["version"])
        return {"garde_v1": garde if isinstance(garde, dict) else None, "evenements": events, "chemin": path}
    except (sqlite3.Error, ValueError) as exc:
        return {"erreur": f"{type(exc).__name__}: {exc}"[:200], "chemin": path}
    finally:
        conn.close()


def _promoted_by(conn: sqlite3.Connection, version: str | None) -> str | None:
    try:
        row = conn.execute("SELECT settings FROM method_versions WHERE version = ?", (version,)).fetchone()
        return (json.loads(row[0]) or {}).get("promu_par") if row and row[0] else None
    except (sqlite3.Error, ValueError, AttributeError):
        return None


# --- Stockage et synchronisation ---------------------------------------------------------------------------

class AttentionBook:
    """data/attention.json : une ligne par occurrence d'un constat (historique gardé 30 jours)."""

    def __init__(self, path: str | None = PATH):
        self.path = Path(path) if path else None
        self.rows: list[dict] = []
        self.next_id = 1
        self.last_check_at: str | None = None
        self.load_error: str | None = None
        if self.path and self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.rows = [r for r in data["rows"] if isinstance(r, dict) and "id" in r]
                self.next_id = max([int(data.get("next_id") or 1)] + [r["id"] + 1 for r in self.rows])
                self.last_check_at = data.get("last_check_at")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.load_error = f"{type(exc).__name__}: {exc}"[:200]
                log.warning("Page À faire : %s illisible (%s), on repart de zéro", self.path, self.load_error)
                self.rows, self.next_id = [], 1
                try:  # gardé pour le diagnostic
                    os.replace(self.path, self.path.with_suffix(".illisible"))
                except OSError:
                    pass

    def save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": 1, "next_id": self.next_id, "last_check_at": self.last_check_at,
                                       "rows": self.rows}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("Page À faire : impossible d'écrire %s (%s)", self.path, exc)

    def open_rows(self) -> list[dict]:
        return [r for r in self.rows if r.get("resolved_at") is None]

    def sync(self, found: list[dict], now: datetime, untouched: set[str] = frozenset()) -> None:
        """Ouvre, met à jour et ferme les constats. `untouched` : règles sans avis ce passage (grâce, échec) dont les
        constats ouverts restent tels quels."""
        stamp = _iso(now)
        opened = {(r["space"], r["key"]): r for r in self.open_rows()}
        seen = set()
        for f in found:
            k = (f["space"], f["key"])
            if k in seen:
                log.warning("Page À faire : constat %s en double dans le même passage, le second est ignoré", k)
                continue
            seen.add(k)
            row = opened.get(k)
            if row is None:
                self.rows.append(f | {"id": self.next_id, "first_seen": stamp, "last_seen": stamp,
                                      "resolved_at": None, "ack_at": None})
                self.next_id += 1
            else:
                row.update({x: f[x] for x in ("kind", "title", "detail", "steps", "evidence", "rule")}, last_seen=stamp)
        for k, row in opened.items():
            if k not in seen and row.get("rule") not in untouched:
                row["resolved_at"] = stamp
        self.last_check_at = stamp
        self._purge(now)

    def _purge(self, now: datetime) -> None:
        limit = _iso(now - timedelta(days=KEEP_DAYS))
        self.rows = [r for r in self.rows if r.get("resolved_at") is None or r["resolved_at"] >= limit]
        if len(self.rows) > MAX_ROWS:
            resolved = sorted((r for r in self.rows if r.get("resolved_at")), key=lambda r: r["resolved_at"])
            drop = {id(r) for r in resolved[:len(self.rows) - MAX_ROWS]}
            self.rows = [r for r in self.rows if id(r) not in drop]

    def get(self, row_id: int) -> dict | None:
        return next((r for r in self.rows if r["id"] == row_id), None)

    def ack(self, row_id: int, on: bool, now: datetime) -> dict | None:
        """Range (`on`) ou réaffiche un constat ouvert ; None s'il est inconnu, réglé ou déjà dans cet état."""
        row = self.get(row_id)
        if row is None or row.get("resolved_at") is not None or bool(row.get("ack_at")) == on:
            return None
        row["ack_at"] = _iso(now) if on else None
        return row


book = AttentionBook()


def _row_view(r: dict, now: datetime, live: bool = False) -> dict:
    ack, last = _parse(r.get("ack_at")), _parse(r.get("last_seen"))
    still = bool(ack and r.get("resolved_at") is None and last and (last - ack).total_seconds() > ACK_STILL_S)
    return {k: r.get(k) for k in ("id", "key", "kind", "space", "title", "detail", "steps", "evidence", "first_seen",
                                  "last_seen", "resolved_at", "ack_at")} | {"live": live, "toujours_constate": still}


def _ordered(rows: list[dict]) -> list[dict]:
    """Calculés à la lecture, puis ouverts, rangés, réglés ; à rang égal : action, problème, info ; Mes trades, commun,
    Papier ; le plus récent d'abord."""
    rows = sorted(rows, key=lambda r: r.get("last_seen") or "", reverse=True)
    return sorted(rows, key=lambda r: (not r["live"], r.get("resolved_at") is not None, r.get("ack_at") is not None,
                                       KINDS.index(r["kind"]), SPACE_ORDER.get(r["space"], 9)))


def live_items(now: datetime) -> list[dict]:
    """Constats calculés à la lecture (jamais stockés ni rangeables)."""
    last = _parse(book.last_check_at)
    age = (now - last).total_seconds() if last else (now - STARTED).total_seconds()
    out = []
    if age > STALE_ENGINE_S:
        f = item("attention:stale", "probleme", "commun", "La page À faire n'est plus mise à jour",
                 f"Dernière vérification il y a {_ago(age)} : ce qu'elle montre peut être dépassé. Le planificateur de "
                 "JARVIS est peut-être bloqué.", ssh_steps() + [cmd("Redémarre JARVIS :", "docker compose restart jarvis")]
                 + diag_steps(), {"dernière vérification": book.last_check_at, "âge (s)": round(age)})
        out.append(f | {"id": None, "first_seen": book.last_check_at, "last_seen": _iso(now), "resolved_at": None,
                        "ack_at": None})
    if book.load_error and (now - STARTED).total_seconds() < 3600:
        f = item("attention:file", "probleme", "commun", "Historique de la page À faire illisible",
                 f"data/attention.json était illisible au démarrage ({book.load_error}) : la page est repartie vide "
                 "(copie gardée dans attention.illisible pour le diagnostic).", diag_steps(),
                 {"erreur": book.load_error})
        out.append(f | {"id": None, "first_seen": _iso(STARTED), "last_seen": _iso(now), "resolved_at": None,
                        "ack_at": None})
    return out


def _counts(rows: list[dict]) -> dict:
    zero = lambda: {k: 0 for k in KINDS}  # noqa: E731
    out = {"total": zero()} | {s: zero() for s in SPACES}
    for r in rows:
        if r.get("resolved_at") is None and not r.get("ack_at"):
            out["total"][r["kind"]] += 1
            out[r["space"]][r["kind"]] += 1
    return out


def view(space: str, now: datetime) -> dict:
    if space != "all" and space not in SPACES:
        raise HTTPException(400, "space doit valoir all, commun, trades ou paper")
    horizon = _iso(now - timedelta(hours=HISTORY_H))
    live = [_row_view(r, now, live=True) for r in live_items(now)]
    stored = [_row_view(r, now) for r in book.rows if r.get("resolved_at") is None or r["resolved_at"] >= horizon]
    every = live + stored
    counts = _counts(every)
    rows = _ordered([r for r in every if space == "all" or r["space"] == space])
    return {"generated_at": _iso(now), "space": space, "items": rows[:MAX_ITEMS], "counts": counts,
            "last_check_at": book.last_check_at, "tronque": len(rows) > MAX_ITEMS,
            "note": "Un constat se ferme tout seul quand sa cause disparaît ; « C'est fait » ou « Masquer » le range "
                    "seulement. Aucun bouton de cette page ne passe d'ordre."}


def summary(now: datetime) -> dict:
    """Pour /api/cockpit/state : compteurs (ouverts, non rangés, tous espaces) et les 3 constats les plus importants."""
    v = view("all", now)
    top = [r for r in v["items"] if r["resolved_at"] is None and not r["ack_at"]][:3]
    return {"counts": v["counts"], "top": [{k: r[k] for k in ("id", "key", "kind", "space", "title", "last_seen")}
                                           for r in top], "last_check_at": v["last_check_at"]}


async def run(now: datetime | None = None) -> list[dict]:
    """Un passage du moteur : toutes les règles, puis la synchronisation. Renvoie les constats trouvés."""
    now = now or datetime.now(timezone.utc)
    try:
        lab = await asyncio.to_thread(lab_snapshot, now)
    except Exception as exc:  # la base illisible ne casse que les règles du labo
        lab = exc
    ctx = {"lab": lab, "uptime_s": (now - STARTED).total_seconds()}
    found, untouched = [], set()
    for name, rule in RULES:
        try:
            out = rule(ctx, now)
        except Exception as exc:
            log.exception("Page À faire : la règle « %s » a échoué", name)
            untouched.add(name)
            f = item(f"rule:{slug(name)}", "probleme", "commun", f"Une vérification de la page À faire a échoué ({name})",
                     f"{type(exc).__name__}: {exc}"[:300] + ". Les autres vérifications continuent ; celle-ci garde ses "
                     "constats tels quels.", diag_steps(), {"règle": name, "erreur": type(exc).__name__})
            found.append(f | {"rule": "_règles"})
            continue
        if out is None:
            untouched.add(name)
            continue
        found += [f | {"rule": name} for f in out]
    book.sync(found, now, untouched)
    book.save()
    return found


# --- Routes ------------------------------------------------------------------------------------------------

@router.get("/attention")
async def attention(space: str = Query("all")):
    """Constats ouverts, rangés et réglés depuis moins de 72 h (au plus 200), constats calculés à la lecture en tête."""
    return view(space, datetime.now(timezone.utc))


async def _ack(row_id: int, on: bool) -> dict:
    now = datetime.now(timezone.utc)
    entry = {"at": _iso(now), "action": "attention_ack" if on else "attention_unack", "space": "commun",
             "args": {"id": row_id}}
    row = book.ack(row_id, on, now)
    if row is None:
        _journal(entry | {"ok": False, "status": 404, "resultat": "inconnu, réglé ou déjà dans cet état"})
        raise HTTPException(404, "Constat inconnu, déjà réglé ou déjà dans cet état")
    book.save()
    _journal(entry | {"ok": True, "status": 200, "resultat": row["title"][:300]})
    return {"ok": True, "item": _row_view(row, now)}


@router.post("/attention/{row_id}/ack", dependencies=[Depends(csrf_ok)])
async def attention_ack(row_id: int):
    return await _ack(row_id, True)


@router.post("/attention/{row_id}/unack", dependencies=[Depends(csrf_ok)])
async def attention_unack(row_id: int):
    return await _ack(row_id, False)
