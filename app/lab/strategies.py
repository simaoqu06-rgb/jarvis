"""Labo v2 : stratégies à déclencheur, mesurées à côté de v1 (pré-enregistrées le 7 octobre 2026).

Pourquoi : v1 achète tout l'univers à heure fixe (9 h 10 New York, toutes les 4 h en crypto) au dernier
cours, sans attendre de signal, et perd −0,23 R par trade. Ses « setups » ne sont que des étiquettes
posées après coup. Ici, chaque stratégie lit les bougies de 5 min connues jusqu'à la clôture de la
bougie t et dit « je me déclenche » (ordre stop d'achat posé au-dessus de la bougie, valable
VALID_BARS bougies) ou rien. Rien n'est vu de l'avenir : la décision tombe à la clôture de la bougie
du signal, l'exécution part de la bougie suivante.

La liste est figée par écrit AVANT de regarder un seul résultat (docs/labo-strategies.md) : chaque
couple stratégie × sortie × marché compte comme un essai (trials()), gagnant ou non. Ajouter une
stratégie = une nouvelle déclaration datée, jamais une retouche silencieuse de celles-ci.

Stratégies (achat seulement, une position au plus par stratégie, actif et jour) :
- pullback : en tendance (clôture de la veille au-dessus de la MM 20 jours, EMA 20 de 5 min qui
  monte, prix au-dessus du VWAP), le prix revient toucher la zone (max de l'EMA 20 et du VWAP) à la
  bougie du signal ou la précédente, après une impulsion (un plus haut ≥ zone + 1 ATR parmi les 12
  bougies d'avant le contact, dans la séance pour les actions) ; bougie de confirmation :
  verte, clôture au-dessus de l'EMA 20, du VWAP et du plus haut de la bougie précédente. Entrée : stop
  d'achat 1 pas au-dessus de son plus haut ; stop sous le creux des 3 dernières bougies - 0,1 ATR ;
- orb15 / orb30 (actions) : range des 15 ou 30 premières minutes ; stop d'achat 1 pas au-dessus du
  plus haut, valable jusqu'à 12 h New York ; stop 1 pas sous le plus bas du range ;
- cassure_retest : clôture au-dessus du plus haut de la veille, puis, dans les 12 bougies, retour
  au niveau (plus bas ≤ niveau + 0,2 %) avec clôture au-dessus ; stop d'achat 1 pas au-dessus de la
  bougie du retest ; stop sous min(son plus bas, niveau) - 0,1 ATR.
Objectifs : 1, 2 et 3 R. Fenêtre des signaux : actions de 9 h 45 à 15 h New York (soldé à la
clôture) ; crypto de 0 h à 20 h UTC (soldé 24 h après le signal).
v1 est rejouée avec les mêmes sorties et tests, sur ses propres objectifs : c'est la référence.

Pour chaque déclenchement, une seule ligne (db.STRAT_COLUMNS) avec :
- r_net : sortie de v1 (un tiers à chaque objectif, stop à l'entrée après TP1) ;
- r_sans_be, r_2r (un seul objectif à 2 R), r_suiveur (stop suiveur 1 R sous le plus haut) ;
- tests de robustesse : glissement ×2 et ×3, entrée une bougie plus tard, ordre limite à peine
  touché (moins de MISS_DEPTH) compté comme non exécuté ;
- témoin aléatoire : même jour, même fenêtre, même stop en %, mêmes objectifs en R, entrée au marché
  à une bougie tirée au hasard (graine fixe). Une stratégie qui ne bat pas son témoin n'a pas
  d'avantage d'entrée.
"""
import hashlib
import random
from bisect import bisect_left
from datetime import date, datetime, time

from app.lab import collector as CO
from app.lab import simulator as SIM

REGISTERED_ON = "2026-10-07"
BAR = CO.BASE_TF * 60
REFERENCE = "v1"
STRATEGIES = {
    "pullback": {"label": "Pullback en tendance + confirmation", "markets": ("stock", "crypto")},
    "orb15": {"label": "Cassure du range d'ouverture (15 min)", "markets": ("stock",)},
    "orb30": {"label": "Cassure du range d'ouverture (30 min)", "markets": ("stock",)},
    "cassure_retest": {"label": "Cassure du plus haut de la veille puis retest", "markets": ("stock", "crypto")},
}
LABELS = {REFERENCE: "v1 (achat à heure fixe, référence)", **{k: v["label"] for k, v in STRATEGIES.items()}}
EXITS = {
    "r_net": "sortie v1 : un tiers à chaque objectif, stop à l'entrée après TP1",
    "r_sans_be": "mêmes objectifs, sans remonter le stop",
    "r_2r": "un seul objectif à 2 R",
    "r_suiveur": "aucun objectif, stop suiveur 1 R sous le plus haut",
}
STRESS = {
    "r_glisse_x2": "glissement ×2", "r_glisse_x3": "glissement ×3",
    "r_retard": "entrée une bougie plus tard", "r_manque": "ordre limite à peine touché non exécuté",
}
TARGETS_R = (1.0, 2.0, 3.0)
EMA_LEN, ATR_LEN, DAILY_SMA = 20, 14, 20
EMA_RISE_BARS = 6
IMPULSE_LOOKBACK, IMPULSE_ATR = 12, 1.0
STOP_BUFFER_ATR = 0.1
VALID_BARS = 3
RETEST_BARS, RETEST_TOL = 12, 0.002
MISS_DEPTH = 0.001          # ordre limite : traversée de moins de 0,1 % = probablement pas exécuté en réel
TRAIL_R = 1.0
STOCK_SIGNAL_FROM_MIN = 15  # actions : signaux à partir de 9 h 45 (après 3 bougies)
STOCK_SIGNAL_UNTIL = time(15, 0)
ORB_UNTIL = time(12, 0)
CRYPTO_SIGNAL_HOURS = 20    # crypto : signaux de 0 h à 20 h UTC, soldés 24 h après
CRYPTO_HORIZON = 86400


def trials() -> list[dict]:
    """Essais déclarés : chaque stratégie (et v1) × sortie × marché, comptés gagnants ou non."""
    out = []
    for market in ("stock", "crypto"):
        names = [REFERENCE] + [k for k, v in STRATEGIES.items() if market in v["markets"]]
        out += [{"market": market, "strategy": n, "exit": e} for n in names for e in EXITS]
    return out


# --- Indicateurs sur bougies de 5 min ---------------------------------------------------------

def ema(values: list[float], n: int) -> list[float]:
    k, out = 2 / (n + 1), []
    for v in values:
        out.append(v if not out else v * k + out[-1] * (1 - k))
    return out


def atr(bars: list[dict], n: int = ATR_LEN) -> list[float | None]:
    """ATR de Wilder ; None tant qu'il n'y a pas n bougies."""
    out: list[float | None] = []
    trs: list[float] = []
    cur = None
    for i, b in enumerate(bars):
        prev = bars[i - 1]["c"] if i else b["c"]
        tr = max(b["h"] - b["l"], abs(b["h"] - prev), abs(b["l"] - prev))
        if cur is None:
            trs.append(tr)
            if len(trs) == n:
                cur = sum(trs) / n
        else:
            cur = (cur * (n - 1) + tr) / n
        out.append(cur)
    return out


def vwap_from(bars: list[dict], start: int) -> list[float | None]:
    """VWAP ancré sur la bougie `start` (ouverture de séance, minuit UTC) ; None avant."""
    out: list[float | None] = [None] * len(bars)
    pv = vol = 0.0
    for i in range(start, len(bars)):
        b = bars[i]
        tp = (b["h"] + b["l"] + b["c"]) / 3
        pv += tp * b["v"]
        vol += b["v"]
        out[i] = pv / vol if vol else tp
    return out


class Day:
    """Bougies d'un actif autour d'un jour, indicateurs et bornes des fenêtres (indices)."""

    def __init__(self, market: str, day: date, bars: list[dict], daily_before: list[dict]):
        self.market, self.day, self.bars = market, day, bars
        self.t = [b["t"] for b in bars]
        self.o = [b["o"] for b in bars]
        self.h = [b["h"] for b in bars]
        self.l = [b["l"] for b in bars]
        self.c = [b["c"] for b in bars]
        if market == "stock":
            bounds = CO.session_ts(day)
            self.start_ts, self.close_ts = bounds if bounds else (0, 0)
            ny = CO.NEW_YORK
            self.signal_from_ts = self.start_ts + STOCK_SIGNAL_FROM_MIN * 60
            self.signal_until_ts = min(int(datetime.combine(day, STOCK_SIGNAL_UNTIL, ny).timestamp()), self.close_ts)
            self.orb_until_ts = int(datetime.combine(day, ORB_UNTIL, ny).timestamp())
        else:
            self.start_ts = CO.utc_midnight(day)
            self.close_ts = self.start_ts + 86400
            self.signal_from_ts = self.start_ts
            self.signal_until_ts = self.start_ts + CRYPTO_SIGNAL_HOURS * 3600
            self.orb_until_ts = self.signal_until_ts
        self.d0 = bisect_left(self.t, self.start_ts)
        self.d1 = bisect_left(self.t, self.close_ts)
        self.first = bisect_left(self.t, self.signal_from_ts)
        self.last = bisect_left(self.t, self.signal_until_ts)  # exclu
        self.ema = ema(self.c, EMA_LEN)
        self.atr = atr(bars)
        self.vwap = vwap_from(bars, self.d0)
        closes = [b["c"] for b in daily_before]
        self.trend_ok = len(closes) >= DAILY_SMA and closes[-1] > sum(closes[-DAILY_SMA:]) / DAILY_SMA
        self.prev_high = daily_before[-1]["h"] if daily_before else None
        self.prev_close = daily_before[-1]["c"] if daily_before else None

    @property
    def usable(self) -> bool:
        return self.d1 > self.d0 and self.first < self.last and self.first >= EMA_LEN

    def tick(self, price: float) -> float:
        return SIM.tick_size(self.market, price)

    def exit_at(self, decided_at: int) -> int:
        return self.close_ts if self.market == "stock" else decided_at + CRYPTO_HORIZON

    def after(self, ts: int) -> list[dict]:
        """Bougies commencées à `ts` ou après (ce que le simulateur a le droit de voir)."""
        return self.bars[bisect_left(self.t, ts):]


# --- Signaux (chacun ne lit que les bougies 0..i) -------------------------------------------

def _cand(d: Day, i: int, entry: float, stop: float, valid_until: int | None = None) -> dict | None:
    if stop >= entry or stop <= 0:
        return None
    decided = d.t[i] + BAR
    return {"i": i, "decided_at": decided, "entry": entry, "stop": stop,
            "entry_until": valid_until or decided + VALID_BARS * BAR}


def pullback(d: Day) -> list[dict]:
    if not d.trend_ok:
        return []
    out = []
    for i in range(max(d.first, EMA_RISE_BARS, 2), d.last):
        a = d.atr[i]
        v = d.vwap[i]
        if not a or v is None:
            continue
        e = d.ema[i]
        if not (e > d.ema[i - EMA_RISE_BARS] and d.c[i] > v):
            continue
        lo = max(d.d0, i - IMPULSE_LOOKBACK) if d.market == "stock" else max(0, i - IMPULSE_LOOKBACK)
        if lo >= i:
            continue
        zone = max(e, v)
        touch = i - 1 if d.l[i - 1] <= zone else i if d.l[i] <= zone else None
        if touch is None or touch <= lo or max(d.h[lo:touch]) < zone + IMPULSE_ATR * a:
            continue  # pas de contact, ou pas d'impulsion AVANT le contact
        if not (d.c[i] > d.o[i] and d.c[i] > e and d.c[i] > v and d.c[i] > d.h[i - 1]):
            continue
        c = _cand(d, i, d.h[i] + d.tick(d.h[i]), min(d.l[i - 2:i + 1]) - STOP_BUFFER_ATR * a)
        if c:
            out.append(c)
    return out


def orb(d: Day, minutes: int) -> list[dict]:
    if d.market != "stock":
        return []
    n = minutes // CO.BASE_TF
    if d.d1 - d.d0 <= n or d.t[d.d0] != d.start_ts:
        return []
    hi, lo = max(d.h[d.d0:d.d0 + n]), min(d.l[d.d0:d.d0 + n])
    c = _cand(d, d.d0 + n - 1, hi + d.tick(hi), lo - d.tick(lo), valid_until=d.orb_until_ts)
    return [c] if c else []


def cassure_retest(d: Day) -> list[dict]:
    level = d.prev_high
    if not level:
        return []
    out, b, i = [], None, max(d.first, 1)
    while i < d.last:
        if b is None:
            if d.c[i] > level:
                b = i
            i += 1
            continue
        if i - b > RETEST_BARS:
            b = None
            continue  # même bougie : peut-être une nouvelle cassure
        if d.l[i] <= level * (1 + RETEST_TOL) and d.c[i] > level and d.atr[i]:
            c = _cand(d, i, d.h[i] + d.tick(d.h[i]), min(d.l[i], level) - STOP_BUFFER_ATR * d.atr[i])
            if c:
                out.append(c)
            b = None
        i += 1
    return out


SIGNALS = {"pullback": pullback, "orb15": lambda d: orb(d, 15), "orb30": lambda d: orb(d, 30),
           "cassure_retest": cassure_retest}


# --- Simulation : sortie de référence, variantes, robustesse et témoin ---------------------

def _plan(entry: float, stop: float, targets: list[float]) -> dict:
    return {"side": "long", "entry": entry, "stop": stop, "targets": [{"price": p} for p in targets]}


def _r_targets(entry: float, stop: float, multiples=TARGETS_R) -> list[float]:
    risk = entry - stop
    return [entry + m * risk for m in multiples]


def _seed(*parts) -> random.Random:
    return random.Random(int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:16], 16))


def _sim(d: Day, plan: dict, decided_at: int, entry_until: int, exit_at: int, cost: dict, order: str,
         last_price: float, **kw) -> dict:
    return SIM.simulate(plan, d.after(decided_at), market=d.market, decided_at=decided_at,
                        entry_until=min(entry_until, exit_at), exit_at=exit_at, last_price=last_price,
                        cost=cost, entry_order=order, **kw)


def _missed(res: dict, d: Day) -> bool:
    """Ordre limite exécuté de justesse (traversée < MISS_DEPTH) : en réel, souvent pas servi."""
    if res.get("entry_mode") != "limite":
        return False
    k = bisect_left(d.t, res["filled_at"])
    entry = res["order_prices"]["entry"]
    return k < len(d.l) and (entry - d.l[k]) / entry < MISS_DEPTH


def measure(d: Day, plan: dict, decided_at: int, entry_until: int, cost: dict, order: str, last_price: float,
            res: dict | None = None) -> dict:
    """Toutes les sorties et tous les tests d'un plan (ordre `order`) décidé à `decided_at`."""
    exit_at = d.exit_at(decided_at)
    res = res or _sim(d, plan, decided_at, entry_until, exit_at, cost, order, last_price)
    out = {"triggered": int(res["triggered"]), "filled_at": res["filled_at"], "entry_mode": res["entry_mode"],
           "minutes_to_fill": res["minutes_to_fill"], "exit_reason": res["exit_reason"],
           "r_net": res["r_net"], "r_gross": res["r_gross"],
           "tp1_hit": int(bool(res["tp_hits"] and res["tp_hits"][0])), "mfe_r": res["mfe_r"], "mae_r": res["mae_r"]}
    if not res["triggered"]:
        return out
    entry, stop = plan["entry"], plan["stop"]
    out["r_sans_be"] = _sim(d, plan, decided_at, entry_until, exit_at, cost, order, last_price,
                            exit_mode="sans_be")["r_net"]
    out["r_2r"] = _sim(d, _plan(entry, stop, _r_targets(entry, stop, (2.0,))), decided_at, entry_until, exit_at,
                       cost, order, last_price)["r_net"]
    out["r_suiveur"] = _sim(d, plan, decided_at, entry_until, exit_at, cost, order, last_price,
                            exit_mode="suiveur", trail_r=TRAIL_R)["r_net"]
    out["r_glisse_x2"] = SIM.net_r(res, cost, slip_factor=2.0)
    out["r_glisse_x3"] = SIM.net_r(res, cost, slip_factor=3.0)
    late = _sim(d, plan, decided_at + BAR, entry_until + BAR, exit_at, cost, order, last_price)
    out["r_retard"] = late["r_net"] if late["triggered"] else None
    out["r_manque"] = None if _missed(res, d) else res["r_net"]
    return out


NO_WITNESS = {"temoin_r": None, "temoin_triggered": 0}


def witness(d: Day, key: tuple, risk_pct: float, multiples: list[float], cost_of, window: tuple[int, int]) -> dict:
    """Entrée au marché à une bougie tirée au hasard dans `window` (indices), même stop en %, mêmes
    objectifs en R, sortie v1."""
    lo, hi = window
    if hi <= lo or risk_pct <= 0:
        return NO_WITNESS
    k = _seed(*key).randrange(lo, hi)
    entry = d.o[k]
    stop = entry * (1 - risk_pct / 100)
    plan = _plan(entry, stop, _r_targets(entry, stop, multiples))
    decided = d.t[k]
    res = _sim(d, plan, decided, decided + BAR, d.exit_at(decided), cost_of(entry), "market", entry)
    return {"temoin_r": res["r_net"], "temoin_triggered": int(res["triggered"])}


def _row(symbol: str, strategy: str, slot: int, plan: dict, decided_at: int, m: dict, wit: dict,
         gap: float | None, rank) -> dict:
    entry, stop = plan["entry"], plan["stop"]
    return {"symbol": symbol, "strategy": strategy, "slot": slot, "signal_at": decided_at,
            "entry": round(entry, 8), "stop": round(stop, 8), "risk_pct": round((entry - stop) / entry * 100, 4),
            **m, **wit, "gap_open_pct": gap, "rank": rank}


def strategy_rows(d: Day, symbol: str, rank=None, gap: float | None = None, names=None) -> list[dict]:
    """Une ligne par stratégie du marché : le premier signal dont l'ordre est exécuté, sinon le premier
    signal (non déclenché) ; rien si aucun signal."""
    if not d.usable:
        return []
    rows = []
    for name, spec in STRATEGIES.items():
        if d.market not in spec["markets"] or (names and name not in names):
            continue
        chosen, busy_until = None, 0
        for cand in SIGNALS[name](d):
            if cand["decided_at"] < busy_until:
                continue  # un ordre précédent était encore posé
            plan = _plan(cand["entry"], cand["stop"], _r_targets(cand["entry"], cand["stop"]))
            cost = SIM.costs(d.market, cand["entry"], rank)
            res = _sim(d, plan, cand["decided_at"], cand["entry_until"], d.exit_at(cand["decided_at"]), cost, "stop",
                       d.c[cand["i"]])
            if chosen is None or res["triggered"]:
                chosen = (cand, plan, cost, res)
            if res["triggered"]:
                break
            busy_until = cand["entry_until"]
        if not chosen:
            continue
        cand, plan, cost, res = chosen
        m = measure(d, plan, cand["decided_at"], cand["entry_until"], cost, "stop", d.c[cand["i"]], res)
        risk_pct = (plan["entry"] - plan["stop"]) / plan["entry"] * 100
        wit = witness(d, (d.market, symbol, d.day, name), risk_pct, list(TARGETS_R),
                      lambda p: SIM.costs(d.market, p, rank), (d.first, d.last)) if m["triggered"] else NO_WITNESS
        rows.append(_row(symbol, name, 0, plan, cand["decided_at"], m, wit, gap, rank))
    return rows


def v1_row(d: Day, symbol: str, plan: dict, decided_at: int, entry_until: int, last_price: float, cost: dict,
           res: dict | None = None, rank=None, gap: float | None = None, window: tuple[int, int] | None = None) -> dict:
    """v1 rejouée avec les sorties et tests du labo v2, sur ses propres objectifs (ordre limité)."""
    targets = [t["price"] for t in plan["targets"]][:3]
    p = _plan(plan["entry"], plan["stop"], targets)
    m = measure(d, p, decided_at, entry_until, cost, "limit", last_price, res)
    risk = plan["entry"] - plan["stop"]
    risk_pct = risk / plan["entry"] * 100 if plan["entry"] else 0
    multiples = [round((t - plan["entry"]) / risk, 4) for t in targets] if risk > 0 else []
    wit = witness(d, (d.market, symbol, d.day, REFERENCE, decided_at), risk_pct, multiples,
                  lambda px: SIM.costs(d.market, px, rank), window or (d.first, d.last)) if m["triggered"] else NO_WITNESS
    return _row(symbol, REFERENCE, decided_at, p, decided_at, m, wit, gap, rank)


# --- Entrées du générateur ------------------------------------------------------------------

STOCK_WARMUP_BARS = 160  # deux séances : EMA 20, ATR 14 et impulsion du matin


def stock_day(day: date, symbol: str, hist5: list[dict], session: list[dict], daily: list[dict],
              rank=None, v1: dict | None = None) -> list[dict]:
    """Lignes du jour pour une action : v1 (si son plan existe) et les stratégies.

    `hist5` : bougies de 5 min avant la séance ; `session` : celles de la séance ; `daily` :
    journalier connu avant le jour ; `v1` : {plan, decided_at, entry_until, last_price, cost, result}."""
    bars = CO.regular_hours(hist5[-STOCK_WARMUP_BARS * 2:])[-STOCK_WARMUP_BARS:] + session
    d = Day("stock", day, bars, daily)
    prev = d.prev_close
    gap = round((session[0]["o"] / prev - 1) * 100, 3) if session and prev else None
    rows = strategy_rows(d, symbol, rank, gap)
    if v1 and d.d1 > d.d0:
        rows.append(v1_row(d, symbol, v1["plan"], v1["decided_at"], v1["entry_until"], v1["last_price"], v1["cost"],
                           v1.get("result"), rank, gap, (d.first, d.last)))
    return rows


def crypto_complete(day: date, now: int | None) -> bool:
    """Toutes les sorties du jour sont connues : signal au plus tard à 20 h UTC, horizon 24 h."""
    return now is None or now >= CO.utc_midnight(day) + (CRYPTO_SIGNAL_HOURS * 3600 + CRYPTO_HORIZON)


def crypto_day(day: date, symbol: str, bars5: list[dict], daily: list[dict], rank=None,
               v1s: list[dict] = ()) -> list[dict]:
    """Lignes du jour UTC pour une crypto : v1 (un par créneau de 4 h) et les stratégies."""
    start = CO.utc_midnight(day)
    lo = bisect_left([b["t"] for b in bars5], start - 86400)
    hi = bisect_left([b["t"] for b in bars5], start + 2 * 86400)
    d = Day("crypto", day, bars5[lo:hi], [b for b in daily if b["t"] < start])
    rows = strategy_rows(d, symbol, rank)
    for v in v1s:
        k0 = bisect_left(d.t, v["decided_at"])
        k1 = bisect_left(d.t, v["decided_at"] + 4 * 3600)
        rows.append(v1_row(d, symbol, v["plan"], v["decided_at"], v["entry_until"], v["last_price"], v["cost"],
                           v.get("result"), rank, None, (k0, k1)))
    return rows
