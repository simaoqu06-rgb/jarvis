"""Délai de visibilité des actus Finnhub, mesuré en direct (avis de GPT sur l'audit anti-fuite, 10 octobre 2026).

Killmode complet (déclaration n° 8) garde dans l'archive les actus publiées au plus tard à 9 h 45 New York
(`archive.keep_news`), d'après l'heure de publication que donne Finnhub. Une actu datée de 9 h 40 mais mise en ligne
chez Finnhub à 9 h 50 serait dans l'archive et pas dans le direct : une information que JARVIS n'avait pas. L'heure de
publication ne prouve pas la disponibilité ; seul le direct peut la mesurer. Ce module ne change aucune décision :
- à 9 h 46 New York, les actus Finnhub des gappers du jour (gap d'ouverture ≥ 4 % et ATR ≥ 4 % comme la règle de
  Killmode, pris dans la population du scanner +10 : screener Alpaca, pas toute la base), telles qu'on les voit ;
- à 11 h, les mêmes demandes : une actu datée d'avant 9 h 45 absente à 9 h 46 est arrivée en retard, et une valeur
  sans actu dans les CATALYST_HOURS avant 9 h 45 à 9 h 46 qui en a une à 11 h aurait reçu de l'archive un
  catalyseur que le direct n'avait pas.
Journal data/finnhub_visibilite.jsonl (une ligne par séance, écrite à 11 h) ; bilan dans l'état du labo
(`summary`, clé `visibilite_finnhub` de /api/lab/status).
"""
import json
import logging
import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import secret
from app.lab import strategies as ST
from app.sources import alpaca, finnhub
from app.trading import calendar as CAL

log = logging.getLogger(__name__)
NEW_YORK = ZoneInfo("America/New_York")
JOURNAL_PATH = os.getenv("JARVIS_FINNHUB_VISIBILITE_PATH", "data/finnhub_visibilite.jsonl")
STATE_PATH = os.getenv("JARVIS_FINNHUB_VISIBILITE_STATE_PATH", "data/finnhub_visibilite_state.json")
DECISION = time(9, 45)        # l'heure de coupure de l'archive (archive.DECISION)
PASSES = {"vue": time(9, 46), "relue": time(11, 0)}
WINDOW = timedelta(minutes=9)
CATALYST_HOURS = 48           # killmode.CATALYST_HOURS : fenêtre du catalyseur avant l'ordre
MAX_SYMBOLS = 20              # appels Finnhub par passage (formule gratuite : 60 par minute, partagés)
NEWS_DAYS = 3


def cutoff_ts(day: date) -> int:
    return int(datetime.combine(day, DECISION, NEW_YORK).timestamp())


def _read_state() -> dict:
    try:
        return json.loads(Path(STATE_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(state: dict) -> None:
    p = Path(STATE_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def journal(entry: dict) -> None:
    p = Path(JOURNAL_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def due(now: datetime) -> str | None:
    """Passage à faire maintenant (une fois par séance), sinon None ; « relue » seulement après « vue »."""
    now_ny = now.astimezone(NEW_YORK)
    if not CAL.is_trading_day(now_ny.date()) or not alpaca.configured() or not secret(finnhub.KEY_NAME):
        return None
    state = _read_state()
    today = state.get("day") == now_ny.date().isoformat()
    done = state.get("faits", []) if today else []
    for kind, at in PASSES.items():
        planned = datetime.combine(now_ny.date(), at, NEW_YORK)
        if timedelta(0) <= now_ny - planned < WINDOW and kind not in done and (kind == "vue" or "vue" in done):
            return kind
    return None


def gappers(pop: dict[str, dict]) -> list[str]:
    """Gappers de la règle de Killmode (achat : gap ≥ +4 %, ATR ≥ 4 %), les plus forts gaps d'abord."""
    ok = [s for s, m in pop.items() if m["gap_pct"] >= ST.KILL_GAP_PCT and (m.get("atr_pct") or 0) >= ST.KILL_ATR_PCT]
    return sorted(ok, key=lambda s: (-pop[s]["gap_pct"], s))[:MAX_SYMBOLS]


def _key(item: dict) -> str:
    return f"{item['published_ts']}|{item.get('title') or item.get('url') or ''}"


async def _news(symbols: list[str], day: date) -> dict[str, list[list]]:
    """[[horodatage de publication, clé]] par valeur, actus datées des NEWS_DAYS derniers jours ; une valeur en
    erreur est absente (jamais comptée comme « sans actu »)."""
    out: dict[str, list[list]] = {}
    for s in symbols:
        try:
            items = await finnhub.company_news_between(s, day - timedelta(days=NEWS_DAYS), day)
        except Exception as exc:
            log.info("Visibilité Finnhub : actus de %s indisponibles : %s", s, exc)
            continue
        out[s] = sorted([i["published_ts"], _key(i)] for i in items)
    return out


def compare(seen: dict[str, list[list]], later: dict[str, list[list]], day: date) -> dict:
    """Ce que la relecture de 11 h montre d'avant 9 h 45 que la vue de 9 h 46 n'avait pas."""
    cut = cutoff_ts(day)
    since = cut - CATALYST_HOURS * 3600
    late, gained, before, common = [], [], 0, 0
    for s in sorted(set(seen) & set(later)):
        known = {k for _, k in seen[s]}
        common += 1
        for ts, k in later[s]:
            if ts <= cut:
                before += 1
                if k not in known:
                    late.append({"symbol": s, "publiee": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                                 "titre": k.split("|", 1)[1][:100]})
        had = any(since <= ts <= cut for ts, _ in seen[s])
        has = any(since <= ts <= cut for ts, _ in later[s])
        if has and not had:
            gained.append(s)
    return {"valeurs": common, "actus_avant_945": before, "en_retard": late, "catalyseur_gagne": gained}


async def run(kind: str, now: datetime | None = None) -> dict | None:
    """Passage « vue » (9 h 46 : liste des gappers et leurs actus) ou « relue » (11 h : comparaison, journal)."""
    # import tardif : plus10 importe le service de trading
    from app.trading import plus10 as PLUS10

    now = now or datetime.now(timezone.utc)
    day = now.astimezone(NEW_YORK).date()
    state = _read_state()
    if kind == "vue":
        _, pop, _ = await PLUS10.lists(now, at_open=True)
        symbols = gappers(pop)
        news = await _news(symbols, day)
        _write_state({"day": day.isoformat(), "faits": ["vue"], "vue_a": now.isoformat(timespec="seconds"),
                      "examinees": len(pop), "gappers": symbols, "actus": news})
        return None
    if state.get("day") != day.isoformat() or not state.get("vue_a"):
        return None  # vue de 9 h 46 manquée : rien à comparer
    later = await _news(state.get("gappers") or [], day)
    entry = {"day": day.isoformat(), "vue_a": state.get("vue_a"), "relue_a": now.isoformat(timespec="seconds"),
             "examinees": state.get("examinees"), "gappers": len(state.get("gappers") or []),
             **compare(state.get("actus") or {}, later, day)}
    journal(entry)
    state["faits"] = [*state.get("faits", []), "relue"]
    _write_state(state)
    return entry


def mark_done(kind: str, now: datetime) -> None:
    """Passage abandonné après une erreur : marqué fait pour ne pas réessayer chaque minute de la fenêtre."""
    state = _read_state()
    day = now.astimezone(NEW_YORK).date().isoformat()
    if state.get("day") != day:
        state = {"day": day, "faits": []}
    if kind not in state.setdefault("faits", []):
        state["faits"].append(kind)
    _write_state(state)


def journal_path(db_file: str | None = None) -> Path:
    """Pour l'état du labo : le journal à côté de la base du labo (même dossier data/), sauf chemin imposé."""
    override = os.getenv("JARVIS_FINNHUB_VISIBILITE_PATH")
    if override or not db_file:
        return Path(override or JOURNAL_PATH)
    return Path(db_file).parent / Path(JOURNAL_PATH).name


def summary(path: str | Path | None = None) -> dict | None:
    """Bilan du journal : séances, actus datées d'avant 9 h 45, part arrivée après 9 h 46, valeurs dont le
    catalyseur aurait changé. None tant qu'aucune séance n'est mesurée."""
    try:
        lines = Path(path or JOURNAL_PATH).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    days = before = late = gained = values = 0
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        days += 1
        values += e.get("valeurs") or 0
        before += e.get("actus_avant_945") or 0
        late += len(e.get("en_retard") or [])
        gained += len(e.get("catalyseur_gagne") or [])
    if not days:
        return None
    return {"seances": days, "valeurs": values, "actus_avant_945": before, "en_retard": late,
            "part_en_retard": round(late / before, 4) if before else None, "catalyseur_gagne": gained,
            "part_catalyseur_gagne": round(gained / values, 4) if values else None}


def line(v: dict) -> str:
    """Ligne de `!labo`."""
    pct = f" ({v['part_en_retard'] * 100:.1f} %)".replace(".", ",") if v.get("part_en_retard") is not None else ""
    return (f"Actus Finnhub (audit anti-fuite) : {v['seances']} séance{'s' if v['seances'] > 1 else ''} mesurée"
            f"{'s' if v['seances'] > 1 else ''}, {v['actus_avant_945']} actus datées d'avant 9 h 45 dont "
            f"{v['en_retard']} arrivées après 9 h 46{pct} · catalyseur que seule l'archive aurait vu : "
            f"{v['catalyseur_gagne']} valeur{'s' if v['catalyseur_gagne'] > 1 else ''} sur {v['valeurs']}")
