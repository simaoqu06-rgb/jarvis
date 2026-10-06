"""Méthode championne appliquée à JARVIS en direct (scanner pré-ouverture).

Le worker du labo écrit lab_champion.json (version et filtres du champion) ; JARVIS le relit quand il
change. Un plan qu'une règle du champion écarte n'est pas proposé : il reste visible dans le message
du scanner (« écartées par la méthode vN ») et dans data/scans.jsonl. Le fichier absent, illisible
ou sans filtre : aucun filtre, une seule ligne de journal ; une erreur ici ne casse jamais le scanner.

Les règles s'évaluent avec le SQL du labo (method.evaluate_rows) sur une ligne aux mêmes champs que
le journal du labo (generator.py) :
- plan.* : le plan compact (generator.compact_plan), avec la note et le verdict recalculés SANS la
  part catalyseur, comme au labo (le catalyseur n'y est pas rejoué) ; composite et decision idem ;
- context.setup, target_sources (du plan), rvol et day_change_pct (universe._metrics sur les séances
  closes, comme l'univers du labo), regime (universe.regime sur SPY), catalyst_replayed = false ;
- regime, regime_trend, regime_vol_tercile : regime.series sur ~500 jours de SPY (table regimes du labo) ;
- weekday, hour_utc, market, symbol, side : du jour et de l'heure du plan.
Champs du labo absents en direct (traités comme « non vérifiés », COALESCE … 0) : universe_reason,
universe_rank, dollar_volume et gap_open_pct (de toute façon interdits dans les règles), sector (vide
partout). Différence assumée : la note du direct utilise les réglages du moment, le labo ceux figés
dans la version de génération.
"""
import json
import logging
from datetime import datetime, timezone

from app.lab import method as M

log = logging.getLogger(__name__)
_cache: dict = {"key": None, "data": None}
_warned: set[str] = set()
EMPTY = {"version": None, "regles": []}


def _warn_once(key: str, msg: str, *args) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning(msg, *args)


def current() -> dict:
    """Champion en place ({version, regles}) ; EMPTY si le fichier manque ou est illisible."""
    try:
        path = M.champion_path()
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
        if _cache["key"] != key:
            data = json.loads(path.read_text(encoding="utf-8"))
            rules = [r for r in data.get("regles") or [] if isinstance(r, dict) and "cond" in r and "kind" in r]
            for r in rules:
                M.rule_sql(r)  # règle illisible : erreur ici, pas au moment du scanner
            _cache.update(key=key, data={"version": data.get("version"), "regles": rules})
            _warned.discard("lecture")
        return _cache["data"]
    except FileNotFoundError:
        _warn_once("absent", "Méthode du labo : %s absent, aucun filtre en direct", M.champion_path())
    except Exception as exc:
        _warn_once("lecture", "Méthode du labo illisible (%s: %s), aucun filtre en direct", type(exc).__name__, exc)
    return EMPTY


def _done(daily: list[dict] | None, today: str) -> list[dict]:
    from app.lab import collector as CO

    return [r for r in daily or [] if CO.ny_day(r["t"]).isoformat() < today]


def stock_row(plan: dict, daily_score: list[dict] | None, daily: list[dict] | None, index_daily: list[dict] | None,
              now: datetime, s) -> dict:
    """Ligne « journal du labo » d'un plan d'action du scanner (voir le docstring du module)."""
    from app.lab import collector as CO
    from app.lab import generator as G
    from app.lab import regime as REG
    from app.lab import universe as U
    from app.trading import score as SC

    day = now.astimezone(CO.NEW_YORK).date()
    today = day.isoformat()
    lab_plan = plan | {"catalyst": None}
    lab_plan["score"] = SC.evaluate(lab_plan, daily_score, None, s)
    done = _done(daily, today)
    metrics = (U._metrics(done) if len(done) >= 2 else None) or {}
    cutoff = CO.ny_midnight(day)
    regime = U.regime(index_daily or [], cutoff, G.ANNUAL_DAYS["stock"]) if index_daily else None
    label = REG.series(index_daily, "stock", [day])[today] if index_daily else {}
    context = {"setup": G.setup_type(plan), "target_sources": [t["source"] for t in plan["targets"]],
               "regime": regime, "rvol": metrics.get("rvol"), "day_change_pct": metrics.get("change_pct"),
               "sector": None, "catalyst_replayed": False}
    return {"market": "stock", "symbol": plan["symbol"], "side": plan["side"], "day": today,
            "planned_at": int(now.timestamp()), "plan": G.compact_plan(lab_plan), "context": context,
            "composite": lab_plan["score"]["composite"], "decision": lab_plan["score"]["decision"],
            "regime_label": label.get("label"), "regime_trend": label.get("trend"),
            "regime_vol_tercile": label.get("vol_tercile")}


def check(rows: list[dict], champ: dict | None = None) -> list[dict]:
    """Verdict du champion pour chaque ligne ; en cas d'erreur, tout est gardé (une ligne de journal)."""
    champ = champ or current()
    try:
        out = M.evaluate_rows(champ["regles"], rows)
    except Exception as exc:
        _warn_once("eval", "Méthode du labo : évaluation impossible (%s: %s), aucun filtre", type(exc).__name__, exc)
        out = [{"garde": True, "regles": []} for _ in rows]
    return [o | {"version": champ.get("version")} for o in out]


def verdict_for(plan: dict, daily_score, daily, index_daily, now: datetime | None, s, champ: dict | None = None) -> dict:
    """Verdict d'un plan d'action du scanner ; jamais d'exception (plan gardé en cas de souci)."""
    champ = champ or current()
    if not champ["regles"]:
        return {"garde": True, "regles": [], "version": champ.get("version")}
    try:
        row = stock_row(plan, daily_score, daily, index_daily, now or datetime.now(timezone.utc), s)
    except Exception as exc:
        _warn_once("ligne", "Méthode du labo : contexte du plan impossible (%s: %s), plan gardé", type(exc).__name__, exc)
        return {"garde": True, "regles": [], "version": champ.get("version")}
    return check([row], champ)[0]
