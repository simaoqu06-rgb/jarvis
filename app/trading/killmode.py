"""JARVIS Killmode : trades à haute volatilité, uniquement sur des setups complets (demande de Simão, 8 octobre 2026).

Une version parallèle de JARVIS : rien de JARVIS n'est modifié, Killmode n'entre dans aucune routine (ni
scanner de 15 h 10, ni cockpit, ni compte fictif, ni message automatique) et il est **désactivé par défaut** :
Simão seul l'allume (`!kill on`) ou l'éteint (`!kill off`) ; l'état est gardé dans data/killmode.json.
Comme JARVIS, il propose des plans et ne passe jamais d'ordre.

Un setup n'est proposé (🔪 KILL) que si les quatre piliers sont réunis :
1. **Volatilité** : gap d'au moins +4 % (à l'ouverture ; avant, celui de la pré-ouverture) et ATR 14 jours
   d'au moins 4 % du cours ;
2. **Catalyseur** : actu de la société de moins de 48 h à tonalité positive, ou résultats publiés hier ou
   aujourd'hui au-dessus des attentes ; pas d'augmentation de capital dans les actus, pas de résultats dans
   les 24 h (pari binaire), pas plus d'actus négatives que positives ;
3. **Graphique** : la règle de prix de la déclaration n° 5 du labo (strategies.killmode_check, le même code
   qu'au labo) : range des 15 premières minutes clôturé au-dessus du VWAP sans combler le gap ; ordre stop
   d'achat 1 pas au-dessus du range, valable jusqu'à 11 h New York (17 h Paris), stop 1 pas sous le range,
   objectifs à 1, 2 et 3 R. Pas proposé si le prix est déjà parti à plus de 0,5 R au-dessus de l'entrée ;
4. **Données complètes** : cours, journalier, bougies de 5 min, profil de la société, calendrier des
   résultats et actus ont tous répondu. Rien n'est deviné : une source muette = pas de setup.
La fiche société (secteur, capitalisation, derniers résultats, tendance), le potentiel +10 du labo pour ce
gap et la lecture EmperorBTC de JARVIS (checklist, résistances au-dessus) accompagnent chaque analyse.

Le direct lit les mêmes données que le labo : clôture de la veille SIP (pas IEX), ATR de Wilder 14 jours,
15 séances suffisent (une introduction récente peut être KILL, comme au labo), et le scan pré-trie sur le gap
d'ouverture autant que sur le gap du moment (une valeur ouverte à +6 % puis revenue à +3 % reste examinée).

Mesure (sa place au labo) : la règle de prix est la stratégie `killmode` du labo v2 (déclaration n° 5),
rejouée et comparée à son témoin comme les autres ; sa ligne du labo accompagne chaque KILL, avec ce qu'elle
mesure (la règle de prix seule, sans catalyseur, sur l'univers du labo). Le catalyseur ne se rejoue pas
(Finnhub gratuit ne remonte qu'un an) : il est mesuré dans le journal data/killmode_calls.json, qui note
- chaque 🔪 KILL proposé ;
- un « témoin catalyseur » : chaque analyse où tout est réuni sauf le catalyseur (même règle de prix, même
  solde), seule façon de voir ce que le catalyseur apporte ;
avec la déclaration du labo, la règle et ses seuils, la source des bougies et le volume des 15 premières
minutes. `!kill bilan` solde chaque appel après la clôture sur les bougies de 5 min d'Alpaca, avec le
simulateur du labo (ordre stop d'achat à l'heure de l'appel, un tiers par objectif, stop à l'entrée après
TP1, soldé à la clôture), net de la commission IBKR par action, et applique la règle de lecture écrite avant
le premier appel : à 30 KILL déclenchés, borne haute de l'IC 95 % sous 0 → JARVIS recommande d'éteindre
Killmode (Simão décide) ; borne basse au-dessus de 0 → on continue ; sinon on continue jusqu'à 100.
"""
import asyncio
import json
import logging
import math
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.core.http import SourceError
from app.lab import collector as CO
from app.lab import potentiel as PO
from app.lab import simulator as SIM
from app.lab import strat_stats as SS
from app.lab import strategies as ST
from app.sources import alpaca, finnhub
from app.trading import catalyst as C
from app.trading import scanner as SC
from app.trading import service as T
from app.trading.settings import settings

log = logging.getLogger(__name__)
NEW_YORK = ZoneInfo("America/New_York")
STATE_PATH = os.getenv("JARVIS_KILLMODE_PATH", "data/killmode.json")
CALLS_PATH = os.getenv("JARVIS_KILLMODE_CALLS_PATH", "data/killmode_calls.json")
CATALYST_HOURS = 48
EARNINGS_BLOCK_HOURS = 24
FRESH_EARNINGS_DAYS = 1
CHASE_R = 0.5          # prix déjà parti de plus de 0,5 R au-dessus de l'entrée : on ne court pas après
SCAN_DEEP = 6          # valeurs analysées à fond par `!kill scan` (les plus forts gaps)
SCAN_CONCURRENCY = 2
BAR = CO.BASE_TF * 60
DAILY_DAYS = 100
FIVE_MIN_DAYS = 6      # ≥ 2 séances d'échauffement (EMA, ATR de 5 min), comme le labo
SETTLE_DELAY = timedelta(minutes=20)  # Alpaca gratuit : bougies SIP avec 16 min de retard
PILLARS = (("volatilite", "Volatilité"), ("catalyseur", "Catalyseur"), ("graphique", "Graphique"),
           ("donnees", "Données complètes"))
# Déclaration du labo qui a posé la règle de prix de killmode. À changer seulement quand une déclaration
# modifie cette règle : le bilan ne mélange pas les appels de deux règles différentes.
RULE = 5
GROUPS = {"kill": "🔪 KILL", "temoin_catalyseur": "Témoin catalyseur (tout réuni sauf le catalyseur)"}
READ_AT = 30           # règle de lecture du journal (écrite avant le premier appel)
READ_MAX = 100
VOL15_DAYS = 20
SC_MIN_SESSIONS = 51   # scanner.features (MM50, RSI) : la fiche société seulement
LAB_SCOPE = "règle de prix seule, sans catalyseur, sur l'univers du labo (~300 actions choisies la veille)"
SCORE_NOTE = ("non prédictive au labo, mesurée sans sa part catalyseur (rejeu oct.-déc. 2024 : notes 5 à 9 "
              "entre −0,16 et −0,25 R)")


def seuils() -> dict:
    """Paramètres de la règle et du direct, notés avec chaque appel."""
    return {"gap": ST.KILL_GAP_PCT, "atr": ST.KILL_ATR_PCT, "range_min": ST.KILL_RANGE_MIN,
            "jusqu_a": ST.KILL_UNTIL.strftime("%H:%M"), "poursuite_r": CHASE_R, "catalyseur_h": CATALYST_HOURS}


class KillError(Exception):
    pass


# --- Interrupteur (Simão seul, jamais une routine) -----------------------------------------

def _read(path: str, default):
    p = Path(path)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("%s illisible, valeur par défaut", path)
        return default


def _write(path: str, data) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def state() -> dict:
    return _read(STATE_PATH, {}) or {}


def active() -> bool:
    return bool(state().get("actif"))


def set_active(on: bool, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    st = state() | {"actif": on, "depuis": now.isoformat(timespec="seconds")}
    _write(STATE_PATH, st)
    return st


# --- Analyse d'une action ------------------------------------------------------------------

def live_day(bars5: list[dict], daily: list[dict], now: datetime) -> ST.Day | None:
    """Le jour de Bourse de `now` tel que le labo le voit : bougies de 5 min closes (échauffement + séance)
    et journalier connu la veille. None si la Bourse est fermée ce jour-là."""
    today = now.astimezone(NEW_YORK).date()
    bounds = CO.session_ts(today)
    if not bounds:
        return None
    cutoff = now.timestamp()
    done = [b for b in bars5 if b["t"] + BAR <= cutoff]
    hist = [b for b in done if b["t"] < bounds[0]][-ST.STOCK_WARMUP_BARS:]
    session = [b for b in done if bounds[0] <= b["t"] < bounds[1]]
    return ST.Day("stock", today, hist + session, sessions_before(daily, today))


def sessions_before(daily: list[dict], today) -> list[dict]:
    """Séances journalières terminées avant `today` (jour de New York)."""
    return [c for c in daily if datetime.fromtimestamp(c["t"], NEW_YORK).date() < today]


def daily_atr_pct(before: list[dict]) -> float | None:
    """ATR de Wilder 14 jours de la veille en % de sa clôture, comme strategies.kill_context."""
    if len(before) > ST.ATR_LEN and before[-1]["c"] > 0:
        return ST.atr(before)[-1] / before[-1]["c"] * 100
    return None


def vol15_adv(d: ST.Day, ctx: dict) -> float | None:
    """Volume des 15 premières minutes rapporté au volume journalier moyen des 20 séances d'avant. Celui du labo
    (kill_context) dès qu'il le donne ; d'ici là la même formule, ici. None si une donnée manque."""
    if "vol15_adv" in ctx:
        return ctx["vol15_adv"]
    n = ST.KILL_RANGE_MIN // CO.BASE_TF
    if d.d1 - d.d0 < n or d.t[d.d0] != d.start_ts or len(d.daily) < VOL15_DAYS:
        return None
    avg = sum(c["v"] for c in d.daily[-VOL15_DAYS:]) / VOL15_DAYS
    return sum(b["v"] for b in d.bars[d.d0:d.d0 + n]) / avg if avg > 0 else None


def candle_source(source: str | None, bars5: list[dict] | None, now: datetime) -> str | None:
    """Source réelle des bougies de 5 min : Alpaca pour le scan ; sinon Twelve Data en temps réel, sauf si
    service.candles est retombé en silence sur Alpaca (16 min de retard), ce qui se voit à l'âge de la
    dernière bougie pendant la séance."""
    if source == "alpaca":
        return "alpaca"
    if not bars5:
        return None
    bounds = CO.session_ts(now.astimezone(NEW_YORK).date())
    ts = now.timestamp()
    if bounds and bounds[0] + 10 * 60 <= ts < bounds[1] and ts - (bars5[-1]["t"] + BAR) > 10 * 60:
        return "alpaca (secours)"
    return "twelvedata"


def open_gap(rows: list[dict], snap: dict, prev: float | None, now: datetime) -> float | None:
    """Gap d'ouverture du jour en % de la clôture SIP de la veille : ouverture de la bougie journalière SIP du
    jour si Alpaca la donne déjà, sinon celle d'IEX (snapshot) ; None avant 9 h 30 New York."""
    today = now.astimezone(NEW_YORK).date()
    bounds = CO.session_ts(today)
    if not prev or not bounds or now.timestamp() < bounds[0]:
        return None
    bar = next((r for r in reversed(rows) if datetime.fromtimestamp(r["t"], NEW_YORK).date() == today), None)
    o = bar["o"] if bar else snap.get("open") if snap.get("open_day") == today.isoformat() else None
    return (o / prev - 1) * 100 if o else None


def catalyst_pillar(cat: dict | None, now: datetime) -> tuple[bool, list[str]]:
    """Pilier catalyseur : (réuni, raisons) ; les raisons disent ce qui le porte ou ce qui manque."""
    if not cat or not cat.get("available"):
        return False, ["Finnhub n'a pas répondu : catalyseur non vérifiable"]
    news = cat.get("news") or []
    fresh = [n for n in news if n.get("published_at")
             and now - datetime.fromisoformat(n["published_at"]) <= timedelta(hours=CATALYST_HOURS)]
    pos = [n for n in fresh if n["tone"] == "positive"]
    neg = [n for n in fresh if n["tone"] == "negative"]
    last = cat.get("last_earnings") or {}
    earnings_beat = (last.get("days_ago") is not None and last["days_ago"] <= FRESH_EARNINGS_DAYS
                     and (last.get("eps_surprise_pct") or 0) > 0)
    blockers = []
    if cat.get("dilution"):
        blockers.append("augmentation de capital dans les actus : nouvelles actions à vendre")
    if not cat.get("earnings_known"):
        blockers.append("date des résultats non vérifiée")
    hours = cat.get("hours_to_earnings")
    if hours is not None and hours <= EARNINGS_BLOCK_HOURS:
        blockers.append(f"résultats dans {hours:.0f} h : pari binaire, pas un setup")
    if len(neg) > len(pos):
        blockers.append(f"plus d'actus négatives ({len(neg)}) que positives ({len(pos)}) en 48 h")
    if not pos and not earnings_beat:
        blockers.append("aucune actu positive de la société en 48 h ni résultats battus hier ou aujourd'hui")
    if blockers:
        return False, blockers
    why = []
    if earnings_beat:
        why.append(f"résultats du {last['date']} au-dessus des attentes (BPA "
                   f"{last['eps_surprise_pct']:+.1f} %)".replace(".", ","))
    if pos:
        why.append(f"{len(pos)} actu{'s' if len(pos) > 1 else ''} positive{'s' if len(pos) > 1 else ''} en 48 h")
    return True, why


def chart_pillar(d: ST.Day | None, now: datetime, price: float | None) -> dict:
    """Pilier graphique : la règle du labo (killmode_check) sur les bougies closes, plus l'état de l'ordre.
    status : avant (range pas encore fini), ok, parti, expire, non, ferme. Le gap et l'ATR sont jugés par le
    pilier volatilité (avant l'ouverture, sur le gap de pré-ouverture)."""
    if d is None:
        return {"status": "ferme", "ok": False, "raisons": ["Bourse de New York fermée aujourd'hui"]}
    if d.d1 > d.d0 and d.t[d.d0] != d.start_ts:
        return {"status": "non", "ok": False,
                "raisons": ["bougie de 9 h 30 absente : la règle du labo ne peut pas juger cette séance"]}
    cand, reasons = ST.killmode_check(d)
    until = int(datetime.combine(d.day, ST.KILL_UNTIL, NEW_YORK).timestamp())
    if cand is None:
        rest = [r for r in reasons if not r.startswith(("gap ", "ATR "))]
        waiting = bool(rest) and rest[-1].startswith("range des 15 premières minutes")
        if waiting and now.timestamp() >= until:
            return {"status": "expire", "ok": False, "raisons": ["pas de range exploitable avant 11 h New York"]}
        if waiting:  # avant l'ouverture, pendant le range, ou bougies en retard (Alpaca)
            return {"status": "avant", "ok": False, "raisons": [rest[-1]], "ouverture_range": d.start_ts}
        return {"status": "non", "ok": False, "raisons": rest or ["pas évalué : volatilité insuffisante"]}
    risk = cand["entry"] - cand["stop"]
    plan = {"entry": cand["entry"], "stop": cand["stop"], "risk": risk,
            "targets": [cand["entry"] + m * risk for m in ST.TARGETS_R],
            "decided_at": cand["decided_at"], "until": cand["entry_until"], "close_ts": d.close_ts,
            "range_high": cand["entry"] - d.tick(cand["entry"]), "range_low": cand["stop"] + d.tick(cand["stop"])}
    if now.timestamp() >= cand["entry_until"]:
        return {"status": "expire", "ok": False, "plan": plan,
                "raisons": ["ordre valable jusqu'à 11 h New York (17 h Paris) : setup terminé pour aujourd'hui"]}
    k = next((j for j in range(cand["i"] + 1, len(d.t)) if d.h[j] >= cand["entry"]), None)
    plan["declenche_a"] = d.t[k] if k is not None else None
    if price is not None and price > cand["entry"] + CHASE_R * risk:
        return {"status": "parti", "ok": False, "plan": plan,
                "raisons": [f"déjà parti : {price:g}, plus de {CHASE_R:g} R au-dessus de l'entrée {cand['entry']:g} "
                            "(on ne court pas après)".replace(".", ",")]}
    return {"status": "ok", "ok": True, "plan": plan, "raisons": []}


async def _safe(coro, label: str, errors: list[str]):
    try:
        return await coro
    except (SourceError, T.PlanError) as exc:
        errors.append(f"{label} : {exc}")
        return None


async def analyze(symbol: str, now: datetime | None = None, source: str | None = None) -> dict:
    """Analyse Killmode complète d'une action US (données, société, catalyseur, graphique, verdict).

    `source` : « alpaca » pour le scan (gratuit, 16 min de retard) ; par défaut Twelve Data en temps réel
    pour les bougies de 5 min, Alpaca en secours."""
    symbol = symbol.strip().upper()
    now = now or datetime.now(timezone.utc)
    if not alpaca.configured():
        raise KillError("clés Alpaca absentes (ALPACA_API_KEY, ALPACA_SECRET_KEY) : Killmode en a besoin")
    today = now.astimezone(NEW_YORK).date().isoformat()
    errors: list[str] = []
    snaps, daily_map, bars5, cat, profile = await asyncio.gather(
        _safe(alpaca.snapshots([symbol]), "cours", errors),
        _safe(alpaca.bars([symbol], 1440, days=DAILY_DAYS, now=now), "journalier", errors),
        _safe(T.candles(symbol, "stock", CO.BASE_TF, source), "bougies de 5 min", errors),
        _safe(C.check(symbol, now), "catalyseur", errors),
        _safe(finnhub.profile(symbol), "profil", errors),
    )
    snap = (snaps or {}).get(symbol) or {}
    daily = (daily_map or {}).get(symbol) or []
    before = sessions_before(daily, now.astimezone(NEW_YORK).date())
    prev = before[-1]["c"] if before else None  # clôture SIP de la veille, comme le labo (pas celle d'IEX)
    price = snap.get("price") or (bars5[-1]["c"] if bars5 else None)
    feats = SC.features(daily, price, prev, today) if daily and price else None  # fiche société (51 séances)
    if price is None:
        errors.append("cours : aucun prix")
    if daily_map is not None and len(before) <= ST.ATR_LEN:
        errors.append(f"journalier : moins de {ST.ATR_LEN + 1} séances")
    if cat:
        errors += [f"Finnhub {e}" for e in cat.get("errors") or []]
    d = live_day(bars5, daily, now) if bars5 and daily else None
    ctx = ST.kill_context(d) if d else {"gap_pct": None, "atr_pct": daily_atr_pct(before)}
    # Gap du labo (ouverture de la bougie de 9 h 30) ; sinon l'ouverture du jour (bougie journalière SIP ou
    # IEX, bougies de 5 min en retard) ; avant 9 h 30, le gap de pré-ouverture.
    gap, gap_kind = ctx["gap_pct"], "d'ouverture"
    if gap is None:
        gap = open_gap(daily, snap, prev, now)
    if gap is None and price and prev:
        gap, gap_kind = (price / prev - 1) * 100, "du moment"
    atr_pct = ctx["atr_pct"]
    if d is not None or (bars5 and daily):
        chart = chart_pillar(d, now, price)
    else:
        chart = {"status": "non", "ok": False, "raisons": ["bougies indisponibles"]}

    vol_reasons = []
    if gap is None:
        vol_reasons.append("gap inconnu")
    elif gap < ST.KILL_GAP_PCT:
        vol_reasons.append(f"gap {gap_kind} {ST._pct_fr(gap)} % (il faut au moins +{ST.KILL_GAP_PCT:g} %)")
    if atr_pct is None:
        vol_reasons.append("ATR journalier inconnu")
    elif atr_pct < ST.KILL_ATR_PCT:
        vol_reasons.append(f"ATR {atr_pct:.1f} % (il faut au moins {ST.KILL_ATR_PCT:g} %)".replace(".", ","))
    cat_ok, cat_reasons = catalyst_pillar(cat, now)
    pillars = {
        "volatilite": {"ok": not vol_reasons, "raisons": vol_reasons or [
            f"gap {gap_kind} {ST._pct_fr(gap)} %, ATR {atr_pct:.1f} %".replace(".", ",")]},
        "catalyseur": {"ok": cat_ok, "raisons": cat_reasons},
        "graphique": {"ok": chart["ok"], "raisons": chart["raisons"] or ["range tenu au-dessus du VWAP, gap pas comblé"]},
        "donnees": {"ok": not errors, "raisons": errors or ["cours, journalier, 5 min, profil, résultats, actus"]},
    }
    others_ok = all(p["ok"] for k, p in pillars.items() if k != "graphique")
    if others_ok and chart["ok"]:
        verdict = "kill"
    elif others_ok and chart["status"] == "avant":
        verdict = "pret"
    else:
        verdict = "non"
    out = {"symbol": symbol, "at": now.isoformat(timespec="seconds"), "price": price, "gap_pct": gap,
           "atr_pct": atr_pct, "vol15_adv": vol15_adv(d, ctx) if d else None, "verdict": verdict,
           "piliers": pillars, "graphique": chart, "societe": _company(symbol, profile, cat, feats, before),
           "catalyseur": cat, "potentiel": PO.hint(gap) if gap is not None else None, "jarvis": None,
           "source": candle_source(source, bars5, now)}
    if verdict == "kill":
        out["jarvis"] = await _jarvis_reading(symbol, chart["plan"], cat, source, now)
    return out


def _company(symbol: str, profile: dict | None, cat: dict | None, feats: dict | None, before: list[dict]) -> dict:
    cat = cat or {}
    return {"nom": (profile or {}).get("name") or cat.get("name") or symbol,
            "secteur": (profile or {}).get("industry"),
            "capitalisation": (profile or {}).get("market_cap") or cat.get("market_cap"),
            "derniers_resultats": cat.get("last_earnings"), "prochains_resultats": cat.get("next_earnings"),
            "heures_avant_resultats": cat.get("hours_to_earnings"), "mesures": feats, "seances": len(before)}


async def _jarvis_reading(symbol: str, plan: dict, cat: dict | None, source: str | None, now: datetime) -> dict | None:
    """Lecture EmperorBTC de JARVIS sur les mêmes entrée et stop : checklist, résistances et délais rejoués.
    Une information, pas un pilier : si elle échoue, l'analyse Killmode reste valable."""
    try:
        p = await T.build_plan(symbol, "stock", "long", entry=round(plan["entry"], 2), stop=round(plan["stop"], 2),
                               now=now, source=source, cat=cat)
    except Exception as exc:  # crédits Twelve Data épuisés, bougies absentes : on s'en passe
        log.info("Lecture JARVIS de %s indisponible pour Killmode : %s", symbol, exc)
        return None
    return {"checklist": p["checklist"], "score": p.get("score"),
            "resistances": [{"price": t["price"], "source": t["source"], "eta": t["eta_text"]} for t in p["targets"]]}


# --- Scan des valeurs en jeu (sur demande seulement) ---------------------------------------

async def scan(now: datetime | None = None) -> dict:
    """Les plus forts gaps du jour (actives et hausses Alpaca, liste suivie), ATR ≥ 4 %, analysés à fond ;
    seuls les setups complets (et ceux qui attendent le range) sont gardés. Pré-tri sur les mesures du labo :
    ATR de Wilder, clôture SIP de la veille, gap d'ouverture ou du moment (le plus fort des deux), 15 séances
    au moins. Aucun filtre de prix, de liquidité ni de capitalisation (choix de Simão)."""
    now = now or datetime.now(timezone.utc)
    if not alpaca.configured():
        raise KillError("clés Alpaca absentes (ALPACA_API_KEY, ALPACA_SECRET_KEY) : Killmode en a besoin")
    today = now.astimezone(NEW_YORK).date()
    try:
        symbols = await SC.universe(settings)
        snaps = await alpaca.snapshots(symbols)
        daily = await alpaca.bars(symbols, 1440, days=DAILY_DAYS, now=now)
    except SourceError as exc:
        raise KillError(f"données Alpaca indisponibles : {exc}") from exc
    movers = []
    for sym in symbols:
        snap = snaps.get(sym) or {}
        rows = daily.get(sym) or []
        before = sessions_before(rows, today)
        atr_pct = daily_atr_pct(before)
        if atr_pct is None or atr_pct < ST.KILL_ATR_PCT:
            continue
        prev = before[-1]["c"]
        price = snap.get("price") or rows[-1]["c"]
        gaps = {"gap_now": (price / prev - 1) * 100 if price and prev > 0 else None,
                "gap_open": open_gap(rows, snap, prev, now) if prev > 0 else None}
        known = [g for g in gaps.values() if g is not None]
        if known and max(known) >= ST.KILL_GAP_PCT:
            movers.append((sym, gaps | {"atr_pct": atr_pct}))
    movers.sort(key=lambda x: -(x[1]["gap_open"] if x[1]["gap_open"] is not None else x[1]["gap_now"]))
    gate = asyncio.Semaphore(SCAN_CONCURRENCY)

    async def one(sym: str):
        async with gate:
            try:
                return await analyze(sym, now, source="alpaca")
            except Exception as exc:
                log.warning("Killmode : analyse de %s en échec : %s", sym, exc)
                return {"symbol": sym, "verdict": "erreur", "erreur": str(exc)}

    results = await asyncio.gather(*(one(s) for s, _ in movers[:SCAN_DEEP]))
    keep = [r for r in results if r["verdict"] in ("kill", "pret")]
    dropped = [r for r in results if r["verdict"] not in ("kill", "pret")]
    return {"at": now.isoformat(timespec="seconds"), "examined": len(symbols), "movers": len(movers),
            "analysed": len(results), "setups": keep, "dropped": dropped}


# --- Journal des appels et bilan -------------------------------------------------------------

def calls() -> list[dict]:
    return _read(CALLS_PATH, []) or []


def group(a: dict) -> str | None:
    """Groupe du journal : « kill » pour un 🔪 KILL ; « temoin_catalyseur » quand tout est réuni (volatilité,
    données, graphique avec un ordre encore à prendre) sauf le catalyseur ; None sinon."""
    if a.get("verdict") == "kill":
        return "kill"
    p = a.get("piliers")
    if (a.get("verdict") == "non" and p and (a.get("graphique") or {}).get("status") == "ok"
            and p["volatilite"]["ok"] and p["donnees"]["ok"] and not p["catalyseur"]["ok"]):
        return "temoin_catalyseur"
    return None


def _group_of(c: dict) -> str:
    return c.get("groupe") or "kill"  # appels notés avant les groupes : des KILL


# Appels notés avant que le journal garde la règle (PR #32) : règle n° 5 avec ces seuils, figés.
LEGACY_VERSION = (5, json.dumps({"gap": 4.0, "atr": 4.0, "range_min": 15, "jusqu_a": "11:00", "poursuite_r": 0.5,
                                 "catalyseur_h": 48}, sort_keys=True))


def _version(c: dict) -> tuple:
    if "regle" not in c:
        return LEGACY_VERSION
    return c["regle"], json.dumps(c["seuils"], sort_keys=True)


def _current() -> tuple:
    return RULE, json.dumps(seuils(), sort_keys=True)


def record(a: dict, now: datetime | None = None) -> dict | None:
    """Note un 🔪 KILL ou un témoin catalyseur (une fois par action, séance et groupe) ; None si déjà noté ou
    hors des deux groupes."""
    g = group(a)
    if g is None:
        return None
    now = now or datetime.now(timezone.utc)
    day = now.astimezone(NEW_YORK).date().isoformat()
    book = calls()
    if any(c["symbol"] == a["symbol"] and c["day"] == day and _group_of(c) in (g, "kill") for c in book):
        return None  # déjà noté ; un témoin ne s'ajoute pas à un KILL de la même séance
    plan = a["graphique"]["plan"]
    cat = a.get("catalyseur") or {}
    call = {"id": uuid.uuid4().hex[:8], "groupe": g, "symbol": a["symbol"], "day": day, "at": int(now.timestamp()),
            "price": a["price"], "entry": plan["entry"], "stop": plan["stop"], "targets": plan["targets"],
            "until": plan["until"], "close_ts": plan["close_ts"], "gap_pct": a["gap_pct"], "atr_pct": a["atr_pct"],
            "vol15_adv": a.get("vol15_adv"), "source": a.get("source"), "declaration": ST.DECLARATION,
            "regle": RULE, "seuils": seuils(),
            "catalyseur": a["piliers"]["catalyseur"]["raisons"],  # ce qui le porte, ou ce qui manque (témoin)
            "actu": (cat.get("news") or [{}])[0].get("title"), "resultat": None}
    book.append(call)
    _write(CALLS_PATH, book)
    return call


def settle(call: dict, bars: list[dict]) -> dict:
    """Solde un appel sur les bougies de 5 min de sa séance : ordre stop d'achat posé à l'heure de l'appel,
    gestion du labo (tiers par objectif, stop à l'entrée après TP1), soldé à la clôture ; net de la commission
    IBKR estimée."""
    after = [b for b in bars if b["t"] >= call["at"]]
    plan = {"side": "long", "entry": call["entry"], "stop": call["stop"],
            "targets": [{"price": p} for p in call["targets"]]}
    res = SIM.simulate(plan, after, market="stock", decided_at=call["at"], entry_until=min(call["until"], call["close_ts"]),
                       exit_at=call["close_ts"], last_price=call["price"] or call["entry"],
                       cost=SIM.costs("stock", call["entry"]), entry_order="stop")
    r_ibkr = None
    if res["triggered"] and res["r_net"] is not None:
        r_ibkr = round(res["r_net"] - (SS.ibkr_commission_r(call["entry"], call["stop"]) or 0.0), 4)
    return {"declenche": bool(res["triggered"]), "r_net": res["r_net"], "r_ibkr": r_ibkr,
            "sortie": res["exit_reason"], "tp": [bool(x) for x in (res.get("tp_hits") or [])],
            "mfe_r": res.get("mfe_r")}


async def settle_due(now: datetime | None = None) -> int:
    """Solde les appels dont la séance est finie (bougies Alpaca, appelé par `!kill bilan`, jamais planifié)."""
    now = now or datetime.now(timezone.utc)
    book = calls()
    due = [c for c in book if c["resultat"] is None and now.timestamp() >= c["close_ts"] + SETTLE_DELAY.total_seconds()]
    done = 0
    for c in due:
        start = datetime.fromtimestamp(c["at"] - BAR, timezone.utc)
        end = datetime.fromtimestamp(c["close_ts"], timezone.utc)
        try:
            rows = (await alpaca.bars_range([c["symbol"]], CO.BASE_TF, start, end)).get(c["symbol"]) or []
        except SourceError as exc:
            log.warning("Killmode : bougies de %s indisponibles pour le bilan : %s", c["symbol"], exc)
            continue
        if not rows:
            continue
        c["resultat"] = settle(c, rows)
        done += 1
    if done:
        _write(CALLS_PATH, book)
    return done


def in_group(book: list[dict], groupe: str) -> list[dict]:
    """Appels d'un groupe. Les groupes sont disjoints par action et séance : un témoin dont l'action a aussi
    un KILL ce jour-là (l'actu est arrivée entre deux analyses) n'est pas un témoin, il est écarté."""
    if groupe != "temoin_catalyseur":
        return [c for c in book if _group_of(c) == groupe]
    kills = {(c["symbol"], c["day"]) for c in book if _group_of(c) == "kill"}
    return [c for c in book if _group_of(c) == groupe and (c["symbol"], c["day"]) not in kills]


def stats(book: list[dict], groupe: str = "kill") -> dict:
    """Bilan d'un groupe du journal ; espérance nette (commission IBKR par action) avec IC 95 % en grappes
    par jour, comme au labo."""
    book = in_group(book, groupe)
    settled = [c for c in book if c.get("resultat")]
    trig = [c for c in settled if c["resultat"]["declenche"] and c["resultat"]["r_ibkr"] is not None]
    m = SS.Mean()
    for c in trig:
        m.add(c["day"], c["resultat"]["r_ibkr"])
    res = m.result()
    return {"appels": len(book), "soldes": len(settled), "en_attente": len(book) - len(settled),
            "declenches": len(trig), "esperance_r": res["esperance_r"], "ic95": res["ic95"],
            "total_r": round(m.total, 2) if m.n else None,
            "gagnants": sum(c["resultat"]["r_ibkr"] > 0 for c in trig),
            "tp1": sum(1 for c in trig if c["resultat"]["tp"][:1] == [True])}


def lab_item() -> dict | None:
    """La stratégie `killmode` au labo v2 : None si la base n'est pas lisible, {} si rien n'est encore rejoué.
    La mesure sur les jours que la règle n'avait pas vus quand elle a été choisie (jours_suivants) fait foi dès
    que le labo la donne."""
    from app.lab import db as lab_db

    try:
        conn = lab_db.open_readonly()
        if conn is None:
            return None
        try:
            rep = SS.cached(conn) or {}
        finally:
            conn.close()
    except Exception as exc:  # une information, jamais une panne de Killmode
        log.warning("Killmode : tableau du labo illisible : %s", exc)
        return None
    it = ((rep.get("strategies") or {}).get("stock") or {}).get("killmode") or {}
    if "jours_vus_jusqu_au" in it or it.get("jours_suivants"):  # None tant qu'aucun jour non vu n'est rejoué
        return (it.get("jours_suivants") or {}) | {"jours_non_vus": True}
    return it


def lab_line(it: dict | None) -> str | None:
    """La ligne du labo en clair, avec ce qu'elle mesure ; None si la base n'est pas lisible."""
    if it is None:
        return None
    head = f"🧪 Labo, {LAB_SCOPE}"
    if not it.get("declenches"):
        return f"{head} : pas encore de trade rejoué" + (
            " sur les jours non vus au choix de la règle." if it.get("jours_non_vus") else ".")
    ci = it.get("ic95_ibkr") or it.get("ic95")
    wit = it.get("temoin") or {}
    return (f"{head} : {SS._r(it.get('esperance_ibkr_r'))} R par trade"
            + (f" [{SS._r(ci[0])} ; {SS._r(ci[1])}]" if ci else "")
            + f" sur {it['declenches']} trades ({it.get('jours', 'n/d')} j"
            + (", jours non vus au choix de la règle" if it.get("jours_non_vus") else "")
            + f") · vs témoin {SS._r(wit.get('ecart_r'))} R"
            + (" · ✅ candidate au labo (pas une promotion)" if it.get("candidate") else "")
            + (" · échantillon faible" if it.get("echantillon_faible") else ""))


def judged(book: list[dict], n: int) -> list[dict]:
    """Les `n` premiers KILL déclenchés et soldés, dans l'ordre des appels : la règle de lecture ne regarde
    qu'à 30 puis à 100, jamais entre les deux (regarder à chaque bilan multiplierait les fausses alertes)."""
    trig = sorted((c for c in in_group(book, "kill") if c.get("resultat") and c["resultat"]["declenche"]
                   and c["resultat"]["r_ibkr"] is not None), key=lambda c: c["at"])
    return trig[:n] if len(trig) >= n else []


def reading(book: list[dict], lab: dict | None) -> list[str]:
    """Règle de lecture du journal, écrite avant le premier appel, sur les KILL d'une même règle : lue une fois
    à 30 KILL déclenchés ; si l'IC 95 % contient 0, relue une fois à 100."""
    n = stats(book, "kill")["declenches"]
    st, look = None, None
    for size in (READ_AT, READ_MAX):
        cut = judged(book, size)
        if not cut:
            break
        st, look = stats(cut, "kill"), size
        if st["ic95"] and (st["ic95"][1] < 0 or st["ic95"][0] > 0):
            break
    if st is None or not st["ic95"]:
        return [f"-# Règle de lecture : rien n'est jugé avant {READ_AT} KILL déclenchés et soldés "
                f"({n} pour l'instant)."]
    ci = st["ic95"]
    span = f"lu sur les {look} premiers KILL : {SS._r(st['esperance_r'])} R, IC 95 % [{SS._r(ci[0])} ; {SS._r(ci[1])}]"
    if ci[1] < 0:
        out = [f"📉 **Espérance négative même en haut de l'intervalle** ({span}) : JARVIS recommande d'éteindre "
               "Killmode ; à toi de décider."]
    elif ci[0] > 0:
        out = [f"📈 **Espérance positive même en bas de l'intervalle** ({span}) : on continue."]
    elif look < READ_MAX:
        out = [f"Pas tranché à {READ_AT} ({span}, zéro dedans) : on continue, prochaine lecture à {READ_MAX} KILL "
               f"({n} pour l'instant)."]
    else:
        out = [f"Pas d'écart démontré à zéro ({span}) : JARVIS ne voit pas d'avantage ; à toi de décider."]
    gap = _lab_gap(st, lab)
    if gap:
        out.append(gap)
    return out


def _lab_gap(st: dict, lab: dict | None) -> str | None:
    """Écart labo/direct seulement s'il dépasse le bruit des deux mesures (z ≥ 1,96 sur la différence)."""
    lab_ci, ci = (lab or {}).get("ic95_ibkr"), st["ic95"]
    if not lab_ci or not ci:
        return None
    m_lab = lab.get("esperance_ibkr_r")
    if m_lab is None:
        m_lab = (lab_ci[0] + lab_ci[1]) / 2
    se = math.hypot((lab_ci[1] - lab_ci[0]) / 3.92, (ci[1] - ci[0]) / 3.92)
    if not se or abs(st["esperance_r"] - m_lab) / se < 1.96:
        return None
    return (f"-# Écart labo/direct : le journal fait {SS._r(st['esperance_r'])} R, le labo {SS._r(m_lab)} R "
            f"[{SS._r(lab_ci[0])} ; {SS._r(lab_ci[1])}], au-delà du bruit des deux mesures (catalyseur, univers ou "
            "données différents) : à comprendre avant d'en tirer quoi que ce soit.")


# --- Discord ---------------------------------------------------------------------------------

def _fr(x: float | None, digits: int = 2) -> str:
    if x is None:
        return "n/d"
    return f"{x:,.{digits}f}".replace(",", " ").replace(".", ",")


def _pct(x: float) -> str:
    return f"{x:+.1f} %".replace(".", ",")


def _cap(x: float | None) -> str:
    if not x:
        return "n/d"
    return f"{x / 1e9:.1f} Md$".replace(".", ",") if x >= 1e9 else f"{x / 1e6:.0f} M$"


def _paris(ts: int, now: datetime) -> str:
    return T.time_fr(datetime.fromtimestamp(ts, timezone.utc), now)


def text(a: dict, lab: str | None = None) -> str:
    """Analyse Killmode en clair pour Discord ; `lab` : la ligne du labo (lab_line), sous un KILL ou un PRÊT."""
    now = datetime.fromisoformat(a["at"])
    co = a["societe"]
    head = {"kill": "🔪 **KILL · setup complet**", "pret": "⏳ **PRÊT · tout est là sauf le graphique**",
            "non": "❌ **Pas un setup Killmode**"}[a["verdict"]]
    lines = [f"{head} · **{a['symbol']}** ({co['nom']}) · {_fr(a['price'])} $ · gap "
             f"{ST._pct_fr(a['gap_pct']) + ' %' if a['gap_pct'] is not None else 'n/d'}"]
    for key, label in PILLARS:
        p = a["piliers"][key]
        lines.append(f"{'✅' if p['ok'] else '❌'} {label} : {' ; '.join(p['raisons'])}")
    chart = a["graphique"]
    plan = chart.get("plan")
    if a["verdict"] == "kill":
        tps = " · ".join(f"TP{i} {_fr(p)} ({_pct((p / plan['entry'] - 1) * 100)}, {m:g} R)"
                         for i, (p, m) in enumerate(zip(plan["targets"], ST.TARGETS_R), start=1))
        lines.append(f"🎯 **Ordre stop d'achat {_fr(plan['entry'])}** (au-dessus du range {_fr(plan['range_low'])}"
                     f" - {_fr(plan['range_high'])}), valable jusqu'à {_paris(plan['until'], now)} · **stop "
                     f"{_fr(plan['stop'])}** ({_pct((plan['stop'] / plan['entry'] - 1) * 100)})")
        lines.append(f"{tps} · un tiers par objectif, stop à l'entrée après TP1, le reste vendu à la clôture")
        cost = SS.expected_cost_r("stock", plan["entry"], plan["stop"], None)
        if cost is not None:
            lines.append(f"-# Frais estimés ≈ {_fr(cost)} R par trade (glissement du labo + 0,005 $ par action ; "
                         "le minimum IBKR par ordre n'est pas compté).")
        if plan.get("declenche_a"):
            lines.append(f"-# Entrée déjà touchée à {_paris(plan['declenche_a'], now)} : encore à moins de "
                         f"{str(CHASE_R).replace('.', ',')} R, l'ordre part au prix du moment.")
        lines.append(f"-# Pour que JARVIS suive le trade : `!prends {a['symbol']} {plan['entry']:.2f} stop "
                     f"{plan['stop']:.2f}` (ses alertes TP/SL, ses propres objectifs).")
    elif a["verdict"] == "pret":
        opening = chart.get("ouverture_range")
        if opening:
            lines.append(f"⏳ Range des 15 premières minutes : repasse `!kill {a['symbol']}` après "
                         f"{_paris(opening + ST.KILL_RANGE_MIN * 60, now)} pour l'ordre en prix.")
    elif plan and chart["status"] in ("parti", "expire"):
        lines.append(f"-# Le plan était : stop d'achat {_fr(plan['entry'])}, stop {_fr(plan['stop'])}.")
    if lab and a["verdict"] in ("kill", "pret"):
        lines.append(lab)
    m = co.get("mesures") or {}
    last = co.get("derniers_resultats") or {}
    nxt = co.get("prochains_resultats") or {}
    company = [f"🏢 {co.get('secteur') or 'secteur n/d'} · capitalisation {_cap(co.get('capitalisation'))}"]
    if m:
        rvol = f"{m['rvol']:g}×".replace(".", ",") if m["rvol"] is not None else "n/d"
        company.append(f"5 j {_pct(m['change_5d_pct'])} · RSI {m['rsi']:.0f} · volume veille {rvol} · "
                       f"{'au-dessus de' if m['price'] > m['ma50'] else 'sous'} la MM50")
    elif 0 < (co.get("seances") or 0) < SC_MIN_SESSIONS:
        company.append(f"historique court ({co['seances']} séances) : MM50 et RSI indisponibles")
    if last:
        sur = last.get("eps_surprise_pct")
        company.append(f"résultats du {last['date']}" + (f" (BPA {_pct(sur)} vs attentes)" if sur is not None else ""))
    if nxt:
        company.append(f"prochains le {nxt['date']}")
    lines.append(" · ".join(company))
    marks = {"positive": "🟢", "negative": "🔴", "dilution": "🟠", "neutre": "⚪"}
    for n in ((a.get("catalyseur") or {}).get("news") or [])[:3]:
        lines.append(f"{marks[n['tone']]} [{n['title'][:110]}](<{n['url']}>)")
    if a.get("potentiel"):
        lines.append(a["potentiel"])
    j = a.get("jarvis")
    if j:
        ch = j["checklist"]
        res = " · ".join(f"{_fr(r['price'])} ({r['source']})" for r in j["resistances"])
        sc = j.get("score") or {}
        lines.append(f"📐 Lecture JARVIS : {ch['verdict']} · checklist {ch['score']}/{ch['max_score']}"
                     + (f" · note v1 {_fr(sc['composite'], 1)}/10 {sc['decision']}, {SCORE_NOTE}" if sc else "")
                     + (f" · résistances au-dessus : {res}" if res else ""))
    lines.append("-# Killmode : actions US, achat seulement. Règle de prix = stratégie `killmode` du labo "
                 "(déclaration n° 5) ; chaque 🔪 est noté et soldé par `!kill bilan`.")
    return "\n".join(lines)


def scan_text(r: dict, lab: str | None = None) -> str:
    now = datetime.fromisoformat(r["at"])
    lines = [f"🔪 **Killmode · scan** · {T.time_fr(now, now)} · {r['examined']} valeurs, {r['movers']} avec gap ≥ "
             f"+{ST.KILL_GAP_PCT:g} % (à l'ouverture ou maintenant) et ATR ≥ {ST.KILL_ATR_PCT:g} %, "
             f"{r['analysed']} analysées à fond"]
    if not r["setups"]:
        lines.append("Aucun setup complet en ce moment. Killmode préfère ne rien proposer qu'un setup à moitié.")
    elif lab:
        lines.append(lab)
    for a in r["setups"]:
        lines.append("")
        lines.append(text(a))
    if r["dropped"]:
        lines.append("\n-# Écartées : " + " · ".join(
            f"{a['symbol']} ({a.get('erreur') or _first_miss(a)})" for a in r["dropped"]))
    lines.append("-# Données Alpaca (16 min de retard) et Finnhub. `!kill TICKER` pour une analyse en temps réel.")
    return "\n".join(lines)


def _first_miss(a: dict) -> str:
    for key, label in PILLARS:
        p = a["piliers"][key]
        if not p["ok"]:
            return f"{label.lower()} : {p['raisons'][0]}"
    return a["verdict"]


def _n(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n > 1 else ''}"


def _version_label(v: tuple) -> str:
    s = json.loads(v[1])
    h, m = (s.get("jusqu_a") or "?:00").split(":")
    until = f"{int(h) if h.isdigit() else h} h" + ("" if m == "00" else f" {m}")
    return (f"règle n° {v[0]} (gap ≥ +{s.get('gap', 0):g} %, ATR ≥ {s.get('atr', 0):g} %, "
            f"range {s.get('range_min')} min, ordre jusqu'à {until} New York, "
            f"poursuite {s.get('poursuite_r', 0):g} R, actus {s.get('catalyseur_h')} h)").replace(".", ",")


def _group_line(label: str, st: dict) -> str:
    ci = st["ic95"]
    return (f"{label} : {_n(st['declenches'], 'déclenché')} · espérance {SS._r(st['esperance_r'])} R par trade"
            + (f" [{SS._r(ci[0])} ; {SS._r(ci[1])}]" if ci else "")
            + f" · total {SS._r(st['total_r'])} R · {_n(st['gagnants'], 'gagnant')} · TP1 touché {st['tp1']} fois")


def bilan_text(book: list[dict]) -> str:
    """Les deux groupes du journal (KILL, témoin catalyseur) de la règle en vigueur, nets de la commission IBKR par
    action ; les règles précédentes à part ; la ligne du labo ; la règle de lecture."""
    current = _current()
    versions = sorted({_version(c) for c in book})
    cur = [c for c in book if _version(c) == current]
    kill, wit = stats(cur, "kill"), stats(cur, "temoin_catalyseur")
    n_kill, n_wit = len(in_group(book, "kill")), len(in_group(book, "temoin_catalyseur"))
    pending = sum(1 for c in book if not c.get("resultat"))
    lines = [f"🔪 **Killmode · bilan** · {_n(n_kill, 'KILL proposé')}, {n_wit} témoin{'s' if n_wit > 1 else ''} "
             f"catalyseur" + (f", {pending} en attente de la clôture" if pending else "")]
    suffix = " · règle en vigueur" if len(versions) > 1 else ""
    for g, st in (("kill", kill), ("temoin_catalyseur", wit)):
        if st["declenches"]:
            lines.append(_group_line(GROUPS[g] + suffix, st))
    if kill["declenches"] or wit["declenches"]:
        lines.append("-# Net de la commission IBKR par action (0,005 $), sans le minimum par ordre. Le témoin a la "
                     "même règle de prix et le même solde : l'écart entre les deux est la seule mesure de l'apport "
                     "du catalyseur.")
    for v in versions:
        if v == current:
            continue
        sub = [c for c in book if _version(c) == v]
        for g in GROUPS:
            st = stats(sub, g)
            if st["declenches"]:
                lines.append(f"-# Ancienne {_version_label(v)} · " + _group_line(GROUPS[g], st))
    kept = {id(c) for g in GROUPS for c in in_group(book, g)}
    for c in [c for c in book if c.get("resultat") and id(c) in kept][-8:]:
        res = c["resultat"]
        out = (f"{SS._r(res['r_ibkr'])} R ({res['sortie']})" if res["declenche"] else "pas déclenché")
        tag = " (témoin)" if _group_of(c) == "temoin_catalyseur" else ""
        lines.append(f"-# {c['day']} {c['symbol']}{tag} · stop d'achat {_fr(c['entry'])}, stop {_fr(c['stop'])} "
                     f"→ {out}")
    lab = lab_item()
    line = lab_line(lab)
    if line:
        lines.append(line)
    lines += reading(cur, lab)
    return "\n".join(lines)


HELP = """**JARVIS Killmode** (désactivé par défaut, toi seul l'allumes ; aucune routine, aucun ordre passé)
`!kill on` / `!kill off`   allumer / éteindre Killmode
`!kill NVDA`               analyse complète : volatilité, catalyseur, graphique, données, fiche société
`!kill scan`               les plus forts gaps du jour, seuls les setups complets sont gardés
`!kill bilan`              KILL et témoins catalyseur soldés après la clôture, la stratégie `killmode` du labo
                           et la règle de lecture (rien n'est jugé avant 30 KILL déclenchés)
`!kill etat`               allumé ou éteint"""


async def command(args: list[str], now: datetime | None = None) -> str:
    """`!kill …` : interrupteur, analyse, scan, bilan. Tout sauf `on`, `etat` et `aide` exige Killmode allumé."""
    now = now or datetime.now(timezone.utc)
    sub = args[0].lower() if args else "aide"
    if sub in ("aide", "help"):
        return HELP
    if sub in ("on", "active", "activer", "start"):
        set_active(True, now)
        return ("🔪 **Killmode allumé.** `!kill TICKER` ou `!kill scan` ; seuls les setups complets sont proposés, "
                "les ordres restent les tiens. `!kill off` pour l'éteindre.")
    if sub in ("off", "desactive", "désactive", "desactiver", "désactiver", "stop"):
        set_active(False, now)
        return "Killmode éteint. JARVIS continue comme avant."
    if sub in ("etat", "état", "status"):
        st = state()
        return (f"Killmode {'allumé' if st.get('actif') else 'éteint'}"
                + (f" depuis le {st['depuis'][:16].replace('T', ' à ')} UTC" if st.get("depuis") else " (par défaut)")
                + f" · {_n(sum(_group_of(c) == 'kill' for c in calls()), 'KILL noté')}.")
    if not active():
        return "Killmode est éteint. `!kill on` pour l'allumer (toi seul peux le faire)."
    if sub == "scan":
        r = await scan(now)
        for a in r["setups"] + r["dropped"]:  # les écartées peuvent être des témoins catalyseur
            record(a, now)
        return scan_text(r, lab_line(lab_item()) if r["setups"] else None)
    if sub == "bilan":
        await settle_due(now)
        return bilan_text(calls())
    a = await analyze(args[0], now)
    record(a, now)
    return text(a, lab_line(lab_item()) if a["verdict"] in ("kill", "pret") else None)
