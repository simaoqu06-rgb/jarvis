"""Contrôle du catalyseur d'une action, à partir de données réelles (Finnhub) et jamais devinées :
prochains et derniers résultats trimestriels, actus des 7 derniers jours qui parlent vraiment de la
société, et leur tonalité.

La tonalité est une lecture par mots-clés des titres (le score de sentiment de Finnhub est payant) :
elle repère les cas nets (relèvement, contrat, résultats battus / abaissement, augmentation de capital,
enquête). Les titres et leurs liens sont toujours joints pour vérifier d'un coup d'œil.
"""
import asyncio
import logging
import re
from datetime import date, datetime, timedelta, timezone

from app.core.http import SourceError
from app.sources import finnhub

log = logging.getLogger(__name__)

POSITIVE = [
    r"upgrade", r"raises? (its )?(guidance|outlook|forecast)", r"\bbeats?\b", r"tops? (estimates|expectations)",
    r"record (high|revenue|quarter|sales)", r"hits? (a )?record", r"wins? .*contract", r"awarded", r"contract",
    r"partnership", r"approv", r"acquired by", r"buyout", r"takeover", r"buyback", r"repurchase",
    r"\bsurg", r"\bsoar", r"\bjump", r"rocket", r"\brall(y|ies)", r"outperform", r"\bboom", r"breakthrough",
    r"price target (raised|hike)", r"raises? .*price target", r"\bbuy rating", r"overweight",
]
NEGATIVE = [
    r"downgrade", r"cuts? (its )?(guidance|outlook|forecast)", r"\bmiss(es|ed)?\b", r"falls short", r"warns?\b",
    r"lawsuit", r"investigation", r"\bprobe", r"\bsec\b", r"recall", r"\bplung", r"\btumbl", r"\bsinks?\b",
    r"\bslides?\b", r"\bslump", r"\bcrash", r"underperform", r"\bsell rating", r"underweight",
    r"cuts? .*price target", r"bankruptcy", r"delist", r"\bhalt", r"short seller", r"fraud", r"resign",
]
# Augmentation de capital : nouvelles actions = pression vendeuse (critère d'exclusion du scanner).
DILUTION = [
    r"public offering", r"stock offering", r"share offering", r"secondary offering", r"registered direct",
    r"private placement", r"at[- ]the[- ]market", r"\batm (program|offering)", r"prices? .*offering",
    r"convertible notes", r"shelf registration", r"dilut",
]


def _has(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text) for p in patterns)


def tone(title: str) -> str:
    text = title.lower()
    if _has(DILUTION, text):
        return "dilution"
    neg, pos = _has(NEGATIVE, text), _has(POSITIVE, text)
    if pos and not neg:
        return "positive"
    if neg and not pos:
        return "negative"
    return "neutre"


def _about(item: dict, symbol: str, name: str | None) -> bool:
    """Garde les actus qui parlent de la société (Finnhub en renvoie beaucoup qui la citent à peine)."""
    title = (item.get("title") or "")
    if re.search(rf"\b{re.escape(symbol)}\b", title):
        return True
    if name:
        first = re.sub(r"[,.]|\b(inc|corp|corporation|co|ltd|plc|holdings|group|class [a-z])\b", "", name, flags=re.I).split()
        if first and len(first[0]) >= 3 and re.search(rf"\b{re.escape(first[0])}", title, re.I):
            return True
    return False


def _earnings_moment(e: dict) -> datetime:
    """Heure approximative de publication : avant l'ouverture (bmo) 13 h UTC, après la clôture (amc) 21 h UTC."""
    hour = {"bmo": 12, "amc": 21}.get(e.get("hour") or "", 16)
    return datetime.fromisoformat(e["date"]).replace(hour=hour, tzinfo=timezone.utc)


async def check(symbol: str, now: datetime | None = None, days: int = 7) -> dict:
    """Catalyseur d'une action. `available` vaut False si Finnhub ne répond pas (la note l'ignore alors)."""
    now = now or datetime.now(timezone.utc)
    today = now.date()
    out = {"available": False, "name": None, "market_cap": None, "next_earnings": None, "hours_to_earnings": None,
           "last_earnings": None, "news": [], "positive": 0, "negative": 0, "dilution": False, "fresh_news": False,
           "errors": []}
    profile, earnings, news = await asyncio.gather(
        finnhub.profile(symbol), finnhub.earnings(symbol, today - timedelta(days=10), today + timedelta(days=30)),
        finnhub.company_news(symbol, 60, days=days), return_exceptions=True,
    )
    if isinstance(profile, dict):
        out["name"], out["market_cap"] = profile["name"], profile["market_cap"]
    for label, value in (("profil", profile), ("résultats", earnings), ("actus", news)):
        if isinstance(value, Exception):
            out["errors"].append(f"{label} : {value}")
    if isinstance(earnings, list):
        out["available"] = True
        upcoming = sorted((e for e in earnings if _earnings_moment(e) >= now), key=_earnings_moment)
        past = sorted((e for e in earnings if _earnings_moment(e) < now), key=_earnings_moment)
        if upcoming:
            e = upcoming[0]
            out["next_earnings"] = e
            out["hours_to_earnings"] = round((_earnings_moment(e) - now).total_seconds() / 3600, 1)
        if past:
            e = past[-1]
            surprise = None
            if e["eps_actual"] is not None and e["eps_estimate"]:
                surprise = round((e["eps_actual"] - e["eps_estimate"]) / abs(e["eps_estimate"]) * 100, 1)
            out["last_earnings"] = e | {"eps_surprise_pct": surprise,
                                        "days_ago": (today - date.fromisoformat(e["date"])).days}
    if isinstance(news, list):
        out["available"] = True
        relevant = [n for n in news if _about(n, symbol, out["name"])]
        for n in relevant[:8]:
            t = tone(n["title"] or "")
            out["news"].append({"title": n["title"], "url": n["url"], "published_at": n["published_at"], "tone": t})
        tones = [tone(n["title"] or "") for n in relevant]
        out["positive"], out["negative"] = tones.count("positive"), tones.count("negative")
        out["dilution"] = "dilution" in tones
        fresh = [n for n in relevant if n["published_at"] and
                 now - datetime.fromisoformat(n["published_at"]) <= timedelta(hours=48)]
        out["fresh_news"] = bool(fresh)
    return out


async def safe_check(symbol: str, now: datetime | None = None) -> dict | None:
    try:
        return await check(symbol, now)
    except SourceError as exc:  # clé absente
        log.info("Catalyseur %s indisponible : %s", symbol, exc)
        return None
