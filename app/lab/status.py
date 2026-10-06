"""État du labo en lecture seule (route /api/lab/status et commande Discord !labo).

Seulement des comptes : avancement du rejeu, trades par marché et par origine, dernier jour en
direct, taille de la base. Les statistiques viendront avec le Labo 2.
"""
import json
import sqlite3
from datetime import date
from pathlib import Path

from app.lab.db import db_path, open_readonly

MARKET_FR = {"stock": "actions", "crypto": "crypto"}
LIMITATIONS = [
    "Crypto : OKX ne liste que les paires cotées aujourd'hui ; les paires retirées pendant la période "
    "rejouée manquent à l'univers (biais du survivant, résultats crypto du rejeu probablement flattés).",
    "Actions : seules les valeurs retirées de la cote encore connues d'Alpaca sont dans l'univers.",
    "Catalyseur (actus, résultats) non rejoué : note composite sans sa part catalyseur.",
    "Univers actions approché (volume, variation et volume relatif de la veille) : le screener Alpaca "
    "du direct ne se rejoue pas.",
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
           "replay": {}, "trades": {}, "live": {}, "method_version": None, "limitations": LIMITATIONS}
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
        row = conn.execute("SELECT version FROM method_versions ORDER BY created_at DESC, version DESC LIMIT 1").fetchone()
        out["method_version"] = row[0] if row else None
        live_start = conn.execute("SELECT value FROM progress WHERE job = 'lab' AND key = 'live_start'").fetchone()
        out["live_start"] = json.loads(live_start[0]) if live_start else None
    except sqlite3.Error as exc:
        out["error"] = f"base illisible : {exc}"
    finally:
        conn.close()
    return out


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
    if st.get("error"):
        lines.append(f"⚠️ {st['error']}")
    return "\n".join(lines)
