"""Scanner avant l'ouverture US : 3 à 5 actions « en jeu », chacune avec son plan EmperorBTC et sa note.

Repris de l'agent scanner d'OpenClaw, mais calculé au lieu d'être deviné, et revu :
- univers : les 100 plus actives et les 50 plus fortes hausses (screener Alpaca), plus la liste
  suivie et `scan_universe` ; les ETF tombent d'eux-mêmes (pas de profil de société chez Finnhub) ;
- en jeu : volume de la veille ≥ 1,5× la moyenne 20 jours OU gap de pré-ouverture ≥ 2 % ;
- tradable : prix ≥ 5 $ (pas de plafond), amplitude journalière (ATR) ≥ 2,5 % pour que des TP
  à +5-10 % soient réalistes ; ni liquidité ni capitalisation minimales (choix de Simão) ;
- dans la tendance : au-dessus des MM 20 et 50 jours, RSI 14 jours entre 45 et 75 ;
- catalyseur réel (Finnhub) : pas d'augmentation de capital, pas de résultats dans les 48 h.
Lot B : sous chaque plan, la part des séances du labo ouvertes au même gap qui ont touché +8 % depuis
l'ouverture (app/lab/potentiel.py, lab_potentiel.json) ; une mesure affichée, jamais un filtre.
Tous ces seuils sont dans app/trading/settings.py et se changent sans toucher au code.
Chaque passage est gardé dans data/scans.jsonl avec les mesures de chaque valeur retenue :
le forward-test (paper.py) s'en sert pour dire quels critères ont vraiment payé.
Labo 3 : les filtres de la méthode championne du labo (app/lab/live.py) s'appliquent aux plans ; un
plan écarté n'est pas proposé (ni passé sur le compte fictif) mais reste listé dans le message et
dans data/scans.jsonl (« ecartees_methode »).
"""
import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app import config
from app.core.http import SourceError
from app.lab import live as LAB
from app.lab import potentiel as PO
from app.sources import alpaca
from app.trading import catalyst as C
from app.trading import indicators as I
from app.trading import plan as P
from app.trading import service as T
from app.trading.settings import settings

log = logging.getLogger(__name__)
NEW_YORK = ZoneInfo("America/New_York")
SCANS_PATH = os.getenv("JARVIS_SCANS_PATH", "data/scans.jsonl")
TICKER = re.compile(r"^[A-Z]{1,5}$")


class ScanError(Exception):
    pass


def features(daily: list[dict], price: float, prev_close: float | None, today: str) -> dict | None:
    """Mesures journalières d'une action (séances terminées seulement) au prix de pré-ouverture."""
    done = [c for c in daily if datetime.fromtimestamp(c["t"], NEW_YORK).date().isoformat() < today]
    if len(done) < 51:
        return None
    closes = [c["c"] for c in done]
    vols = [c["v"] for c in done]
    avg_vol = sum(vols[-21:-1]) / 20
    prev_close = prev_close or closes[-1]
    return {
        "price": price,
        "gap_pct": round((price / prev_close - 1) * 100, 2),
        "rvol": round(vols[-1] / avg_vol, 2) if avg_vol else None,
        "dollar_volume": round(sum(c["c"] * c["v"] for c in done[-20:]) / 20),
        "atr_pct": round(P.atr(done) / closes[-1] * 100, 2),
        "rsi": round(I.rsi_series(closes)[-1], 1),
        "ma20": round(I.sma(closes, 20), 4),
        "ma50": round(I.sma(closes, 50), 4),
        "change_5d_pct": round((closes[-1] / closes[-6] - 1) * 100, 2),
        "day_change_pct": round((closes[-1] / closes[-2] - 1) * 100, 3) if closes[-2] > 0 else None,
    }


def filter_reasons(f: dict, s) -> list[str]:
    """Critères ratés (liste vide = la valeur passe)."""
    out = []
    if f["price"] < s["scan_price_min"] or (s["scan_price_max"] and f["price"] > s["scan_price_max"]):
        out.append("prix")
    if not ((f["rvol"] or 0) >= s["scan_rvol_min"] or f["gap_pct"] >= s["scan_gap_min_pct"]):
        out.append("pas en jeu")
    if f["atr_pct"] < s["scan_atr_pct_min"]:
        out.append("trop calme")
    if s["scan_above_ma20"] and f["price"] < f["ma20"]:
        out.append("sous la MM20")
    if s["scan_above_ma50"] and f["price"] < f["ma50"]:
        out.append("sous la MM50")
    if not s["rsi_min"] <= f["rsi"] <= s["rsi_max"]:
        out.append("RSI")
    return out


def pre_score(f: dict) -> float:
    """Classement avant le plan : volume inhabituel, gap et amplitude, plafonnés pour qu'un seul ne domine pas."""
    return min(f["rvol"] or 0, 5) + min(max(f["gap_pct"], 0), 10) / 2 + min(f["atr_pct"], 10) / 2


async def universe(s) -> list[str]:
    actives, gainers = await asyncio.gather(alpaca.most_actives(100), alpaca.top_gainers(50), return_exceptions=True)
    symbols = []
    for group in (actives, gainers, config.WATCHLIST_STOCKS, s["scan_universe"]):
        if isinstance(group, Exception):
            log.warning("Screener Alpaca : %s", group)
            continue
        symbols += [x.upper() for x in group]
    return list(dict.fromkeys(x for x in symbols if TICKER.match(x)))


async def scan(now: datetime | None = None, s=settings, save: bool = True) -> dict:
    if not alpaca.configured():
        raise ScanError("clés Alpaca absentes (ALPACA_API_KEY, ALPACA_SECRET_KEY) : le scanner en a besoin")
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(NEW_YORK).date().isoformat()
    try:
        symbols = await universe(s)
        snaps = await alpaca.snapshots(symbols)
        daily = await alpaca.bars(symbols, 1440, days=100, now=now)
    except SourceError as exc:
        raise ScanError(f"données Alpaca indisponibles : {exc}") from exc
    rejected: dict[str, int] = {}
    passed = []
    for symbol in symbols:
        snap = snaps.get(symbol) or {}
        rows = daily.get(symbol) or []
        price = snap.get("price") or (rows[-1]["c"] if rows else None)
        f = features(rows, price, None, today) if price else None  # veille : clôture SIP, plus juste que l'IEX
        if not f:
            rejected["historique"] = rejected.get("historique", 0) + 1
            continue
        misses = filter_reasons(f, s)
        for m in misses:
            rejected[m] = rejected.get(m, 0) + 1
        if not misses:
            passed.append((symbol, f))
    passed.sort(key=lambda x: pre_score(x[1]), reverse=True)

    # Catalyseur (3 appels Finnhub par valeur) sur les mieux classées seulement.
    shortlist = passed[: s["scan_max_results"] * 3]
    # 4 valeurs à la fois (12 appels) : les 45 appels d'un coup dépassaient la limite de Finnhub (429),
    # et un calendrier des résultats en échec faisait sauter le veto résultats.
    gate = asyncio.Semaphore(4)

    async def _check(sym: str):
        async with gate:
            return await C.safe_check(sym, now)

    cats = await asyncio.gather(*(_check(sym) for sym, _ in shortlist))
    picks, skipped, filtered = [], [], []
    champ = LAB.current()
    index_daily = await _index_daily(now) if champ["regles"] else None
    for (symbol, f), cat in zip(shortlist, cats):
        # ETF seulement si Finnhub a bien répondu sans profil de société ; un profil en échec (quota,
        # délai) n'exclut rien, et la capitalisation n'entre jamais en compte (choix de Simão).
        if cat and cat.get("company") is False:
            skipped.append(f"{symbol} (pas une société : ETF ?)")
            continue
        if s["scan_exclude_dilution"] and cat and cat.get("dilution"):
            skipped.append(f"{symbol} (augmentation de capital)")
            continue
        if cat and s["earnings_veto_hours"] and not cat.get("earnings_known", True):
            skipped.append(f"{symbol} (date des résultats non vérifiée)")
            continue
        hours = (cat or {}).get("hours_to_earnings")
        if hours is not None and s["earnings_veto_hours"] and hours <= s["earnings_veto_hours"]:
            skipped.append(f"{symbol} (résultats dans {hours:.0f} h)")
            continue
        try:
            plan = await T.build_plan(symbol, "stock", "long", entry=round(f["price"], 2), now=now,
                                      source="alpaca", cat=cat)
        except T.PlanError as exc:
            skipped.append(f"{symbol} (plan impossible : {exc})")
            continue
        if champ["regles"]:
            try:  # bougies déjà en cache (lues par build_plan) : celles de la note du plan
                daily_score = await T._optional_candles(symbol, "stock", 1440, "alpaca")
            except Exception:
                daily_score = daily.get(symbol)
            verdict = LAB.verdict_for(plan, daily_score, daily.get(symbol), index_daily, now, s, champ)
            if not verdict["garde"]:
                filtered.append({"symbol": symbol, "version": verdict["version"], "regles": verdict["regles"],
                                 "composite": plan["score"]["composite"]})
                continue
        picks.append({"symbol": symbol, "features": f, "plan": plan, "potentiel": PO.hint(f["gap_pct"])})
        if len(picks) == s["scan_max_results"]:
            break
    picks.sort(key=lambda p: p["plan"]["score"]["composite"], reverse=True)
    result = {"at": now.isoformat(timespec="seconds"), "examined": len(symbols), "passed": len(passed),
              "rejected": rejected, "skipped": skipped, "picks": picks, "ecartees_methode": filtered,
              "methode": champ.get("version")}
    if save:
        _save(result)
    return result


async def _index_daily(now: datetime) -> list[dict] | None:
    """~500 jours de SPY pour le régime de marché des filtres du labo ; None si Alpaca ne répond pas."""
    try:
        return (await alpaca.bars(["SPY"], 1440, days=500, now=now)).get("SPY")
    except Exception as exc:  # le scanner continue : les règles sur le régime ne seront pas vérifiées
        log.warning("Régime de marché (SPY) indisponible pour les filtres du labo : %s", exc)
        return None


def _save(result: dict) -> None:
    path = Path(SCANS_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    light = result | {"picks": [
        {"symbol": p["symbol"], "features": p["features"], "entry": p["plan"]["entry"], "stop": p["plan"]["stop"],
         "targets": [t["price"] for t in p["plan"]["targets"]], "score": p["plan"]["score"]}
        for p in result["picks"]]}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(light, ensure_ascii=False) + "\n")


def _rvol(rvol: float | None) -> str:
    return f"{rvol:g}×" if rvol is not None else "n/d"


def text(result: dict) -> str:
    """Le scanner en clair pour Discord."""
    now = datetime.fromisoformat(result["at"])
    picks = result["picks"]
    go = sum(1 for p in picks if p["plan"]["score"]["go"])
    lines = [f"🔭 **Scanner pré-ouverture** · {T.time_fr(now, now)} · {result['examined']} valeurs examinées, "
             f"{result['passed']} passent les filtres, {len(picks)} plans dont {go} GO"]
    if not picks:
        lines.append("Rien qui coche tout aujourd'hui.")
    for i, p in enumerate(picks, start=1):
        f, plan, sc = p["features"], p["plan"], p["plan"]["score"]
        parts = sc["parts"]
        sub = " · ".join(f"{label} {parts[k]['score']:g}" for k, label in
                         (("technique", "tech"), ("catalyst", "cata"), ("risk", "risque")) if parts[k]["score"] is not None)
        lines.append(f"\n**{i}. {p['symbol']}** {f['gap_pct']:+.1f} % en pré-ouverture · volume veille {_rvol(f['rvol'])} · "
                     f"RSI {f['rsi']:.0f} · ATR {f['atr_pct']:.1f} %")
        lines.append(f"{'✅' if sc['go'] else '⛔'} **{sc['decision']} · {sc['composite']:g}/10** ({sub})"
                     + (f" · {' ; '.join(sc['reasons'])}" if sc["reasons"] else ""))
        tps = " · ".join(
            f"{t['name']} {T._num(t['price'])} ({t['pct']:+.1f} %"
            + (f", ~{T.duration_fr(t['minutes_median'])})" if t["minutes_median"] else ")")
            for t in plan["targets"])
        lines.append(f"entrée {T._num(plan['entry'])} · SL {T._num(plan['stop'])} ({plan['stop_pct']:+.1f} %) · {tps}")
        lines += T.confirmation_lines(plan)
        if p.get("potentiel"):
            lines.append(p["potentiel"])
        news = (plan.get("catalyst") or {}).get("news") or []
        if news:
            lines.append(f"📰 [{news[0]['title'][:100]}](<{news[0]['url']}>)")
    if result["skipped"]:
        lines.append("\n-# Écartées : " + ", ".join(result["skipped"]))
    if result.get("ecartees_methode"):
        lines.append(f"-# Écartées par la méthode {result.get('methode') or ''} du labo : " + ", ".join(
            f"{e['symbol']} ({', '.join(f'« {n} »' for n in e['regles'])})" for e in result["ecartees_methode"]))
    if result["rejected"]:
        lines.append("-# Filtres : " + ", ".join(f"{k} {v}" for k, v in sorted(result["rejected"].items(), key=lambda x: -x[1])))
    lines.append("-# Données Alpaca (pré-ouverture, bougies avec 16 min de retard) et Finnhub. "
                 "`!plan TICKER` pour un plan à jour en temps réel.")
    return "\n".join(lines)
