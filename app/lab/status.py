"""État du labo en lecture seule (route /api/lab/status et commande Discord !labo).

Avancement du rejeu, trades par marché et par origine, dernier jour en direct, taille de la base,
étape en cours du worker et son dernier signe de vie (lab.heartbeat), et quelques chiffres du Labo 2 lus dans le cache du worker : espérance en R avec son intervalle de
confiance, fiches catalyseurs, règles en attente et testées.

Avis du 10 octobre (I2, I4) : jours sautés (skipped:<D>, retirés des jours faits), jours calculés sans quelques
symboles que la source n'a pas donnés (missing_symbols), valeurs de l'univers sans trade (absent), et trades par
révision du code (trades.code_rev) pour voir quand une même version mélange plusieurs codes.
2e tour d'avis (C4) : une ligne « n° <déclaration en vigueur> : complète » ou ce qui reste à re-mesurer (declaration_state),
aussi dans le brief.

Deuxième tour d'avis du 10 octobre : garde du Labo 1 (garde_v1, empreinte v1 calculée au démarrage du worker ; en
pause, une alerte en tête de !labo) et recalcul ponctuel des séances du direct d'avant une rupture (recalcul_v1 ;
une séance « non recalculable » est dite).
2e tour d'avis, relecture : archive d'audit en échec (archive_line), alerte en tête de !labo tant qu'elle dure.
"""
import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from app.lab import heartbeat
from app.lab import seal as SEAL
from app.lab.db import db_path, open_readonly

MARKET_FR = {"stock": "actions", "crypto": "crypto"}
WORKER_SILENT_S = 1800  # comme le healthcheck docker : 30 min sans battement = boucle bloquée
LIMITATIONS = [
    "Crypto : OKX ne liste que les paires cotées aujourd'hui ; les paires retirées pendant la période "
    "rejouée manquent à l'univers (biais du survivant, résultats crypto du rejeu probablement flattés).",
    "Actions : seules les valeurs retirées de la cote encore connues d'Alpaca sont dans l'univers.",
    "Catalyseur (actus, résultats) non rejoué pour v1 et les stratégies : note composite sans sa part catalyseur. "
    "Killmode complet (n° 8) le rejoue sur l'archive des actus Finnhub, de octobre 2025 à aujourd'hui seulement, "
    "avec le calendrier des résultats et les profils tels que Finnhub les donne au moment de l'archive. "
    "Disponibilité historique des actus non vérifiée : l'archive se fie à l'heure de publication de Finnhub, qui ne "
    "prouve pas que l'actu était en ligne à 9 h 45 (le délai se mesure en direct depuis le 10 octobre 2026 : "
    "visibilite_finnhub). Calendrier des résultats : limite non vérifiable, une date déplacée après coup ne se voit pas "
    "et peut changer le veto « résultats dans 24 h ».",
    "Univers actions approché (volume, variation et volume relatif de la veille) : le screener Alpaca "
    "du direct ne se rejoue pas. Killmode n'y voit qu'une partie des actions en gap ≥ 4 % : la part est "
    "comptée dans le tableau des stratégies (couverture).",
    "Actions : bougies ajustées des splits connus aujourd'hui ; avant un regroupement d'actions, le prix "
    "paraît plus élevé, d'où un glissement, une commission en R et un pas de cotation sous-estimés sur les "
    "petites valeurs (l'inverse avant une division) ; le pas de cotation pèse aussi sur le déclenchement et les "
    "objectifs touchés. Le scanner +10 (n° 9) et Killmode complet (n° 8) s'exécutent, eux, aux prix réellement "
    "négociés. Sauf deux mesures sans essai du scanner +10, lues sur le journalier ajusté : les tranches de prix de "
    "la veille et le minimum de 5 $ des filtres de 15 h 10 rejoués (avant un regroupement, un titre à moins de 5 $ "
    "y paraît au-dessus : le coût de ce minimum est sous-estimé).",
    "Confirmation des GO (n° 7) : le plan au stop plancher du direct n'existe que pour les jours rejoués depuis "
    "la n° 7 (le rattrapage n'a pas l'historique de 15 min pour le refaire) ; les GO du labo ne passent ni par "
    "les filtres du scanner, ni par le catalyseur, ni par les leçons.",
]


def _progress(conn: sqlite3.Connection, job: str) -> dict:
    return {k: json.loads(v) for k, v in conn.execute("SELECT key, value FROM progress WHERE job = ?", (job,))}


def day_counts(p: dict) -> dict:
    """Jours sautés, jours calculés sans tous leurs symboles et valeurs de l'univers sans trade, d'un job (rejeu
    ou direct) lu dans progress."""
    days = [v for k, v in p.items() if k.startswith("day:") and isinstance(v, dict)]
    return {"jours_sautes": sum(1 for k in p if k.startswith("skipped:")),
            "jours_partiels": sum(1 for v in days if v.get("missing")),
            "absents": sum(v.get("absent") or 0 for v in days)}


def code_revisions(conn: sqlite3.Connection) -> dict:
    """Trades (hors scellé) par version et révision du code ; « inconnue… » : calculés avant le suivi."""
    from app.lab import stats as STATS  # import tardif, comme market_days

    return (STATS.code_revisions(conn) or {}).get("par_version") or {}


def revision_mixes(conn: sqlite3.Connection, par: dict) -> dict:
    """Versions dont les trades viennent de plusieurs comportements (stats.revision_mixes), raisons courtes."""
    from app.lab import stats as STATS

    return {version: [short for short, _ in mixes] for version, mixes in STATS.revision_mixes(conn, par).items()}


def _days_between(market: str, start: str, end: str) -> int:
    from app.lab.runner import market_days  # import tardif : le serveur web n'en a besoin qu'ici

    return len(market_days(market, date.fromisoformat(start), date.fromisoformat(end)))


def status(path: str | None = None) -> dict:
    path = path or db_path()
    size = sum(p.stat().st_size for p in Path(path).parent.glob(Path(path).name + "*")) if Path(path).exists() else 0
    out = {"db": path, "exists": Path(path).exists(), "db_size_mb": round(size / 1e6, 1),
           "replay": {}, "trades": {}, "live": {}, "method_version": None, "limitations": LIMITATIONS,
           "worker": heartbeat.read(file=Path(path).parent / "lab.heartbeat")}
    conn = open_readonly(path)
    if conn is None:
        return out
    try:
        for market in ("stock", "crypto"):
            p = _progress(conn, f"replay:{market}")
            total = p.get("days_total") or 0
            counts = day_counts(p)
            done = _days_between(market, p["start"], p["last_day"]) if p.get("start") and p.get("last_day") else 0
            done = max(0, done - counts["jours_sautes"])  # I4 : un jour sauté n'est pas fait
            out["replay"][market] = {"start": p.get("start"), "end": p.get("end"), "last_day": p.get("last_day"),
                                     "days_done": done, "days_total": total,
                                     "pct": round(100 * done / total, 1) if total else 0.0, **counts}
            live = _progress(conn, f"live:{market}")
            out["live"][market] = {"last_day": live.get("last_day"), **day_counts(live)}
        for market, origin, n in conn.execute("SELECT market, origin, COUNT(*) FROM trades GROUP BY market, origin"):
            out["trades"].setdefault(market, {})[origin] = n
        out["revisions_code"] = code_revisions(conn)
        out["revisions_melangees"] = revision_mixes(conn, out["revisions_code"])
        out["scelle"] = {**SEAL.describe(), "trades": {}}
        try:  # base d'avant le scellé ouverte en lecture seule : table absente
            for market, n in conn.execute("SELECT market, COUNT(*) FROM trades_sealed GROUP BY market"):
                out["scelle"]["trades"][market] = n
        except sqlite3.Error:
            pass
        out["labo_v2"] = labo_v2(conn)
        out["declaration"] = declaration_state(conn)
        from app.lab import archive as ARC  # import tardif, comme runner

        out["archive_actus"] = ARC.counts(conn)
        from app.lab import method as M  # import tardif, comme runner

        out["methode"] = M.state(conn)
        out["method_version"] = out["methode"]["champion"]["version"]
        live_start = conn.execute("SELECT value FROM progress WHERE job = 'lab' AND key = 'live_start'").fetchone()
        out["live_start"] = json.loads(live_start[0]) if live_start else None
        guard = conn.execute("SELECT value FROM progress WHERE job = 'lab' AND key = 'garde_v1'").fetchone()
        out["garde_v1"] = json.loads(guard[0]) if guard else None
        arch = conn.execute("SELECT value FROM progress WHERE job = 'lab' AND key = 'archive_audit'").fetchone()
        out["archive_audit"] = json.loads(arch[0]) if arch else None  # relecture W1 : archive d'audit en échec
        out["recalcul_v1"] = {k: v for k, v in _progress(conn, "recalcul").items() if not k.startswith("attempts:")}
        out["labo2"] = headline(conn)
        from app.trading import visibilite as VIS  # import tardif, comme runner

        out["visibilite_finnhub"] = VIS.summary(VIS.journal_path(path))
    except sqlite3.Error as exc:
        out["error"] = f"base illisible : {exc}"
    finally:
        conn.close()
    return out


def labo_v2(conn: sqlite3.Connection) -> dict:
    """Lignes des stratégies (hors scellé) et jours encore à rattraper, par marché."""
    from app.lab import rattrapage as RAT  # import tardif : tire runner et generator

    out: dict = {"lignes": {}, "rattrapage": {}}
    try:
        for market, n in conn.execute("SELECT market, COUNT(*) FROM strat_trades GROUP BY market"):
            out["lignes"][market] = n
        for market in ("stock", "crypto"):
            out["rattrapage"][market] = RAT.remaining(conn, market)
    except sqlite3.Error:
        pass
    return out


def declaration_state(conn: sqlite3.Connection) -> dict | None:
    """2e tour d'avis (C4) : la déclaration en vigueur est-elle entièrement mesurée ? Jours à rattraper (tableau des stratégies,
    strat_stats.partial : marché partiel tant qu'il reste des jours du rejeu ou des lignes d'avant), lignes d'avant
    encore exclues, séances de la n° 9 et de Killmode complet à recalculer. « complete » : tout est à zéro, condition
    écrite pour déclarer la suivante (docs/labo-strategies.md, « Avant la déclaration suivante »). None si la base n'est pas lisible."""
    from app.lab import kill_complet as KC  # imports tardifs : tirent killmode, runner et generator
    from app.lab import ouverture as OUV
    from app.lab import plus10 as P10
    from app.lab import strat_stats as SS
    from app.lab import strategies as ST

    try:
        part = SS.partial(conn)
        n12 = OUV.progress(conn) or {}
        out = {"numero": ST.LATEST_DECLARATION, "lignes_v2": ST.DECLARATION,
               "jours": {m: (part.get(m) or {}).get("jours_rattrapage", 0) for m in ("stock", "crypto")},
               "lignes_anciennes": {m: (part.get(m) or {}).get("lignes_exclues", 0) for m in ("stock", "crypto")},
               "seances_n9": P10.a_recalculer(conn), "seances_kill_complet": KC.stale_days(conn),
               "seances_n12": n12.get("seances_a_calculer", 0), "bougies_9h30": n12.get("bougies_9h30")}
    except sqlite3.Error:
        return None
    out["complete"] = not part and not out["seances_n9"] and not out["seances_kill_complet"] and not out["seances_n12"]
    return out


def declaration_line(n: dict | None) -> str | None:
    """« n° <déclaration en vigueur> : complète », ou ce qui reste à re-mesurer."""
    if not n:
        return None
    head = f"n° {n['numero']}"
    if n["complete"]:
        return f"{head} : complète (stratégies, n° 9 et Killmode complet re-mesurés, n° 12 calculée)"
    days, old, parts = n["jours"], n["lignes_anciennes"], []
    if days["stock"] or days["crypto"]:
        parts.append(f"{days['stock']} j actions, {days['crypto']} j crypto à rattraper")
    stray = sum(old[m] for m in old if not days[m])
    if stray:
        parts.append(f"{stray} ligne{'s' if stray > 1 else ''} d'avant sans jour à refaire (exclue{'s' if stray > 1 else ''})")
    if n["seances_n9"]:
        parts.append(f"{n['seances_n9']} séance{'s' if n['seances_n9'] > 1 else ''} n° 9 à recalculer")
    if n["seances_kill_complet"]:
        parts.append(f"{n['seances_kill_complet']} séance{'s' if n['seances_kill_complet'] > 1 else ''} Killmode "
                     "complet à recalculer")
    k = n.get("seances_n12") or 0
    if k:
        seen = n.get("bougies_9h30") or {}
        parts.append(f"{k} séance{'s' if k > 1 else ''} n° 12 à calculer (bougies de 9 h 30 : {seen.get('lues', 0)} "
                     f"séances lues sur {seen.get('utiles', 0)})")
    return f"⏳ {head} : " + ", ".join(parts)


def archive_line(arch: dict | None, numero: int | None = None) -> str | None:
    """Relecture W1 : archive d'audit en échec (worker, progress lab/archive_audit) ; tant qu'elle n'est pas faite, rien
    de ce qu'une nouvelle déclaration réécrit n'avance. None si rien n'est en échec."""
    if not arch or arch.get("etat") != "echec":
        return None
    try:
        when = datetime.fromisoformat(arch["depuis"]).astimezone(timezone.utc)
        since = when.strftime(" depuis le %d/%m/%Y à %H:%M UTC")
    except (KeyError, TypeError, ValueError):
        since = ""
    rattrapage = f"rattrapage de la n° {numero}" if numero else "rattrapage"
    return (f"⚠️ Archive d'audit en échec{since} ({arch.get('raison') or 'raison inconnue'}) : {rattrapage}, Killmode "
            "complet, n° 9 et recalcul du direct à l'arrêt, rien n'est réécrit (docker compose logs jarvis-lab)")


def headline(conn: sqlite3.Connection) -> dict:
    """Chiffres clés du Labo 2 (cache des statistiques, fiches, règles) ; vide sur une base du Labo 1."""
    from app.lab import stats as STATS
    from app.lab import validator as V

    out: dict = {}
    cached = STATS.cached(conn)
    if cached:
        g = cached.get("global") or {}
        out["stats"] = {"declenches": g.get("declenches"), "esperance_r": g.get("esperance_r"), "ic95": g.get("ic95"),
                        "version": (cached.get("filtres") or {}).get("version"), "calcule_le": cached.get("calcule_le")}
    try:
        # À classer : fiches qui ont leurs actus et pas de type. Les autres attendent leurs actus
        # (a_chercher) ou n'en auront pas (au-delà des ~12 mois de Finnhub, aucune trouvée, erreur).
        row = conn.execute("SELECT COUNT(*), SUM(news_status = 'ok'), SUM(news_status = 'ok' AND catalyst_type IS NULL), "
                           "SUM(news_status = 'a_chercher'), SUM(news_status NOT IN ('ok', 'a_chercher')) "
                           "FROM catalysts").fetchone()
        out["fiches"] = {"total": row[0], "avec_actus": row[1] or 0, "a_classer": row[2] or 0,
                         "actus_a_chercher": row[3] or 0, "sans_actus": row[4] or 0}
    except sqlite3.Error:
        pass
    out["regles"] = V.counts(conn)
    return out


def _date_fr(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%d/%m/%Y")


def _fr(x: float, digits: int = 2) -> str:
    return f"{x:+.{digits}f}".replace(".", ",")


def _gaps_text(replay: dict, live: dict | None) -> str:
    """I4 : jours sautés, jours calculés sans tous leurs symboles, valeurs de l'univers sans trade (rejeu + direct)."""
    n = {k: (replay.get(k) or 0) + ((live or {}).get(k) or 0) for k in ("jours_sautes", "jours_partiels", "absents")}
    parts = []
    if n["jours_sautes"]:
        parts.append(f"⚠️ {n['jours_sautes']} jour(s) sauté(s)")
    if n["jours_partiels"]:
        parts.append(f"{n['jours_partiels']} jour(s) calculé(s) sans tous leurs symboles")
    if n["absents"]:
        parts.append(f"{n['absents']} valeur(s) de l'univers sans trade")
    return "".join(f" · {p}" for p in parts)


def _jours(n: int) -> str:
    return f"{n} jour{'s' if n > 1 else ''}"


def _rattrapage_text(left: dict) -> str:
    """Relecture W6 : jours du rejeu à rattraper et jours du direct en attente de calcul, à part. En actions, chaque
    jour du direct passe par le rattrapage (journalier de la séance suivante) : « rattrapage : 1 jours actions » à côté
    de « n° 10 : complète » se contredisaient à la lecture."""
    replay = {m: (left.get(m) or {}).get("replay", 0) for m in ("stock", "crypto")}
    live = {m: (left.get(m) or {}).get("live", 0) for m in ("stock", "crypto")}
    out = ""
    if any(replay.values()):
        out += f" · rattrapage : {_jours(replay['stock'])} actions, {_jours(replay['crypto'])} crypto"
    if any(live.values()):
        out += " · direct en attente de calcul : " + ", ".join(f"{_jours(n)} {MARKET_FR[m]}" for m, n in live.items() if n)
    return out


def text(st: dict) -> str:
    """Résumé en quelques lignes pour Discord."""
    if not st["exists"]:
        return "🧪 **Labo** : pas encore démarré (aucune base). `docker compose up -d` lance le service jarvis-lab."
    lines = [f"🧪 **Labo de performance** · méthode {st.get('method_version') or 'n/d'} · base {st['db_size_mb']:g} Mo"]
    from app.lab import empreinte as E  # import tardif, comme stats

    pause = E.guard_text(st.get("garde_v1"))
    if pause:  # garde du Labo 1 : rien de ce qui suit n'avance côté v1
        lines.append(f"{pause} : rejeu et direct arrêtés, aucune ligne v1 écrite ; Labo 2, rattrapage et grade continuent")
    decl = st.get("declaration") or {}
    arch = archive_line(st.get("archive_audit"), decl.get("lignes_v2", decl.get("numero")))
    if arch:  # relecture W1 : sans elle, le rattrapage arrêté n'avait aucune cause visible
        lines.append(arch)
    for market in ("stock", "crypto"):
        r = st["replay"].get(market) or {}
        t = st["trades"].get(market) or {}
        live = (st["live"].get(market) or {}).get("last_day")
        lines.append(f"{MARKET_FR[market].capitalize()} : rejeu {r.get('pct', 0):g} % ({r.get('days_done', 0)}/"
                     f"{r.get('days_total', 0)} jours) · {t.get('replay', 0)} trades rejoués, {t.get('live', 0)} en direct"
                     + (f" · dernier jour en direct {live}" if live else "") + _gaps_text(r, st["live"].get(market)))
    mixes = st.get("revisions_melangees") or {}
    for version, revs in (st.get("revisions_code") or {}).items():
        if mixes.get(version):  # une même version, plusieurs comportements (pas seulement plusieurs sources)
            lines.append(f"⚠️ Méthode {version} calculée par {len(revs)} révisions du code : "
                         + ", ".join(f"{rev} {n} trades" for rev, n in revs.items()) + " · " + ", ".join(mixes[version]))
    for r in (st.get("recalcul_v1") or {}).values():
        if isinstance(r, dict) and r.get("etat") == "non_recalculable":
            lines.append(f"⚠️ Séance du direct {MARKET_FR.get(r.get('marche'), r.get('marche'))} du {_date_fr(r['jour'])} "
                         f"non recalculable ({r.get('raison')}) : ses trades restent ceux d'avant {r.get('avant')}")
    l2 = st.get("labo2") or {}
    stt = l2.get("stats")
    if stt and stt.get("esperance_r") is not None:
        ci = stt.get("ic95")
        lines.append(f"Espérance {_fr(stt['esperance_r'], 3)} R" + (f" [{_fr(ci[0])} ; {_fr(ci[1])}]" if ci else "")
                     + f" sur {stt.get('declenches') or 0} trades déclenchés (méthode {stt.get('version') or 'n/d'})")
    fiches, regles = l2.get("fiches"), l2.get("regles") or {}
    if fiches is not None or regles:
        f = fiches or {}
        detail = (f" ({f.get('a_classer', 0)} à classer, {f.get('actus_a_chercher', 0)} en attente d'actus, "
                  f"{f.get('sans_actus', 0)} sans actus)") if f.get("total") else ""
        lines.append(f"Fiches catalyseurs : {f.get('total', 0)}{detail} · règles : {regles.get('en_attente', 0)} "
                     f"en attente, {regles.get('validee', 0) + regles.get('rejetee', 0)} testées")
    v2 = st.get("labo_v2")
    if v2 and (v2.get("lignes") or any(sum(r.values()) for r in (v2.get("rattrapage") or {}).values())):
        lines.append(f"Labo v2 : {sum((v2.get('lignes') or {}).values())} lignes de stratégies (`!strategies`)"
                     + _rattrapage_text(v2.get("rattrapage") or {}))
    if st.get("declaration"):  # 2e tour d'avis (C4)
        lines.append(declaration_line(st["declaration"]))
    if st.get("archive_actus"):
        from app.lab import archive as ARC

        lines.append(ARC.line(st["archive_actus"]))
    if st.get("visibilite_finnhub"):
        from app.trading import visibilite as VIS

        lines.append(VIS.line(st["visibilite_finnhub"]))
    sc = st.get("scelle")
    if sc:
        n = sum((sc.get("trades") or {}).values())
        lines.append(f"🔒 Bloc scellé : rejeu du {_date_fr(sc['du'])} au {_date_fr(sc['au'])}, "
                     f"{n} trades calculés, jamais montrés")
    if st.get("methode"):
        from app.lab import method as M

        lines.append(M.state_line(st["methode"]))
    w = st.get("worker")
    if w:
        ago = w.get("il_y_a_s") or 0
        when = "à l'instant" if ago < 60 else f"il y a {ago // 60} min" if ago < 3600 else f"il y a {ago / 3600:.1f} h".replace(".", ",")
        lines.append(("⚠️ Worker muet" if ago >= WORKER_SILENT_S else "Worker") + f" : {w.get('etape') or 'étape inconnue'}"
                     + f" · dernier signe de vie {when}")
    if st.get("error"):
        lines.append(f"⚠️ {st['error']}")
    return "\n".join(lines)
