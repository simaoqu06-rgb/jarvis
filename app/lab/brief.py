"""Tout ce qu'il faut à la routine du soir en un appel (GET /api/lab/brief, < ~45 Ko), et le quota
de règles par soirée (au plus 3 acceptées par la route POST /api/lab/rules).

Contenu : dernier résumé du soir, statistiques clés (cache du worker), fiches catalyseurs à classer
des 2 derniers jours (plafonnées), règles soumises récemment et leur verdict, méthode en place et
challenger, dernier message du matin, place restante pour ce soir, types de catalyseurs permis et
carnet des leçons (actives et démenties récentes, à relire avant d'analyser) et grade de JARVIS
(mesure, critères, mode survie, historique) pour le council du grade.
Depuis le 10 octobre 2026 (fil « JARVIS s'améliore-t-il vraiment », choix « Tout le socle » de Simão), les
analystes ne reçoivent plus seulement des totaux : `detail` (espérance par setup, régime, heure, note et jour,
calculée par le code avec IC en grappes), `strategies` (tableau du labo v2 en bref, confirmation des GO) et
`autopsie` (où les trades échouent, élan contre le hasard, grade indicatif des GO confirmés).
`filtres_scanner` : ce que coûte chaque filtre du scanner de 15 h 10, mesuré sur les trades v1 d'actions (filtres.py).
`declaration` (2e tour d'avis, C4) : la déclaration en vigueur est-elle entièrement re-mesurée (status.declaration_state) ;
aucune hypothèse « prête à déclarer » avant « complete ».
"""
import fcntl
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.lab import autopsie as AUT
from app.lab import catalysts as CAT
from app.lab import filtres as FIL
from app.lab import grade as GRADE
from app.lab import lessons as L
from app.lab import method as M
from app.lab import stats as STATS
from app.lab import strat_stats as SS
from app.lab import summary as SUM
from app.lab import validator as V

PARIS = ZoneInfo("Europe/Paris")
MAX_BYTES = 45_000
DETAIL_DIMS = ("setup", "regime", "heure", "note", "jour_semaine")
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


def _slice(m: dict) -> dict:
    return {"n": m.get("declenches"), "jours": m.get("jours"), "esperance_r": m.get("esperance_r"),
            "ic95": m.get("ic95"), "total_r": m.get("total_r"), "tp1": m.get("tp1_avant_stop")}


def _detail(conn) -> dict | None:
    """Espérance par tranche (stats.compute, IC en grappes par jour) : les chiffres que les analystes
    doivent citer au lieu de les recalculer. Les tranches trop petites sont écartées."""
    cached = STATS.cached(conn)
    if not cached:
        return None
    out = {}
    for market, v in (cached.get("marches") or {}).items():
        par = v.get("par") or {}
        out[market] = {dim: {k: _slice(m) for k, m in (par.get(dim) or {}).items() if not m.get("echantillon_faible")}
                       for dim in DETAIL_DIMS if par.get(dim)}
    return out


def _strategies(conn) -> dict | None:
    """Labo v2 en bref : par stratégie, espérance de référence, avant frais, frais, témoin aléatoire."""
    rep = SS.cached(conn)
    if not rep:
        return None
    out = {"declaration": {k: (rep.get("declaration") or {}).get(k) for k in ("numero", "essais", "z_candidate")},
           "partiel": rep.get("partiel"),  # I2 : rattrapage d'une nouvelle déclaration en cours (tableau partiel)
           "marches": {}}
    for market, strats in (rep.get("strategies") or {}).items():
        out["marches"][market] = {
            name: {"n": s.get("declenches"), "jours": s.get("jours"), "esperance_r": s.get("esperance_ref_r"),
                   "ic95": s.get("ic95"), "brut_r": s.get("brut_r"), "frais_r": s.get("frais_r"),
                   "candidate": s.get("candidate"), "candidate_compte": s.get("candidate_compte"),  # n° 10
                   "rentable_20e": s.get("rentable_20e"), "mise_20e_r": (s.get("mise_20e") or {}).get("esperance_r"),
                   "temoin_ecart_r": (s.get("temoin") or {}).get("ecart_r"), "temoin_z": (s.get("temoin") or {}).get("z"),
                   "couts_20e_ecart_r": {k: (v.get("ecart") or {}).get("esperance_r") for k, v in s["couts_20e"].items()}
                   if s.get("couts_20e") else None}  # 20/10/2026 : `!frais`, écart apparié à mise_20e_r
            for name, s in strats.items()}
    conf = (rep.get("confirmation_go") or {})
    out["confirmation_go"] = {v: {"go": r.get("go"), "confirme_ou_rien_moins_brut": r.get("ecart_par_go")}
                              for v, r in conf.items() if isinstance(r, dict)}
    return out


def _catalysts(conn, now: datetime) -> list[dict]:
    """Fiches à classer : seulement celles qui ont leurs actus, les plus récentes d'abord (rejeu compris),
    puis celles des 2 derniers jours pour lesquelles Finnhub n'a rien trouvé (type « aucun »).
    Une fiche dont les actus sont encore à chercher attend le passage suivant au lieu d'occuper une
    place ; une fiche hors de portée (au-delà des ~12 mois de Finnhub, crypto) ne se classe pas."""
    since = (now.astimezone(timezone.utc).date() - timedelta(days=2)).isoformat()
    out = []
    fiches = CAT.query(conn, unclassified=True, news_status="ok", limit=MAX_CATALYSTS)
    if len(fiches) < MAX_CATALYSTS:
        fiches += CAT.query(conn, since=since, unclassified=True, news_status="aucune",
                            limit=MAX_CATALYSTS - len(fiches))
    for c in fiches:
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


def _lessons(conn) -> dict | None:
    try:
        return L.brief(conn)
    except Exception:  # base d'avant le carnet (worker pas encore redémarré)
        return None


def _grade(conn) -> dict | None:
    try:
        return GRADE.brief(conn)
    except Exception:  # base d'avant le grade (worker pas encore redémarré)
        return None


def _declaration(conn) -> dict | None:
    """2e tour d'avis (C4) : la déclaration en vigueur est-elle entièrement re-mesurée, et sinon ce qui reste."""
    from app.lab import status as STATUS  # import tardif, comme le serveur

    try:
        n = STATUS.declaration_state(conn)
    except Exception:  # une information, jamais une panne du brief
        return None
    return n and n | {"texte": STATUS.declaration_line(n)}


def _drop_detail(detail: dict | None) -> bool:
    """Retire du détail la dimension la moins utile qui reste (jour, puis note), sur tous les marchés."""
    for dim in ("jour_semaine", "note"):
        if any(dim in d for d in (detail or {}).values()):
            for d in detail.values():
                d.pop(dim, None)
            return True
    return False


def size(data: dict) -> int:
    return len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode())


def build(conn, now: datetime, db_file: str, morning: dict | None = None, dm: bool = False) -> dict:
    used = quota_used(db_file, now)
    data = {"genere_le": now.astimezone(timezone.utc).isoformat(timespec="seconds"), "soiree": soiree(now),
            "regles_restantes_ce_soir": max(0, RULES_PER_EVENING - len(used)),
            "message_prive": "activé" if dm else "rodage",
            "methode": M.state(conn), "resume": (SUM.latest(conn) or {}).get("resume"), "stats": _stats(conn),
            "fiches_a_classer": _catalysts(conn, now), "types_permis": list(CAT.TYPES),
            "regles_recentes": _rules(conn, now), "compteurs": V.counts(conn), "lecons": _lessons(conn),
            "grade": _grade(conn), "declaration": _declaration(conn),
            "detail": _detail(conn), "strategies": _strategies(conn), "autopsie": AUT.compact(AUT.cached(conn)),
            "filtres_scanner": FIL.compact(FIL.cached(conn)),
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
        elif data["lecons"] and data["lecons"]["dementies"]:
            data["lecons"]["dementies"] = data["lecons"]["dementies"][:-2]
        elif data["fiches_a_classer"]:
            data["fiches_a_classer"] = data["fiches_a_classer"][:-1]
        elif data["regles_recentes"]:
            data["regles_recentes"] = data["regles_recentes"][:-1]
        elif _drop_detail(data["detail"]):
            pass
        elif data["autopsie"] and data["autopsie"].get("strategies"):
            data["autopsie"]["strategies"] = None
        elif data["filtres_scanner"] and data["filtres_scanner"].get("par_prix"):
            data["filtres_scanner"]["par_prix"] = None
        elif data["lecons"] and len(data["lecons"]["actives"]) > 10:
            data["lecons"]["actives"] = data["lecons"]["actives"][:-5]  # confirmées en tête : on garde celles-là
        else:
            break
    if cut:
        data["tronque"] = True
    return data
