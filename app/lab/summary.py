"""Résumé du soir du labo : un JSON compact (< ~8 Ko) et quelques lignes en français, pour la routine
Claude du soir (Labo 3). Construit et gardé une fois par jour par le worker (table summaries),
après la clôture US (22 h 30 à Paris, et au moins 30 min après la clôture du jour selon le calendrier
NYSE, jours courts compris) ; la crypto y figure aussi, y compris le week-end.

Contenu : chiffres du jour (dernière séance close pour les actions, dernier jour UTC complet pour la
crypto) et cumulés (version de méthode en cours), tranches qui se dégradent (statistiquement
nettes seulement : au moins 30 trades et z ≤ -3,2 contre le reste du marché), mouvements du
potentiel +10 du jour avec leurs fiches catalyseurs, idées déjà rejetées (à ne pas reproposer).
"""
import json
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.lab import collector as CO
from app.lab import stats as STATS
from app.lab import validator as V
from app.lab.db import LabDB, now_iso

PARIS = ZoneInfo("Europe/Paris")
SUMMARY_AT = time(22, 30)
MAX_BYTES = 8000
MAX_MOVERS = 10
MAX_NEWS = 3
MAX_DEGRADATIONS = 8
MAX_REJECTED = 30


def due(now: datetime, last_day: str | None) -> str | None:
    """Jour (Paris) du résumé à construire maintenant, ou None."""
    local = now.astimezone(PARIS)
    if local.time() < SUMMARY_AT:
        return None
    day = local.date()
    if last_day == day.isoformat():
        return None
    bounds = CO.session_ts(day)
    if bounds and now.timestamp() < bounds[1] + 30 * 60:
        return None
    return day.isoformat()


def stock_day(day: date) -> date:
    return day if CO.session_ts(day) else CO.previous_trading_day(day)


def compact(m: dict | None) -> dict | None:
    if not m:
        return None
    return {k: m.get(k) for k in ("plans", "declenches", "esperance_r", "ic95", "reussite", "tp1_avant_stop",
                                  "delais_tenus", "echantillon_faible")}


def _movers(conn, market: str, day: str, version: str | None) -> list[dict]:
    rows = conn.execute(
        "SELECT symbol, up_pct, down_pct, gap_pct, t_high FROM day_moves WHERE market = ? AND day = ? "
        "AND up_pct >= ? ORDER BY up_pct DESC LIMIT ?", (market, day, STATS.MOVE_PCT, MAX_MOVERS)).fetchall()
    out = []
    for symbol, up, down, gap, t_high in rows:
        vcond = " AND method_version = ?" if version else ""
        params = (market, day, symbol, *((version,) if version else ()))
        plan = conn.execute(
            "SELECT COUNT(*), MAX(triggered), MAX(tp1_hit), MAX(r_net), MAX(composite) FROM trades "
            f"WHERE market = ? AND day = ? AND symbol = ?{vcond}", params).fetchone()
        cat = conn.execute("SELECT id, news, news_status, catalyst_type, start_ts FROM catalysts "
                           "WHERE market = ? AND day = ? AND symbol = ?", (market, day, symbol)).fetchone()
        news = []
        if cat:
            items = json.loads(cat[1] or "[]")
            for n in items[:MAX_NEWS]:
                news.append({"titre": (n.get("titre") or "")[:110], "heure": n.get("publie_a"),
                             "position": n.get("position"), "source": n.get("source")})
        out.append({"marche": market, "symbole": symbol, "hausse_pct": round(up, 1),
                    "gap_pct": round(gap, 1) if gap is not None else None,
                    "heure_haut": datetime.fromtimestamp(t_high, CO.NEW_YORK if market == "stock" else timezone.utc)
                    .strftime("%H:%M") if t_high else None,
                    "plan": bool(plan[0]), "declenche": bool(plan[1]), "tp1": bool(plan[2]),
                    "meilleur_r": round(plan[3], 2) if plan[3] is not None else None,
                    "note": plan[4], "fiche": cat[0] if cat else None,
                    "actus_etat": cat[2] if cat else None, "type": (cat[3] if cat else None) or "à classer",
                    "actus": news})
    return out


def _methode(conn) -> dict:
    """Labo 3 : méthode en place et challenger (les chiffres ci-dessus portent sur tous les plans de la
    version de génération, filtres de la méthode non appliqués)."""
    from app.lab import method as M

    champ, chall = M.champion(conn), M.challenger(conn)
    return {"en_place": champ["version"], "filtres": [r["nom"] for r in champ["regles"]],
            "challenger": chall["version"] if chall else None}


def build(conn, day: str, full: dict | None = None) -> dict:
    """Résumé du jour `day` (date de Paris). `full` : statistiques cumulées déjà calculées (version en cours)."""
    d = date.fromisoformat(day)
    version = V.current_version(conn)
    sd, cd = stock_day(d).isoformat(), (d - timedelta(days=1)).isoformat()
    if full is None:
        full = STATS.compute(conn, version=version) if version else {"marches": {}, "degradations": []}
    today = {}
    for market, mday in (("stock", sd), ("crypto", cd)):
        st = STATS.compute(conn, version=version, market=market, since=mday, until=mday)
        today[market] = {"jour": mday, **(compact(st["global"]) or {}),
                         "capture": {k: st["capture_potentiel"][k] for k in ("mouvements", "tp1")}}
    cumul = {m: compact(v["mesures"]) | {"baisse_max_r": v["mesures"].get("baisse_max_r"),
                                          "pente_note": v["calibration_note"].get("pente_r_par_point")}
             for m, v in (full.get("marches") or {}).items()}
    capture = full.get("capture_potentiel")
    data = {
        "jour": day, "genere_le": now_iso(), "version": version,
        "jour_chiffres": today, "cumul": cumul,
        "capture_cumulee": {k: capture[k] for k in ("mouvements", "avec_plan", "tp1", "taux_capture")} if capture else None,
        "degradations": [{k: g[k] for k in ("marche", "dimension", "valeur", "n", "esperance_r", "ecart_r", "z")}
                         for g in (full.get("degradations") or [])[:MAX_DEGRADATIONS]],
        "potentiel_10": _movers(conn, "stock", sd, version) + _movers(conn, "crypto", cd, version),
        "regles": V.counts(conn),
        "methode": _methode(conn),
        "idees_rejetees": [{"empreinte": r["empreinte"], "nom": r["nom"], "raison": (r["raison"] or "")[:120],
                            "regle": json.dumps(r["regle"], ensure_ascii=False, separators=(",", ":"))[:200]}
                           for r in V.rejected(conn, MAX_REJECTED)],
    }
    return shrink(data)


def size(data: dict) -> int:
    return len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode())


def shrink(data: dict, limit: int = MAX_BYTES) -> dict:
    """Retire des éléments de fin de liste (idées rejetées, puis actus, puis mouvements, puis
    dégradations) jusqu'à tenir dans `limit` octets ; la troncature est signalée."""
    cut = False
    while size(data) > limit:
        if len(data["idees_rejetees"]) > 5:
            data["idees_rejetees"].pop()
        elif any(len(m["actus"]) > 1 for m in data["potentiel_10"]):
            for m in data["potentiel_10"]:
                if len(m["actus"]) > 1:
                    m["actus"].pop()
        elif len(data["potentiel_10"]) > 3:
            data["potentiel_10"].pop()
        elif len(data["degradations"]) > 2:
            data["degradations"].pop()
        elif data["idees_rejetees"]:
            data["idees_rejetees"].pop()
        else:
            break
        cut = True
    if cut:
        data["tronque"] = True
    return data


def _r(x) -> str:
    return "n/d" if x is None else f"{x:+.3f}".replace(".", ",") + " R"


def text(data: dict) -> str:
    """Rendu court en français."""
    lines = [f"🧪 Labo · résumé du {data['jour']} · méthode {data.get('version') or 'n/d'}"]
    for market, label in (("stock", "Actions"), ("crypto", "Crypto")):
        t = data["jour_chiffres"].get(market) or {}
        c = data["cumul"].get(market)
        line = f"{label} ({t.get('jour')}) : {t.get('declenches') or 0} trades déclenchés, espérance {_r(t.get('esperance_r'))}"
        if c:
            ci = c.get("ic95")
            line += f" · cumul {c.get('declenches')} trades, {_r(c.get('esperance_r'))}"
            if ci:
                line += f" [{ci[0]:+.2f} ; {ci[1]:+.2f}]".replace(".", ",")
        lines.append(line)
    if data["degradations"]:
        lines.append("Se dégradent : " + " ; ".join(
            f"{g['marche']} {g['dimension']}={g['valeur']} ({_r(g['esperance_r'])}, z {g['z']:g})"
            for g in data["degradations"][:4]))
    movers = data["potentiel_10"]
    if movers:
        lines.append("Potentiel +10 : " + ", ".join(
            f"{m['symbole']} +{m['hausse_pct']:g} %" + (" (TP1)" if m["tp1"] else " (plan)" if m["plan"] else "")
            for m in movers[:6]))
    rg = data["regles"]
    lines.append(f"Règles : {rg.get('en_attente', 0)} en attente, {rg.get('validee', 0)} validées, "
                 f"{rg.get('rejetee', 0)} rejetées ({rg.get('essais', 0)} essais)")
    return "\n".join(lines)


def store(db: LabDB, data: dict, txt: str) -> None:
    db.conn.execute("INSERT OR REPLACE INTO summaries VALUES (?, ?, ?, ?)",
                    (data["jour"], data["genere_le"], json.dumps(data, ensure_ascii=False), txt))


def latest(conn, day: str | None = None) -> dict | None:
    try:
        sql = "SELECT day, created_at, data, text FROM summaries"
        row = conn.execute(sql + (" WHERE day = ?" if day else " ORDER BY day DESC LIMIT 1"),
                           (day,) if day else ()).fetchone()
    except Exception:
        return None
    if not row:
        return None
    return {"jour": row[0], "genere_le": row[1], "resume": json.loads(row[2]), "texte": row[3]}
