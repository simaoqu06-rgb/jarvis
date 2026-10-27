"""Déclaration n° 12 (27 octobre 2026, docs/labo-strategies.md) : cassures des 5 premières minutes sur les valeurs en
jeu, rejouées sur toute la cote.

Pourquoi : Simão tient la méthode Gemini pour gagnante, et le challenger en direct (app/trading/gemini.py) ne se lira
qu'à 100 plans déclenchés. Le rejeu du labo, lui, choisit ses actions à 9 h 10 sur les chiffres de la veille : il n'a vu
que 8,7 % des gappers (couverture de Killmode). La n° 9 et Killmode complet (n° 8) ont rejoué les gappers de toute la
base, mais avec la cassure du range de 15 min et les sorties du labo ou de Killmode : ils perdent (n° 9 : −0,15 R par
trade à 20 € ; Killmode complet : −0,29 R, le catalyseur n'ajoutant que +0,06 R, non significatif). Jamais rejouées :
- la passe A de Gemini telle que la fiche la décrit (cassure des 5 premières minutes confirmée par le volume, gap de +5
  à +25 %, volume relatif ≥ 4, moitié à +10 %, reste à +20 %, nuit gardée selon la règle de Simão) ;
- l'ORB 5 minutes des « stocks in play » de Zarattini, Barbon et Aziz (SSRN 4729284, 2024) : les 20 plus forts volumes
  relatifs de l'ouverture, sens de la première bougie, stop à 10 % de l'ATR, sortie à la clôture.

Données (rien n'est vu de l'avenir) :
- table open5 : la première bougie de 5 min (9 h 30 - 9 h 35) de chaque action de la base qui a coté la séance, SIP,
  prix réellement négociés (adjustment=raw), connue à 9 h 35. Le journalier ajusté des divisions (bars, tf 1440)
  donne la clôture de la veille, l'ATR de Wilder 14 j et les volumes moyens ; `facteur` (ouverture ajustée du jour sur
  ouverture brute de la bougie) les remet aux prix négociés du jour, et une division dans la fenêtre d'un volume
  relatif (facteur qui varie de plus de SPLIT_TOL) le rend inconnu ;
- bougies d'1 min SIP brutes de la séance (et de la suivante pour la passe A) des seules valeurs retenues, de leurs
  témoins et de SPY, relues et gardées en mémoire, comme le solde du challenger en direct.

Passe A de Gemini, le code du direct (gemini.plan_a, gemini.settle_plan, mêmes seuils SEUILS), sur une population
refaite : les MA_TOP plus forts volumes de la première bougie et les TG_TOP plus fortes hausses à 9 h 35 (le screener
d'Alpaca du direct ne se rejoue pas). Gap de +5 à +25 % ; les DEEP_MAX premières au volume de la bougie rapporté au
volume journalier moyen de 20 séances ; plan sur la bougie de 9 h 30 ; volume relatif ≥ 4 (bougie de 9 h 30 contre la
moyenne des RVOL_DAYS dernières séances, parmi les RVOL_WINDOW d'avant, où la valeur a échangé dans les 5 premières
minutes, au moins RVOL_MIN_DAYS) ; les FINNHUB_MAX premières dans cet ordre. Ordre actif à ACTIF_DES (le direct : deux
minutes après le message de 9 h 36). Écarts au direct, dits : ni catalyseur ni nombre d'actions (aucune donnée datée
sur toute la période), ni la liste de surveillance ; SPY à la place de l'ETF du secteur pour la règle de nuit ; la
bougie de 9 h 30 et la condition de volume viennent du SIP (le direct prend la bougie IEX, dont le volume n'est qu'une
petite part du SIP, et la compare au volume SIP d'1 min). Un essai par sortie qui décide : j0 (la règle du plan) et jx
(règle de nuit) ; j1 est publié sans essai. Témoin : comme le direct, une valeur tirée au hasard dans la population
(graine fixe), achetée au marché à la minute du déclenchement, même géométrie en %, mêmes sorties.

ORB des stocks in play (règles de la section 2.1 et 4 de l'article) : volume relatif = volume de la bougie de 9 h 30
sur la moyenne simple des SIP_RVOL_DAYS séances d'avant (0 une séance cotée sans échange à l'ouverture) ; au moins
SIP_RVOL_MIN ; bougies sans corps retirées avant le classement (système en direct des auteurs) ; les SIP_TOP plus forts,
ceux qui qualifient s'il y en a moins ; sens de la bougie de 9 h 30 (verte : achat, rouge : vente) ; ordre stop au plus
haut (achat) ou au plus bas (vente) du range, actif dès 9 h 35 jusqu'à SIP_UNTIL, rempli au niveau ou à l'ouverture
de la minute au-delà ; stop à SIP_STOP_ATR de l'ATR 14 j (Wilder, cité par l'article) du prix d'exécution ; sortie au
dernier cours de la séance. Trois versions : sip (sans filtre, règle de Simão), sip_filtres (les filtres de l'article :
ouverture > 5 $, volume moyen 14 j ≥ 1 M d'actions, ATR 14 j > 0,50 $ ; elle seule mesure ce que la règle « pas de
filtre de liquidité » coûte, et ne peut pas devenir une règle de JARVIS sans Simão) et sip_range (sans filtre, stop à
l'autre bout du range de 5 min, règle de l'article frère sur QQQ, SSRN 4416622 : un stop plus large que les frais,
l'alternative que la revue de littérature du 26/10/2026 donne au risque principal, le coût d'exécution en R). Les
achats sont des essais ; les ventes à découvert sont publiées sans essai (coût d'emprunt, locate et règle SSR non
modélisés). Témoin : une valeur tirée au hasard dans la population de la version (SIP_TEMOINS tirages, le premier qui
échange sert), prise au marché à la minute de l'entrée, même sens, même stop en %, sortie à la clôture. Dans la minute
de l'entrée, un stop touché compte (ordre des prix inconnu : le pire, comme le simulateur du labo) ; la variante « stop
vérifié à partir de la minute suivante » est publiée à côté.

Mesure qui décide : r_frais, le R aux frais de Simão (20 € de risque au change EURUSD_DEFAULT, actions entières,
commission IBKR du plan du compte, glissement du labo d'au moins un demi-pas ; un bracket par objectif pour la passe A
comme le direct, un ordre d'entrée et un de sortie pour l'ORB). Règle « candidate » du labo (n° 8 et 10), sur les
séances hors bloc scellé : borne basse de l'IC 95 % de r_frais > 0, écart au témoin au seuil en t (Bonferroni sur tous
les essais déclarés, Student à jours − 1), au moins 30 trades, les deux moitiés chronologiques et le glissement doublé
positifs. Une ligne qui tient devient candidate au bloc scellé, ouvert une seule fois et par Simão. Le bloc scellé
n'est ni lu ni calculé ici.
"""
import json
import logging
import math
import random
import sqlite3
import time as clock
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

from app.lab import collector as CO
from app.lab import heartbeat
from app.lab import seal as SEAL
from app.lab import sources as SRC
from app.lab import strat_stats as SS
from app.lab import strategies as ST
from app.lab.db import LabDB
from app.sources import alpaca
from app.trading import calendar as CAL
from app.trading import gemini as G
from app.trading import killmode as K

log = logging.getLogger(__name__)
NUMERO = 12
MESURE = 1               # révision de la mesure : une séance calculée sous une autre est refaite
JOB = "ouverture:n12"
OPEN5_JOB = "ouverture5"
OPEN5_BATCH = 200        # symboles par requête Alpaca (une bougie chacun : une page ; 200 comme gemini.CHUNK)
MINUTE_BATCH = 100       # bougies d'1 min : ~39 000 par séance et par lot, quatre pages
RVOL_WINDOW = 24         # séances : les 35 jours calendaires de bougies de 5 min que lit le direct (gemini.scan)
WARMUP = RVOL_WINDOW     # séances d'open5 lues avant chaque séance calculée
SPLIT_TOL = 0.25         # écart du facteur prix ajusté / prix négocié au-delà duquel une division a eu lieu
DAILY_LOOKBACK_DAYS = 50 # ~34 séances de journalier : ATR 14 j, volumes moyens de 20 séances
MIN_SESSIONS = 21        # comme le direct (au moins 21 bougies journalières)
MIN_COTE = 1000          # moins d'actions cotées : journalier de la séance incomplet, séance reprise plus tard
SETTLED_AFTER = 20 * 60  # secondes après la clôture du lendemain (SIP gratuit : 16 min de retard)
# Historique à calculer avant que la n° 12 soit « complète » (status.declaration_state) ; les séances suivantes sont
# l'avancement ordinaire, comme les jours neufs de la n° 9.
BACKLOG_UNTIL = date(2026, 10, 26)
EURUSD = K.EURUSD_DEFAULT
# Passe A (app/trading/gemini.py, version 1 des règles)
MA_TOP, TG_TOP = 100, 50
ACTIF_DES = time(9, 38)
MARKET = G.MARKET
# ORB des stocks in play (SSRN 4729284, section 4)
SIP_TOP = 20
SIP_RVOL_DAYS = 14
SIP_RVOL_MIN = 1.0
SIP_STOP_ATR = 0.10
SIP_UNTIL = time(15, 59)  # l'ordre reste actif toute la séance (aucune annulation dans l'article)
SIP_FILTERS = {"prix_min": 5.0, "volume_min": 1_000_000, "atr_min": 0.50}
SIP_TEMOINS = 5
# Version → (filtres de l'article, stop) ; « atr » : SIP_STOP_ATR de l'ATR ; « range » : l'autre bout du range.
SIP_VERSIONS = {"sip": (False, "atr"), "sip_filtres": (True, "atr"), "sip_range": (False, "range")}
STOP_BUCKETS = ((0.0, 0.10, "< 10 c"), (0.10, 0.20, "10-20 c"), (0.20, 0.50, "20-50 c"), (0.50, 1e9, "≥ 50 c"))
RVOL_BUCKETS = ((0.0, 2.0, "1-2"), (2.0, 5.0, "2-5"), (5.0, 30.0, "5-30"), (30.0, 1e18, "> 30"))
# Ventilations descriptives (revues B et C du 26/10/2026 : mesurer ce que coûte la règle « pas de filtre » sans filtrer)
PRICE_BUCKETS = ((0.0, 5.0, "< 5 $"), (5.0, 20.0, "5-20 $"), (20.0, 1e18, "≥ 20 $"))
GAP_BUCKETS = ((-1e18, -4.0, "< −4 %"), (-4.0, 4.0, "−4 à +4 %"), (4.0, 10.0, "4-10 %"), (10.0, 20.0, "10-20 %"),
               (20.0, 1e18, "≥ 20 %"))
VARIANTS = ("j0", "j1", "jx")
# Essai → (version, sortie ou sens). Les autres lignes (gemini_a_j1, sip_court*) sont publiées sans essai.
TRIALS = {"gemini_a_j0": ("gemini", "j0"), "gemini_a_jx": ("gemini", "jx"), "sip_long": ("sip", "long"),
          "sip_long_filtres": ("sip_filtres", "long"), "sip_long_range": ("sip_range", "long")}
LABELS = {"gemini_a_j0": "Gemini A, sortie le jour même", "gemini_a_j1": "Gemini A, sortie le lendemain (sans essai)",
          "gemini_a_jx": "Gemini A, règle de nuit", "sip_long": "Stocks in play, achat, sans filtre",
          "sip_long_filtres": "Stocks in play, achat, filtres de l'article",
          "sip_long_range": "Stocks in play, achat, stop au range, sans filtre",
          "sip_court": "Stocks in play, vente à découvert, sans filtre (sans essai)",
          "sip_court_filtres": "Stocks in play, vente à découvert, filtres de l'article (sans essai)",
          "sip_court_range": "Stocks in play, vente à découvert, stop au range (sans essai)"}
COLUMNS = ("day", "symbol", "essai", "rang", "cote", "gap_pct", "rvol", "prix", "entry", "stop", "risque",
           "declenche", "t_entree", "sortie", "r_brut", "r_frais", "r_frais_x2", "r_suivante", "hors_mise", "incomplet",
           "valeur", "temoin", "temoin_r_brut", "temoin_r_frais", "detail")


# --- Calendrier --------------------------------------------------------------------------------------

def _ts(day: date, at: time) -> int:
    return int(datetime.combine(day, at, CO.NEW_YORK).timestamp())


def next_trading_day(day: date) -> date:
    d = day + timedelta(days=1)
    while not CAL.is_trading_day(d):
        d += timedelta(days=1)
    return d


def full_session(day: date) -> bool:
    """Séance entière (9 h 30 - 16 h) : les jours de clôture anticipée sont sautés, comme le direct."""
    bounds = CO.session_ts(day)
    return bool(bounds) and bounds[1] - bounds[0] == 390 * 60


def last_daily(conn: sqlite3.Connection) -> date | None:
    last = conn.execute("SELECT MAX(ts) FROM bars WHERE market = 'stock' AND tf = 1440").fetchone()[0]
    return CO.ny_day(last) if last is not None else None


def computed_days(conn: sqlite3.Connection, now: datetime | None = None) -> list[date]:
    """Séances de la n° 12 : celles de l'univers du labo (comme la n° 9), hors bloc scellé, séances entières, dont le
    journalier est en base et dont la séance suivante est close depuis SETTLED_AFTER (la passe A se solde sur deux
    séances)."""
    now = now or datetime.now(timezone.utc)
    known = last_daily(conn)
    if known is None:
        return []
    out = []
    for (d,) in conn.execute("SELECT DISTINCT day FROM universe WHERE market = 'stock' AND day <= ? ORDER BY day",
                             (known.isoformat(),)):
        day = date.fromisoformat(d)
        if SEAL.sealed("replay", day) or not full_session(day):
            continue
        nxt = CO.session_ts(next_trading_day(day))
        if nxt and nxt[1] + SETTLED_AFTER <= now.timestamp():
            out.append(day)
    return out


def warmup(day: date) -> list[date]:
    """La séance et les WARMUP séances d'avant (la plus récente d'abord), dont la bougie de 9 h 30 de toute la cote
    sert au calcul."""
    out = [day]
    for _ in range(WARMUP):
        out.append(CO.previous_trading_day(out[-1]))
    return out


# --- Collecte de la bougie de 9 h 30 de toute la cote ------------------------------------------------------

def open5_days(conn: sqlite3.Connection, now: datetime | None = None) -> list[date]:
    """Séances dont la bougie de 9 h 30 de toute la cote est utile : chaque séance calculée et les WARMUP séances qui la
    précèdent (bloc scellé compris : ce sont des cours, pas des résultats de stratégie), la plus ancienne d'abord."""
    want: set[date] = set()
    for day in computed_days(conn, now):
        want.update(warmup(day))
    return sorted(want)


def symbols_traded(conn: sqlite3.Connection, day: date) -> list[str]:
    """Actions de la base qui ont une bougie journalière ce jour-là (elles ont coté)."""
    return [s for (s,) in conn.execute("SELECT symbol FROM bars WHERE market = 'stock' AND tf = 1440 AND ts = ? "
                                       "ORDER BY symbol", (CO.ny_midnight(day),))]


async def collect_open5(db: LabDB, now: datetime, max_calls: int, transport=None) -> int:
    """Worker : bougies de 9 h 30 des séances utiles pas encore lues, `max_calls` requêtes au plus (une séance entière au
    moins) ; une séance n'est notée lue que si tous ses lots ont répondu. Renvoie le nombre de requêtes faites."""
    if max_calls <= 0:
        return 0
    done = db.keys(OPEN5_JOB)
    calls = 0
    for day in open5_days(db.conn, now):
        if day.isoformat() in done:
            continue
        bounds = CO.session_ts(day)
        symbols = symbols_traded(db.conn, day)
        if not bounds or len(symbols) < MIN_COTE:
            continue
        batches = [symbols[i:i + OPEN5_BATCH] for i in range(0, len(symbols), OPEN5_BATCH)]
        if calls and calls + len(batches) > max_calls:
            break
        heartbeat.beat(f"labo : bougies de 9 h 30 (n° 12) {day}")
        start = datetime.fromtimestamp(bounds[0], timezone.utc)
        end = datetime.fromtimestamp(bounds[0] + CO.BASE_TF * 60, timezone.utc)
        rows, ok = [], True
        for batch in batches:
            calls += 1
            try:
                got = await SRC.alpaca_bars(batch, CO.BASE_TF, start, end, transport=transport, adjustment="raw")
            except Exception as exc:  # noqa: BLE001 (la séance est reprise au passage suivant)
                log.warning("Labo : n° 12, bougies de 9 h 30 du %s indisponibles (%s)", day, exc)
                ok = False
                break
            for symbol, bars in got.items():
                b = next((b for b in bars if b["t"] == bounds[0]), None)
                if b and b["v"]:
                    rows.append((day.isoformat(), symbol, b["o"], b["h"], b["l"], b["c"], b["v"]))
            heartbeat.beat()
        if not ok:
            break
        if len(rows) < MIN_COTE:  # réponse vide sans erreur : notée lue, la séance ne se calculerait jamais
            log.warning("Labo : n° 12, %d bougies de 9 h 30 seulement le %s, séance reprise plus tard", len(rows), day)
            continue
        with db.transaction():
            db.conn.execute("DELETE FROM open5 WHERE day = ?", (day.isoformat(),))
            db.conn.executemany("INSERT INTO open5 (day, symbol, o, h, l, c, v) VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
            db.set(OPEN5_JOB, day.isoformat(), {"symboles": len(symbols), "bougies": len(rows)})
    return calls


# --- Population d'une séance (lecture seule) ------------------------------------------------------------

def _wilder_atr(rows: list[tuple]) -> float | None:
    """ATR de Wilder 14 j (strategies.atr) sur des bougies journalières (t, o, h, l, c, v) terminées."""
    return ST.atr([{"h": h, "l": lo, "c": c} for _, _, h, lo, c, _ in rows])[-1] if len(rows) > ST.ATR_LEN else None


def population(conn: sqlite3.Connection, day: date) -> dict[str, dict] | None:
    """Chaque action qui a coté `day` et échangé dans ses 5 premières minutes, avec sa bougie de 9 h 30 : gap
    d'ouverture (journalier ajusté), hausse à 9 h 35, clôture de la veille, ATR 14 j et volumes moyens aux prix et en
    actions négociés du jour, volumes relatifs de la passe A (rvol_g) et de l'article (rvol_z). None si la bougie de
    9 h 30 de la séance n'a pas été lue ou si le journalier de la séance est incomplet."""
    if conn.execute("SELECT 1 FROM progress WHERE job = ? AND key = ?", (OPEN5_JOB, day.isoformat())).fetchone() is None:
        return None
    sessions = warmup(day)[RVOL_WINDOW:0:-1]  # les RVOL_WINDOW séances d'avant, la plus ancienne d'abord
    cutoff = CO.ny_midnight(day)
    daily: dict[str, list[tuple]] = defaultdict(list)
    for symbol, *bar in conn.execute(
            "SELECT symbol, ts, o, h, l, c, v FROM bars WHERE market = 'stock' AND tf = 1440 AND ts >= ? AND ts < ? "
            "ORDER BY symbol, ts", (cutoff - DAILY_LOOKBACK_DAYS * 86400, cutoff + 86400)):
        daily[symbol].append(tuple(bar))
    opens: dict[str, dict[str, tuple]] = defaultdict(dict)
    for d, symbol, o, h, lo, c, v in conn.execute(
            "SELECT day, symbol, o, h, l, c, v FROM open5 WHERE day >= ? AND day <= ?",
            (sessions[0].isoformat(), day.isoformat())):
        opens[symbol][d] = (o, h, lo, c, v)
    prev_day = CO.previous_trading_day(day)
    t0 = CO.session_ts(day)[0]
    out: dict[str, dict] = {}
    for symbol, rows in daily.items():
        mine = opens.get(symbol) or {}
        today = mine.get(day.isoformat())
        if not today or CO.ny_day(rows[-1][0]) != day:
            continue
        before = rows[:-1]
        if not before or CO.ny_day(before[-1][0]) != prev_day or before[-1][4] <= 0 or today[0] <= 0:
            continue
        o5, h5, l5, c5, v5 = today
        adj_open, adj_prev = rows[-1][1], before[-1][4]
        factor = adj_open / o5
        by_day = {CO.ny_day(r[0]): r for r in before}

        def factor_ok(d: date) -> bool:
            r, bar = by_day.get(d), mine.get(d.isoformat())
            return bool(r and bar and bar[0] > 0 and abs(r[1] / bar[0] / factor - 1) <= SPLIT_TOL)

        used = [d for d in sessions if d.isoformat() in mine][-G.RVOL_DAYS:]
        rvol_g = None
        if len(used) >= G.RVOL_MIN_DAYS and all(factor_ok(d) for d in used):
            mean = sum(mine[d.isoformat()][4] for d in used) / len(used)
            rvol_g = v5 / mean if mean > 0 else None
        window = sessions[-SIP_RVOL_DAYS:]
        rvol_z = None
        if all(d in by_day for d in window) and all(factor_ok(d) for d in window if d.isoformat() in mine):
            mean = sum(mine[d.isoformat()][4] if d.isoformat() in mine else 0.0 for d in window) / len(window)
            rvol_z = v5 / mean if mean > 0 else None
        vols = [r[5] or 0.0 for r in before]
        atr = _wilder_atr(before)
        out[symbol] = {
            "bar": {"t": t0, "o": o5, "h": h5, "l": l5, "c": c5, "v": v5},
            "seances": len(before), "facteur": factor, "prev_raw": adj_prev / factor,
            "gap_pct": (adj_open / adj_prev - 1) * 100, "chg_pct": (c5 / o5 * adj_open / adj_prev - 1) * 100,
            "adv20": sum(vols[-20:]) / 20 * factor if len(vols) >= 20 else None,
            "adv14": sum(vols[-14:]) / 14 * factor if len(vols) >= 14 else None,
            "atr": atr / factor if atr else None, "rvol_g": rvol_g, "rvol_z": rvol_z, "v5": v5}
    return out if len(out) >= MIN_COTE else None


def witnesses(day: date, tag: str, symbol: str, pool: list[str], k: int) -> list[str]:
    """Témoins tirés au hasard (graine fixe par séance, version et valeur)."""
    rng = random.Random(f"n12|{day.isoformat()}|{tag}|{symbol}")
    pool = sorted(pool)
    return rng.sample(pool, min(k, len(pool)))


# --- Passe A de Gemini ------------------------------------------------------------------------------------

def gemini_population(pop: dict[str, dict]) -> list[str]:
    """Le screener du direct refait : MA_TOP plus forts volumes de la bougie de 9 h 30 et TG_TOP plus fortes hausses à
    9 h 35 (égalités par symbole)."""
    ma = sorted(pop, key=lambda s: (-pop[s]["v5"], s))[:MA_TOP]
    tg = sorted(pop, key=lambda s: (-pop[s]["chg_pct"], s))[:TG_TOP]
    return list(dict.fromkeys(ma + tg))


def gemini_candidates(pop: dict[str, dict], people: list[str]) -> tuple[list[tuple[str, dict, float]], dict[str, str]]:
    """Le tri du direct (gemini.scan, passe A), dans son ordre : gap de +5 à +25 %, DEEP_MAX premières au volume de la
    bougie sur le volume moyen de 20 séances, plan sur la bougie de 9 h 30, volume relatif ≥ 4 ; FINNHUB_MAX gardées."""
    pre = []
    for s in people:
        p = pop[s]
        if p["seances"] < MIN_SESSIONS or not G.SEUILS["gap_min"] <= p["gap_pct"] <= G.SEUILS["gap_max"]:
            continue
        pre.append((s, p["v5"] / p["adv20"] if p["adv20"] else 0.0))
    pre = sorted(pre, key=lambda x: (-x[1], x[0]))[:G.DEEP_MAX]
    cands, ecartees = [], {}
    for s, _ in pre:
        p = pop[s]
        plan, why = G.plan_a([p["bar"]], p["prev_raw"])
        if not plan:
            ecartees[s] = why
            continue
        if p["rvol_g"] is None or p["rvol_g"] < G.SEUILS["rvol_min"]:
            ecartees[s] = "volume relatif " + ("inconnu" if p["rvol_g"] is None else f"{p['rvol_g']:.1f}")
            continue
        cands.append((s, plan, p["rvol_g"]))
    return cands[:G.FINNHUB_MAX], ecartees


def gemini_plans(day: date, pop: dict[str, dict]) -> tuple[list[dict], dict]:
    people = gemini_population(pop)
    cands, ecartees = gemini_candidates(pop, people)
    chosen = {s for s, _, _ in cands}
    plans = []
    for rang, (s, plan, rv) in enumerate(cands, start=1):
        plans.append({"symbol": s, "kind": "A", **plan, "rvol": round(rv, 2), "rang": rang, "day": day.isoformat(),
                      "actif_des": _ts(day, ACTIF_DES), "etf": MARKET, "bar_o": pop[s]["bar"]["o"],
                      "temoins": witnesses(day, "A", s, [x for x in people if x not in chosen], G.TEMOINS)})
    return plans, {"population": len(people), "plans": [p["symbol"] for p in plans], "ecartees": ecartees}


def gemini_rows(p: dict, bars: dict[str, list[dict]], next_day: date, pop: dict[str, dict]) -> list[dict]:
    """Lignes j0, j1 et jx d'un plan (gemini.settle_plan, le code du direct) ; glissement doublé à part."""
    res = G.settle_plan(p, bars, next_day, EURUSD)
    x2 = G.settle_plan(p, bars, next_day, EURUSD, slip_mult=2.0) if res.get("declenche") else {}
    entree = res.get("entree") or {}
    base = {"day": p["day"], "symbol": p["symbol"], "rang": p["rang"], "cote": "long",
            "gap_pct": round(pop[p["symbol"]]["gap_pct"], 3), "rvol": p["rvol"], "prix": p["bar_o"],
            "entry": p["entry"], "stop": entree.get("stop", p["stop_range"]),
            "risque": round(entree["prix"] - entree["stop"], 4) if entree else None,
            "declenche": int(bool(res.get("declenche"))), "t_entree": entree.get("t"), "temoin": res.get("temoin"),
            "r_suivante": None}
    out = []
    for v in VARIANTS:
        row = res.get(v) or {}
        w = row.get("temoin") or {}
        shares = row.get("actions")
        out.append(base | {"essai": f"gemini_a_{v}", "sortie": ",".join(why for _, _, why in row.get("sorties") or []),
                           "r_brut": row.get("r_brut"), "r_frais": row.get("r_frais"),
                           "r_frais_x2": (x2.get(v) or {}).get("r_frais"),
                           "hors_mise": int(bool(row.get("hors_mise"))), "incomplet": int(bool(row.get("incomplet"))),
                           "valeur": round(shares * entree["prix"], 2) if shares and entree else None,
                           "temoin_r_brut": w.get("r_brut"),
                           "temoin_r_frais": None if w.get("incomplet") else w.get("r_frais"),
                           "detail": json.dumps({"entree": entree or None, "sorties": row.get("sorties"),
                                                 "nuit": row.get("nuit"), "pourquoi": row.get("pourquoi"),
                                                 "temoins": p["temoins"]}, ensure_ascii=False)})
    return out


# --- ORB des stocks in play ----------------------------------------------------------------------------

def sip_pool(pop: dict[str, dict], filtres: bool) -> list[str]:
    """Valeurs que l'article examine ce jour-là : toutes celles qui ont un volume relatif, un ATR et une première bougie
    avec un corps (sans filtre), ou celles qui passent en plus ses trois filtres."""
    out = []
    for s, p in pop.items():
        if p["rvol_z"] is None or not p["atr"] or side(p["bar"]) is None:
            continue
        if filtres and (p["bar"]["o"] <= SIP_FILTERS["prix_min"] or (p["adv14"] or 0) < SIP_FILTERS["volume_min"]
                        or p["atr"] <= SIP_FILTERS["atr_min"]):
            continue
        out.append(s)
    return out


def sip_list(pop: dict[str, dict], pool: list[str]) -> list[str]:
    """Les SIP_TOP plus forts volumes relatifs d'au moins SIP_RVOL_MIN (égalités par symbole)."""
    ok = [s for s in pool if pop[s]["rvol_z"] >= SIP_RVOL_MIN]
    return sorted(ok, key=lambda s: (-pop[s]["rvol_z"], s))[:SIP_TOP]


def side(bar: dict) -> str | None:
    return "long" if bar["c"] > bar["o"] else "short" if bar["c"] < bar["o"] else None


def sip_level(bar5: dict, sens: str) -> float:
    """Niveau de l'ordre stop : le plus haut du range (achat) ou son plus bas (vente)."""
    return bar5["h"] if sens == "long" else bar5["l"]


def sip_stop(bar5: dict, atr: float, sens: str, px: float, mode: str) -> float:
    """Stop : SIP_STOP_ATR de l'ATR du prix d'exécution (« atr »), ou l'autre bout du range (« range »)."""
    if mode == "range":
        return bar5["l"] if sens == "long" else bar5["h"]
    return px - SIP_STOP_ATR * atr if sens == "long" else px + SIP_STOP_ATR * atr


def sip_fill(bar5: dict, atr: float, sens: str, bars: list[dict], day: date, mode: str = "atr") -> dict | None:
    """Ordre stop au bout du range de 9 h 30, actif de 9 h 35 à SIP_UNTIL, sur les bougies d'1 min : {t, prix, stop,
    niveau} (prix bruts, sans glissement) ; prix = le niveau, ou l'ouverture de la minute au-delà."""
    start, until = _ts(day, time(9, 35)), _ts(day, SIP_UNTIL)
    level = sip_level(bar5, sens)
    for b in bars:
        if b["t"] < start:
            continue
        if b["t"] >= until:
            return None
        if sens == "long" and b["h"] >= level:
            px = max(level, b["o"])
            return {"t": b["t"], "prix": px, "stop": sip_stop(bar5, atr, sens, px, mode), "niveau": level}
        if sens == "short" and b["l"] <= level:
            px = min(level, b["o"])
            return {"t": b["t"], "prix": px, "stop": sip_stop(bar5, atr, sens, px, mode), "niveau": level}
    return None


def sip_manage(f: dict, sens: str, bars: list[dict], day: date, same_minute: bool = True) -> dict:
    """Stop fixe, sinon sortie au dernier cours de la séance. `same_minute` : le stop est cherché dès la minute de
    l'entrée (le pire, l'ordre des prix y est inconnu) ; sinon à partir de la suivante. {prix, raison, au_marche,
    incomplet}."""
    close_ts = CO.session_ts(day)[1]
    last = None
    for b in bars:
        if b["t"] < f["t"] or (not same_minute and b["t"] == f["t"]):
            continue
        if b["t"] >= close_ts:
            break
        last = b
        gap = b["t"] > f["t"] and (b["o"] <= f["stop"] if sens == "long" else b["o"] >= f["stop"])
        if gap:  # ouverture de la minute au-delà du stop (suspension, trou) : vendu à l'ouverture
            return {"prix": b["o"], "raison": "stop", "au_marche": True, "incomplet": False}
        if (b["l"] <= f["stop"]) if sens == "long" else (b["h"] >= f["stop"]):
            return {"prix": f["stop"], "raison": "stop", "au_marche": True, "incomplet": False}
    if last is None:
        return {"prix": None, "raison": "donnees", "au_marche": True, "incomplet": True}
    complete = last["t"] + 60 >= close_ts
    return {"prix": last["c"], "raison": "cloture" if complete else "fin des données", "au_marche": True,
            "incomplet": not complete}


def sip_result(f: dict, m: dict, sens: str, slip_mult: float = 1.0) -> dict:
    """R brut (sans frais ni glissement) et R aux frais de Simão : 20 € de risque en actions entières, un ordre
    d'entrée (stop, glissement) et un de sortie (stop ou au marché, glissement), commission IBKR du plan du compte."""
    sign = 1 if sens == "long" else -1
    risk = sign * (f["prix"] - f["stop"])
    if risk <= 0 or m["prix"] is None:
        return {"r_brut": None, "r_frais": None, "hors_mise": False, "actions": None}
    brut = sign * (m["prix"] - f["prix"]) / risk
    entry = f["prix"] + sign * slip_mult * G.slip(f["prix"])
    risk_paid = sign * (entry - f["stop"])
    shares = math.floor(K.RISK_EUR * EURUSD / risk_paid + 1e-9) if risk_paid > 0 else 0
    if shares < 1:
        return {"r_brut": round(brut, 4), "r_frais": None, "hors_mise": True, "actions": None}
    exit_px = m["prix"] - sign * (slip_mult * G.slip(m["prix"]) if m["au_marche"] else 0.0)
    pnl = shares * sign * (exit_px - entry) - G.commission(shares, entry) - G.commission(shares, exit_px)
    return {"r_brut": round(brut, 4), "r_frais": round(pnl / (shares * risk_paid), 4), "hors_mise": False,
            "actions": shares}


def sip_witness(f: dict, bars: list[dict]) -> dict | None:
    """Le témoin, pris au marché à la minute de l'entrée, même stop en % du prix."""
    b = next((b for b in bars if f["t"] <= b["t"] < f["t"] + 300), None)
    if not b or b["o"] <= 0:
        return None
    px = b["o"]
    return {"t": b["t"], "prix": px, "stop": px * f["stop"] / f["prix"]}


def sip_witnesses(day: date, version: str, symbols: list[str], pool: list[str]) -> dict[str, list[str]]:
    chosen = set(symbols)
    others = [x for x in pool if x not in chosen]
    return {s: witnesses(day, version, s, others, SIP_TEMOINS) for s in symbols}


def sip_rows(day: date, version: str, symbols: list[str], pop: dict[str, dict], draws: dict[str, list[str]],
             bars: dict[str, list[dict]]) -> list[dict]:
    """Lignes d'une version (SIP_VERSIONS) : essai sip_long ou sip_court, suffixé de la version."""
    mode = SIP_VERSIONS[version][1]
    suffix = version.removeprefix("sip")
    out = []
    for rang, s in enumerate(symbols, start=1):
        p = pop[s]
        sens = side(p["bar"])
        if sens is None:
            continue
        f = sip_fill(p["bar"], p["atr"], sens, bars.get(s) or [], day, mode)
        row = {"day": day.isoformat(), "symbol": s, "essai": ("sip_long" if sens == "long" else "sip_court") + suffix,
               "rang": rang, "cote": sens, "gap_pct": round(p["gap_pct"], 3), "rvol": round(p["rvol_z"], 2),
               "prix": p["bar"]["o"], "entry": sip_level(p["bar"], sens), "stop": None, "risque": None,
               "declenche": int(bool(f)), "t_entree": None, "sortie": None, "r_brut": None, "r_frais": None,
               "r_frais_x2": None, "r_suivante": None, "hors_mise": 0, "incomplet": 0, "valeur": None, "temoin": None,
               "temoin_r_brut": None, "temoin_r_frais": None, "detail": None}
        if f:
            m = sip_manage(f, sens, bars.get(s) or [], day)
            res, x2 = sip_result(f, m, sens), sip_result(f, m, sens, slip_mult=2.0)
            later = sip_result(f, sip_manage(f, sens, bars.get(s) or [], day, same_minute=False), sens)
            row |= {"stop": round(f["stop"], 4), "risque": round(abs(f["prix"] - f["stop"]), 4), "t_entree": f["t"],
                    "sortie": m["raison"], "r_brut": res["r_brut"], "r_frais": res["r_frais"],
                    "r_frais_x2": x2["r_frais"], "r_suivante": later["r_frais"],
                    "hors_mise": int(bool(res["hors_mise"])), "incomplet": int(m["incomplet"]),
                    "valeur": round(res["actions"] * f["prix"], 2) if res["actions"] else None}
            for w in draws.get(s) or []:
                wf = sip_witness(f, bars.get(w) or [])
                if not wf:
                    continue
                wm = sip_manage(wf, sens, bars.get(w) or [], day)
                wr = sip_result(wf, wm, sens)
                row |= {"temoin": w, "temoin_r_brut": wr["r_brut"],
                        "temoin_r_frais": None if wm["incomplet"] else wr["r_frais"]}
                break
            row["detail"] = json.dumps({"entree": f, "sortie": m, "rvol_z": p["rvol_z"], "atr": p["atr"]},
                                       ensure_ascii=False)
        out.append(row)
    return out


# --- Calcul d'une séance -----------------------------------------------------------------------------------

async def minute_bars(symbols: list[str], day: date, until: date, transport=None) -> dict[str, list[dict]]:
    """Bougies d'1 min SIP brutes de la séance régulière, de `day` à `until` (compris)."""
    out: dict[str, list[dict]] = {}
    start = datetime.fromtimestamp(CO.session_ts(day)[0], timezone.utc)
    end = datetime.fromtimestamp(CO.session_ts(until)[1], timezone.utc)
    for i in range(0, len(symbols), MINUTE_BATCH):
        batch = symbols[i:i + MINUTE_BATCH]
        got = await SRC.alpaca_bars(batch, 1, start, end, transport=transport, adjustment="raw")
        for s in batch:
            out[s] = alpaca.regular_hours(got.get(s) or [])
        heartbeat.beat()
    return out


async def day_rows(conn: sqlite3.Connection, day: date, transport=None) -> tuple[list[dict], dict] | None:
    """Lignes et comptes d'une séance ; None si la bougie de 9 h 30 de la séance n'est pas lue (ou le journalier
    incomplet)."""
    pop = population(conn, day)
    if pop is None:
        return None
    plans, stats = gemini_plans(day, pop)
    lists, draws, sizes = {}, {}, {}
    for filtres in (False, True):
        version = "sip_filtres" if filtres else "sip"
        pool = sip_pool(pop, filtres)
        sizes[version] = len(pool)
        lists[version] = sip_list(pop, pool)
        draws[version] = sip_witnesses(day, version, lists[version], pool)
    stats |= {"cote": len(pop), "sip": dict(lists), "sip_population": sizes}
    lists["sip_range"], draws["sip_range"] = lists["sip"], draws["sip"]  # même liste et mêmes témoins, autre stop
    nxt = next_trading_day(day)
    both = sorted({MARKET, *(p["symbol"] for p in plans), *(w for p in plans for w in p["temoins"])}) if plans else []
    today = sorted({*(s for v in lists.values() for s in v), *(w for d in draws.values() for ws in d.values()
                                                               for w in ws)} - set(both))
    bars = await minute_bars(today, day, day, transport) if today else {}
    if both:
        bars |= await minute_bars(both, day, nxt, transport)
    rows: list[dict] = []
    for p in plans:
        rows += gemini_rows(p, bars, nxt, pop)
    for version in SIP_VERSIONS:
        rows += sip_rows(day, version, lists[version], pop, draws[version], bars)
    return rows, stats


def ready_days(db: LabDB, now: datetime, limit: int = 30) -> list[date]:
    """Séances calculables pas encore calculées sous la mesure MESURE : la bougie de 9 h 30 de la séance et des WARMUP
    séances d'avant est lue."""
    done = {k for k, v in db.keys(JOB).items() if v.get("mesure") == MESURE}
    seen = db.keys(OPEN5_JOB)
    out = []
    for day in computed_days(db.conn, now):
        if day.isoformat() in done:
            continue
        if all(d.isoformat() in seen for d in warmup(day)):
            out.append(day)
        if len(out) >= limit:
            break
    return out


async def step(db: LabDB, now: datetime, max_seconds: float, transport=None) -> int:
    """Worker : calcule des séances dans le temps donné ; renvoie le nombre de séances faites."""
    if max_seconds <= 0:
        return 0
    started, done = clock.monotonic(), 0
    for day in ready_days(db, now):
        if done and clock.monotonic() - started >= max_seconds:
            break
        heartbeat.beat(f"labo : ouverture 5 min (n° 12) {day}")
        try:
            got = await day_rows(db.conn, day, transport)
        except Exception as exc:  # noqa: BLE001 (réseau : la séance est reprise au passage suivant)
            log.warning("Labo : n° 12, séance %s reportée (%s: %s)", day, type(exc).__name__, exc)
            break
        if got is None:
            continue
        rows, stats = got
        with db.transaction():
            db.conn.execute("DELETE FROM ouverture_trades WHERE day = ?", (day.isoformat(),))
            db.conn.executemany(f"INSERT INTO ouverture_trades ({', '.join(COLUMNS)}) VALUES "
                                f"({', '.join('?' for _ in COLUMNS)})", [tuple(r.get(c) for c in COLUMNS) for r in rows])
            db.set(JOB, day.isoformat(), stats | {"mesure": MESURE, "regles_gemini": G.VERSION})
            db.bump("ouverture")  # le cache du tableau (strat_stats.signature) voit une séance refaite sur place
        log.info("Labo : n° 12 %s, %d plans A, %d lignes", day, len(stats["plans"]), len(rows))
        done += 1
    return done


# --- Tableau (lecture seule) ----------------------------------------------------------------------------

def progress(conn: sqlite3.Connection, now: datetime | None = None) -> dict | None:
    """Avancement : séances de l'historique (jusqu'à BACKLOG_UNTIL) pas encore calculées sous MESURE, séances plus
    récentes en attente (avancement ordinaire), séances faites, et bougies de 9 h 30 lues sur celles qu'il faut. None
    si la base n'est pas lisible."""
    try:
        done = {k for k, v in conn.execute("SELECT key, value FROM progress WHERE job = ?", (JOB,))
                if json.loads(v).get("mesure") == MESURE}
        seen = {k for (k,) in conn.execute("SELECT key FROM progress WHERE job = ?", (OPEN5_JOB,))}
        days = computed_days(conn, now)
        want = open5_days(conn, now)
    except sqlite3.Error:
        return None
    todo = [d for d in days if d.isoformat() not in done]
    return {"seances_a_calculer": sum(d <= BACKLOG_UNTIL for d in todo),
            "seances_nouvelles": sum(d > BACKLOG_UNTIL for d in todo),
            "seances_calculees": len(days) - len(todo),
            "bougies_9h30": {"lues": sum(d.isoformat() in seen for d in want), "utiles": len(want)}}


def _halves(rows: list[tuple[str, float]]) -> tuple[float | None, float | None]:
    m = SS.Mean()
    for d, x in rows:
        m.add(d, x)
    return SS._halves(m)


def _mean(rows: list[tuple[str, float]]) -> dict:
    m = SS.Mean()
    for d, x in rows:
        m.add(d, x)
    return m.result()


def _median(xs: list[float]) -> float | None:
    xs = sorted(xs)
    if not xs:
        return None
    mid = len(xs) // 2
    return round(xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2, 2)


def item(rows: list[dict], trials: int, essai: str) -> dict:
    """Bilan d'une ligne : r_frais des trades déclenchés, complets, à la taille de Simão, et la règle « candidate »
    du labo pour les essais."""
    sel = [r for r in rows if r["essai"] == essai]
    fired = [r for r in sel if r["declenche"]]
    ok = [r for r in fired if r["r_frais"] is not None and not r["incomplet"]]
    main = _mean([(r["day"], r["r_frais"]) for r in ok])
    ecart = _mean([(r["day"], r["r_frais"] - r["temoin_r_frais"]) for r in ok if r["temoin_r_frais"] is not None])
    h1, h2 = _halves([(r["day"], r["r_frais"]) for r in ok])
    x2 = _mean([(r["day"], r["r_frais_x2"]) for r in ok if r["r_frais_x2"] is not None])["esperance_r"]
    out = {"nom": LABELS.get(essai, essai), "lignes": len(sel), "declenches": len(fired), "mesures": len(ok),
           "hors_mise": sum(r["hors_mise"] for r in fired), "incomplets": sum(r["incomplet"] for r in fired),
           "r_frais": main, "brut": _mean([(r["day"], r["r_brut"]) for r in fired if r["r_brut"] is not None]),
           "temoin": _mean([(r["day"], r["temoin_r_frais"]) for r in ok if r["temoin_r_frais"] is not None]),
           "ecart_temoin": ecart, "moitie_1": h1, "moitie_2": h2, "glisse_x2": x2,
           "reussite": round(sum(r["r_frais"] > 0 for r in ok) / len(ok), 4) if ok else None,
           "valeur_mediane": _median([r["valeur"] for r in ok if r["valeur"]])}
    out["par_annee"] = {y: _mean([(r["day"], r["r_frais"]) for r in ok if r["day"][:4] == y])
                        for y in sorted({r["day"][:4] for r in ok})}
    out["par_prix"] = {label: _mean([(r["day"], r["r_frais"]) for r in ok if r["prix"] is not None
                                     and lo <= r["prix"] < hi]) for lo, hi, label in PRICE_BUCKETS}
    out["par_gap"] = {label: _mean([(r["day"], r["r_frais"]) for r in ok if r["gap_pct"] is not None
                                    and lo <= r["gap_pct"] < hi]) for lo, hi, label in GAP_BUCKETS}
    if essai.startswith("sip"):
        out["stop_minute_suivante"] = _mean([(r["day"], r["r_suivante"]) for r in ok if r["r_suivante"] is not None])
        out["par_stop"] = {label: _mean([(r["day"], r["r_frais"]) for r in ok if r["risque"] is not None
                                         and lo <= r["risque"] < hi]) for lo, hi, label in STOP_BUCKETS}
        out["par_rvol"] = {label: _mean([(r["day"], r["r_frais"]) for r in ok if lo <= r["rvol"] < hi])
                           for lo, hi, label in RVOL_BUCKETS}
    if essai in TRIALS:
        seuil = SS.candidate_t(ecart["grappes"], trials) if ecart["n"] else None
        ci = main["ic95"]
        out |= {"essai": True, "seuil_t": seuil,
                "tient": bool(ci and ci[0] > 0 and SS.passes(ecart["z"], seuil) and len(ok) >= SS.MIN_N
                              and all(x is not None and x > 0 for x in (h1, h2, x2)))}
    return out


def report(conn: sqlite3.Connection, trials: int | None = None) -> dict | None:
    """Tableau de la n° 12 (lecture seule) ; None avant la première bougie de 9 h 30 lue."""
    try:
        prog = {k: json.loads(v) for k, v in conn.execute("SELECT key, value FROM progress WHERE job = ?", (JOB,))}
        prog = {k: v for k, v in prog.items() if v.get("mesure") == MESURE}
        cols = conn.execute(f"SELECT {', '.join(COLUMNS)} FROM ouverture_trades ORDER BY day").fetchall()
        seen = conn.execute("SELECT COUNT(*) FROM progress WHERE job = ?", (OPEN5_JOB,)).fetchone()[0]
    except sqlite3.Error:
        return None
    if not prog:
        return {"numero": NUMERO, "seances": 0, "bougies_9h30_lues": seen} if seen else None
    if trials is None:
        from app.lab import council as CNC  # import tardif, comme strat_stats.report

        ST.register_council(CNC.load(conn))
        trials = len(ST.trials())
    rows = [r for r in (dict(zip(COLUMNS, c)) for c in cols) if r["day"] in prog]
    days = sorted(prog)
    stats = [prog[d] for d in days]
    return {"numero": NUMERO, "mesure": MESURE, "seances": len(days), "du": days[0], "au": days[-1],
            "bougies_9h30_lues": seen, "essais": len(TRIALS), "essais_labo": trials, "scelle": SEAL.describe(),
            "plans_a": sum(len(s.get("plans") or []) for s in stats),
            "regles_gemini": sorted({s.get("regles_gemini") for s in stats if s.get("regles_gemini") is not None}),
            "population_a": round(sum(s.get("population") or 0 for s in stats) / len(stats), 1),
            "cote": round(sum(s.get("cote") or 0 for s in stats) / len(stats), 1),
            "lignes": {e: item(rows, trials, e) for e in LABELS}}


def day_detail(conn: sqlite3.Connection, day: str) -> dict | None:
    """Une séance calculée : listes, plans écartés et lignes (pour vérifier un jour sur les graphiques)."""
    try:
        got = conn.execute("SELECT value FROM progress WHERE job = ? AND key = ?", (JOB, day)).fetchone()
        cols = conn.execute(f"SELECT {', '.join(COLUMNS)} FROM ouverture_trades WHERE day = ? ORDER BY essai, rang",
                            (day,)).fetchall()
    except sqlite3.Error:
        return None
    if not got:
        return None
    rows = []
    for c in cols:
        r = dict(zip(COLUMNS, c))
        r["detail"] = json.loads(r["detail"]) if r["detail"] else None
        rows.append(r)
    return {"jour": day, "comptes": json.loads(got[0]), "lignes": rows}


def _r(x: float | None) -> str:
    return "n/d" if x is None else f"{x:+.3f}".replace(".", ",").replace("-", "−")


def text(rep: dict | None) -> str:
    """Résumé pour Discord (`!ouverture`) et `!strategies`."""
    if not rep:
        return "🌅 Ouverture 5 min (n° 12) : rien de collecté pour l'instant."
    if not rep.get("seances"):
        return (f"🌅 Ouverture 5 min (n° 12) : collecte des bougies de 9 h 30 en cours ({rep.get('bougies_9h30_lues', 0)} "
                "séances lues), aucune séance calculée.")
    n, k = rep["seances"], rep["plans_a"]
    lines = [f"🌅 **Ouverture 5 min (n° 12)** · {n} séance{'s' if n > 1 else ''} du {rep['du']} au {rep['au']} · "
             f"{k} plan{'s' if k > 1 else ''} A · seuil sur {rep['essais_labo']} essais"]
    for it in rep["lignes"].values():
        m = it["r_frais"]
        if not it["mesures"]:
            continue
        ic = m.get("ic95") or [None, None]
        e = it["ecart_temoin"]
        verdict = ""
        if it.get("essai"):
            verdict = " ✅ tient : candidate au bloc scellé" if it["tient"] else ""
        lines.append(f"- {it['nom']} : {_r(m['esperance_r'])} R à 20 € [{_r(ic[0])} ; {_r(ic[1])}] · "
                     f"{it['mesures']} trades sur {m['jours']} jours · brut {_r(it['brut']['esperance_r'])} · écart au "
                     f"témoin {_r(e['esperance_r'])} (t {SS._z(e['z'])}"
                     + (f", seuil {SS._z(it['seuil_t'])}" if it.get("essai") else "") + f"){verdict}")
    if len(rep.get("regles_gemini") or []) > 1:
        lines.append(f"⚠️ Passe A rejouée sous plusieurs versions des règles du direct ({rep['regles_gemini']}) : la n° 12 "
                     "déclare la v1, une autre version demande une nouvelle déclaration.")
    lines.append("-# Rejeu hors bloc scellé, aux frais de ton compte. Passe A sans catalyseur ni nombre d'actions (pas de "
                 "données datées). Une ligne qui tient ne devient une méthode qu'après le bloc scellé, ouvert une fois par "
                 "toi.")
    return "\n".join(lines)
