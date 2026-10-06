"""Message du matin du labo (Labo 3), côté serveur web : un seul message privé groupé par jour.

Entre 8 h et 9 h (heure de Paris), au premier passage de la boucle du serveur (chaque minute) :
les règles validées par le worker et pas encore proposées deviennent des propositions (même carnet,
mêmes `!oui N` / `!non N` et mêmes boutons que les réglages, app/trading/proposals.py), avec leur
preuve chiffrée et leur « pourquoi », plus le nombre d'idées écartées depuis le matin précédent.
- Rien de validé : pas de message.
- Jamais en dehors de 8 h - 9 h ; manqué (serveur arrêté, bot déconnecté) : les propositions
  repartent dans le message du lendemain.
- Au plus un message par jour, même après un redémarrage : l'état est gardé dans
  data/lab_morning.json (JARVIS_LAB_MORNING_PATH) et marqué « envoi » AVANT l'envoi ; un envoi
  interrompu par un redémarrage n'est jamais refait (au plus une fois plutôt que deux).
- Rodage : JARVIS_LAB_DM=off (défaut) : tout tourne, le message est seulement journalisé et gardé
  (GET /api/lab/brief, page /cockpit/labo, `!labo`, `!propositions`), pas envoyé.

Événements du worker (méthode en place, challenger retiré) : relayés une fois chacun (table lab_events).
Le serveur n'écrit jamais dans data/lab.db.
"""
import json
import logging
import os
import sqlite3
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.lab import method as M
from app.lab import validator as V
from app.lab.db import open_readonly

log = logging.getLogger(__name__)
PARIS = ZoneInfo("Europe/Paris")
DM_START, DM_END = time(8, 0), time(9, 0)
MAX_ITEMS = 5
MAX_SEND_ATTEMPTS = 5
MESSAGE_MAX = 1950
KEEP_DAYS = 60
_warned: dict[str, str] = {}


def dm_on() -> bool:
    return os.getenv("JARVIS_LAB_DM", "off").strip().lower() in ("on", "1", "true", "oui", "vrai")


def paris_day(now: datetime) -> str:
    return now.astimezone(PARIS).date().isoformat()


def in_window(now: datetime) -> bool:
    """8 h ≤ heure de Paris < 9 h (heure d'été comme d'hiver : zoneinfo)."""
    return DM_START <= now.astimezone(PARIS).time() < DM_END


class State:
    """data/lab_morning.json : un point par jour (état d'envoi), dernier événement relayé."""

    def __init__(self, path: str | None = None):
        self.path = Path(path or os.getenv("JARVIS_LAB_MORNING_PATH", "data/lab_morning.json"))
        self.data: dict = {"jours": {}, "evenements": None, "dernier_point": None}
        try:
            self.data |= json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            log.warning("État du message du matin illisible (%s), on repart de zéro", self.path)
        for d in self.data["jours"].values():
            if d.get("etat") == "envoi":  # redémarrage pendant l'envoi : jamais renvoyé
                d["etat"] = "interrompu"
                log.warning("Labo : message du matin du %s interrompu par un redémarrage, pas renvoyé", d.get("jour"))

    def save(self) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).date().isoformat()
        self.data["jours"] = {k: v for k, v in self.data["jours"].items() if k >= cutoff}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("Impossible d'écrire %s : %s", self.path, exc)

    def day(self, day: str) -> dict | None:
        return self.data["jours"].get(day)

    def latest(self) -> dict | None:
        days = [d for d in self.data["jours"].values() if d.get("etat") != "rien"]
        return max(days, key=lambda d: d["jour"]) if days else None


# --- Contenu ------------------------------------------------------------------------------

def validated_rules(conn) -> list[dict]:
    """Règles validées sans décision de Simão (les plus anciennes d'abord)."""
    return [V.get_rule(conn, h) for (h,) in conn.execute(
        "SELECT hash FROM rules WHERE status = 'validee' AND decision IS NULL ORDER BY tested_at, id")]


def rejected_since(conn, since: str) -> int:
    row = conn.execute("SELECT COUNT(*) FROM rules WHERE status IN ('rejetee', 'invalide') AND tested_at > ? "
                       "AND decision IS NULL", (since,)).fetchone()
    return row[0] or 0


def text(day: str, items: list[dict], rejected: int, dm: bool = True) -> str:
    """Le message (≤ MESSAGE_MAX caractères : le « pourquoi » est raccourci si besoin)."""
    def cut(t: str, n: int | None) -> str:
        return t if n is None or len(t) <= n else t[:n - 1].rstrip() + "…"

    def render(why_max: int, proof_max: int | None) -> str:
        lines = [f"🧪 **Labo · idées validées cette nuit** ({day})"]
        for i, p in enumerate(items, start=1):
            lines.append(f"\n**{i}. « {p['nom']} »** · proposition {p['id']}")
            lines.append(cut(p["why"], proof_max))
            why = (p.get("pourquoi") or "").strip()
            if why and why_max:
                lines.append("Pourquoi : " + (why if len(why) <= why_max else why[:why_max - 1].rstrip() + "…"))
        if rejected:
            lines.append(f"\n-# {rejected} autre{'s' if rejected > 1 else ''} idée{'s' if rejected > 1 else ''} "
                         f"testée{'s' if rejected > 1 else ''} depuis hier, écartée{'s' if rejected > 1 else ''} "
                         "par le validateur.")
        ids = " / ".join(f"`!oui {p['id']}`" for p in items[:2])
        lines.append(f"-# Boutons ci-dessous, ou {ids} (`!non N` pour refuser). Approuvée, une règle part en "
                     "essai au labo et ne s'applique au direct qu'après avoir fait mieux en direct.")
        return "\n".join(lines)

    for why_max, proof_max in ((400, None), (250, None), (150, None), (80, None), (0, None), (0, 220), (0, 120)):
        out = render(why_max, proof_max)
        if len(out) <= MESSAGE_MAX:
            return out
    return out[:MESSAGE_MAX]


def build(conn, proposals, now: datetime, since: str | None) -> dict:
    """Crée les propositions du jour (nouvelles règles validées, plus celles jamais envoyées) et le texte."""
    day = paris_day(now)
    champion = M.champion(conn)["version"]
    items = []
    for rule in validated_rules(conn):
        p = proposals.lab(rule["empreinte"])
        if p is None:
            p = proposals.propose_lab(rule["empreinte"], rule.get("nom") or rule["empreinte"][:10], champion,
                                      M.proof_fr(rule), rule.get("pourquoi"), day)
        elif p["status"] != "pending" or p.get("offerte"):
            continue
        if p:
            items.append(p)
    # Propositions jamais envoyées dont la règle n'est plus dans la liste (déjà décidée ailleurs) : ignorées.
    items = items[:MAX_ITEMS]
    for p in items:
        p["groupe"] = day
    proposals.save()
    since = since or (now - timedelta(days=1)).astimezone(timezone.utc).isoformat(timespec="seconds")
    rejected = rejected_since(conn, since)
    return {"jour": day, "cree_le": now.isoformat(timespec="seconds"), "propositions": [p["id"] for p in items],
            "regles": [p["empreinte"] for p in items], "rejetees": rejected,
            "texte": text(day, items, rejected) if items else None, "envoye_a": [], "essais": 0}


def tick(now: datetime, proposals, state: State, bot_on: bool, dm: bool | None = None,
         conn_factory=open_readonly) -> dict | None:
    """Prépare le point du jour s'il est l'heure (8 h - 9 h) et qu'il n'a pas encore été fait. Renvoie
    le point préparé (état « a_envoyer », « journalise » ou « rien »), sinon None."""
    if not in_window(now):
        return None
    day = paris_day(now)
    if state.day(day):
        return None
    conn = conn_factory()
    if conn is None:
        return None  # base absente : nouvel essai à la minute suivante (dans la fenêtre)
    try:
        digest = build(conn, proposals, now, state.data.get("dernier_point"))
    except sqlite3.Error as exc:  # base d'un ancien schéma (worker pas encore redémarré) : une ligne par jour
        if _warned.get("base") != day:
            _warned["base"] = day
            log.warning("Labo : message du matin pas encore possible (base illisible : %s), nouvel essai chaque minute "
                        "jusqu'à 9 h", exc)
        return None
    finally:
        conn.close()
    dm = dm_on() if dm is None else dm
    if not digest["texte"]:
        digest["etat"] = "rien"
    elif not dm or not bot_on:
        digest["etat"] = "journalise"
        for pid in digest["propositions"]:
            p = proposals.get(pid)
            if p:
                p["offerte"] = "journalisee"
        proposals.save()
        reason = "rodage, JARVIS_LAB_DM=off" if not dm else "bot Discord absent"
        log.info("Labo : message du matin gardé sans envoi (%s) :\n%s", reason, digest["texte"])
    else:
        digest["etat"] = "a_envoyer"
    state.data["jours"][day] = digest
    state.data["dernier_point"] = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    state.save()
    return digest


async def send_due(state: State, proposals, now: datetime, owner_ids: set[int], fetch_user, view_for) -> int:
    """Envoie en privé le point du jour « a_envoyer » (bot connecté). Hors de la fenêtre, il est marqué
    « manque » et ses propositions repartent le lendemain. Renvoie le nombre de messages envoyés."""
    sent = 0
    for day, d in list(state.data["jours"].items()):
        if d.get("etat") != "a_envoyer":
            continue
        if day != paris_day(now) or not in_window(now):
            d["etat"] = "manque"
            state.save()
            log.warning("Labo : message du matin du %s manqué (hors de 8 h - 9 h), propositions reportées", day)
            continue
        pending = [pid for pid in d["propositions"] if (p := proposals.get(pid)) and p["status"] == "pending"]
        if not pending:
            d["etat"] = "sans_objet"
            state.save()
            continue
        d["etat"] = "envoi"
        state.save()  # au plus une fois : marqué avant l'envoi
        ok = True
        for oid in sorted(owner_ids):
            if oid in d["envoye_a"]:
                continue
            try:
                user = await fetch_user(oid)
                await user.send(d["texte"], view=view_for(pending))
                d["envoye_a"].append(oid)
                sent += 1
            except Exception as exc:  # pas envoyé : nouvel essai au prochain passage (dans la fenêtre)
                log.warning("Labo : message du matin non envoyé au compte %s : %s", oid, exc)
                ok = False
        d["essais"] = d.get("essais", 0) + 1
        if ok:
            d["etat"] = "envoye"
            d["envoye_le"] = now.isoformat(timespec="seconds")
            for pid in d["propositions"]:
                p = proposals.get(pid)
                if p:
                    p["offerte"] = True
            proposals.save()
        else:
            d["etat"] = "a_envoyer" if d["essais"] < MAX_SEND_ATTEMPTS else "echec"
        state.save()
    return sent


def pull_events(state: State, now: datetime, conn_factory=open_readonly) -> list[dict]:
    """Événements du worker pas encore relayés (au plus une fois : l'état avance avant l'envoi). Au
    premier lancement, seuls ceux des dernières 24 h sont relayés."""
    conn = conn_factory()
    if conn is None:
        return []
    try:
        last = state.data.get("evenements")
        if last is None:
            row = conn.execute("SELECT MAX(id) FROM lab_events WHERE ts < ?", (int(now.timestamp()) - 86400,)).fetchone()
            last = row[0] or 0
        evs = M.events(conn, last)
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    if evs or state.data.get("evenements") is None:
        state.data["evenements"] = max([last] + [e["id"] for e in evs])
        state.save()
    return evs


def status_line(state: State, proposals) -> str:
    """Une ligne pour `!labo` : propositions du labo en attente et dernier point du matin."""
    pending = proposals.pending("labo")
    line = f"Propositions du labo en attente : {len(pending)}" + (
        f" ({', '.join(str(p['id']) for p in pending)})" if pending else "")
    last = state.latest()
    if last:
        line += f" · dernier point du matin {last['jour']} : {ETATS.get(last['etat'], last['etat'])}"
    line += " · message privé " + ("activé" if dm_on() else "en rodage (JARVIS_LAB_DM=off)")
    return line


ETATS = {"a_envoyer": "à envoyer", "envoi": "en cours d'envoi", "envoye": "envoyé", "journalise": "gardé sans envoi",
         "manque": "manqué, reporté", "echec": "envoi en échec", "interrompu": "interrompu", "sans_objet": "déjà décidé",
         "rien": "rien de validé"}


state = None  # instance du serveur (créée par le scheduler au premier passage)


def shared_state() -> State:
    global state
    if state is None:
        state = State()
    return state
