"""Potentiel +10 : ce que la mémoire des catalyseurs dit avant l'ouverture (lot B, 8 octobre 2026).

« Potentiel +10 » (vocabulaire de Simão) : une action capable de faire environ +10 % en intraday, depuis
l'ouverture. C'est une zone, pas un seuil : on compte les séances dont le plus haut atteint +8 % au-dessus
de l'ouverture, et on montre aussi +10 %.

Deux lectures, sans rien filtrer ni noter (rien ici ne change un plan) :
- par gap d'ouverture (table day_moves : chaque valeur de l'univers du labo, chaque séance rejouée ou en
  direct) : sur les séances ouvertes avec un gap de +2 à +5 %, quelle part a touché +8 % depuis
  l'ouverture ? Le gap est la seule mesure de day_moves connue avant l'ouverture (le volume relatif des
  30 premières minutes, non) ; le scanner, lui, voit le gap de pré-ouverture à 9 h 10, une approximation
  du gap à l'ouverture ;
- par type de catalyseur (table catalysts, types posés le soir par la routine) : mouvements de ±8 % en
  hausse et en baisse, amplitude médiane, et contre-exemples (même type de news, pas de mouvement). Les
  contre-exemples sont tirés au sort (quelques valeurs un jour sur cinq) : les comptes ne sont pas des
  probabilités, ils disent seulement si un type de news fait souvent bouger.

Limite connue au 8 octobre 2026 : Finnhub gratuit ne remonte qu'environ 12 mois, et le rejeu en est encore
au début 2025 : presque toutes les fiches sont « hors profondeur » et une trentaine seulement sont classées.
Les types se rempliront quand le rejeu atteindra les 12 derniers mois ; le gap, lui, est mesuré dès
maintenant sur tous les jours rejoués.

Le worker recalcule quand day_moves ou les types changent, garde le résultat en cache (stats_cache,
clé « potentiel ») et écrit lab_potentiel.json, lu par le scanner en direct (une ligne 🎯 par plan).
"""
import json
import logging
import os
from pathlib import Path
from statistics import median

from app.lab import stats as STATS
from app.lab.db import LabDB, db_path, now_iso

log = logging.getLogger(__name__)
CACHE_KEY = "potentiel"
FILE = "lab_potentiel.json"
ZONE_PCT = 8.0
PLUS10_PCT = 10.0
MIN_N = 30
GAP_BUCKETS = ((-1e9, -5.0, "gap ≤ −5 %"), (-5.0, -2.0, "gap −5 à −2 %"), (-2.0, -0.5, "gap −2 à −0,5 %"),
               (-0.5, 0.5, "gap ±0,5 %"), (0.5, 2.0, "gap +0,5 à +2 %"), (2.0, 5.0, "gap +2 à +5 %"),
               (5.0, 10.0, "gap +5 à +10 %"), (10.0, 1e9, "gap ≥ +10 %"))
_cache: dict = {"key": None, "data": None}


def gap_bucket(gap: float | None) -> str | None:
    if gap is None:
        return None
    return next(label for lo, hi, label in GAP_BUCKETS if lo <= gap < hi)


def _pct(k: int, n: int) -> float | None:
    return round(100 * k / n, 1) if n else None


def _cell(ups: list[float]) -> dict:
    n = len(ups)
    return {"n": n, "zone_pct": _pct(sum(u >= ZONE_PCT for u in ups), n),
            "plus10_pct": _pct(sum(u >= PLUS10_PCT for u in ups), n),
            "hausse_mediane_pct": round(median(ups), 2) if ups else None, "echantillon_faible": n < MIN_N}


def by_gap(conn) -> dict:
    """Part des séances qui touchent +8 % (et +10 %) depuis l'ouverture, par gap d'ouverture (actions)."""
    cells: dict[str, list[float]] = {}
    days = set()
    for day, gap, up in conn.execute("SELECT day, gap_pct, up_pct FROM day_moves WHERE market = 'stock' "
                                     "AND gap_pct IS NOT NULL AND up_pct IS NOT NULL"):
        cells.setdefault(gap_bucket(gap), []).append(up)
        days.add(day)
    every = [u for ups in cells.values() for u in ups]
    return {"ensemble": _cell(every), "jours": len(days), "du": min(days) if days else None,
            "au": max(days) if days else None,
            "par_gap": {label: _cell(cells[label]) for _, _, label in GAP_BUCKETS if label in cells}}


def by_type(conn) -> dict:
    """Fiches catalyseurs classées, par type : mouvements en hausse et en baisse, contre-exemples."""
    out: dict = {}
    for kind, typ, direction, amp in conn.execute(
            "SELECT kind, catalyst_type, direction, amplitude_pct FROM catalysts "
            "WHERE market = 'stock' AND catalyst_type IS NOT NULL"):
        t = out.setdefault(typ, {"hausse": 0, "baisse": 0, "contre_exemples": 0, "_amp": []})
        if kind == "contre_exemple":
            t["contre_exemples"] += 1
            continue
        t["hausse" if direction == "hausse" else "baisse"] += 1
        if amp is not None:
            t["_amp"].append(abs(amp))
    for t in out.values():
        amps = t.pop("_amp")
        t["amplitude_mediane_pct"] = round(median(amps), 2) if amps else None
    status = dict(conn.execute("SELECT COALESCE(news_status, '?'), COUNT(*) FROM catalysts WHERE market = 'stock' "
                               "AND kind = 'mouvement' GROUP BY 1").fetchall())
    classees = conn.execute("SELECT COUNT(*) FROM catalysts WHERE market = 'stock' AND catalyst_type IS NOT NULL "
                            "AND kind = 'mouvement'").fetchone()[0]
    return {"par_type": dict(sorted(out.items(), key=lambda kv: -(kv[1]["hausse"] + kv[1]["baisse"]))),
            "fiches": {"mouvements": sum(status.values()), "classees": classees, "actus": status}}


def report(conn) -> dict:
    return {"zone_pct": ZONE_PCT, "plus10_pct": PLUS10_PCT, "min_n": MIN_N, **by_gap(conn), **by_type(conn)}


def signature(conn) -> list:
    moves = conn.execute("SELECT COUNT(*), COALESCE(MAX(day), '') FROM day_moves WHERE market = 'stock'").fetchone()
    typed = conn.execute("SELECT COUNT(*), COALESCE(MAX(classified_at), '') FROM catalysts "
                         "WHERE catalyst_type IS NOT NULL").fetchone()
    return [*moves, *typed]


def cached(conn) -> dict | None:
    return STATS.cached(conn, CACHE_KEY)


# --- Fichier lu en direct par le scanner -----------------------------------------------------

def path(db_file: str | None = None) -> Path:
    override = os.getenv("JARVIS_LAB_POTENTIEL", "").strip()
    return Path(override) if override else Path(db_file or db_path()).parent / FILE


def step(db: LabDB) -> int | None:
    """Passage du worker : recalcule si day_moves ou les types ont changé ; renvoie le nombre de séances."""
    sig = signature(db.conn)
    if sig == db.get("potentiel", "signature") and cached(db.conn):
        return None
    data = report(db.conn)
    with db.transaction():
        STATS.store(db.conn, CACHE_KEY, data, data["ensemble"]["n"], now_iso())
        db.set("potentiel", "signature", sig)
    if db.path != ":memory:":
        from app.lab.method import _write_atomic

        _write_atomic(path(db.path), {"par_gap": data["par_gap"], "jours": data["jours"], "du": data["du"],
                                      "au": data["au"], "ecrit_le": now_iso()})
    return data["ensemble"]["n"]


def current() -> dict | None:
    """Contenu de lab_potentiel.json (relu quand il change) ; None s'il manque ou est illisible."""
    try:
        p = path()
        st = p.stat()
        key = (str(p), st.st_mtime_ns, st.st_size)
        if _cache["key"] != key:
            _cache.update(key=key, data=json.loads(p.read_text(encoding="utf-8")))
        return _cache["data"]
    except FileNotFoundError:
        return None
    except Exception as exc:
        log.warning("Potentiel +10 illisible (%s: %s)", type(exc).__name__, exc)
        return None


def _fr(x: float) -> str:
    return f"{x:g}".replace(".", ",")


def hint(gap_pct: float | None, data: dict | None = None) -> str | None:
    """Ligne du scanner : la part historique des séances à ce gap qui ont touché +8 % depuis l'ouverture.
    None si le fichier manque ou si la tranche a moins de MIN_N séances."""
    data = data if data is not None else current()
    label = gap_bucket(gap_pct)
    cell = ((data or {}).get("par_gap") or {}).get(label) if label else None
    if not cell or cell.get("n", 0) < MIN_N or cell.get("zone_pct") is None:
        return None
    return (f"🎯 Potentiel +10 : {_fr(cell['zone_pct'])} % des séances à {label} ont touché +8 % depuis "
            f"l'ouverture ({_fr(cell['plus10_pct'])} % +10 %), sur {cell['n']} séances du labo")


def text(rep: dict | None) -> str:
    """`!potentiel` sur Discord."""
    if not rep:
        return "🎯 **Potentiel +10** : pas encore calculé (le worker du labo ne l'a pas encore mis en cache)."
    base = rep.get("ensemble") or {}
    if base.get("n"):
        lines = [f"🎯 **Potentiel +10** (actions de l'univers du labo, {rep['jours']} jours du {rep['du']} au "
                 f"{rep['au']}) : {_fr(base['zone_pct'])} % des séances touchent +8 % depuis l'ouverture, "
                 f"{_fr(base['plus10_pct'])} % +10 %", "**Par gap d'ouverture**"]
    else:
        lines = ["🎯 **Potentiel +10** : pas encore de séance mesurée (mouvements du jour pas encore calculés)."]
    for label, c in (rep.get("par_gap") or {}).items():
        lines.append(f"{label} : {_fr(c['zone_pct'])} % ≥ +8 %, {_fr(c['plus10_pct'])} % ≥ +10 % · {c['n']} séances"
                     + (" · échantillon faible" if c["echantillon_faible"] else ""))
    f = rep.get("fiches") or {}
    lines.append(f"**Par type de catalyseur** ({f.get('classees', 0)} fiches classées sur {f.get('mouvements', 0)} "
                 f"mouvements ; actus hors profondeur Finnhub : {(f.get('actus') or {}).get('hors_profondeur', 0)})")
    types = rep.get("par_type") or {}
    if not types:
        lines.append("Aucun type encore : les fiches se classent le soir, et Finnhub ne remonte qu'environ 12 mois.")
    for typ, t in list(types.items())[:12]:
        amp = f" · amplitude médiane {_fr(t['amplitude_mediane_pct'])} %" if t["amplitude_mediane_pct"] is not None else ""
        lines.append(f"{typ} : {t['hausse']} hausses, {t['baisse']} baisses{amp} · {t['contre_exemples']} contre-exemples")
    lines.append("-# Mesure seulement : rien ici ne filtre ni ne note un plan.")
    return "\n".join(lines)
