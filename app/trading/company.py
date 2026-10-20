"""Fiche société d'une action (`!societe NVDA`, GET /api/trading/company) : ce que JARVIS sait de l'entreprise
avant de donner un avis (demande de Simão, 2026-10-08 : « qu'il soit expert de l'entreprise, que rien ne
lui échappe »).

Uniquement des faits datés, jamais devinés (Finnhub gratuit et bougies journalières) :
  - profil : secteur, capitalisation, nombre d'actions, bourse ;
  - résultats : prochaine publication (90 jours) et BPA des derniers trimestres, battu ou manqué ;
  - un an de cours : record et plus bas sur 52 semaines, MM 50 et 200 jours, ATR journalier, bêta ;
  - les plus gros mouvements d'une séance sur un an, avec le titre d'actu de ce jour-là ;
  - chiffres clés : croissance du chiffre d'affaires, marges, valorisation ;
  - analystes (dernier mois) et dirigeants (ventes et achats en bourse sur 90 jours) ;
  - dépôts SEC d'émission de titres sur 30 jours (S-1, S-3, 424B : augmentation de capital ou obligations) ;
  - actus des 7 derniers jours et leur tonalité (lue par mots-clés : le serveur ne lit pas les articles,
    les liens sont joints).
Ce qui change un trade (résultats proches, augmentation de capital) est appliqué par `!avis` et `!position`
(app/trading/advice.py) ; la fiche donne le reste du tableau.
"""
import asyncio
import logging
import time
from datetime import date, datetime, time as dtime, timedelta, timezone

from app.core.http import SourceError
from app.sources import finnhub
from app.trading import catalyst as C
from app.trading import indicators as I
from app.trading import levels as L
from app.trading import plan as P
from app.trading import service as S

log = logging.getLogger(__name__)
TTL = 1800             # fiche resservie pendant 30 min (une quinzaine d'appels Finnhub par fiche)
MOVES = 4              # plus fortes variations d'une séance gardées sur un an
INSIDER_DAYS = 90
FILING_DAYS = 30
EARNINGS_DAYS = 90
ISSUE_FORMS = ("S-1", "S-3", "F-1", "F-3", "424B")  # préfixes : S-3ASR, 424B5…
PARTS_FR = {"profile": "profil", "metric": "chiffres clés", "analysts": "analystes", "insiders": "dirigeants",
            "filings": "dépôts SEC", "surprises": "résultats passés", "calendar": "calendrier des résultats"}
TONE = {"positive": "🟢", "negative": "🔴", "dilution": "🟠", "neutre": "⚪"}
_cache: dict[str, tuple[float, dict]] = {}


def _date_fr(d: date) -> str:
    return f"{'1er' if d.day == 1 else d.day} {L.MONTHS[d.month - 1]}"


def _ny_day(t: int) -> date:
    return datetime.fromtimestamp(t, I.NEW_YORK).date()


def money(x: float | None) -> str | None:
    if not x:
        return None
    if x >= 1e9:
        return f"{x / 1e9:,.0f} Md$".replace(",", " ") if x >= 1e10 else f"{x / 1e9:.1f} Md$"
    if x >= 1e6:
        return f"{x / 1e6:,.0f} M$".replace(",", " ")
    return f"{x:,.0f} $".replace(",", " ")


def count(x: float) -> str:
    if abs(x) >= 1e9:
        return f"{x / 1e9:.2f} Md"
    if abs(x) >= 1e6:
        return f"{x / 1e6:,.0f} M".replace(",", " ")
    return f"{x:,.0f}".replace(",", " ")


def big_moves(daily: list[dict], n: int = MOVES) -> list[dict]:
    """Les `n` plus fortes variations d'une séance sur un an (clôture contre clôture de la veille), avec le gap
    d'ouverture et les bornes de temps où chercher leur cause (de la clôture de la veille à celle du jour)."""
    year = daily[-(L.YEAR_BARS + 1):]
    out = []
    for a, b in zip(year, year[1:]):
        if a["c"]:
            day, prev = _ny_day(b["t"]), _ny_day(a["t"])
            out.append({"day": day.isoformat(), "pct": round((b["c"] / a["c"] - 1) * 100, 1),
                        "gap_pct": round((b["o"] / a["c"] - 1) * 100, 1),
                        "from_ts": int(datetime.combine(prev, dtime(16), I.NEW_YORK).timestamp()),
                        "to_ts": int(datetime.combine(day, dtime(16), I.NEW_YORK).timestamp())})
    return sorted(out, key=lambda m: -abs(m["pct"]))[:n]


def move_headline(items: list[dict], symbol: str, name: str | None, move: dict) -> dict | None:
    """Le titre qui explique le mieux un mouvement : une actu sur la société publiée entre la clôture de la
    veille et celle du jour, de la même tonalité que le mouvement si possible, la plus ancienne d'abord."""
    found = sorted((n for n in items if move["from_ts"] <= (n.get("published_ts") or 0) <= move["to_ts"]
                    and C._about(n, symbol, name)), key=lambda n: n["published_ts"])
    want = ("positive",) if move["pct"] > 0 else ("negative", "dilution")
    best = next((n for n in found if C.tone(n["title"] or "") in want), found[0] if found else None)
    return {"title": best["title"], "url": best["url"]} if best else None


async def _headline(symbol: str, name: str | None, move: dict) -> dict | None:
    day = date.fromisoformat(move["day"])
    try:
        items = await finnhub.company_news_between(symbol, day - timedelta(days=4), day)
    except SourceError as exc:
        log.info("Actus du %s pour %s indisponibles : %s", move["day"], symbol, exc)
        return None
    return move_headline(items, symbol, name, move)


async def sheet(symbol: str, now: datetime | None = None) -> dict:
    """Fiche société (servie depuis le cache pendant TTL). SourceError si rien n'est disponible."""
    symbol = symbol.strip().upper()
    now = now or datetime.now(timezone.utc)
    hit = _cache.get(symbol)
    if hit and time.monotonic() - hit[0] < TTL:
        return hit[1]
    today = now.date()
    calls = {
        "profile": finnhub.profile(symbol),
        "metric": finnhub.basic_financials(symbol),
        "analysts": finnhub.recommendations(symbol),
        "insiders": finnhub.insider_transactions(symbol, today - timedelta(days=INSIDER_DAYS), today),
        "filings": finnhub.filings(symbol, today - timedelta(days=FILING_DAYS), today),
        "surprises": finnhub.earnings_surprises(symbol),
        "calendar": finnhub.earnings(symbol, today, today + timedelta(days=EARNINGS_DAYS)),
    }
    results = await asyncio.gather(*calls.values(), return_exceptions=True)
    out: dict = {"symbol": symbol, "at": now.isoformat(timespec="seconds"), "errors": []}
    for key, value in zip(calls, results):
        if isinstance(value, SourceError):
            out[key] = None
            out["errors"].append(f"{PARTS_FR[key]} : {value}")
        elif isinstance(value, BaseException):
            raise value
        else:
            out[key] = value
    out["catalyst"] = await C.safe_check(symbol, now)
    daily = await S._optional_candles(symbol, "stock", 1440)
    if out["profile"] is None and not daily:
        raise SourceError(f"{symbol} : ni profil ni cours ({' ; '.join(out['errors']) or 'ticker inconnu ?'})")
    out["price"], out["moves"] = None, []
    if daily and len(daily) > P.ATR_PERIOD:
        year = daily[-L.YEAR_BARS:]
        hi, lo = max(year, key=lambda b: b["h"]), min(year, key=lambda b: b["l"])
        closes = [b["c"] for b in daily]
        out["price"] = {"last": daily[-1]["c"], "day": _ny_day(daily[-1]["t"]).isoformat(),
                        "high": hi["h"], "high_day": _ny_day(hi["t"]).isoformat(),
                        "low": lo["l"], "low_day": _ny_day(lo["t"]).isoformat(),
                        "ma50": I.sma(closes, 50), "ma200": I.sma(closes, 200), "atr": P.atr(daily)}
        name = (out["profile"] or {}).get("name")
        moves = big_moves(daily)
        heads = await asyncio.gather(*(_headline(symbol, name, m) for m in moves))
        out["moves"] = [m | {"news": h} for m, h in zip(moves, heads)]
    _cache[symbol] = (time.monotonic(), out)
    return out


def _metric(m: dict, *keys: str) -> float | None:
    for k in keys:
        v = m.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def _pct(x: float) -> str:
    return f"{x:+.1f} %"


def next_earnings(s: dict, today: date) -> dict | None:
    rows = [e for e in s.get("calendar") or [] if e.get("date") and date.fromisoformat(e["date"]) >= today]
    return min(rows, key=lambda e: e["date"]) if rows else None


def issue_filings(s: dict) -> list[dict]:
    return [f for f in s.get("filings") or [] if str(f["form"]).upper().startswith(ISSUE_FORMS)]


def sheet_text(s: dict) -> str:
    sym, prof = s["symbol"], s.get("profile") or {}
    today = datetime.fromisoformat(s["at"]).date()
    head = [prof.get("industry"), money(prof.get("market_cap")) and f"capitalisation {money(prof.get('market_cap'))}",
            prof.get("shares") and f"{count(prof['shares'])} d'actions", prof.get("exchange")]
    lines = [f"🏢 **Fiche {prof.get('name') or sym}** ({sym})" + "".join(f" · {h}" for h in head if h)]

    # Résultats : ce qui peut faire un gap binaire.
    nxt = next_earnings(s, today)
    if nxt:
        moment = {"bmo": " avant l'ouverture", "amc": " après la clôture"}.get(nxt.get("hour") or "", "")
        days = (date.fromisoformat(nxt["date"]) - today).days
        text = f"📅 Prochains résultats le {_date_fr(date.fromisoformat(nxt['date']))}{moment} (dans {days} j)"
    elif s.get("calendar") is not None:
        text = f"📅 Aucune publication de résultats annoncée d'ici {EARNINGS_DAYS} jours"
    else:
        text = "📅 Calendrier des résultats indisponible"
    past = [e for e in s.get("surprises") or [] if e["surprise_pct"] is not None]
    if past:
        beat = sum(e["surprise_pct"] > 0 for e in past)
        text += (f" · BPA des {len(past)} derniers trimestres : battu {beat} fois sur {len(past)} ("
                 + ", ".join(_pct(e["surprise_pct"]) for e in past) + ", du plus récent au plus ancien)")
    lines.append(text)

    pr = s.get("price")
    metric = s.get("metric") or {}
    if pr:
        last = pr["last"]
        bits = [f"📈 Cours {S._num(last)} ({_date_fr(date.fromisoformat(pr['day']))})",
                f"record sur 52 semaines {S._num(pr['high'])} le {_date_fr(date.fromisoformat(pr['high_day']))} "
                f"({_pct((last / pr['high'] - 1) * 100)})",
                f"plus bas {S._num(pr['low'])} le {_date_fr(date.fromisoformat(pr['low_day']))} "
                f"({_pct((last / pr['low'] - 1) * 100)})"]
        for key, label in (("ma50", "MM 50 j"), ("ma200", "MM 200 j")):
            if pr[key]:
                bits.append(f"{label} {S._num(round(pr[key], 2))} ({'au-dessus' if last > pr[key] else 'en dessous'})")
        bits.append(f"ATR journalier {S._num(round(pr['atr'], 2))} ({pr['atr'] / last * 100:.1f} % du cours)")
        beta = _metric(metric, "beta")
        if beta is not None:
            bits.append(f"bêta {beta:.2f}")
        lines.append(" · ".join(bits))
    if s.get("moves"):
        lines.append("⚡ Plus fortes séances sur un an :")
        for m in s["moves"]:
            news = m.get("news")
            cause = f"[{news['title'][:110]}](<{news['url']}>)" if news else "aucune actu de la société trouvée ce jour-là"
            lines.append(f"• {_pct(m['pct'])} le {_date_fr(date.fromisoformat(m['day']))} "
                         f"(ouverture {_pct(m['gap_pct'])}) : {cause}")

    figures = []
    growth, quarter = _metric(metric, "revenueGrowthTTMYoy"), _metric(metric, "revenueGrowthQuarterlyYoy")
    if growth is not None:
        figures.append(f"Chiffre d'affaires {_pct(growth)} sur un an"
                       + (f" ({_pct(quarter)} au dernier trimestre)" if quarter is not None else ""))
    for key, label in (("grossMarginTTM", "marge brute"), ("netProfitMarginTTM", "marge nette")):
        v = _metric(metric, key)
        if v is not None:
            figures.append(f"{label} {v:.0f} %")
    pe = _metric(metric, "peTTM", "peExclExtraTTM", "peBasicExclExtraTTM")
    ps = _metric(metric, "psTTM")
    if pe is not None:
        figures.append(f"PER {pe:.0f}" if pe > 0 else "pertes sur un an (PER négatif)")
    if ps is not None:
        figures.append(f"cours/ventes {ps:.1f}")
    if figures:
        lines.append("💵 " + " · ".join(figures))

    rec = (s.get("analysts") or [None])[0]
    if rec:
        buys, sells = rec["strong_buy"] + rec["buy"], rec["sell"] + rec["strong_sell"]
        month = date.fromisoformat(rec["period"][:10])
        lines.append(f"👥 Analystes ({L.MONTHS[month.month - 1]} {month.year}) : {buys} à l'achat "
                     f"(dont {rec['strong_buy']} achat fort), {rec['hold']} neutres, {sells} à la vente")
    ins = s.get("insiders")
    if ins is not None:
        sales = [t for t in ins if t["code"] == "S" and t["change"]]
        buys = [t for t in ins if t["code"] == "P" and t["change"]]
        if sales or buys:
            parts = [f"{len(sales)} vente{'s' if len(sales) > 1 else ''} en bourse ({count(sum(t['change'] for t in sales))} "
                     "actions)" if sales else "aucune vente en bourse",
                     f"{len(buys)} achat{'s' if len(buys) > 1 else ''} (+{count(sum(t['change'] for t in buys))} actions)"
                     if buys else "aucun achat"]
            lines.append(f"🧑‍💼 Dirigeants sur {INSIDER_DAYS} jours : " + ", ".join(parts))
        else:
            lines.append(f"🧑‍💼 Dirigeants : aucune vente ni achat en bourse sur {INSIDER_DAYS} jours")
    if s.get("filings") is not None:
        issues = issue_filings(s)
        if issues:
            lines.append(f"🗂️ Dépôts SEC sur {FILING_DAYS} jours : "
                         + ", ".join(f"[{f['form']}](<{f['url']}>) le {_date_fr(date.fromisoformat(f['filed']))}"
                                     if f.get("url") else f"{f['form']} le {_date_fr(date.fromisoformat(f['filed']))}"
                                     for f in issues[:4])
                         + " : émission de titres possible (actions ou obligations), à vérifier dans le document")
        else:
            lines.append(f"🗂️ Aucun dépôt SEC d'émission de titres (S-1, S-3, 424B) sur {FILING_DAYS} jours")

    cat = s.get("catalyst") or {}
    if cat.get("available"):
        pos, neg = cat.get("positive", 0), cat.get("negative", 0)
        dil = " · ⚠️ augmentation de capital dans les actus" if cat.get("dilution") else ""
        lines.append(f"📰 Actus 7 j : {pos} positive{'s' if pos > 1 else ''}, {neg} négative{'s' if neg > 1 else ''}{dil}")
        lines += [f"{TONE[n['tone']]} [{n['title'][:110]}](<{n['url']}>)" for n in cat.get("news", [])[:5]]

    stakes = []
    if nxt and (date.fromisoformat(nxt["date"]) - today).days <= 14:
        stakes.append("résultats dans moins de 15 jours : `!avis` n'entre pas dans les 48 h qui précèdent et "
                      "`!position` dit de sortir avant")
    if cat.get("dilution"):
        stakes.append("augmentation de capital dans les actus : `!avis` ne propose pas de long, `!position` dit de sortir")
    lines.append("⚖️ Pour un trade : " + (" ; ".join(stakes) if stakes else
                                          "rien dans le calendrier ni dans les actus n'oblige à sortir ou à attendre."))
    foot = ("-# Sources : Finnhub (profil, chiffres clés, analystes, dirigeants, dépôts SEC, résultats, actus) et bougies "
            "journalières. Les actus sont classées par mots-clés, pas lues : ouvre les liens.")
    if s.get("errors"):
        causes = {e.split(" : ", 1)[-1] for e in s["errors"]}
        foot += (f" Finnhub indisponible : {causes.pop()}." if len(causes) == 1 and len(s["errors"]) == len(PARTS_FR)
                 else " Indisponible : " + " ; ".join(s["errors"]) + ".")
    lines.append(foot)
    return "\n".join(lines)
