"""Méthode championne appliquée à JARVIS en direct (scanner pré-ouverture et tous les plans).

Le worker du labo écrit lab_champion.json (version et filtres du champion) ; JARVIS le relit quand il
change. Un plan qu'une règle du champion écarte n'est pas proposé : il reste visible dans le message
du scanner (« écartées par la méthode vN ») et dans data/scans.jsonl. Le fichier absent, illisible
ou sans filtre : aucun filtre, une seule ligne de journal ; une erreur ici ne casse jamais le scanner.
Hors scanner (`!plan`, `!prends`, API, plans crypto), plan_verdict évalue le même filtre (service.build_plan) :
le grade est mesuré sur les trades que garde le champion, un plan écarté ne reçoit donc pas son risque
(risque proposé 0 %, « écarté par la méthode vN »), il reste affiché. Exception : `!prends … go`, dont le GO
confirmé a déjà été gardé au scanner (le plan recalculé au déclenchement n'est pas refiltré).

Les règles s'évaluent avec le SQL du labo (method.evaluate_rows) sur une ligne aux mêmes champs que
le journal du labo (generator.py) :
- plan.* : le plan compact (generator.compact_plan), avec la note et le verdict recalculés SANS la
  part catalyseur, comme au labo (le catalyseur n'y est pas rejoué) ; composite et decision idem ;
- context.setup, target_sources (du plan), rvol et day_change_pct (universe._metrics sur les séances
  closes, comme l'univers du labo), regime (universe.regime sur SPY), catalyst_replayed = false ;
- regime, regime_trend, regime_vol_tercile : regime.series sur ~500 jours de SPY (table regimes du labo) ;
- weekday, hour_utc, market, symbol, side : du jour et de l'heure du plan.
Crypto (crypto_row) : la ligne de lessons.crypto_row (contexte du générateur), le symbole au nom du labo (paire
OKX « XXX-USDT ») et le régime de BTC (context.regime, regime*) sur ses bougies journalières, comme
generator.crypto_day ; BTC indisponible : les règles sur le régime ne s'appliquent pas, comme sans SPY.
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


INDEX_FIELDS = {"regime", "regime_trend", "regime_vol_tercile"}


def needs_index(rule: dict) -> bool:
    """La règle lit le régime de marché (bougies de SPY) : regime*, context.regime…"""
    from app.lab import validator as V

    return any(leaf.get("field") in INDEX_FIELDS or str(leaf.get("field", "")).startswith("context.regime")
               for leaf in V.leaves_of(rule["cond"]))


def _verdict(champ: dict, index_daily, make_row, index_name: str = "SPY") -> dict:
    """Verdict d'un plan sur la ligne que construit `make_row` ; jamais d'exception (plan gardé en cas de souci).
    Indice indisponible (panne propre au direct) : les règles sur le régime ne s'appliquent pas, plutôt
    que d'écarter tous les plans (une règle « ne garder que » sur un champ inconnu écarte tout)."""
    if champ["regles"] and not index_daily:
        try:
            kept = [r for r in champ["regles"] if not needs_index(r)]
        except Exception:
            kept = []
        if len(kept) != len(champ["regles"]):
            _warn_once(f"indice:{index_name}", "Méthode du labo : régime de marché (%s) indisponible, règles sur le "
                                               "régime non appliquées", index_name)
            champ = champ | {"regles": kept}
    if not champ["regles"]:
        return {"garde": True, "regles": [], "version": champ.get("version")}
    try:
        row = make_row()
    except Exception as exc:
        _warn_once("ligne", "Méthode du labo : contexte du plan impossible (%s: %s), plan gardé", type(exc).__name__, exc)
        return {"garde": True, "regles": [], "version": champ.get("version")}
    return check([row], champ)[0]


def verdict_for(plan: dict, daily_score, daily, index_daily, now: datetime | None, s, champ: dict | None = None) -> dict:
    """Verdict d'un plan d'action (scanner, plan_verdict) ; jamais d'exception (plan gardé en cas de souci)."""
    champ = champ or current()
    return _verdict(champ, index_daily,
                    lambda: stock_row(plan, daily_score, daily, index_daily, now or datetime.now(timezone.utc), s))


LAB_QUOTE = "USDT"  # paires du labo crypto : OKX au comptant contre USDT (« BTC-USDT »)
INDEX_DIRECT = {"stock": "SPY", "crypto": "BTC"}  # indice du régime en direct (Alpaca, Kraken)


def lab_symbol(symbol: str) -> str:
    """Symbole d'un plan crypto du direct (« BTC », Kraken) au nom du labo (« BTC-USDT »)."""
    symbol = symbol.upper()
    return symbol if "-" in symbol else f"{symbol}-{LAB_QUOTE}"


def crypto_row(plan: dict, daily: list[dict] | None, index_daily: list[dict] | None, now: datetime) -> dict:
    """Ligne « journal du labo » d'un plan crypto en direct (voir le docstring du module)."""
    from datetime import date

    from app.lab import collector as CO
    from app.lab import generator as G
    from app.lab import lessons as L
    from app.lab import regime as REG
    from app.lab import universe as U

    row = L.crypto_row(plan, daily, now)
    row["symbol"] = lab_symbol(row["symbol"])
    if index_daily:
        day = date.fromisoformat(row["day"])
        row["context"]["regime"] = U.regime(index_daily, CO.utc_midnight(day), G.ANNUAL_DAYS["crypto"])
        label = REG.series(index_daily, "crypto", [day])[row["day"]]
        row |= {"regime_label": label.get("label"), "regime_trend": label.get("trend"),
                "regime_vol_tercile": label.get("vol_tercile")}
    return row


def crypto_verdict(plan: dict, daily, index_daily, now: datetime | None, champ: dict | None = None) -> dict:
    """Verdict d'un plan crypto ; jamais d'exception (plan gardé en cas de souci)."""
    champ = champ or current()
    return _verdict(champ, index_daily, lambda: crypto_row(plan, daily, index_daily, now or datetime.now(timezone.utc)),
                    INDEX_DIRECT["crypto"])


_index: dict = {}


async def index_daily(market: str, now: datetime) -> list[dict] | None:
    """Bougies journalières de l'indice du régime (SPY : ~500 jours Alpaca ; BTC : Kraken), gardées pour la journée
    UTC ; None si la source ne répond pas (les règles sur le régime ne s'appliquent alors pas)."""
    day = now.astimezone(timezone.utc).date().isoformat()
    if _index.get(market, (None,))[0] != day:
        if market == "stock":
            from app.trading import scanner

            bars = await scanner._index_daily(now)
        else:
            from app.trading import service as T

            bars = await T._optional_candles(INDEX_DIRECT["crypto"], "crypto", 1440)
        if not bars:
            return None  # pas gardé : réessayé au plan suivant
        _index[market] = (day, bars)
    return _index[market][1]


async def plan_verdict(plan: dict, daily: list[dict] | None, now: datetime, s=None, champ: dict | None = None) -> dict | None:
    """Verdict du champion pour un plan hors scanner (`!plan`, `!prends`, API, crypto) avec les mêmes fonctions que
    le scanner ; None si aucune règle du champion ne touche le marché du plan (rien à évaluer) ou pour un plan
    vendeur (le labo ne mesure que des trades longs, comme les leçons : ses règles ne disent rien d'un short).
    Mêmes sémantiques en cas de donnée manquante : plan gardé, une ligne de journal. Jamais d'exception."""
    try:
        champ = champ or current()
        market = plan.get("market")
        rules = M.market_rules(champ["regles"], market)
        if not rules or market not in INDEX_DIRECT or plan.get("side") != "long":
            return None
        champ = champ | {"regles": rules}
        index = await index_daily(market, now) if any(needs_index(r) for r in rules) else None
        if market == "stock":
            from app.trading.settings import settings

            return verdict_for(plan, daily, daily, index, now, settings if s is None else s, champ)
        return crypto_verdict(plan, daily, index, now, champ)
    except Exception as exc:
        _warn_once("plan", "Méthode du labo : verdict du plan impossible (%s: %s), plan gardé", type(exc).__name__, exc)
        return {"garde": True, "regles": [], "version": (champ or {}).get("version")}
