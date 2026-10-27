"""Déclaration n° 12 (27 octobre 2026, docs/labo-strategies.md) : cassures des 5 premières minutes sur les valeurs en
jeu, rejouées sur toute la cote. Protocole v2, figé le 27/10 avant tout calcul après deux tours d'avis de GPT et de
Gemini (/mnt/project-files/labo/methode-gagnante/protocole-n12-v2-fige-2026-10-27.md) : relecture des minutes qui
décident en transactions et NBBO, ordres de l'ORB actifs à 9 h 38, deux témoins, essai 6, 150 trades sur 100 séances.

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
  témoins et de SPY, relues et gardées en mémoire, comme le solde du challenger en direct ;
- pour les achats de l'ORB et leurs témoins, les minutes qui décident (achat, stop) relues transaction par transaction
  avec le NBBO SIP, et la cotation en vigueur à l'entrée des témoins, gardées en mémoire le temps de la séance.

Passe A de Gemini, le code du direct (gemini.plan_a, gemini.settle_plan, mêmes seuils SEUILS), sur une population
refaite : les MA_TOP plus forts volumes de la première bougie et les TG_TOP plus fortes hausses à 9 h 35 (le screener
d'Alpaca du direct ne se rejoue pas). Gap de +5 à +25 % ; les DEEP_MAX premières au volume de la bougie rapporté au
volume journalier moyen de 20 séances ; plan sur la bougie de 9 h 30 ; volume relatif ≥ 4 (bougie de 9 h 30 contre la
moyenne des RVOL_DAYS dernières séances, parmi les RVOL_WINDOW d'avant, où la valeur a échangé dans les 5 premières
minutes, au moins RVOL_MIN_DAYS) ; les FINNHUB_MAX premières dans cet ordre. Ordre actif à ACTIF_DES (le direct : deux
minutes après le message de 9 h 36). Écarts au direct, dits : ni catalyseur ni nombre d'actions (aucune donnée datée
sur toute la période), ni la liste de surveillance ; SPY à la place de l'ETF du secteur pour la règle de nuit ; la
bougie de 9 h 30 et les volumes relatifs viennent du SIP (le direct prend la bougie IEX, une petite part du volume, et
compare IEX à IEX) ; la condition de volume de la cassure se lit, comme au solde du direct (règles v2), sur les bougies
SIP d'1 min. Un essai par sortie qui décide : j0 (la règle du plan) et jx
(règle de nuit) ; j1 est publié sans essai. Témoin : comme le direct, une valeur tirée au hasard dans la population
(graine fixe), achetée au marché à la minute du déclenchement, même géométrie en %, mêmes sorties.

ORB des stocks in play (règles de la section 2.1 et 4 de l'article) : volume relatif = volume de la bougie de 9 h 30
sur la moyenne simple des SIP_RVOL_DAYS séances d'avant (0 une séance cotée sans échange à l'ouverture) ; au moins
SIP_RVOL_MIN ; bougies sans corps retirées avant le classement (système en direct des auteurs) ; les SIP_TOP plus forts,
ceux qui qualifient s'il y en a moins ; sens de la bougie de 9 h 30 (verte : achat, rouge : vente) ; ordre stop au plus
haut (achat) ou au plus bas (vente) du range, actif à ACTIF_DES (Simão saisit ses ordres à la main, comme la passe A en
direct ; un niveau déjà franchi entre 9 h 35 et 9 h 38 part au marché sur la première transaction au-delà, ni annulé
ni déplacé) jusqu'à SIP_UNTIL ; stop à SIP_STOP_ATR de l'ATR 14 j (Wilder, cité par l'article) du prix de la
transaction qui déclenche ; sortie au dernier cours de la séance. Versions : sip (sans filtre, règle de Simão),
sip_filtres (les filtres de l'article : ouverture > 5 $, volume moyen 14 j ≥ 1 M d'actions, ATR 14 j > 0,50 $ ; elle
seule mesure ce que la règle « pas de filtre de liquidité » coûte, et ne peut pas devenir une règle de JARVIS sans
Simão), sip_range (sans filtre, stop à l'autre bout du range de 5 min, règle de l'article frère sur QQQ, SSRN 4416622 :
un stop plus large que les frais) et l'essai 6, sip_long_top5 (les TOP5 premières valeurs vertes de sip, stop au range,
POSITIONS_MAX achats au plus par séance : la version jouable avec le budget de risque ouvert du grade).

Relecture (protocole v2, A ; la méthode du journal Killmode, killmode.tick_bars) : pour chaque achat de l'ORB, la
minute qui déclenche l'achat et celle qui déclenche le stop, transaction par transaction : ne déclenche qu'une
transaction qui fait un dernier cours (conditions de vente filtrées, corrections écartées) dans un NBBO valide à 0,5 %
près ; achat au pire ask de la cotation en vigueur et de la suivante, stop au pire bid ; dans la minute de l'achat, seules
les transactions qui suivent comptent. Une minute qui décide et reste non relue (plafond TICK_MINUTES_DAY, refus
d'Alpaca) est jouée sur sa bougie, stop compté dans la minute de l'achat (le pire) ; une panne passagère reporte la
séance. Bornes publiées : le même trade sur bougies (r_bougies, le pire ; r_suivante, stop cherché à partir de la minute
suivante). Ventes à découvert publiées sans essai, sur bougies (coût d'emprunt, locate et règle SSR non modélisés).
Réplication de l'article (`article`, sans essai) : filtres, top 20, achats et ventes, ordres actifs à ARTICLE_DES, sur
bougies, à nos frais et aux siens (ARTICLE_COMMISSION par action et par côté).

Témoins (protocole v2, B) : A, qui décide, une valeur tirée au hasard dans la population de la version moins les valeurs
retenues (SIP_TEMOINS tirages, le premier qui échange sert), prise au marché au début de sa minute (ask relu), même sens,
même stop en %, sortie à la clôture, relue comme la stratégie ; B, publié, les mêmes valeurs que la ligne prises au
marché à ACTIF_DES avec la règle de stop de la ligne, toutes, déclenchées ou non (l'écart B, par séance, isole la
cassure) ; SPY, publié, à la minute de l'entrée.

Mesure qui décide : r_frais, le R aux frais de Simão (20 € de risque au change EURUSD_DEFAULT, actions entières,
commission IBKR du plan du compte, prix relus, sinon glissement du labo d'au moins un demi-pas ; un bracket par objectif
pour la passe A comme le direct, un ordre d'entrée et un de sortie pour l'ORB). Règle « candidate » du labo (n° 8 et
10), sur les séances hors bloc scellé : borne basse de l'IC 95 % de r_frais > 0, écart au témoin A au seuil en t
(Bonferroni sur tous les essais déclarés, Student à jours − 1), au moins N12_MIN_TRADES trades sur N12_MIN_SEANCES
séances, les deux moitiés chronologiques et le glissement doublé positifs. Une ligne qui tient devient candidate au
bloc scellé, ouvert une seule fois et par Simão. Le bloc scellé n'est ni lu ni calculé ici.
"""
import json
import logging
import math
import random
import sqlite3
import time as clock
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

from app.config import env_float
from app.core.http import SourceError
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
# Passe A (app/trading/gemini.py) : version des règles du direct déclarée (gemini.VERSION)
REGLES_GEMINI = 2
MA_TOP, TG_TOP = 100, 50
# Ordres actifs : la passe A comme le direct (deux minutes après son message de 9 h 36), et les essais ORB (protocole
# v2, C1 : Simão saisit ses ordres à la main) ; la réplication de l'article à 9 h 35 (F4).
ACTIF_DES = time(9, 38)
ARTICLE_DES = time(9, 35)
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
# Essai 6 (protocole v2, C5) : les TOP5 premières valeurs vertes de sip, stop au range, POSITIONS_MAX achats au plus par
# séance (risque ouvert de 3 % du capital à 1 % par trade, la lettre A du grade : 60 € à 20 € par trade).
TOP5 = 5
POSITIONS_MAX = 3
# Relecture en transactions (protocole v2, A) : passes au plus (une minute lue peut déplacer le déclenchement) et
# minutes lues au plus par séance (3 requêtes Alpaca chacune, plus quand une page ne suffit pas).
TICK_ROUNDS = 6
TICK_MINUTES_DAY = 400
LOTS_UNTIL = date(2025, 11, 3)  # tailles des cotations SIP en lots de LOT actions avant, en actions depuis (Alpaca)
LOT = 100
# Règle candidate (protocole v2, D3) et effet minimal détectable à 80 % de puissance (D5)
N12_MIN_TRADES, N12_MIN_SEANCES = 150, 100
POWER_Z = 0.8416
ARTICLE_COMMISSION = 0.0035  # $ par action et par côté, sans minimum : les frais de l'article (F4)
CAPITAL_DEFAUT = 2000.0      # € : vue « portefeuille contraint » (F5) quand JARVIS_CAPITAL manque, et dit
STOP_BUCKETS = ((0.0, 0.10, "< 10 c"), (0.10, 0.20, "10-20 c"), (0.20, 0.50, "20-50 c"), (0.50, 1e9, "≥ 50 c"))
RVOL_BUCKETS = ((0.0, 2.0, "1-2"), (2.0, 5.0, "2-5"), (5.0, 30.0, "5-30"), (30.0, 1e18, "> 30"))
# Ventilations descriptives (revues B et C du 26/10/2026 : mesurer ce que coûte la règle « pas de filtre » sans filtrer)
PRICE_BUCKETS = ((0.0, 5.0, "< 5 $"), (5.0, 20.0, "5-20 $"), (20.0, 1e18, "≥ 20 $"))
GAP_BUCKETS = ((-1e18, -4.0, "< −4 %"), (-4.0, 4.0, "−4 à +4 %"), (4.0, 10.0, "4-10 %"), (10.0, 20.0, "10-20 %"),
               (20.0, 1e18, "≥ 20 %"))
VARIANTS = ("j0", "j1", "jx")
# Essais (strategies.N12_TRIALS) ; les autres lignes (gemini_a_j1, sip_court*, article) sont publiées sans essai.
TRIALS = ("gemini_a_j0", "gemini_a_jx", "sip_long", "sip_long_filtres", "sip_long_range", "sip_long_top5")
ORB_TRIALS = ("sip_long", "sip_long_filtres", "sip_long_range", "sip_long_top5")
LABELS = {"gemini_a_j0": "Gemini A, sortie le jour même", "gemini_a_j1": "Gemini A, sortie le lendemain (sans essai)",
          "gemini_a_jx": "Gemini A, règle de nuit", "sip_long": "Stocks in play, achat, sans filtre",
          "sip_long_filtres": "Stocks in play, achat, filtres de l'article",
          "sip_long_range": "Stocks in play, achat, stop au range, sans filtre",
          "sip_long_top5": "Stocks in play, achat, top 5, stop au range, 3 positions",
          "sip_court": "Stocks in play, vente à découvert, sans filtre (sans essai)",
          "sip_court_filtres": "Stocks in play, vente à découvert, filtres de l'article (sans essai)",
          "sip_court_range": "Stocks in play, vente à découvert, stop au range (sans essai)",
          "article": "L'article tel quel : filtres, achats et ventes, 9 h 35, sur bougies (sans essai)"}
COLUMNS = ("day", "symbol", "essai", "rang", "cote", "gap_pct", "rvol", "prix", "entry", "stop", "risque",
           "declenche", "t_entree", "sortie", "r_brut", "r_frais", "r_frais_x2", "r_suivante", "hors_mise", "incomplet",
           "valeur", "temoin", "temoin_r_brut", "temoin_r_frais", "univers", "franchi", "releve", "carnet", "t_sortie",
           "px_entree", "px_sortie", "actions", "temoin_b_r_frais", "spy_r_frais", "r_article", "r_bougies", "detail")
# Mesures d'un achat de l'essai 6 sauté (trois positions déjà prises)
SKIPPED = ("stop", "risque", "t_entree", "r_brut", "r_frais", "r_frais_x2", "r_suivante", "valeur", "temoin",
           "temoin_r_brut", "temoin_r_frais", "releve", "carnet", "t_sortie", "px_entree", "px_sortie", "actions",
           "spy_r_frais", "r_bougies", "detail")


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
    """Séances de la n° 12 : celles de l'univers du labo (comme la n° 9), hors bloc scellé et sans séance suivante dans le
    bloc (la passe A tenue la nuit s'y solderait : le 2 avril 2026, protocole v2, E1), séances entières, dont le
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
        following = next_trading_day(day)
        if SEAL.sealed("replay", day) or SEAL.sealed("replay", following) or not full_session(day):
            continue
        nxt = CO.session_ts(following)
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


def gemini_rows(p: dict, bars: dict[str, list[dict]], next_day: date, pop: dict[str, dict],
                seen: frozenset | set = frozenset()) -> list[dict]:
    """Lignes j0, j1 et jx d'un plan (gemini.settle_plan, le code du direct) ; glissement doublé à part ; `seen` :
    l'univers du labo de 9 h 10 ce jour-là."""
    res = G.settle_plan(p, bars, next_day, EURUSD)
    x2 = G.settle_plan(p, bars, next_day, EURUSD, slip_mult=2.0) if res.get("declenche") else {}
    entree = res.get("entree") or {}
    base = {"day": p["day"], "symbol": p["symbol"], "rang": p["rang"], "cote": "long",
            "gap_pct": round(pop[p["symbol"]]["gap_pct"], 3), "rvol": p["rvol"], "prix": p["bar_o"],
            "entry": p["entry"], "stop": entree.get("stop", p["stop_range"]),
            "risque": round(entree["prix"] - entree["stop"], 4) if entree else None,
            "declenche": int(bool(res.get("declenche"))), "t_entree": entree.get("t"), "temoin": res.get("temoin"),
            "r_suivante": None, "univers": int(p["symbol"] in seen)}
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
    """Stop : SIP_STOP_ATR de l'ATR de `px` (« atr »), ou l'autre bout du range (« range »)."""
    if mode == "range":
        return bar5["l"] if sens == "long" else bar5["h"]
    return px - SIP_STOP_ATR * atr if sens == "long" else px + SIP_STOP_ATR * atr


def franchi(bar5: dict, sens: str, bars: list[dict], day: date) -> bool:
    """Niveau déjà atteint sur les bougies de 9 h 35 à 9 h 37 : à ACTIF_DES, l'ordre stop part au marché sur la première
    transaction au-delà s'il y est encore (protocole v2, C2)."""
    a, b = _ts(day, ARTICLE_DES), _ts(day, ACTIF_DES)
    level = sip_level(bar5, sens)
    return any(a <= x["t"] < b and ((x["h"] >= level) if sens == "long" else (x["l"] <= level)) for x in bars)


def sip_fill(bar5: dict, atr: float, sens: str, bars: list[dict], day: date, mode: str = "atr",
             start: time = ACTIF_DES) -> dict | None:
    """Ordre stop au bout du range de 9 h 30, actif de `start` à SIP_UNTIL, sur les bougies d'1 min dont les minutes
    relues sont remplacées par leurs transactions (killmode.refine) : la première bougie ou transaction qui atteint le
    niveau. {t, ns, seq, brut, prix, mesure, taille_ask, stop, niveau} : brut, le niveau ou l'ouverture au-delà
    (bougie), ou la transaction ; prix, le pire ask (achat) ou bid (vente) quand la transaction est relue avec sa
    cotation (mesure), sinon brut (le glissement du labo s'ajoute au R) ; stop calculé sur brut."""
    t0, until = _ts(day, start), _ts(day, SIP_UNTIL)
    level = sip_level(bar5, sens)
    for b in bars:
        if b["t"] < t0:
            continue
        if b["t"] >= until:
            return None
        if (b["h"] >= level) if sens == "long" else (b["l"] <= level):
            brut = max(level, b["o"]) if sens == "long" else min(level, b["o"])
            f = {"t": b["t"], "ns": b.get("ns"), "seq": b.get("seq"), "brut": brut, "prix": brut, "mesure": False,
                 "taille_ask": None, "stop": sip_stop(bar5, atr, sens, brut, mode), "niveau": level}
            quote = b.get("ask") if sens == "long" else b.get("bid")
            if "ns" in b and quote is not None:
                f |= {"prix": max(brut, quote) if sens == "long" else min(brut, quote), "mesure": True,
                      "taille_ask": b.get("ask_size") if sens == "long" else None}
            return f
    return None


def sip_manage(f: dict, sens: str, bars: list[dict], day: date, same_minute: bool = True,
               debut: bool = False) -> dict:
    """Stop fixe f["stop"], sinon sortie au dernier cours de la séance. Dans la minute de l'entrée : après un achat sur
    une transaction relue, seules les transactions qui la suivent comptent ; après un achat sur une bougie, toute la
    bougie compte (le pire : l'ordre des prix y est inconnu) ; `debut` : entrée au début de la minute (témoins), tout
    ce qui suit est après elle, ouverture comprise ; `same_minute` False : stop cherché à partir de la minute suivante
    (borne). Stop sur une transaction relue : vendu au pire bid (racheté au pire ask) après elle. {t, ns, brut, prix,
    mesure, raison, au_marche, incomplet}."""
    close_ts = CO.session_ts(day)[1]
    key = (f["ns"], f.get("seq") or 0) if f.get("ns") is not None else None
    last = None
    for b in bars:
        if b["t"] < f["t"]:
            continue
        if b["t"] == f["t"] and not debut:
            if not same_minute or (key is not None and (b.get("ns", -1), b.get("seq") or 0) <= key):
                continue
        if b["t"] >= close_ts:
            break
        last = b
        tick = "ns" in b
        if not tick and (b["t"] > f["t"] or debut) and (b["o"] <= f["stop"] if sens == "long" else b["o"] >= f["stop"]):
            # ouverture de la minute au-delà du stop (suspension, trou) : vendu à l'ouverture
            return {"t": b["t"], "ns": None, "brut": b["o"], "prix": b["o"], "mesure": False, "raison": "stop",
                    "au_marche": True, "incomplet": False}
        if (b["l"] <= f["stop"]) if sens == "long" else (b["h"] >= f["stop"]):
            if tick:
                quote = b.get("bid") if sens == "long" else b.get("ask")
                px = b["o"] if quote is None else min(b["o"], quote) if sens == "long" else max(b["o"], quote)
                return {"t": b["t"], "ns": b["ns"], "brut": b["o"], "prix": px, "mesure": quote is not None,
                        "raison": "stop", "au_marche": True, "incomplet": False}
            return {"t": b["t"], "ns": None, "brut": f["stop"], "prix": f["stop"], "mesure": False, "raison": "stop",
                    "au_marche": True, "incomplet": False}
    if last is None:
        return {"t": None, "ns": None, "brut": None, "prix": None, "mesure": False, "raison": "donnees",
                "au_marche": True, "incomplet": True}
    complete = last["t"] + 60 >= close_ts
    return {"t": last["t"], "ns": None, "brut": last["c"], "prix": last["c"], "mesure": False,
            "raison": "cloture" if complete else "fin des données", "au_marche": True, "incomplet": not complete}


def sip_result(f: dict, m: dict, sens: str, slip_mult: float = 1.0) -> dict:
    """R brut (prix des transactions, sans écart ni frais) et R aux frais de Simão : 20 € de risque en actions entières
    sur le prix d'exécution, un ordre d'entrée (stop ou au marché) et un de sortie (stop ou au marché), commission IBKR
    du plan du compte. Prix relus (`mesure`) : tels quels ; sinon le glissement du labo. `slip_mult` : robustesse, un
    glissement du labo de plus sur un prix relu, le glissement multiplié sur un autre. Prix d'exécution et actions
    rendus pour la vue « portefeuille contraint »."""
    sign = 1 if sens == "long" else -1
    brut_in = f.get("brut", f["prix"])
    risk = sign * (brut_in - f["stop"])
    none = {"r_brut": None, "r_frais": None, "hors_mise": False, "actions": None, "px_entree": None, "px_sortie": None}
    if risk <= 0 or m["prix"] is None:
        return none
    brut = sign * (m.get("brut", m["prix"]) - brut_in) / risk

    def slips(px: float, measured: bool) -> float:
        return ((slip_mult - 1) if measured else slip_mult) * G.slip(px)

    entry = f["prix"] + sign * slips(f["prix"], f.get("mesure", False))
    risk_paid = sign * (entry - f["stop"])
    shares = math.floor(K.RISK_EUR * EURUSD / risk_paid + 1e-9) if risk_paid > 0 else 0
    if shares < 1:
        return none | {"r_brut": round(brut, 4), "hors_mise": True}
    exit_px = m["prix"] - sign * (slips(m["prix"], m.get("mesure", False)) if m["au_marche"] else 0.0)
    pnl = shares * sign * (exit_px - entry) - G.commission(shares, entry) - G.commission(shares, exit_px)
    return {"r_brut": round(brut, 4), "r_frais": round(pnl / (shares * risk_paid), 4), "hors_mise": False,
            "actions": shares, "px_entree": round(entry, 4), "px_sortie": round(exit_px, 4)}


def article_r(f: dict, m: dict, sens: str) -> float | None:
    """R aux frais de l'article (protocole v2, F4) : ARTICLE_COMMISSION par action et par côté, sans écart ni
    glissement."""
    sign = 1 if sens == "long" else -1
    risk = sign * (f["brut"] - f["stop"])
    if risk <= 0 or m["brut"] is None:
        return None
    return round((sign * (m["brut"] - f["brut"]) - 2 * ARTICLE_COMMISSION) / risk, 4)


def _at_market(t: int, brut: float, stop: float, sens: str, quote: list | None) -> dict:
    """Ordre au marché au début de la minute `t` : l'ask (achat) ou le bid (vente) de la cotation en vigueur, lue et
    valide (mesure), sinon `brut`, l'ouverture de la minute (le glissement du labo s'ajoute au R)."""
    w = {"t": t, "ns": None, "seq": None, "brut": brut, "prix": brut, "mesure": False, "stop": stop}
    if K._quote_ok(quote):
        w |= {"prix": quote[2] if sens == "long" else quote[1], "mesure": True}
    return w


def first_bar(bars: list[dict], t0: int) -> dict | None:
    """Première bougie d'1 min des 5 minutes qui commencent à `t0` (sans elle, la valeur n'a pas échangé : pas de
    témoin)."""
    return next((b for b in bars if t0 <= b["t"] < t0 + 300 and b["o"] > 0), None)


def sip_witness(f: dict, bars: list[dict], sens: str = "long", quote: list | None = None) -> dict | None:
    """Témoin A (et SPY) : pris au marché au début de sa première minute de bourse dans les 5 qui suivent le début de
    la minute où la stratégie entre, même stop en % du prix de la transaction de la stratégie."""
    b = first_bar(bars, f["t"])
    if not b:
        return None
    return _at_market(b["t"], b["o"], b["o"] * f["stop"] / f.get("brut", f["prix"]), sens, quote)


def sip_immediate(bar5: dict, atr: float, sens: str, bars: list[dict], day: date, mode: str,
                  quote: list | None = None) -> dict | None:
    """Témoin B (protocole v2, B2) : la même valeur prise au marché au début de sa première minute de bourse à partir de
    ACTIF_DES, dans le sens de la bougie de 9 h 30, avec la règle de stop de la ligne sur ce prix (l'autre bout du
    range seulement s'il est du bon côté, sinon pas de trade)."""
    b = first_bar(bars, _ts(day, ACTIF_DES))
    if not b:
        return None
    stop = sip_stop(bar5, atr, sens, b["o"], mode)
    if (stop >= b["o"]) if sens == "long" else (stop <= b["o"]):
        return None
    return _at_market(b["t"], b["o"], stop, sens, quote)


def sip_witnesses(day: date, version: str, symbols: list[str], pool: list[str]) -> dict[str, list[str]]:
    chosen = set(symbols)
    others = [x for x in pool if x not in chosen]
    return {s: witnesses(day, version, s, others, SIP_TEMOINS) for s in symbols}


def _row(day: date, s: str, essai: str, rang: int, sens: str, p: dict, seen) -> dict:
    return dict.fromkeys(COLUMNS) | {
        "day": day.isoformat(), "symbol": s, "essai": essai, "rang": rang, "cote": sens,
        "gap_pct": round(p["gap_pct"], 3), "rvol": round(p["rvol_z"], 2), "prix": p["bar"]["o"],
        "entry": sip_level(p["bar"], sens), "declenche": 0, "hors_mise": 0, "incomplet": 0, "univers": int(s in seen)}


def _trade(row: dict, f: dict, m: dict, sens: str) -> dict:
    """Une ligne déclenchée : entrée, sortie, R aux frais et glissement doublé."""
    res, x2 = sip_result(f, m, sens), sip_result(f, m, sens, slip_mult=2.0)
    return row | {"declenche": 1, "stop": round(f["stop"], 4), "risque": round(abs(f["brut"] - f["stop"]), 4),
                  "t_entree": f["t"], "t_sortie": m["t"], "sortie": m["raison"], "r_brut": res["r_brut"],
                  "r_frais": res["r_frais"], "r_frais_x2": x2["r_frais"], "hors_mise": int(res["hors_mise"]),
                  "incomplet": int(m["incomplet"]), "actions": res["actions"], "px_entree": res["px_entree"],
                  "px_sortie": res["px_sortie"],
                  "valeur": round(res["actions"] * res["px_entree"], 2) if res["actions"] else None}


def sip_rows(day: date, version: str, symbols: list[str], pop: dict[str, dict], draws: dict[str, list[str]],
             bars: dict[str, list[dict]], ticks: dict | None = None, q0s: dict | None = None, need: dict | None = None,
             seen: frozenset | set = frozenset()) -> list[dict]:
    """Lignes d'une version (SIP_VERSIONS) : essai sip_long ou sip_court, suffixé de la version. Achats relus en
    transactions quand `ticks` est donné ({valeur: {minute: transactions et cotations}}, protocole v2, A), avec les
    cotations en vigueur `q0s` ({(valeur, début de minute): cotation}) : ce qui manque est ajouté à `need` (« full » :
    minutes à relire, « q0 » : cotations). Ventes, et achats sans `ticks`, sur bougies d'1 min. Chaque ligne a son
    témoin A, son témoin B (toutes les valeurs, déclenchées ou non), SPY et les bornes sur bougies."""
    mode = SIP_VERSIONS[version][1]
    suffix = version.removeprefix("sip")
    spy = bars.get(MARKET) or []
    out = []
    for rang, s in enumerate(symbols, start=1):
        p = pop[s]
        sens = side(p["bar"])
        if sens is None:
            continue
        raw = bars.get(s) or []
        relu = sens == "long" and ticks is not None

        def quote(sym: str, t: int) -> list | None:
            if not relu:
                return None
            if (sym, t) not in q0s:
                need["q0"].add((sym, t))
                return None
            return q0s[(sym, t)]

        def path(sym: str, rows: list[dict]) -> list[dict]:
            return K.refine(rows, ticks[sym]) if relu and ticks.get(sym) else rows

        def read(sym: str, m: dict) -> None:  # sortie sur un stop d'une bougie : minute à relire
            if relu and m["raison"] == "stop" and m["ns"] is None:
                need["full"].add((sym, m["t"]))

        row = _row(day, s, ("sip_long" if sens == "long" else "sip_court") + suffix, rang, sens, p, seen)
        row["franchi"] = int(franchi(p["bar"], sens, raw, day))
        bf = sip_immediate(p["bar"], p["atr"], sens, raw, day, mode)
        if bf:
            bf = _at_market(bf["t"], bf["brut"], bf["stop"], sens, quote(s, bf["t"]))  # cotation lue s'il y a un trade
            bm = sip_manage(bf, sens, path(s, raw), day, debut=True)
            read(s, bm)
            row["temoin_b_r_frais"] = None if bm["incomplet"] else sip_result(bf, bm, sens)["r_frais"]
        f = sip_fill(p["bar"], p["atr"], sens, path(s, raw), day, mode)
        if f:
            if relu and f["ns"] is None:
                need["full"].add((s, f["t"]))
            m = sip_manage(f, sens, path(s, raw), day)
            read(s, m)
            row = _trade(row, f, m, sens)
            fb = sip_fill(p["bar"], p["atr"], sens, raw, day, mode)  # bornes sur bougies
            if fb:
                row |= {"r_bougies": sip_result(fb, sip_manage(fb, sens, raw, day), sens)["r_frais"],
                        "r_suivante": sip_result(fb, sip_manage(fb, sens, raw, day, same_minute=False), sens)["r_frais"]}
            if relu:
                row["releve"] = int(f["ns"] is not None and not (m["raison"] == "stop" and m["ns"] is None))
                if f["taille_ask"] is not None and row["actions"]:
                    row["carnet"] = int(f["taille_ask"] * (LOT if day < LOTS_UNTIL else 1) >= row["actions"])
            row["_ordre"] = (f["t"], f["ns"] if f["ns"] is not None else f["t"] * 10**9, rang)
            for w in draws.get(s) or []:
                wraw = bars.get(w) or []
                wb = first_bar(wraw, f["t"])
                if not wb:
                    continue
                wf = sip_witness(f, wraw, sens, quote(w, wb["t"]))
                wm = sip_manage(wf, sens, path(w, wraw), day, debut=True)
                read(w, wm)
                wr = sip_result(wf, wm, sens)
                row |= {"temoin": w, "temoin_r_brut": wr["r_brut"],
                        "temoin_r_frais": None if wm["incomplet"] else wr["r_frais"]}
                break
            sf = sip_witness(f, spy, sens)
            if sf:
                sm = sip_manage(sf, sens, spy, day, debut=True)
                row["spy_r_frais"] = None if sm["incomplet"] else sip_result(sf, sm, sens)["r_frais"]
            row["detail"] = json.dumps({"entree": f, "sortie": m, "rvol_z": p["rvol_z"], "atr": p["atr"]},
                                       ensure_ascii=False)
        out.append(row)
    return out


def top5_rows(rows: list[dict]) -> list[dict]:
    """Essai 6 (protocole v2, C5) : les TOP5 premières valeurs vertes de sip, lignes de sip_range ; les POSITIONS_MAX
    premiers achats dans l'ordre du temps (à égalité, le rang) sont pris, les ordres restants annulés au dernier achat
    (« sautée ») ; le témoin B prend les POSITIONS_MAX premiers rangs à 9 h 38."""
    longs = [r for r in rows if r["essai"] == "sip_long_range"][:TOP5]
    taken = {r["symbol"] for r in sorted((r for r in longs if r["declenche"]), key=lambda r: r["_ordre"])[:POSITIONS_MAX]}
    out = []
    for i, r in enumerate(longs, start=1):
        row = r | {"essai": "sip_long_top5", "rang": i}
        if r["declenche"] and r["symbol"] not in taken:
            row |= dict.fromkeys(SKIPPED) | {"declenche": 0, "hors_mise": 0, "incomplet": 0,
                                             "sortie": f"sautée : {POSITIONS_MAX} positions"}
        if i > POSITIONS_MAX:
            row["temoin_b_r_frais"] = None
        out.append(row)
    return out


def article_rows(day: date, symbols: list[str], pop: dict[str, dict], bars: dict[str, list[dict]],
                 seen: frozenset | set = frozenset()) -> list[dict]:
    """Réplication de l'article (protocole v2, F4, sans essai) : ses filtres, top 20, achats et ventes, ordres actifs à
    ARTICLE_DES, stop SIP_STOP_ATR de l'ATR, sortie à la clôture, sur bougies d'1 min ; R à nos frais et aux siens."""
    out = []
    for rang, s in enumerate(symbols, start=1):
        p = pop[s]
        sens = side(p["bar"])
        if sens is None:
            continue
        raw = bars.get(s) or []
        row = _row(day, s, "article", rang, sens, p, seen)
        f = sip_fill(p["bar"], p["atr"], sens, raw, day, "atr", start=ARTICLE_DES)
        if f:
            m = sip_manage(f, sens, raw, day)
            row = _trade(row, f, m, sens) | {
                "r_article": article_r(f, m, sens), "r_bougies": None,
                "r_suivante": sip_result(f, sip_manage(f, sens, raw, day, same_minute=False), sens)["r_frais"]}
            row["r_bougies"] = row["r_frais"]
        out.append(row)
    return out


def orb_rows(day: date, pop: dict[str, dict], lists: dict[str, list[str]], draws: dict[str, dict],
             bars: dict[str, list[dict]], ticks: dict | None = None, q0s: dict | None = None,
             seen: frozenset | set = frozenset()) -> tuple[list[dict], dict]:
    """Lignes de l'ORB d'une séance avec les transactions et cotations lues jusque-là, et ce qu'il reste à lire."""
    need: dict[str, set] = {"full": set(), "q0": set()}
    rows: list[dict] = []
    for version in SIP_VERSIONS:
        rows += sip_rows(day, version, lists[version], pop, draws[version], bars, ticks, q0s, need, seen)
    rows += top5_rows(rows)
    rows += article_rows(day, lists["sip_filtres"], pop, bars, seen)
    return rows, need


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


async def quote_at(symbol: str, t: int, transport=None) -> list | None:
    """Cotation NBBO SIP en vigueur à l'instant `t` : la dernière de l'heure qui précède (killmode.QUOTE_LOOKBACK),
    [ns, bid, ask, taille du bid, taille de l'ask] ; une requête Alpaca."""
    at = datetime.fromtimestamp(t, timezone.utc)
    rows = K._quote_rows(await alpaca.ticks("quotes", symbol, at - timedelta(seconds=K.QUOTE_LOOKBACK), at,
                                            sort="desc", first=1, transport=transport, call=SRC._alpaca_call))
    return rows[0] if rows and rows[0][0] <= t * 10**9 else None


async def minute_ticks(symbol: str, m: int, transport=None) -> dict:
    """Transactions (`t`), cotations NBBO (`q`) SIP de la minute `m` et la cotation en vigueur à son début (`q0`),
    comme killmode.minute_ticks : trois requêtes Alpaca (plus quand une page ne suffit pas)."""
    start, end = (datetime.fromtimestamp(x, timezone.utc) for x in (m, m + 60))
    lim = (m + 60) * 10**9
    trades = await alpaca.ticks("trades", symbol, start, end, transport=transport, call=SRC._alpaca_call)
    quotes = await alpaca.ticks("quotes", symbol, start, end, transport=transport, call=SRC._alpaca_call)
    return {"t": [r for r in K._trade_rows(trades) if r[0] < lim], "q": [r for r in K._quote_rows(quotes) if r[0] < lim],
            "q0": await quote_at(symbol, m, transport)}


async def read_ticks(day: date, pop: dict[str, dict], lists: dict, draws: dict, bars: dict[str, list[dict]],
                     seen: frozenset | set = frozenset(), transport=None) -> tuple[list[dict], dict]:
    """Lignes de l'ORB, minutes qui décident relues par passes (TICK_ROUNDS au plus, TICK_MINUTES_DAY minutes au plus) :
    une minute lue peut déplacer l'achat ou le stop et en désigner une autre. Une panne passagère d'Alpaca remonte (la
    séance est reportée) ; une minute refusée pour de bon reste non lue, jouée sur sa bougie, et comptée."""
    ticks: dict[str, dict] = {}
    q0s: dict[tuple[str, int], list | None] = {}
    failed: set[tuple[str, int]] = set()
    reads = quotes_read = 0
    for rnd in range(TICK_ROUNDS + 1):
        rows, need = orb_rows(day, pop, lists, draws, bars, ticks, q0s, seen)
        full = sorted(k for k in need["full"] - failed if str(k[1]) not in ticks.get(k[0], {}))
        quotes = sorted(k for k in need["q0"] if k not in q0s)
        if rnd == TICK_ROUNDS or not (full or quotes) or (reads >= TICK_MINUTES_DAY and not quotes):
            break
        for s, m in full[:max(0, TICK_MINUTES_DAY - reads)]:
            heartbeat.beat()
            try:
                data = await minute_ticks(s, m, transport)
            except (SourceError, KeyError, TypeError, ValueError) as exc:
                if isinstance(exc, SourceError) and SRC.retryable(exc):
                    raise
                failed.add((s, m))
                log.info("Labo : n° 12, transactions de %s à %s illisibles : %s", s, m, exc)
                continue
            ticks.setdefault(s, {})[str(m)] = data
            q0s.setdefault((s, m), data["q0"])
            reads += 1
        for s, m in quotes:
            if (s, m) in q0s:
                continue
            try:
                q0s[(s, m)] = await quote_at(s, m, transport)
            except (SourceError, KeyError, TypeError, ValueError) as exc:
                if isinstance(exc, SourceError) and SRC.retryable(exc):
                    raise
                q0s[(s, m)] = None
            quotes_read += 1
    return rows, {"minutes_relues": reads, "cotations_relues": quotes_read, "minutes_refusees": len(failed),
                  "a_relire": len(full), "plafond_releve": bool(full) and reads >= TICK_MINUTES_DAY}


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
    seen = {s for (s,) in conn.execute("SELECT symbol FROM universe WHERE market = 'stock' AND day = ?",
                                       (day.isoformat(),))}
    nxt = next_trading_day(day)
    both = sorted({MARKET, *(p["symbol"] for p in plans), *(w for p in plans for w in p["temoins"])}) if plans else []
    today = sorted({MARKET, *(s for v in lists.values() for s in v),
                    *(w for d in draws.values() for ws in d.values() for w in ws)} - set(both))
    bars = await minute_bars(today, day, day, transport) if today else {}
    if both:
        bars |= await minute_bars(both, day, nxt, transport)
    rows, counts = await read_ticks(day, pop, lists, draws, bars, seen, transport)
    for p in plans:
        rows += gemini_rows(p, bars, nxt, pop, seen)
    return rows, stats | counts


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
        log.info("Labo : n° 12 %s, %d plans A, %d lignes, %d minutes relues", day, len(stats["plans"]), len(rows),
                 stats.get("minutes_relues", 0))
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


def _halves_t(rows: list[tuple[str, float]]) -> list[dict]:
    """Les deux moitiés chronologiques des séances (⌊J/2⌋ premières, puis le reste), chacune avec son t : la variante
    de Gemini (aucune moitié sous t = −1), publiée sans décider."""
    days = sorted({d for d, _ in rows})
    if len(days) < 2:
        return []
    first = set(days[:len(days) // 2])
    return [_mean([(d, x) for d, x in rows if (d in first) == part]) for part in (True, False)]


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


def _quantile(xs: list[float], q: float) -> float | None:
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 2)


def _share(flags: list) -> float | None:
    xs = [x for x in flags if x is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


def _ecart_b(sel: list[dict], ok: list[dict]) -> dict:
    """Écart au témoin B (protocole v2, B2), par séance : R moyen des achats de la ligne moins R moyen de B sur ses
    valeurs (déclenchées ou non) ; chaque séance compte une fois."""
    mine, other = defaultdict(list), defaultdict(list)
    for r in ok:
        mine[r["day"]].append(r["r_frais"])
    for r in sel:
        if r.get("temoin_b_r_frais") is not None:
            other[r["day"]].append(r["temoin_b_r_frais"])
    return _mean([(d, sum(mine[d]) / len(mine[d]) - sum(other[d]) / len(other[d])) for d in sorted(mine) if d in other])


def item(rows: list[dict], trials: int, essai: str, capital_usd: float | None = None) -> dict:
    """Bilan d'une ligne : r_frais des trades déclenchés, complets, à la taille de Simão, mesures publiées sans essai
    (protocole v2, F) et la règle « candidate » du labo pour les essais (D3)."""
    sel = [r for r in rows if r["essai"] == essai]
    fired = [r for r in sel if r["declenche"]]
    ok = [r for r in fired if r["r_frais"] is not None and not r["incomplet"]]
    pairs = [(r["day"], r["r_frais"]) for r in ok]
    main = _mean(pairs)
    gaps = [(r["day"], r["r_frais"] - r["temoin_r_frais"]) for r in ok if r["temoin_r_frais"] is not None]
    ecart = _mean(gaps)
    h1, h2 = _halves(pairs)
    x2 = _mean([(r["day"], r["r_frais_x2"]) for r in ok if r["r_frais_x2"] is not None])["esperance_r"]
    best = set(sorted(range(len(ok)), key=lambda i: (-ok[i]["r_frais"], i))[:2])
    values = [r["valeur"] for r in ok if r["valeur"]]
    out = {"nom": LABELS.get(essai, essai), "lignes": len(sel), "declenches": len(fired), "mesures": len(ok),
           "hors_mise": sum(r["hors_mise"] for r in fired), "incomplets": sum(r["incomplet"] for r in fired),
           "r_frais": main, "brut": _mean([(r["day"], r["r_brut"]) for r in fired if r["r_brut"] is not None]),
           "temoin": _mean([(r["day"], r["temoin_r_frais"]) for r in ok if r["temoin_r_frais"] is not None]),
           "ecart_temoin": ecart, "moitie_1": h1, "moitie_2": h2, "glisse_x2": x2,
           "reussite": round(sum(r["r_frais"] > 0 for r in ok) / len(ok), 4) if ok else None,
           "valeur_mediane": _median(values), "valeur_p90": _quantile(values, 0.9),
           "sans_2_meilleurs": _mean([x for i, x in enumerate(pairs) if i not in best]),
           "moities_t": _halves_t(pairs), "moities_ecart": list(_halves(gaps)),
           "par_univers": {"vue à 9 h 10": _mean([(r["day"], r["r_frais"]) for r in ok if r.get("univers") == 1]),
                           "manquée à 9 h 10": _mean([(r["day"], r["r_frais"]) for r in ok if r.get("univers") == 0])}}
    if capital_usd:
        out["au_dela_du_capital"] = {f"{k} ×": round(sum(v > k * capital_usd for v in values) / len(values), 4)
                                     if values else None for k in (1, 2, 4)}
    out["par_annee"] = {y: _mean([(r["day"], r["r_frais"]) for r in ok if r["day"][:4] == y])
                        for y in sorted({r["day"][:4] for r in ok})}
    out["par_prix"] = {label: _mean([(r["day"], r["r_frais"]) for r in ok if r["prix"] is not None
                                     and lo <= r["prix"] < hi]) for lo, hi, label in PRICE_BUCKETS}
    out["par_gap"] = {label: _mean([(r["day"], r["r_frais"]) for r in ok if r["gap_pct"] is not None
                                    and lo <= r["gap_pct"] < hi]) for lo, hi, label in GAP_BUCKETS}
    if essai.startswith("sip") or essai == "article":
        out["stop_minute_suivante"] = _mean([(r["day"], r["r_suivante"]) for r in ok if r["r_suivante"] is not None])
        out["par_stop"] = {label: _mean([(r["day"], r["r_frais"]) for r in ok if r["risque"] is not None
                                         and lo <= r["risque"] < hi]) for lo, hi, label in STOP_BUCKETS}
        out["par_rvol"] = {label: _mean([(r["day"], r["r_frais"]) for r in ok if lo <= r["rvol"] < hi])
                           for lo, hi, label in RVOL_BUCKETS}
    if essai.startswith("sip"):
        out |= {"bougies": _mean([(r["day"], r["r_bougies"]) for r in ok if r.get("r_bougies") is not None]),
                "ecart_b": _ecart_b(sel, ok),
                "ecart_spy": _mean([(r["day"], r["r_frais"] - r["spy_r_frais"]) for r in ok
                                    if r.get("spy_r_frais") is not None]),
                "par_franchi": {"franchi avant 9 h 38": _mean([(r["day"], r["r_frais"]) for r in ok
                                                              if r.get("franchi") == 1]),
                                "pas franchi": _mean([(r["day"], r["r_frais"]) for r in ok if r.get("franchi") == 0])},
                "relus": _share([r.get("releve") for r in fired]),
                "au_dela_de_l_ask": _share([None if r.get("carnet") is None else 1 - r["carnet"] for r in fired])}
    if essai == "article":
        out["r_article"] = _mean([(r["day"], r["r_article"]) for r in fired
                                  if r.get("r_article") is not None and not r["incomplet"]])
    if essai in TRIALS:
        seuil = SS.candidate_t(ecart["grappes"], trials) if ecart["n"] else None
        ci = main["ic95"]
        out |= {"essai": True, "seuil_t": seuil,
                "effet_detectable": round((seuil + POWER_Z) * ecart["se"], 4)
                if seuil is not None and ecart["se"] is not None else None,
                "tient": bool(ci and ci[0] > 0 and SS.passes(ecart["z"], seuil) and len(ok) >= N12_MIN_TRADES
                              and main["jours"] >= N12_MIN_SEANCES
                              and all(x is not None and x > 0 for x in (h1, h2, x2)))}
    return out


def capital() -> tuple[float, bool]:
    """Capital de Simão en euros (JARVIS_CAPITAL) et s'il est supposé (CAPITAL_DEFAUT quand il manque)."""
    c = env_float("JARVIS_CAPITAL", 0.0)
    return (c, False) if c > 0 else (CAPITAL_DEFAUT, True)


def portfolio(rows: list[dict], essai: str, capital_eur: float) -> dict:
    """Portefeuille contraint (protocole v2, F5) : les POSITIONS_MAX premiers achats de chaque séance (heure d'entrée,
    puis rang), valeur de chaque position plafonnée au tiers du capital (au comptant) ou aux 4/3 (marge intraday 4×),
    mêmes prix d'exécution ; en R de portefeuille (1 R = 20 €) par trade et par séance, et en euros au total."""
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if (r["essai"] == essai and r["declenche"] and r["r_frais"] is not None and not r["incomplet"]
                and r.get("actions") and r.get("px_entree") and r.get("px_sortie") is not None):
            by_day[r["day"]].append(r)
    one_r = K.RISK_EUR * EURUSD
    out = {"capital_eur": capital_eur}
    for name, cap in (("comptant", capital_eur * EURUSD / POSITIONS_MAX),
                      ("marge_4x", 4 * capital_eur * EURUSD / POSITIONS_MAX)):
        per_trade, per_day, small = [], [], 0
        for d in sorted(by_day):
            total = 0.0
            for r in sorted(by_day[d], key=lambda r: (r["t_entree"], r["rang"]))[:POSITIONS_MAX]:
                n = min(r["actions"], math.floor(cap / r["px_entree"] + 1e-9))
                if n < 1:
                    small += 1
                    continue
                sign = 1 if r["cote"] == "long" else -1
                pnl = (n * sign * (r["px_sortie"] - r["px_entree"]) - G.commission(n, r["px_entree"])
                       - G.commission(n, r["px_sortie"]))
                per_trade.append((d, pnl / one_r))
                total += pnl / one_r
            per_day.append((d, total))
        out[name] = {"plafond_usd": round(cap, 2), "par_trade": _mean(per_trade), "par_seance": _mean(per_day),
                     "euros": round(sum(x for _, x in per_day) * K.RISK_EUR, 2), "trop_cheres": small}
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
    years: dict[str, list[float]] = defaultdict(list)
    for d in days:
        years[d[:4]].append(prog[d].get("cote") or 0)
    cap, assumed = capital()
    return {"numero": NUMERO, "mesure": MESURE, "seances": len(days), "du": days[0], "au": days[-1],
            "bougies_9h30_lues": seen, "essais": len(TRIALS), "essais_labo": trials, "scelle": SEAL.describe(),
            "plans_a": sum(len(s.get("plans") or []) for s in stats),
            "regles_gemini": sorted({s.get("regles_gemini") for s in stats if s.get("regles_gemini") is not None}),
            "population_a": round(sum(s.get("population") or 0 for s in stats) / len(stats), 1),
            "cote": round(sum(s.get("cote") or 0 for s in stats) / len(stats), 1),
            "cote_par_annee": {y: round(sum(v) / len(v), 1) for y, v in sorted(years.items())},
            "relecture": {"minutes": sum(s.get("minutes_relues") or 0 for s in stats),
                          "cotations": sum(s.get("cotations_relues") or 0 for s in stats),
                          "refusees": sum(s.get("minutes_refusees") or 0 for s in stats),
                          "seances_au_plafond": sum(bool(s.get("plafond_releve")) for s in stats)},
            "capital": {"euros": cap, "suppose": assumed},
            "lignes": {e: item(rows, trials, e, cap * EURUSD) for e in LABELS},
            "portefeuille": {e: portfolio(rows, e, cap) for e in ORB_TRIALS}}


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


def _eur(x: float) -> str:
    return f"{x:+,.0f}".replace(",", " ").replace("-", "−")


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
        line = (f"- {it['nom']} : {_r(m['esperance_r'])} R à 20 € [{_r(ic[0])} ; {_r(ic[1])}] · "
                f"{it['mesures']} trades sur {m['jours']} jours · brut {_r(it['brut']['esperance_r'])} · écart au "
                f"témoin {_r(e['esperance_r'])} (t {SS._z(e['z'])}"
                + (f", seuil {SS._z(it['seuil_t'])}" if it.get("essai") else "") + ")")
        b = it.get("ecart_b") or {}
        if b.get("n"):
            line += f" · contre l'achat à 9 h 38 {_r(b['esperance_r'])}"
        if it.get("effet_detectable") is not None:
            line += f" · effet détectable {_r(it['effet_detectable'])}"
        if (it.get("r_article") or {}).get("n"):
            line += f" · aux frais de l'article {_r(it['r_article']['esperance_r'])}"
        if it.get("essai") and it["tient"]:
            line += " ✅ tient : candidate au bloc scellé"
        lines.append(line)
    top5 = ((rep.get("portefeuille") or {}).get("sip_long_top5") or {}).get("comptant") or {}
    if (top5.get("par_seance") or {}).get("n"):
        cap, k = rep.get("capital") or {}, top5["par_seance"]["n"]
        lines.append(f"- Top 5 avec {cap.get('euros', CAPITAL_DEFAUT):,.0f} € ".replace(",", " ")
                     + ("(capital supposé) " if cap.get("suppose") else "")
                     + f"au comptant : {_eur(top5['euros'])} € sur {k} séance{'s' if k > 1 else ''} "
                     f"({_r(top5['par_seance']['esperance_r'])} R par séance)")
    autres = [v for v in rep.get("regles_gemini") or [] if v != REGLES_GEMINI]
    if autres:
        lines.append(f"⚠️ Passe A rejouée en partie sous d'autres règles du direct ({', '.join(f'v{v}' for v in autres)}) : "
                     f"la n° 12 déclare la v{REGLES_GEMINI}, une autre version demande une nouvelle déclaration.")
    lines.append("-# Rejeu hors bloc scellé, aux frais de ton compte ; achats de l'ORB relus en transactions, ordres "
                 "actifs à 9 h 38. Passe A sans catalyseur ni nombre d'actions (pas de données datées). Une ligne qui "
                 "tient ne devient une méthode qu'après le bloc scellé, ouvert une fois par toi.")
    return "\n".join(lines)
