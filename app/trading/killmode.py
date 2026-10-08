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

Mesure (sa place au labo) : la règle de prix est la stratégie `killmode` du labo v2 (déclaration n° 5),
rejouée et comparée à son témoin comme les autres. Le catalyseur ne se rejoue pas (Finnhub gratuit ne
remonte qu'un an) : chaque 🔪 KILL proposé est noté dans data/killmode_calls.json et `!kill bilan` le solde
après la clôture sur les bougies de 5 min d'Alpaca, avec le simulateur du labo (ordre stop d'achat à l'heure
de l'appel, un tiers par objectif, stop à l'entrée après TP1, soldé à la clôture), net de la commission IBKR.
"""
import asyncio
import json
import logging
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
    before = [c for c in daily if datetime.fromtimestamp(c["t"], NEW_YORK).date() < today]
    return ST.Day("stock", today, hist + session, before)


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
    price = snap.get("price") or (bars5[-1]["c"] if bars5 else None)
    feats = SC.features(daily, price, snap.get("prev_close"), today) if daily and price else None
    if price is None:
        errors.append("cours : aucun prix")
    if daily and feats is None:
        errors.append("journalier : moins de 51 séances (introduction récente ?)")
    if cat:
        errors += [f"Finnhub {e}" for e in cat.get("errors") or []]
    d = live_day(bars5, daily, now) if bars5 and daily else None
    ctx = ST.kill_context(d) if d else {"gap_pct": None, "atr_pct": None}
    pre_gap = feats["gap_pct"] if feats else None
    gap = ctx["gap_pct"] if ctx["gap_pct"] is not None else pre_gap
    atr_pct = ctx["atr_pct"]
    if d is not None or (bars5 and daily):
        chart = chart_pillar(d, now, price)
    else:
        chart = {"status": "non", "ok": False, "raisons": ["bougies indisponibles"]}

    vol_reasons = []
    if gap is None:
        vol_reasons.append("gap inconnu")
    elif gap < ST.KILL_GAP_PCT:
        vol_reasons.append(f"gap {ST._pct_fr(gap)} % (il faut au moins +{ST.KILL_GAP_PCT:g} %)")
    if atr_pct is None:
        vol_reasons.append("ATR journalier inconnu")
    elif atr_pct < ST.KILL_ATR_PCT:
        vol_reasons.append(f"ATR {atr_pct:.1f} % (il faut au moins {ST.KILL_ATR_PCT:g} %)".replace(".", ","))
    cat_ok, cat_reasons = catalyst_pillar(cat, now)
    pillars = {
        "volatilite": {"ok": not vol_reasons, "raisons": vol_reasons or [
            f"gap {ST._pct_fr(gap)} %, ATR {atr_pct:.1f} %".replace(".", ",")]},
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
           "atr_pct": atr_pct, "verdict": verdict, "piliers": pillars, "graphique": chart,
           "societe": _company(symbol, profile, cat, feats), "catalyseur": cat,
           "potentiel": PO.hint(gap) if gap is not None else None, "jarvis": None}
    if verdict == "kill":
        out["jarvis"] = await _jarvis_reading(symbol, chart["plan"], cat, source, now)
    return out


def _company(symbol: str, profile: dict | None, cat: dict | None, feats: dict | None) -> dict:
    cat = cat or {}
    return {"nom": (profile or {}).get("name") or cat.get("name") or symbol,
            "secteur": (profile or {}).get("industry"),
            "capitalisation": (profile or {}).get("market_cap") or cat.get("market_cap"),
            "derniers_resultats": cat.get("last_earnings"), "prochains_resultats": cat.get("next_earnings"),
            "heures_avant_resultats": cat.get("hours_to_earnings"), "mesures": feats}


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
    seuls les setups complets (et ceux qui attendent le range) sont gardés. Aucun filtre de prix, de
    liquidité ni de capitalisation (choix de Simão)."""
    now = now or datetime.now(timezone.utc)
    if not alpaca.configured():
        raise KillError("clés Alpaca absentes (ALPACA_API_KEY, ALPACA_SECRET_KEY) : Killmode en a besoin")
    today = now.astimezone(NEW_YORK).date().isoformat()
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
        price = snap.get("price") or (rows[-1]["c"] if rows else None)
        f = SC.features(rows, price, snap.get("prev_close"), today) if price else None
        if f and f["gap_pct"] >= ST.KILL_GAP_PCT and f["atr_pct"] >= ST.KILL_ATR_PCT:
            movers.append((sym, f))
    movers.sort(key=lambda x: -x[1]["gap_pct"])
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


def record(a: dict, now: datetime | None = None) -> dict | None:
    """Note un 🔪 KILL proposé (une fois par action et par séance) ; None si déjà noté ou pas un KILL."""
    if a.get("verdict") != "kill":
        return None
    now = now or datetime.now(timezone.utc)
    day = now.astimezone(NEW_YORK).date().isoformat()
    book = calls()
    if any(c["symbol"] == a["symbol"] and c["day"] == day for c in book):
        return None
    plan = a["graphique"]["plan"]
    cat = a.get("catalyseur") or {}
    call = {"id": uuid.uuid4().hex[:8], "symbol": a["symbol"], "day": day, "at": int(now.timestamp()),
            "price": a["price"], "entry": plan["entry"], "stop": plan["stop"], "targets": plan["targets"],
            "until": plan["until"], "close_ts": plan["close_ts"], "gap_pct": a["gap_pct"], "atr_pct": a["atr_pct"],
            "catalyseur": a["piliers"]["catalyseur"]["raisons"],
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


def stats(book: list[dict]) -> dict:
    settled = [c for c in book if c.get("resultat")]
    trig = [c for c in settled if c["resultat"]["declenche"] and c["resultat"]["r_ibkr"] is not None]
    rs = [c["resultat"]["r_ibkr"] for c in trig]
    return {"appels": len(book), "soldes": len(settled), "en_attente": len(book) - len(settled),
            "declenches": len(trig), "esperance_r": round(sum(rs) / len(rs), 3) if rs else None,
            "total_r": round(sum(rs), 2) if rs else None,
            "gagnants": sum(r > 0 for r in rs), "tp1": sum(1 for c in trig if c["resultat"]["tp"][:1] == [True])}


def lab_line() -> str | None:
    """La stratégie `killmode` au labo v2 (rejeu), si le worker l'a déjà mesurée."""
    from app.lab import db as lab_db

    conn = lab_db.open_readonly()
    if conn is None:
        return None
    try:
        rep = SS.cached(conn) or {}
    finally:
        conn.close()
    it = ((rep.get("strategies") or {}).get("stock") or {}).get("killmode")
    if not it:
        return "🧪 Labo (rejeu, déclaration n° 5) : pas encore de trade Killmode rejoué."
    ci = it.get("ic95_ibkr") or it.get("ic95")
    wit = it.get("temoin") or {}
    return (f"🧪 Labo (rejeu) : {SS._r(it.get('esperance_ibkr_r'))} R par trade"
            + (f" [{SS._r(ci[0])} ; {SS._r(ci[1])}]" if ci else "")
            + f" sur {it['declenches']} trades ({it['jours']} j) · vs témoin {SS._r(wit.get('ecart_r'))} R"
            + (" · ✅ candidate" if it.get("candidate") else "")
            + (" · échantillon faible" if it.get("echantillon_faible") else ""))


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


def text(a: dict) -> str:
    """Analyse Killmode en clair pour Discord."""
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
    m = co.get("mesures") or {}
    last = co.get("derniers_resultats") or {}
    nxt = co.get("prochains_resultats") or {}
    company = [f"🏢 {co.get('secteur') or 'secteur n/d'} · capitalisation {_cap(co.get('capitalisation'))}"]
    if m:
        rvol = f"{m['rvol']:g}×".replace(".", ",") if m["rvol"] is not None else "n/d"
        company.append(f"5 j {_pct(m['change_5d_pct'])} · RSI {m['rsi']:.0f} · volume veille {rvol} · "
                       f"{'au-dessus de' if m['price'] > m['ma50'] else 'sous'} la MM50")
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
                     + (f" · note {sc['composite']:g}/10 {sc['decision']}" if sc else "")
                     + (f" · résistances au-dessus : {res}" if res else ""))
    lines.append("-# Killmode : actions US, achat seulement. Règle de prix = stratégie `killmode` du labo "
                 "(déclaration n° 5) ; chaque 🔪 est noté et soldé par `!kill bilan`.")
    return "\n".join(lines)


def scan_text(r: dict) -> str:
    now = datetime.fromisoformat(r["at"])
    lines = [f"🔪 **Killmode · scan** · {T.time_fr(now, now)} · {r['examined']} valeurs, {r['movers']} avec gap ≥ "
             f"+{ST.KILL_GAP_PCT:g} % et ATR ≥ {ST.KILL_ATR_PCT:g} %, {r['analysed']} analysées à fond"]
    if not r["setups"]:
        lines.append("Aucun setup complet en ce moment. Killmode préfère ne rien proposer qu'un setup à moitié.")
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


def bilan_text(book: list[dict]) -> str:
    st = stats(book)
    lines = [f"🔪 **Killmode · bilan** · {st['appels']} setup{'s' if st['appels'] > 1 else ''} proposé"
             f"{'s' if st['appels'] > 1 else ''}, {st['soldes']} soldé{'s' if st['soldes'] > 1 else ''}"
             + (f", {st['en_attente']} en attente de la clôture" if st["en_attente"] else "")]
    if st["declenches"]:
        lines.append(f"{st['declenches']} déclenchés · espérance {SS._r(st['esperance_r'])} R par trade (net, "
                     f"commission IBKR comprise) · total {SS._r(st['total_r'])} R · {st['gagnants']} gagnants · "
                     f"TP1 touché {st['tp1']} fois")
    for c in [c for c in book if c.get("resultat")][-8:]:
        res = c["resultat"]
        out = (f"{SS._r(res['r_ibkr'])} R ({res['sortie']})" if res["declenche"] else "pas déclenché")
        lines.append(f"-# {c['day']} {c['symbol']} · stop d'achat {_fr(c['entry'])}, stop {_fr(c['stop'])} → {out}")
    line = lab_line()
    if line:
        lines.append(line)
    if st["soldes"] < 30:
        lines.append("-# Moins de 30 setups soldés : chiffres indicatifs, pas encore une mesure.")
    return "\n".join(lines)


HELP = """**JARVIS Killmode** (désactivé par défaut, toi seul l'allumes ; aucune routine, aucun ordre passé)
`!kill on` / `!kill off`   allumer / éteindre Killmode
`!kill NVDA`               analyse complète : volatilité, catalyseur, graphique, données, fiche société
`!kill scan`               les plus forts gaps du jour, seuls les setups complets sont gardés
`!kill bilan`              setups proposés soldés après la clôture, et la stratégie `killmode` du labo
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
                + f" · {len(calls())} setups notés.")
    if not active():
        return "Killmode est éteint. `!kill on` pour l'allumer (toi seul peux le faire)."
    if sub == "scan":
        r = await scan(now)
        for a in r["setups"]:
            record(a, now)
        return scan_text(r)
    if sub == "bilan":
        await settle_due(now)
        return bilan_text(calls())
    a = await analyze(args[0], now)
    record(a, now)
    return text(a)
