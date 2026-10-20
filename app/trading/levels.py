"""Carte des niveaux d'un actif (formation EmperorBTC) : d'où viennent les prix concrets que donne JARVIS
sur une position ou un actif (où vendre, où mettre le stop, où renforcer, où entrer).

Un niveau vient toujours d'un endroit où le prix a réagi, et sa source est citée dans le texte :
  - sommets et creux de swing des bougies du plan (15 min pour les actions, 1 h pour la crypto), retenus
    seulement si le prix s'en est éloigné d'au moins 1 ATR ensuite (comme les zones du labo, déclaration n° 3) ;
  - sommets et creux de swing journaliers sur un an ;
  - plus haut et plus bas du jour, de la veille et sur 52 semaines ;
  - gaps journaliers pas encore comblés (actions) ;
  - VWAP du jour et range des 30 premières minutes (actions).
Des niveaux à moins de 0,5 ATR du premier niveau d'une zone forment une seule zone (comme le labo) ; ses
contacts sont les sommets, creux et extrêmes de séance distincts (à plus d'une heure d'écart). Une zone est
« solide » si elle a au moins deux contacts ou vient d'un niveau journalier (swing, veille, jour, 52 semaines,
gap) : seules les zones solides servent de cible ou d'appui ; un sommet isolé de 15 min, le VWAP ou le range
d'ouverture restent affichés comme niveaux, sans décider d'un prix (council du 2026-10-08 : le labo ne les a
pas mesurés).

Le stop suit la structure (sous le dernier creux de swing, et sous toute sa zone, à au moins 1 ATR du prix,
jamais redescendu), les ventes se placent juste avant chaque résistance, les entrées et renforts se font sur
un repli vers un support ou sur la cassure d'une résistance (EmperorBTC : « Ajout sur un retest. Ajout lors
de la rupture Swing. »), avec leur R/R. Ces zones disent où le prix a réagi, pas où il va : le labo n'a
encore validé aucune règle d'entrée ni de sortie.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.trading import indicators as I
from app.trading import plan as P
from app.trading import service as S

SWING_SIDE = 3              # sommets et creux de réaction des bougies du plan : 3 bougies de chaque côté (comme v1)
DAILY_SWING_SIDE = 2        # en journalier, 2 séances de chaque côté (un an de séances : 3 en laisserait trop peu)
REACTION_ATR = 1.0          # un sommet (creux) du plan ne compte que si le prix s'en éloigne d'1 ATR…
REACTION_BARS = 12          # … dans les 12 bougies qui suivent
PLAN_BARS = {"stock": 130, "crypto": 120}  # bougies du plan regardées : 5 séances de 15 min, 5 jours d'1 h
DAILY_BARS = 260            # un an de bougies journalières
YEAR_BARS = 252             # 52 semaines de séances
MERGE_ATR = 0.5             # même zone : à moins de 0,5 ATR (bougies du plan) du premier niveau de la zone
CONTACT_GAP = 3600          # deux contacts à moins d'une heure n'en font qu'un
OPENING_RANGE_MIN = 30
MIN_GAP_DAILY_ATR = 0.1     # partie non comblée d'un gap plus petite que ça (en ATR journalier) : ignorée
FIRST_SALE_ATR = 0.5        # première vente à au moins 0,5 ATR du prix (une résistance collée au prix ne compte pas)
SALE_SPACING_DAILY_ATR = 0.25  # ventes espacées d'au moins un quart d'ATR journalier (1 ATR du plan sans journalier)
MIN_ADD_RISK_ATR = 0.5      # renfort sur repli : au moins 0,5 ATR entre l'achat et le stop de la position
MONTHS = ("janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc.")
UNIT = {"stock": "15 min", "crypto": "1 h"}
STEP = {"stock": 15 * 60, "crypto": 3600}
# Ordre des sources dans le texte d'une zone (les plus parlantes d'abord) ; jusqu'à RANK_GAP, niveau journalier.
RANK_YEAR, RANK_DAILY, RANK_SESSION, RANK_GAP, RANK_SWING, RANK_DAY = range(6)


@dataclass
class Point:
    price: float
    source: str
    rank: int
    span: tuple[int, int] | None = None  # moment du contact (début, fin) ; None : niveau calculé (VWAP, gap…)


@dataclass
class Zone:
    low: float
    high: float
    sources: list[str] = field(default_factory=list)
    contacts: int = 0
    strong: bool = False

    def label(self) -> str:
        return S._num(self.low) if S._num(self.low) == S._num(self.high) else f"{S._num(self.low)}–{S._num(self.high)}"

    def why(self, limit: int = 3) -> str:
        text = ", ".join(self.sources[:limit]) + (" …" if len(self.sources) > limit else "")
        return text + (f", {self.contacts} contacts" if self.contacts >= 2 else "")

    def text(self) -> str:
        return f"{self.label()} ({self.why()})"


def day_fr(t: int, market: str) -> str:
    d = datetime.fromtimestamp(t, I.NEW_YORK if market == "stock" else timezone.utc)
    return f"{'1er' if d.day == 1 else d.day} {MONTHS[d.month - 1]}"


def tick(x: float, market: str) -> float:
    """Prix d'ordre : au centime pour une action (0,0001 sous 1 $), 6 chiffres significatifs pour une crypto."""
    if market == "stock":
        return round(x, 2 if x >= 1 else 4)
    return P.round_price(x, 6)


def _reacted(after: list[dict], price: float, high: bool, move: float) -> bool:
    """Le prix s'est éloigné d'au moins `move` du sommet (high) ou du creux dans les bougies `after`."""
    if not after:
        return False
    return price - min(b["l"] for b in after) >= move if high else max(b["h"] for b in after) - price >= move


def _contacts(spans: list[tuple[int, int]]) -> int:
    """Contacts distincts : des moments séparés de moins de CONTACT_GAP n'en font qu'un (congestion, ou
    sommet de 15 min le jour même d'un sommet journalier)."""
    n, end = 0, None
    for t0, t1 in sorted(spans):
        if end is None or t0 >= end + CONTACT_GAP:
            n += 1
            end = t1
        else:
            end = max(end, t1)
    return n


def _extremes(rows: list[dict], label: str, step: int) -> list[Point]:
    """Plus haut et plus bas d'une séance, datés au moment de la bougie qui les a faits."""
    hi = max(rows, key=lambda b: b["h"])
    lo = min(rows, key=lambda b: b["l"])
    return [Point(hi["h"], f"plus haut {label}", RANK_SESSION, (hi["t"], hi["t"] + step)),
            Point(lo["l"], f"plus bas {label}", RANK_SESSION, (lo["t"], lo["t"] + step))]


def _gaps(daily: list[dict], market: str, d_atr: float) -> list[Point]:
    """Gaps journaliers dont une partie n'est pas encore comblée : support (gap haussier) ou résistance
    (gap baissier) ; le comblement complet est le bord le plus éloigné du prix d'aujourd'hui."""
    out = []
    for i in range(1, len(daily)):
        prev, cur, later = daily[i - 1], daily[i], daily[i + 1:]
        if cur["l"] > prev["h"]:
            lo, hi = prev["h"], min([cur["l"]] + [b["l"] for b in later])
            names = ("comblement du gap", "haut du gap")
        elif cur["h"] < prev["l"]:
            lo, hi = max([cur["h"]] + [b["h"] for b in later]), prev["l"]
            names = ("bas du gap", "comblement du gap")
        else:
            continue
        if hi - lo < MIN_GAP_DAILY_ATR * d_atr:
            continue  # comblé, ou presque
        day = day_fr(cur["t"], market)
        out += [Point(lo, f"{names[0]} du {day}", RANK_GAP), Point(hi, f"{names[1]} du {day}", RANK_GAP)]
    return out


def points(bars: list[dict], daily: list[dict] | None, market: str, now: datetime, a: float,
           d_atr: float | None) -> list[Point]:
    """Tous les niveaux connus à `now` (la dernière bougie du plan est tenue pour en cours, comme dans le plan)."""
    step = STEP[market]
    recent = bars[:-1][-PLAN_BARS[market]:]
    out = []
    highs, lows = I.swings(recent, side=SWING_SIDE)
    for found, high in ((highs, True), (lows, False)):
        for j, price in found:
            if _reacted(recent[j + 1:j + 1 + REACTION_BARS], price, high, REACTION_ATR * a):
                t = recent[j]["t"]
                out.append(Point(price, f"{'sommet' if high else 'creux'} {UNIT[market]} du {day_fr(t, market)}",
                                 RANK_SWING, (t, t + step)))
    today = I.current_session(bars, market, now)
    session = [b for b in bars if I.session_key(b["t"], market) == today and not b.get("pending")]
    if session:
        out += _extremes(session, f"du {day_fr(session[0]['t'], market)}", step)
    prev = I.previous_session(bars, market, now)
    if prev:
        rows = [b for b in bars if I.session_key(b["t"], market) == prev["day"]]
        out += _extremes(rows, f"de la veille ({day_fr(rows[0]['t'], market)})", step)
    if daily:
        days = daily[-DAILY_BARS:]
        year = days[-YEAR_BARS:]
        hi = max(year, key=lambda b: b["h"])
        lo = min(year, key=lambda b: b["l"])
        out += [Point(hi["h"], f"record sur 52 semaines ({day_fr(hi['t'], market)})", RANK_YEAR, (hi["t"], hi["t"] + 86400)),
                Point(lo["l"], f"plus bas sur 52 semaines ({day_fr(lo['t'], market)})", RANK_YEAR, (lo["t"], lo["t"] + 86400))]
        marks = {(hi["t"], hi["h"]), (lo["t"], lo["l"])}
        dh, dl = I.swings(days, side=DAILY_SWING_SIDE)
        for found, high in ((dh, True), (dl, False)):
            for j, price in found:
                t = days[j]["t"]
                if (t, price) not in marks:
                    out.append(Point(price, f"{'sommet' if high else 'creux'} journalier du {day_fr(t, market)}",
                                     RANK_DAILY, (t, t + 86400)))
        if market == "stock" and d_atr:
            out += _gaps(days, market, d_atr)
    vw = I.vwap(bars, market, now)
    if vw:
        out.append(Point(vw, "VWAP du jour", RANK_DAY))
    if market == "stock" and session:
        start = session[0]["t"]
        opening = [b for b in session if b["t"] < start + OPENING_RANGE_MIN * 60]
        if now.timestamp() >= start + OPENING_RANGE_MIN * 60:
            out += [Point(max(b["h"] for b in opening), "haut du range d'ouverture", RANK_DAY),
                    Point(min(b["l"] for b in opening), "bas du range d'ouverture", RANK_DAY)]
    return [p for p in out if p.price > 0]


def merge(pts: list[Point], width: float) -> list[Zone]:
    """Zones : les niveaux à moins de `width` du premier niveau de la zone en font partie (comme le labo)."""
    zones, group = [], []
    for p in sorted(pts, key=lambda x: x.price):
        if group and p.price - group[0].price > width:
            zones.append(_zone(group))
            group = []
        group.append(p)
    if group:
        zones.append(_zone(group))
    return zones


def _zone(group: list[Point]) -> Zone:
    sources = []
    for p in sorted(group, key=lambda x: x.rank):
        if p.source not in sources:
            sources.append(p.source)
    contacts = _contacts([p.span for p in group if p.span])
    strong = contacts >= 2 or min(p.rank for p in group) <= RANK_GAP
    return Zone(group[0].price, group[-1].price, sources, contacts, strong)


def build(bars: list[dict], daily: list[dict] | None, market: str, now: datetime | None = None) -> dict:
    """Carte des niveaux : ATR des bougies du plan (closes), ATR journalier, zones de la plus basse à la plus haute."""
    now = now or datetime.now(timezone.utc)
    bars = S.with_open_bar(bars, S.SETTINGS[market]["interval"], now)
    a = P.atr(bars[:-1])
    d_atr = P.atr(daily) if daily and len(daily) > P.ATR_PERIOD else None
    return {"atr": a, "daily_atr": d_atr, "zones": merge(points(bars, daily, market, now, a, d_atr), MERGE_ATR * a),
            "bars": bars, "market": market}


# --- Prix concrets -----------------------------------------------------------------------

def _sign(side: str) -> int:
    return 1 if side == "long" else -1


def ahead(zones: list[Zone], side: str, price: float, gap: float = 0.0, strong: bool = True) -> list[Zone]:
    """Zones devant le prix dans le sens du trade (résistances pour un long), à plus de `gap`, les plus proches
    d'abord ; seulement les zones solides par défaut."""
    zs = [z for z in zones if z.strong or not strong]
    if side == "long":
        return sorted((z for z in zs if z.low > price + gap), key=lambda z: z.low)
    return sorted((z for z in zs if z.high < price - gap), key=lambda z: -z.high)


def behind(zones: list[Zone], side: str, price: float, gap: float = 0.0, strong: bool = True) -> list[Zone]:
    """Zones derrière le prix (supports pour un long), les plus proches d'abord."""
    return ahead(zones, "short" if side == "long" else "long", price, gap, strong)


def spacing(m: dict) -> float:
    """Écart minimal entre deux ventes : un quart d'ATR journalier (1 ATR du plan sans historique journalier)."""
    return SALE_SPACING_DAILY_ATR * m["daily_atr"] if m["daily_atr"] else m["atr"]


def sales(m: dict, side: str, price: float, legs: int = 3) -> list[dict]:
    """Prix de vente de chaque part restante : juste avant chacune des prochaines résistances solides (supports
    pour un short), espacées d'au moins `spacing` ; au-delà de la dernière zone connue, des paliers du même écart."""
    a, gap, sign = m["atr"], spacing(m), _sign(side)
    out = []
    for z in ahead(m["zones"], side, price, FIRST_SALE_ATR * a):
        px = tick((z.low if side == "long" else z.high) - sign * P.TARGET_BUFFER_ATR * a, m["market"])
        if out and (px - out[-1]["price"]) * sign < gap:
            continue
        out.append({"price": px, "zone": z})
        if len(out) == legs:
            break
    while len(out) < legs:
        base = out[-1]["price"] if out else price
        out.append({"price": tick(base + sign * gap, m["market"]), "zone": None})
    return out


def sale_source(s: dict, side: str) -> str:
    if s["zone"] is None:
        return f"palier : aucune {'résistance' if side == 'long' else 'zone de support'} connue plus loin"
    return f"{'sous' if side == 'long' else 'au-dessus de'} {s['zone'].text()}"


def trailing_stop(m: dict, side: str, price: float, current: float | None) -> tuple[float | None, str | None]:
    """Stop suiveur EmperorBTC : sous le dernier creux de swing des bougies du plan (au-dessus du dernier sommet
    pour un short), et sous toute la zone de ce creux s'il est dans une zone, avec la marge du plan, à au moins
    1 ATR du prix. Jamais moins bon que `current` : renvoie (current, None) si la structure ne le remonte pas."""
    a, sign, market = m["atr"], _sign(side), m["market"]
    recent = m["bars"][:-1][-P.SWING_LOOKBACK:]
    highs, lows = I.swings(recent, side=2)
    for j, p in reversed(lows if side == "long" else highs):
        zone = next((z for z in m["zones"] if z.low <= p <= z.high), None)
        edge = (min(p, zone.low) if side == "long" else max(p, zone.high)) if zone else p
        stop = tick(edge - sign * P.STOP_BUFFER_ATR * a, market)
        if (price - stop) * sign < a:
            continue
        if current is None or (stop - current) * sign > 0:
            where = (f"sous le dernier creux {UNIT[market]}" if side == "long" else
                     f"au-dessus du dernier sommet {UNIT[market]}")
            where += f", {S._num(p)} le {day_fr(recent[j]['t'], market)}"
            if zone and zone.contacts >= 2:
                where += f", et {'sous' if side == 'long' else 'au-dessus de'} sa zone {zone.label()}"
            return stop, where
        break
    return current, None


def _clamped(entry: float, stop: float, a: float, sign: int, market: str) -> float:
    """Stop structurel ramené entre 1 et 3 ATR de l'entrée (comme le plan)."""
    d = min(max((entry - stop) * sign, a), 3 * a)
    return tick(entry - sign * d, market)


def _candidate(m: dict, side: str, kind: str, entry: float, stop: float, zone: Zone | None,
               rr_min: float) -> dict | None:
    risk = abs(entry - stop)
    if risk <= 0 or (entry - stop) * _sign(side) <= 0:
        return None
    targets = sales(m, side, entry, 3)
    rr = [round(abs(t["price"] - entry) / risk, 1) for t in targets]
    # R/R mesuré sur la 2e vente, comme la note (rr_min « mesuré sur le TP2 »).
    return {"kind": kind, "side": side, "entry": entry, "stop": stop, "zone": zone, "targets": targets, "rr": rr,
            "ok": rr[1] >= rr_min}


def _edges(z: Zone, side: str) -> tuple[float, float]:
    """(bord par lequel on entre, bord sous lequel va le stop) d'une zone, dans le sens du trade."""
    return (z.high, z.low) if side == "long" else (z.low, z.high)


def entries(m: dict, side: str, price: float, stop_now: float | None, rr_min: float) -> list[dict]:
    """Entrées EmperorBTC : maintenant (stop structurel du plan), sur repli vers le support solide le plus proche
    (ordre limite juste au-dessus) et sur cassure de la résistance solide la plus proche (ordre stop juste
    au-dessus, stop sous la zone cassée), chacune avec un stop entre 1 et 3 ATR, ses trois ventes et son R/R."""
    a, sign, market = m["atr"], _sign(side), m["market"]
    out = []
    if stop_now is not None:
        out.append(_candidate(m, side, "maintenant", tick(price, market), tick(stop_now, market), None, rr_min))
    near = FIRST_SALE_ATR * a
    for kind, zones in (("repli", behind(m["zones"], side, price, near)), ("cassure", ahead(m["zones"], side, price))):
        if zones:
            top, bottom = _edges(zones[0], side)
            entry = tick(top + sign * P.TARGET_BUFFER_ATR * a, market)
            stop = _clamped(entry, bottom - sign * P.STOP_BUFFER_ATR * a, a, sign, market)
            out.append(_candidate(m, side, kind, entry, stop, zones[0], rr_min))
    return [c for c in out if c]


def adds(m: dict, side: str, price: float, stop: float, rr_min: float, opened: float | None = None) -> list[dict]:
    """Renforts d'une position gagnante (EmperorBTC : « Ajout sur un retest. Ajout lors de la rupture Swing. ») :
      - repli sur le support solide le plus proche au-dessus du stop : achat juste au-dessus, même stop ;
      - cassure de la résistance solide la plus proche : achat juste au-dessus, et le stop de toute la position
        remonte sous la zone cassée (`new_stop`, s'il y gagne).
    Jamais sous le prix d'entrée de la position (`opened`) : renforcer, ce n'est pas faire baisser son prix moyen.
    Gardés seulement si le R/R sur la 2e vente tient."""
    a, sign, market = m["atr"], _sign(side), m["market"]
    out = []
    sup = behind(m["zones"], side, price, FIRST_SALE_ATR * a)
    if sup:
        top, bottom = _edges(sup[0], side)
        entry = tick(top + sign * P.TARGET_BUFFER_ATR * a, market)
        # Un support sous le stop ne sert pas : un repli jusque-là sortirait la position.
        if ((bottom - stop) * sign > 0 and (entry - stop) * sign >= MIN_ADD_RISK_ATR * a
                and (opened is None or (entry - opened) * sign > 0)):
            out.append(_candidate(m, side, "repli", entry, stop, sup[0], rr_min))
    res = ahead(m["zones"], side, price)
    if res:
        top, bottom = _edges(res[0], side)
        entry = tick(top + sign * P.TARGET_BUFFER_ATR * a, market)
        new_stop = _clamped(entry, bottom - sign * P.STOP_BUFFER_ATR * a, a, sign, market)
        c = _candidate(m, side, "cassure", entry, max(new_stop, stop) if sign == 1 else min(new_stop, stop),
                       res[0], rr_min)
        if c:
            c["new_stop"] = c["stop"] if (c["stop"] - stop) * sign > 0 else None
            out.append(c)
    return [c for c in out if c and c["ok"]]
