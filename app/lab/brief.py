"""Tout ce qu'il faut à la routine du soir en un appel (GET /api/lab/brief, < ~20 Ko), et le quota
de règles par soirée (au plus 3 acceptées par la route POST /api/lab/rules).

Contenu : dernier résumé du soir, statistiques clés (cache du worker), fiches catalyseurs à classer
des 2 derniers jours (plafonnées), règles soumises récemment et leur verdict, méthode en place et
challenger, dernier message du matin, place restante pour ce soir et types de catalyseurs permis.
"""
import fcntl
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.lab import catalysts as CAT
from app.lab import method as M
from app.lab import stats as STATS
from app.lab import summary as SUM
from app.lab import validator as V

PARIS = ZoneInfo("Europe/Paris")
MAX_BYTES = 20_000
RULES_PER_EVENING = 3
MAX_CATALYSTS = 40
MAX_RULES = 30
RECENT_DAYS = 14
_lock = threading.Lock()


def soiree(now: datetime) -> str:
    """Soirée de la routine (date de Paris) : de midi à midi le lendemain, pour qu'une routine qui
    déborde après minuit compte toujours pour la même soirée."""
    return (now.astimezone(PARIS) - timedelta(hours=12)).date().isoformat()


class QuotaFull(Exception):
    pass


def _quota_path(db_file: str) -> Path:
    return M.queue_root(db_file) / "quota" / "soirees.json"


def quota_used(db_file: str, now: datetime) -> list[str]:
    try:
        data = json.loads(_quota_path(db_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data.get(soiree(now), [])


def take_quota(db_file: str, rule_hash: str, now: datetime, limit: int = RULES_PER_EVENING) -> int:
    """Compte une nouvelle règle pour la soirée ; QuotaFull au-delà de `limit`. La même règle reposée
    ne compte qu'une fois. Verrou de fichier : deux requêtes simultanées ne passent pas à 4."""
    path = _quota_path(db_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = soiree(now)
    with _lock, open(path.with_suffix(".lock"), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        used = data.get(key, [])
        if rule_hash not in used:
            if len(used) >= limit:
                raise QuotaFull(f"déjà {limit} règles acceptées pour la soirée du {key} (au plus {limit} par soir)")
            used.append(rule_hash)
        keep = sorted(data)[-6:]  # quelques soirées seulement
        data = {k: data[k] for k in keep if k != key} | {key: used}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)
        return limit - len(used)


def release_quota(db_file: str, rule_hash: str, now: datetime) -> None:
    """Rend la place si la mise en file a échoué (file pleine)."""
    path = _quota_path(db_file)
    with _lock, open(path.with_suffix(".lock"), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        key = soiree(now)
        data[key] = [h for h in data.get(key, []) if h != rule_hash]
        path.write_text(json.dumps(data), encoding="utf-8")


def _stats(conn) -> dict | None:
    cached = STATS.cached(conn)
    if not cached:
        return None
    return {"calcule_le": cached.get("calcule_le"), "version": (cached.get("filtres") or {}).get("version"),
            "global": SUM.compact(cached.get("global")),
            "marches": {m: SUM.compact(v.get("mesures")) for m, v in (cached.get("marches") or {}).items()},
            "capture": {k: (cached.get("capture_potentiel") or {}).get(k)
                        for k in ("mouvements", "avec_plan", "tp1", "taux_capture")},
            "degradations": [{k: g.get(k) for k in ("marche", "dimension", "valeur", "n", "esperance_r", "z")}
                             for g in (cached.get("degradations") or [])[:5]]}


def _catalysts(conn, now: datetime) -> list[dict]:
    since = (now.astimezone(timezone.utc).date() - timedelta(days=2)).isoformat()
    out = []
    for c in CAT.query(conn, since=since, unclassified=True, limit=MAX_CATALYSTS):
        out.append({"id": c["id"], "marche": c["marche"], "jour": c["jour"], "symbole": c["symbole"],
                    "genre": c["genre"], "direction": c["direction"], "amplitude_pct": c["amplitude_pct"],
                    "gap_pct": c["gap_pct"], "depart": c["depart"],
                    "actus": [{"titre": (n.get("titre") or "")[:100], "heure": n.get("publie_a"),
                               "position": n.get("position")} for n in (c["actus"] or [])[:3]]})
    return out


def _rules(conn, now: datetime) -> list[dict]:
    since = (now.astimezone(timezone.utc) - timedelta(days=RECENT_DAYS)).isoformat(timespec="seconds")
    out = []
    for r in V.list_rules(conn, limit=200):
        if (r["testee_le"] or r["soumise_le"] or "") < since:
            continue
        m = r["mesures"] or {}
        out.append({"empreinte": r["empreinte"][:12], "nom": r["nom"], "etat": r["etat"], "decision": r.get("decision"),
                    "soiree": r.get("soiree"), "regle": M.rule_fr({"kind": r["regle"]["kind"], "cond": r["regle"]["cond"],
                                                                   "markets": r["regle"].get("markets")}),
                    "raison": (r["raisons"] or [None])[0], "amelioration_moyenne_r": m.get("amelioration_moyenne_r"),
                    "touches_test": m.get("touches_test"), "pourquoi": (r.get("pourquoi") or "")[:200] or None})
        if len(out) >= MAX_RULES:
            break
    return out


def size(data: dict) -> int:
    return len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode())


def build(conn, now: datetime, db_file: str, morning: dict | None = None, dm: bool = False) -> dict:
    used = quota_used(db_file, now)
    data = {"genere_le": now.astimezone(timezone.utc).isoformat(timespec="seconds"), "soiree": soiree(now),
            "regles_restantes_ce_soir": max(0, RULES_PER_EVENING - len(used)),
            "message_prive": "activé" if dm else "rodage",
            "methode": M.state(conn), "resume": (SUM.latest(conn) or {}).get("resume"), "stats": _stats(conn),
            "fiches_a_classer": _catalysts(conn, now), "types_permis": list(CAT.TYPES),
            "regles_recentes": _rules(conn, now), "compteurs": V.counts(conn),
            "point_du_matin": {k: morning.get(k) for k in ("jour", "etat", "rejetees", "propositions", "texte")}
            if morning else None}
    cut = False
    while size(data) > MAX_BYTES:
        cut = True
        if len(data["fiches_a_classer"]) > 10:
            data["fiches_a_classer"] = data["fiches_a_classer"][:len(data["fiches_a_classer"]) - 5]
        elif len(data["regles_recentes"]) > 10:
            data["regles_recentes"] = data["regles_recentes"][:-5]
        elif data["resume"] and data["resume"].get("idees_rejetees"):
            data["resume"]["idees_rejetees"] = data["resume"]["idees_rejetees"][:-5]
        elif data["resume"] and len(data["resume"].get("potentiel_10") or []) > 3:
            data["resume"]["potentiel_10"] = data["resume"]["potentiel_10"][:-1]
        elif data["fiches_a_classer"]:
            data["fiches_a_classer"] = data["fiches_a_classer"][:-1]
        elif data["regles_recentes"]:
            data["regles_recentes"] = data["regles_recentes"][:-1]
        else:
            break
    if cut:
        data["tronque"] = True
    return data
