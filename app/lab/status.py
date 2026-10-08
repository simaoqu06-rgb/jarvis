"""État du labo en lecture seule (route /api/lab/status et commande Discord !labo).

Avancement du rejeu, trades par marché et par origine, dernier jour en direct, taille de la base,
étape en cours du worker et son dernier signe de vie (lab.heartbeat), et quelques chiffres du Labo 2 lus dans le cache du worker : espérance en R avec son intervalle de
confiance, fiches catalyseurs, règles en attente et testées.
"""
import json
import sqlite3
from datetime import date
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
    "Catalyseur (actus, résultats) non rejoué : note composite sans sa part catalyseur.",
    "Univers actions approché (volume, variation et volume relatif de la veille) : le screener Alpaca "
    "du direct ne se rejoue pas. Killmode n'y voit qu'une partie des actions en gap ≥ 4 % : la part est "
    "comptée dans le tableau des stratégies (couverture).",
    "Actions : bougies ajustées des splits connus aujourd'hui ; avant un regroupement d'actions, le prix "
    "paraît plus élevé, d'où un glissement, une commission en R et un pas de cotation sous-estimés sur les "
    "petites valeurs (l'inverse avant une division).",
    "Confirmation des GO (n° 7) : le plan au stop plancher du direct n'existe que pour les jours rejoués depuis "
    "la n° 7 (le rattrapage n'a pas l'historique de 15 min pour le refaire) ; les GO du labo ne passent ni par "
    "les filtres du scanner, ni par le catalyseur, ni par les leçons.",
]


def _progress(conn: sqlite3.Connection, job: str) -> dict:
    return {k: json.loads(v) for k, v in conn.execute("SELECT key, value FROM progress WHERE job = ?", (job,))}


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
            done = _days_between(market, p["start"], p["last_day"]) if p.get("start") and p.get("last_day") else 0
            out["replay"][market] = {"start": p.get("start"), "end": p.get("end"), "last_day": p.get("last_day"),
                                     "days_done": done, "days_total": total,
                                     "pct": round(100 * done / total, 1) if total else 0.0}
            out["live"][market] = {"last_day": _progress(conn, f"live:{market}").get("last_day")}
        for market, origin, n in conn.execute("SELECT market, origin, COUNT(*) FROM trades GROUP BY market, origin"):
            out["trades"].setdefault(market, {})[origin] = n
        out["scelle"] = {**SEAL.describe(), "trades": {}}
        try:  # base d'avant le scellé ouverte en lecture seule : table absente
            for market, n in conn.execute("SELECT market, COUNT(*) FROM trades_sealed GROUP BY market"):
                out["scelle"]["trades"][market] = n
        except sqlite3.Error:
            pass
        out["labo_v2"] = labo_v2(conn)
        from app.lab import method as M  # import tardif, comme runner

        out["methode"] = M.state(conn)
        out["method_version"] = out["methode"]["champion"]["version"]
        live_start = conn.execute("SELECT value FROM progress WHERE job = 'lab' AND key = 'live_start'").fetchone()
        out["live_start"] = json.loads(live_start[0]) if live_start else None
        out["labo2"] = headline(conn)
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


def text(st: dict) -> str:
    """Résumé en quelques lignes pour Discord."""
    if not st["exists"]:
        return "🧪 **Labo** : pas encore démarré (aucune base). `docker compose up -d` lance le service jarvis-lab."
    lines = [f"🧪 **Labo de performance** · méthode {st.get('method_version') or 'n/d'} · base {st['db_size_mb']:g} Mo"]
    for market in ("stock", "crypto"):
        r = st["replay"].get(market) or {}
        t = st["trades"].get(market) or {}
        live = (st["live"].get(market) or {}).get("last_day")
        lines.append(f"{MARKET_FR[market].capitalize()} : rejeu {r.get('pct', 0):g} % ({r.get('days_done', 0)}/"
                     f"{r.get('days_total', 0)} jours) · {t.get('replay', 0)} trades rejoués, {t.get('live', 0)} en direct"
                     + (f" · dernier jour en direct {live}" if live else ""))
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
        left = {m: sum(r.values()) for m, r in (v2.get("rattrapage") or {}).items()}
        lines.append(f"Labo v2 : {sum((v2.get('lignes') or {}).values())} lignes de stratégies (`!strategies`)"
                     + (f" · rattrapage : {left.get('stock', 0)} jours actions, {left.get('crypto', 0)} jours crypto"
                        if any(left.values()) else ""))
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
