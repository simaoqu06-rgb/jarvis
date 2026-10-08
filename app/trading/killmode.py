"""JARVIS Killmode : trades à haute volatilité, uniquement sur des setups complets (demande de Simão, 8 octobre 2026).

Une version parallèle de JARVIS : rien de JARVIS n'est retiré, Killmode n'entre dans aucune routine (ni
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
mesure (la règle de prix seule, sans catalyseur, sur l'univers du labo). Le journal data/killmode_calls.json
note chaque 🔪 KILL proposé et un « témoin catalyseur » (tout réuni sauf le catalyseur, même règle de prix, même
solde ; « propre » quand il ne manque qu'une actu positive), avec la version (règle, seuils, empreinte du code du
catalyseur, version du direct), les actus comptées, le contexte (veille, volume, source, secteur) et les codes
du catalyseur. `!kill bilan` (permis Killmode éteint) solde chaque appel après la clôture sur les bougies SIP
d'Alpaca non ajustées d'1 min de toute la séance (gardées pour un re-solde) : ordre stop d'achat actif une minute
pleine après l'appel, un tiers par objectif, stop à l'entrée après TP1, soldé à la clôture ; une minute dont
l'ordre des prix est inconnu est jouée dans le pire ordre (le meilleur est affiché). Le chiffre qui décide est
le R aux frais réels de Simão (20 € de risque, un bracket IBKR par objectif en actions entières, minimum par
ordre, glissement d'au moins un demi-pas).

Règle de lecture, écrite avant le premier appel et revue le 8 octobre avant tout KILL soldé (avis de GPT et
Gemini, council) : au 30e KILL déclenché, un audit (borne haute de l'IC 95 % sous 0 → JARVIS recommande
d'éteindre, sinon « pas d'alerte », jamais une validation) ; au 100e, la lecture finale (avantage démontré
seulement si l'IC tient par jour, par semaine et avec 3 c de glissement par côté) ; une borne de dégât (IC
99,9 %) dès 10 KILL. Simão décide toujours. Le journal ne voit qu'une catastrophe ou un gros écart ; la valeur
prédictive du filtre catalyseur à règle de prix égale (écart au témoin, pas l'effet causal d'une actu) se mesure
au labo.
"""
import asyncio
import gzip
import hashlib
import inspect
import json
import logging
import math
import os
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import NormalDist
from zoneinfo import ZoneInfo

from app.core.http import SourceError
from app.lab import collector as CO
from app.lab import potentiel as PO
from app.lab import simulator as SIM
from app.lab import sources as LSRC
from app.lab import stats as LS
from app.lab import strat_stats as SS
from app.lab import strategies as ST
from app.sources import alpaca, finnhub, frankfurter
from app.trading import catalyst as C
from app.trading import scanner as SC
from app.trading import service as T
from app.trading.settings import settings

log = logging.getLogger(__name__)
NEW_YORK = ZoneInfo("America/New_York")
STATE_PATH = os.getenv("JARVIS_KILLMODE_PATH", "data/killmode.json")
CALLS_PATH = os.getenv("JARVIS_KILLMODE_CALLS_PATH", "data/killmode_calls.json")
SCANS_PATH = os.getenv("JARVIS_KILLMODE_SCANS_PATH", "data/killmode_scans.json")
SCANS_KEPT = 1000
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
# Version de l'analyse en direct (2, 8 octobre : pas de seconde entrée quand le trade du labo est fini, bougies
# closes jugées sur la fin réelle des données, frais réels). Le bilan ne mélange pas les appels de deux versions ;
# la définition du catalyseur (code de catalyst.py, partagé avec JARVIS) y entre par son empreinte : le moindre
# changement ouvre une version. Le solde a sa propre méthode (SETTLE_METHOD) : il est refait sur tout le journal.
DIRECT_VERSION = 2
GROUPS = {"kill": "🔪 KILL", "temoin_catalyseur": "Témoin catalyseur (tout réuni sauf le catalyseur)"}
# Raisons qui font tomber le pilier catalyseur (codes notés dans le journal). Le « témoin propre » n'a que
# « pas_de_positive » : ni actu positive ni veto, la seule population que Killmode aurait pu trader.
CAT_CODES = {"finnhub_muet": "Finnhub muet", "dilution": "augmentation de capital",
             "resultats_non_verifies": "date des résultats non vérifiée", "resultats_24h": "résultats dans 24 h",
             "negatives": "actus négatives", "pas_de_positive": "aucune actu positive"}
# Mots qui décrivent le mouvement du cours plutôt qu'une cause : un titre positif seulement par eux
# (« Acme shares jump 25% ») est noté « mouvement » ; il porte le pilier, et le bilan le mesure à part.
MOVE_WORDS = [r"\bsurg", r"\bsoar", r"\bjump", r"rocket", r"\brall(y|ies)", r"\bboom", r"record high",
              r"hits? (a )?record"]
READ_AT = 30           # règle de lecture du journal (écrite avant le premier appel, revue le 8 octobre)
READ_MAX = 100
HARM_MIN = 10          # borne de dégât : regardée à chaque bilan dès 10 KILL déclenchés, IC 99,9 %
MIN_WEEKS = 8          # un verdict positif à 100 KILL demande au moins 8 semaines distinctes
UNSETTLED_DAYS = 7     # appel sans bougies 7 jours après sa séance : insoldable, compté à part
STRESS_CENTS = 0.03    # stress de Gemini : 3 cents d'écart à l'entrée et à chaque sortie au marché
STRESS_SUB_DOLLAR = 0.01  # sous 1 $, 3 cents n'ont pas de sens (pas de 0,0001 $) : 1 % du prix
SPLIT_GAP_PCT = 50.0   # gap de +50 % ou plus : clôture de la veille vérifiée chez Finnhub (un regroupement
                       # d'actions pas encore ajusté donne au moins +100 %)
SPLIT_TOLERANCE = 0.2
SLIP_ALERT_R = 0.25    # risque réel : sortie à 0,25 R ou plus sous le stop actif
LATE_GAP = 30 * 60     # solde : trade ouvert à la clôture sans bougie dans la dernière demi-heure = incomplet
# Méthode du solde, datée dans docs/labo-strategies.md ; un changement re-solde tout le journal depuis les bougies
# gardées (KILL et témoins restent comparables). 1 (PR #41) : minutes jusqu'à la fin de la bougie de 5 min de
# l'appel, puis 5 min. 2 (8 octobre, avant tout solde, second avis de GPT et Gemini) : toute la séance en bougies
# d'1 min, ordre actif LATENCY après l'appel, bougie ambiguë jouée dans le pire ordre (le meilleur est affiché).
SETTLE_METHOD = 2
STEP = 60              # bougies du solde : 1 min
LATENCY = 60           # appel lu et ordre saisi chez IBKR : l'ordre est actif à la 1re minute pleine après 60 s
BARS_DIR = os.getenv("JARVIS_KILLMODE_BARS_PATH", "data/killmode_bars")  # bougies d'1 min de chaque solde
# Frais réels de Simão (le chiffre qui décide) : 20 € de risque par trade, un bracket IBKR par objectif (une
# entrée et une sortie chacun, donc deux minimums de commission), plan Fixe sauf JARVIS_IBKR_PLAN=degressif ;
# glissement du labo, jamais moins d'un demi-pas par côté.
RISK_EUR = float(os.getenv("JARVIS_RISK_EUR") or 20.0)
IBKR_PLAN = (os.getenv("JARVIS_IBKR_PLAN") or "fixe").strip().lower()
IBKR_PLANS = {"fixe": {"per_share": 0.005, "min": 1.0, "max_pct": 0.01, "extra": 0.0},
              "degressif": {"per_share": 0.0035, "min": 0.35, "max_pct": 0.01, "extra": 0.0032}}
EURUSD_DEFAULT = 1.15  # change si la BCE (Frankfurter) ne répond pas au bilan
VOL15_DAYS = 20
SC_MIN_SESSIONS = 51   # scanner.features (MM50, RSI) : la fiche société seulement
LAB_SCOPE = "règle de prix seule, sans catalyseur, sur l'univers du labo (~300 actions choisies la veille)"
SCORE_NOTE = ("non prédictive au labo, mesurée sans sa part catalyseur (rejeu oct.-déc. 2024 : notes 5 à 9 "
              "entre −0,16 et −0,25 R)")


def catalyst_fingerprint() -> str:
    """Empreinte de la définition du catalyseur dans catalyst.py (code de tone, _about et check, listes de mots)
    et ici (mots de mouvement) : notée dans les seuils, elle sépare d'elle-même les appels de deux définitions."""
    parts = []
    for fn in (C.tone, C._about, C.check, C._has):
        try:
            parts.append(inspect.getsource(fn))
        except (OSError, TypeError):  # sources absentes : le code compilé
            parts.append(fn.__code__.co_code.hex())
    parts.append(repr([C.POSITIVE, C.NEGATIVE, C.DILUTION, C.DILUTION_WEAK, C.NOT_DILUTION, MOVE_WORDS]))
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()[:10]


CATALYST_CODE = catalyst_fingerprint()  # à l'import : le code tel qu'il tourne


def seuils() -> dict:
    """Paramètres de la règle, du catalyseur, du direct et des frais réels, notés avec chaque appel."""
    return {"gap": ST.KILL_GAP_PCT, "atr": ST.KILL_ATR_PCT, "range_min": ST.KILL_RANGE_MIN,
            "jusqu_a": ST.KILL_UNTIL.strftime("%H:%M"), "poursuite_r": CHASE_R, "catalyseur_h": CATALYST_HOURS,
            "resultats_h": EARNINGS_BLOCK_HOURS, "resultats_battus_j": FRESH_EARNINGS_DAYS,
            "catalyseur_code": CATALYST_CODE, "objectifs": list(ST.TARGETS_R), "direct": DIRECT_VERSION,
            "frais_reels": f"{RISK_EUR:g} EUR, brackets IBKR, demi-pas"}


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

def live_day(bars5: list[dict], daily: list[dict], now: datetime, data_end: datetime | None = None) -> ST.Day | None:
    """Le jour de Bourse de `now` tel que le labo le voit : bougies de 5 min closes (échauffement + séance)
    et journalier connu la veille. None si la Bourse est fermée ce jour-là. `data_end` : fin réelle des
    données (Alpaca : maintenant − 16 min) ; une bougie qui la chevauche est en cours, pas close."""
    today = now.astimezone(NEW_YORK).date()
    bounds = CO.session_ts(today)
    if not bounds:
        return None
    cutoff = (data_end or now).timestamp()
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


def fresh_news(cat: dict | None, now: datetime) -> list[dict]:
    """Actus de moins de CATALYST_HOURS déjà publiées à `now` (jamais une actu datée d'après l'analyse), de la
    plus récente à la plus ancienne."""
    out = []
    for n in (cat or {}).get("news") or []:
        if not n.get("published_at"):
            continue
        age = now - datetime.fromisoformat(n["published_at"])
        if timedelta(0) <= age <= timedelta(hours=CATALYST_HOURS):
            out.append(n)
    return sorted(out, key=lambda n: n["published_at"], reverse=True)


def motif(title: str) -> str:
    """« mouvement » si le titre n'est positif que par un mot qui décrit le cours (jump, soar…), sinon « fond »."""
    text_ = (title or "").lower()
    substantive = [p for p in C.POSITIVE if p not in MOVE_WORDS]
    return "fond" if C._has(substantive, text_) else "mouvement"


def _earnings_beat(cat: dict) -> bool:
    last = cat.get("last_earnings") or {}
    return (last.get("days_ago") is not None and last["days_ago"] <= FRESH_EARNINGS_DAYS
            and (last.get("eps_surprise_pct") or 0) > 0)


def catalyst_codes(cat: dict | None, now: datetime) -> list[str]:
    """Codes des raisons qui font tomber le pilier catalyseur (CAT_CODES) ; [] s'il est réuni."""
    if not cat or not cat.get("available"):
        return ["finnhub_muet"]
    fresh = fresh_news(cat, now)
    pos = sum(n["tone"] == "positive" for n in fresh)
    neg = sum(n["tone"] == "negative" for n in fresh)
    hours = cat.get("hours_to_earnings")
    codes = []
    if cat.get("dilution"):
        codes.append("dilution")
    if not cat.get("earnings_known"):
        codes.append("resultats_non_verifies")
    if hours is not None and hours <= EARNINGS_BLOCK_HOURS:
        codes.append("resultats_24h")
    if neg > pos:
        codes.append("negatives")
    if not pos and not _earnings_beat(cat):
        codes.append("pas_de_positive")
    return codes


def catalyst_pillar(cat: dict | None, now: datetime) -> tuple[bool, list[str]]:
    """Pilier catalyseur : (réuni, raisons) ; les raisons disent ce qui le porte ou ce qui manque."""
    codes = catalyst_codes(cat, now)
    if codes == ["finnhub_muet"]:
        return False, ["Finnhub n'a pas répondu : catalyseur non vérifiable"]
    fresh = fresh_news(cat, now)
    pos = sum(n["tone"] == "positive" for n in fresh)
    neg = sum(n["tone"] == "negative" for n in fresh)
    if codes:
        hours = cat.get("hours_to_earnings") or 0
        reasons = {"dilution": "augmentation de capital dans les actus : nouvelles actions à vendre",
                   "resultats_non_verifies": "date des résultats non vérifiée",
                   "resultats_24h": f"résultats dans {hours:.0f} h : pari binaire, pas un setup",
                   "negatives": f"plus d'actus négatives ({neg}) que positives ({pos}) en 48 h",
                   "pas_de_positive": "aucune actu positive de la société en 48 h ni résultats battus hier ou "
                                      "aujourd'hui"}
        return False, [reasons[c] for c in codes]
    why = []
    if _earnings_beat(cat):
        last = cat["last_earnings"]
        why.append(f"résultats du {last['date']} au-dessus des attentes (BPA "
                   f"{last['eps_surprise_pct']:+.1f} %)".replace(".", ","))
    if pos:
        why.append(f"{pos} actu{'s' if pos > 1 else ''} positive{'s' if pos > 1 else ''} en 48 h")
    return True, why


LAB_EXITS = {"stop": "stop touché", "stop_be": "TP1 puis retour à l'entrée", "tp3": "TP3 atteint"}


def lab_trade_done(d: ST.Day, cand: dict) -> str | None:
    """Le trade du labo de cette séance (ordre stop d'achat posé à 9 h 45, une seule entrée par séance), rejoué
    sur les bougies closes : sa sortie s'il est déjà terminé, sinon None."""
    plan = ST._plan(cand["entry"], cand["stop"], ST._r_targets(cand["entry"], cand["stop"]))
    res = ST._sim(d, plan, cand["decided_at"], cand["entry_until"], d.close_ts, SIM.costs("stock", cand["entry"]),
                  "stop", d.c[cand["i"]])
    return LAB_EXITS.get(res["exit_reason"]) if res["triggered"] else None


def chart_pillar(d: ST.Day | None, now: datetime, price: float | None) -> dict:
    """Pilier graphique : la règle du labo (killmode_check) sur les bougies closes, plus l'état de l'ordre.
    status : avant (range pas encore fini), ok, parti, fini (le trade du labo est déjà sorti), expire, non,
    ferme. Le gap et l'ATR sont jugés par le pilier volatilité (avant l'ouverture, sur le gap de
    pré-ouverture)."""
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
    done = lab_trade_done(d, cand)
    if not done and k is not None and price is not None and price <= cand["stop"]:
        done = "prix sous le stop depuis la cassure"  # bougies en retard (Alpaca) : le prix du moment le dit
    if done:
        return {"status": "fini", "ok": False, "plan": plan,
                "raisons": [f"le trade du jour est déjà terminé ({done}) : pas de seconde entrée, comme au labo"]}
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


def price_now(snap: dict, bars5: list[dict] | None, src: str | None) -> tuple[float | None, int | None]:
    """Prix de l'appel et son heure : le dernier échange IEX, sauf si la dernière bougie temps réel (Twelve Data)
    a commencé après lui (IEX ne voit qu'une petite part des échanges) ; à défaut, la dernière bougie."""
    price = snap.get("price")
    at = int(datetime.fromisoformat(snap["as_of"]).timestamp()) if snap.get("as_of") else None
    last = bars5[-1] if bars5 else None
    if last and (price is None or (src == "twelvedata" and at is not None and at < last["t"])):
        return last["c"], last["t"]
    return price, at


def adv(before: list[dict]) -> float | None:
    """Volume journalier moyen (SIP) des VOL15_DAYS séances d'avant ; None si l'historique est trop court."""
    if len(before) < VOL15_DAYS:
        return None
    avg = sum(c.get("v") or 0 for c in before[-VOL15_DAYS:]) / VOL15_DAYS
    return avg or None


async def split_check(symbol: str, prev: float) -> str | None:
    """Gap de +50 % ou plus (SPLIT_GAP_PCT) : la clôture SIP de la veille doit coller à celle de Finnhub (un
    regroupement d'actions pas encore ajusté donne un faux gap). Le problème en clair, ou None si tout colle."""
    try:
        q = (await finnhub.stock_quotes([symbol]))[0]
    except SourceError as exc:
        return f"gap de +{SPLIT_GAP_PCT:g} % ou plus et clôture de la veille non vérifiable ({exc})"
    pc = q.get("previous_close")
    if not pc:
        return f"gap de +{SPLIT_GAP_PCT:g} % ou plus et clôture de la veille non vérifiable (Finnhub muet)"
    if abs(prev / pc - 1) > SPLIT_TOLERANCE:
        return (f"clôture de la veille {prev:g} (Alpaca) contre {pc:g} (Finnhub) : regroupement d'actions "
                "possible, gap faux").replace(".", ",")
    return None


async def analyze(symbol: str, now: datetime | None = None, source: str | None = None) -> dict:
    """Analyse Killmode complète d'une action US (données, société, catalyseur, graphique, verdict).

    `source` : « alpaca » pour le scan (gratuit, 16 min de retard) ; par défaut Twelve Data en temps réel
    pour les bougies de 5 min, Alpaca en secours. Les bougies en cache ne servent que si elles ont été lues
    pendant la bougie en cours (sinon la dernière bougie close pourrait être prise dans son état partiel)."""
    symbol = symbol.strip().upper()
    now = now or datetime.now(timezone.utc)
    if not alpaca.configured():
        raise KillError("clés Alpaca absentes (ALPACA_API_KEY, ALPACA_SECRET_KEY) : Killmode en a besoin")
    today = now.astimezone(NEW_YORK).date().isoformat()
    errors: list[str] = []
    fresh = now.timestamp() - int(now.timestamp()) // BAR * BAR
    snaps, daily_map, bars5, cat, profile = await asyncio.gather(
        _safe(alpaca.snapshots([symbol]), "cours", errors),
        _safe(alpaca.bars([symbol], 1440, days=DAILY_DAYS, now=now), "journalier", errors),
        _safe(T.candles(symbol, "stock", CO.BASE_TF, source, max_age=fresh), "bougies de 5 min", errors),
        _safe(C.check(symbol, now), "catalyseur", errors),
        _safe(finnhub.profile(symbol), "profil", errors),
    )
    snap = (snaps or {}).get(symbol) or {}
    daily = (daily_map or {}).get(symbol) or []
    before = sessions_before(daily, now.astimezone(NEW_YORK).date())
    prev = before[-1]["c"] if before else None  # clôture SIP de la veille, comme le labo (pas celle d'IEX)
    src = candle_source(source, bars5, now)
    price, price_at = price_now(snap, bars5, src)
    feats = SC.features(daily, price, prev, today) if daily and price else None  # fiche société (51 séances)
    if price is None:
        errors.append("cours : aucun prix")
    if daily_map is not None and len(before) <= ST.ATR_LEN:
        errors.append(f"journalier : moins de {ST.ATR_LEN + 1} séances")
    if cat:
        errors += [f"Finnhub {e}" for e in cat.get("errors") or []]
    # Alpaca : données arrêtées à maintenant − 16 min, la bougie qui chevauche cette limite n'est pas close.
    data_end = now - alpaca.SIP_DELAY if src and src.startswith("alpaca") else None
    d = live_day(bars5, daily, now, data_end) if bars5 and daily else None
    ctx = ST.kill_context(d) if d else {"gap_pct": None, "atr_pct": daily_atr_pct(before)}
    # Gap du labo (ouverture de la bougie de 9 h 30) ; sinon l'ouverture du jour (bougie journalière SIP ou
    # IEX, bougies de 5 min en retard) ; avant 9 h 30, le gap de pré-ouverture.
    gap, gap_kind = ctx["gap_pct"], "d'ouverture"
    if gap is None:
        gap = open_gap(daily, snap, prev, now)
    if gap is None and price and prev:
        gap, gap_kind = (price / prev - 1) * 100, "du moment"
    if gap is not None and gap >= SPLIT_GAP_PCT and prev:
        problem = await split_check(symbol, prev)
        if problem:
            errors.append(f"journalier : {problem}")
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
    out = {"symbol": symbol, "at": now.isoformat(timespec="seconds"), "price": price, "price_at": price_at,
           "gap_pct": gap, "gap_kind": gap_kind, "prev_close": prev, "adv20": adv(before), "atr_pct": atr_pct,
           "vol15_adv": vol15_adv(d, ctx) if d else None, "verdict": verdict, "piliers": pillars, "graphique": chart,
           "codes": catalyst_codes(cat, now), "societe": _company(symbol, profile, cat, feats, before),
           "catalyseur": cat, "potentiel": PO.hint(gap) if gap is not None else None, "jarvis": None, "source": src}
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
    cut = [{"symbol": sym, "gap_ouverture": g["gap_open"], "gap_moment": g["gap_now"], "atr_pct": g["atr_pct"]}
           for sym, g in movers[SCAN_DEEP:]]
    return {"at": now.isoformat(timespec="seconds"), "examined": len(symbols), "movers": len(movers),
            "analysed": len(results), "setups": keep, "dropped": dropped, "coupees": cut}


def log_scan(r: dict) -> None:
    """Garde les valeurs que le scan n'a pas analysées (au-delà des SCAN_DEEP plus forts gaps) avec l'heure :
    on pourra les juger après coup, avec les actus publiées avant le scan, sans rien changer à Killmode."""
    book = _read(SCANS_PATH, []) or []
    book.append({"at": r["at"], "analysees": [a["symbol"] for a in r["setups"] + r["dropped"]],
                 "coupees": r.get("coupees") or []})
    _write(SCANS_PATH, book[-SCANS_KEPT:])


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


def clean(c: dict) -> bool:
    """Témoin propre : le catalyseur ne tombe que faute d'actu positive (ni veto, ni Finnhub muet), la seule
    population que Killmode aurait tradée avec une bonne actu."""
    return _group_of(c) == "temoin_catalyseur" and c.get("codes") == ["pas_de_positive"]


# Appels notés avant que le journal garde la règle (PR #32) : règle n° 5 avec ces seuils, figés.
LEGACY_VERSION = (5, json.dumps({"gap": 4.0, "atr": 4.0, "range_min": 15, "jusqu_a": "11:00", "poursuite_r": 0.5,
                                 "catalyseur_h": 48}, sort_keys=True))


def _version(c: dict) -> tuple:
    if "regle" not in c:
        return LEGACY_VERSION
    return c["regle"], json.dumps(c["seuils"], sort_keys=True)


def _current() -> tuple:
    return RULE, json.dumps(seuils(), sort_keys=True)


def call_motif(cat: dict | None, now: datetime) -> str | None:
    """Ce qui porte le catalyseur : « resultats » (battus hier ou aujourd'hui), « fond » (une actu positive dit
    une cause), « mouvement » (seulement des titres sur le cours, « shares jump ») ; None sans rien de positif."""
    if not cat:
        return None
    if _earnings_beat(cat):
        return "resultats"
    pos = [n for n in fresh_news(cat, now) if n["tone"] == "positive"]
    if not pos:
        return None
    return "fond" if any(motif(n["title"]) == "fond" for n in pos) else "mouvement"


def record(a: dict, now: datetime | None = None) -> dict | None:
    """Note un 🔪 KILL ou un témoin catalyseur (une fois par action, séance et groupe) ; None si déjà noté ou
    hors des deux groupes. Le groupe est figé à l'écriture : un témoin reste un témoin si l'action devient KILL
    plus tard dans la séance (l'écarter après coup choisirait les témoins sur une info postérieure) ; la
    comparaison des groupes prend le premier appel de chaque action et séance (first_calls)."""
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
    co = a.get("societe") or {}
    fresh = fresh_news(cat, now)
    pos = [n for n in fresh if n["tone"] == "positive"]
    call = {"id": uuid.uuid4().hex[:8], "groupe": g, "symbol": a["symbol"], "day": day, "at": int(now.timestamp()),
            "price": a["price"], "price_at": a.get("price_at"), "entry": plan["entry"], "stop": plan["stop"],
            "targets": plan["targets"], "decided_at": plan["decided_at"], "declenche_a": plan.get("declenche_a"),
            "until": plan["until"], "close_ts": plan["close_ts"], "gap_pct": a["gap_pct"],
            "gap_type": a.get("gap_kind"), "prev_close": a.get("prev_close"), "atr_pct": a["atr_pct"],
            "vol15_adv": a.get("vol15_adv"), "adv20": a.get("adv20"), "source": a.get("source"),
            "secteur": co.get("secteur"), "capitalisation": co.get("capitalisation"),
            "declaration": ST.DECLARATION, "regle": RULE, "seuils": seuils(),
            "catalyseur": a["piliers"]["catalyseur"]["raisons"],  # ce qui le porte, ou ce qui manque (témoin)
            "codes": a["codes"] if "codes" in a else catalyst_codes(cat, now), "motif": call_motif(cat, now),
            "actus": [{"titre": n["title"], "publiee": n["published_at"], "ton": n["tone"],
                       "motif": motif(n["title"]) if n["tone"] == "positive" else None} for n in fresh[:8]],
            "resultats": cat.get("last_earnings"), "actu": pos[0]["title"] if pos else None, "resultat": None}
    book.append(call)
    _write(CALLS_PATH, book)
    return call


def kill_today(symbol: str, now: datetime | None = None) -> dict | None:
    """Le dernier 🔪 KILL noté aujourd'hui (New York) sur `symbol` : ses objectifs servent à `!prends … kill`
    donné sans `tp`, pour que le suivi applique la gestion que le journal mesure."""
    day = (now or datetime.now(timezone.utc)).astimezone(NEW_YORK).date().isoformat()
    return next((c for c in reversed(calls()) if c["symbol"] == symbol.upper() and c["day"] == day
                 and _group_of(c) == "kill"), None)


def sip_view(call: dict, bars: list[dict]) -> dict | None:
    """La règle de prix refaite sur les bougies SIP de la séance (tout le marché ; en direct, Twelve Data ne voit
    qu'une part des échanges) : signal, entrée et stop, même ordre qu'en direct à un pas près, volume des 15
    premières minutes sur le volume moyen. None sans clôture de la veille notée ou sans bougie de 9 h 30."""
    prev, adv20 = call.get("prev_close"), call.get("adv20")
    bounds = CO.session_ts(date.fromisoformat(call["day"]))
    n = ST.KILL_RANGE_MIN // CO.BASE_TF
    s = [b for b in bars if bounds and bounds[0] <= b["t"] < bounds[1]]
    if not prev or len(s) < n or s[0]["t"] != bounds[0]:
        return None
    hi, lo = max(b["h"] for b in s[:n]), min(b["l"] for b in s[:n])
    vwap = ST.vwap_from(s, 0)[n - 1]
    gap = (s[0]["o"] / prev - 1) * 100
    signal = gap >= ST.KILL_GAP_PCT and vwap is not None and s[n - 1]["c"] > vwap and lo > prev
    entry, stop = hi + SIM.tick_size("stock", hi), lo - SIM.tick_size("stock", lo)
    tick = SIM.tick_size("stock", call["entry"]) * 1.001
    return {"signal": signal, "gap_pct": round(gap, 2), "entree": round(entry, 4), "stop": round(stop, 4),
            "meme_ordre": signal and abs(entry - call["entry"]) <= tick and abs(stop - call["stop"]) <= tick,
            "vol15_adv": round(sum(b["v"] for b in s[:n]) / adv20, 4) if adv20 else None}


def _incomplete(call: dict, after: list[dict], res: dict, step: int = STEP) -> bool:
    """Trade encore ouvert à la clôture sans bougie dans la dernière demi-heure (suspension, données coupées) :
    soldé à un prix d'avant, peut-être faux."""
    if not res["triggered"] or res["exit_reason"] != "cloture":
        return False
    last = max((b["t"] for b in after if b["t"] + step <= call["close_ts"]), default=None)
    return last is None or last + step < call["close_ts"] - LATE_GAP


def activation(call: dict, step: int = STEP) -> int:
    """Début de la première bougie pleine LATENCY secondes après l'appel : l'ordre posé chez IBKR (message lu,
    ordre saisi), le même délai pour chaque KILL et chaque témoin. Ce qui se passe avant n'est jamais vu."""
    return -(-(call["at"] + LATENCY) // step) * step


def _exits(res: dict) -> list[dict]:
    return [{"prix": e["price"], "part": e["part"], "raison": e["reason"]} for e in res["exits"]]


def settle(call: dict, bars: list[dict], step: int = STEP) -> dict:
    """Solde un appel sur les bougies de sa séance (`step` secondes, 1 min) : ordre stop d'achat actif à
    `activation` (une bougie qui ouvre au-dessus de l'entrée remplit à son ouverture : l'ordre part au prix du
    moment, jamais sous l'entrée), gestion du labo (un tiers par objectif, stop à l'entrée après TP1), soldé à la
    clôture. Une bougie dont l'ordre des prix est inconnu et change l'issue est jouée dans le pire ordre
    (`sorties`, d'où vient le chiffre qui décide) et dans le meilleur (`sorties_fav`, affiché) ; `ambigus` dit
    lesquelles et pourquoi. `r_ibkr` rejoue l'appel comme le labo (bougies de 5 min dès la suivante, résolution
    et frais du labo), pour la comparaison au labo seulement. Garde le détail (remplissage, sorties, pire
    excursion) d'où le bilan recalcule les frais réels, et la règle refaite sur les bougies SIP de la séance."""
    act = activation(call, step)
    plan = {"side": "long", "entry": call["entry"], "stop": call["stop"],
            "targets": [{"price": p} for p in call["targets"]]}
    kw = {"market": "stock", "entry_until": min(call["until"], call["close_ts"]), "exit_at": call["close_ts"],
          "last_price": call["price"] or call["entry"], "cost": SIM.costs("stock", call["entry"]),
          "entry_order": "stop"}
    after = [b for b in bars if b["t"] >= act]
    res = SIM.simulate(plan, after, decided_at=act, bar_seconds=step, resolution="prudente", **kw)
    fav = SIM.simulate(plan, after, decided_at=act, bar_seconds=step, resolution="favorable", **kw)
    five = CO.aggregate(bars, CO.BASE_TF, "stock") if step < BAR else bars
    b5 = -(-call["at"] // BAR) * BAR  # le labo : bougies de 5 min dès celle qui suit l'appel
    lab = SIM.simulate(plan, [b for b in five if b["t"] >= b5], decided_at=b5, **kw)
    risk = call["entry"] - call["stop"]
    seen = {a["t"]: a["cas"] for a in fav.get("ambigus", []) + res.get("ambigus", [])}
    out = {"methode": SETTLE_METHOD, "pas": step, "activation": act, "declenche": bool(res["triggered"]),
           "r_net": res["r_net"], "r_ibkr": None, "sortie": res["exit_reason"],
           "tp": [bool(x) for x in (res.get("tp_hits") or [])], "mfe_r": res.get("mfe_r"), "mae_r": res.get("mae_r"),
           "remplissage": res["fill_price"], "mode_entree": res["entry_mode"], "sorties": _exits(res),
           "sorties_fav": _exits(fav), "sortie_fav": fav["exit_reason"],
           "ambigus": [{"t": t, "cas": seen[t]} for t in sorted(seen)],
           "entree_r": None, "sous_stop_r": None, "donnees_incompletes": _incomplete(call, after, res, step),
           "sip": sip_view(call, five)}
    if lab["triggered"] and lab["r_net"] is not None:
        out["r_ibkr"] = round(lab["r_net"] - (SS.ibkr_commission_r(call["entry"], call["stop"]) or 0.0), 4)
    if res["triggered"] and res["r_net"] is not None:
        out["entree_r"] = round((res["fill_price"] - call["entry"]) / risk, 4)
        for e in res["exits"]:
            if e["reason"] in ("stop", "stop_be"):  # stop actif : le stop, ou l'entrée après TP1
                active = call["stop"] if e["reason"] == "stop" else call["entry"]
                out["sous_stop_r"] = round((active - e["price"]) / risk, 4)
    return out


async def eurusd() -> float | None:
    """Taux BCE du jour (Frankfurter) pour convertir les 20 € de risque ; None si la source ne répond pas."""
    try:
        return (await frankfurter.fx_rates("EUR", ["USD"]))["rates"]["USD"]
    except (SourceError, KeyError, TypeError) as exc:  # le bilan passe au change par défaut
        log.info("Killmode : taux EUR/USD indisponible : %s", exc)
        return None


def _bars_file(call: dict) -> Path:
    return Path(BARS_DIR) / f"{call['day']}_{call['symbol']}.json.gz"


async def _alpaca_call(fn, *args, **kwargs):
    return await LSRC.with_retry(fn, *args, limiter=LSRC.ALPACA_LIMIT, **kwargs)


async def session_bars(call: dict) -> list[dict]:
    """Bougies d'1 min SIP non ajustées de toute la séance de l'appel, en une seule requête (≈ 390 bougies), gardées
    dans BARS_DIR : un changement de méthode re-solde sur les mêmes prix sans rien redemander. Non ajustées : un
    regroupement d'actions entre l'appel et le bilan ne fausse pas le solde."""
    path = _bars_file(call)
    if path.exists():
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    bounds = CO.session_ts(date.fromisoformat(call["day"]))
    if not bounds:
        return []
    start, end = (datetime.fromtimestamp(x, timezone.utc) for x in bounds)
    rows = (await alpaca.bars_range([call["symbol"]], 1, start, end, adjustment="raw", call=_alpaca_call)
            ).get(call["symbol"]) or []
    rows = [b for b in rows if bounds[0] <= b["t"] < bounds[1]]
    if rows:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(rows, f)
        os.replace(tmp, path)
    return rows


async def settle_due(now: datetime | None = None) -> int:
    """Solde les appels dont la séance est finie, et re-solde sur les mêmes bougies ceux d'une méthode précédente
    (l'ancien solde est gardé dans `soldes_anciens`) ; appelé par `!kill bilan`, jamais planifié. Une erreur sur
    un appel est notée (`erreur_solde`, affichée au bilan) sans bloquer les autres, et l'appel est retenté au
    bilan suivant. Un appel encore sans bougies UNSETTLED_DAYS jours après sa séance est « insoldable », compté
    à part. Renvoie le nombre d'appels soldés ou déclarés insoldables."""
    now = now or datetime.now(timezone.utc)
    book = calls()
    ready = now.timestamp() - SETTLE_DELAY.total_seconds()
    due = [c for c in book if not c.get("insoldable") and c["close_ts"] <= ready
           and (not c.get("resultat") or c["resultat"].get("methode", 1) != SETTLE_METHOD)]
    rate = await eurusd() if any(not c.get("resultat") for c in due) else None
    changed = done = 0
    for c in due:
        old = c.get("resultat")
        try:
            rows = await session_bars(c)
            if rows:
                kept = ({"eurusd": old.get("eurusd"), "plan_ibkr": old.get("plan_ibkr") or IBKR_PLAN} if old
                        else {"eurusd": rate, "plan_ibkr": IBKR_PLAN})
                c["resultat"] = settle(c, rows) | kept
                if old:
                    c["soldes_anciens"] = (c.get("soldes_anciens") or []) + [old]
            elif not old and now.timestamp() >= c["close_ts"] + UNSETTLED_DAYS * 86400:
                c["insoldable"] = True
            else:
                continue
        except (SourceError, OSError, ValueError, KeyError, TypeError) as exc:  # ni les autres appels, ni le bilan
            log.warning("Killmode : solde de %s du %s impossible : %s", c["symbol"], c["day"], exc)
            c["erreur_solde"] = f"{type(exc).__name__} : {exc}"[:200]
            changed += 1
            continue
        c.pop("erreur_solde", None)
        changed += 1
        done += 1
    if changed:
        _write(CALLS_PATH, book)
    return done


def shares(risk: float, rate: float | None = None) -> int:
    """Actions achetées pour RISK_EUR de risque à `risk` $ par action (au moins une)."""
    return max(1, math.floor(RISK_EUR * (rate or EURUSD_DEFAULT) / risk + 1e-9))


def _brackets(q: int, n: int) -> list[int]:
    """Parts de `q` actions en `n` brackets aussi égaux que possible (TP1 reçoit le reste), comme les plans IBKR."""
    n = max(1, min(n, q))
    k, r = divmod(q, n)
    return [k + (1 if i < r else 0) for i in range(n)]


def order_commission(shares: float, price: float, plan: dict) -> float:
    """Commission IBKR d'un ordre exécuté (en $) : par action, minimum par ordre, plafond 1 % de sa valeur."""
    if shares <= 0:
        return 0.0
    base = min(max(plan["min"], plan["per_share"] * shares), plan["max_pct"] * shares * price)
    return base + plan["extra"] * shares


def _half_tick_slip(price: float) -> float:
    """Glissement par action et par côté : celui du labo, jamais moins d'un demi-pas de cotation."""
    return max(SIM.stock_slippage_bps(price) / 10000 * price, SIM.tick_size("stock", price) / 2)


def _legs(q: int, n_targets: int, exits: list[dict]) -> list[tuple[int, dict]]:
    """Chaque bracket en actions entières (`_brackets`) et la sortie du solde qui le ferme : celle qui couvre le
    milieu du bracket dans les parts du solde (un tiers par objectif). Avec moins d'actions que d'objectifs, un
    bracket prend l'objectif du milieu de sa part (1 action : TP2 ; 2 actions : TP1 et TP3)."""
    legs, cum = [], 0.0
    for e in exits:
        cum += e["part"]
        legs.append((cum, e))
    out, done = [], 0
    for n in _brackets(q, n_targets):
        mid = (done + n / 2) / q
        done += n
        out.append((n, next((e for c, e in legs if mid <= c + 1e-9), legs[-1][1])))
    return out


def _at_size(call: dict, res: dict | None, exits_key: str = "sorties", stress: bool = False) -> float | None:
    """R du trade à la taille de Simão : RISK_EUR de risque (change du jour du solde), un bracket IBKR par objectif
    en actions entières, gain et frais bracket par bracket (une entrée et une sortie chacun, chaque ordre avec son
    minimum, plan IBKR noté au solde). Glissement du labo avec un plancher d'un demi-pas par côté ; `stress` : 3 c
    à l'entrée et à chaque sortie au marché (1 % du prix sous 1 $), objectifs au prix de l'ordre. Sur le risque
    prévu : un mauvais remplissage n'améliore jamais le chiffre. None si l'ordre n'est pas parti ou si le solde
    n'a pas le détail."""
    if not res or not res.get("declenche") or not res.get(exits_key) or res.get("remplissage") is None:
        return None
    entry, stop = call["entry"], call["stop"]
    risk = entry - stop
    q = shares(risk, res.get("eurusd"))
    plan = IBKR_PLANS.get(res.get("plan_ibkr") or IBKR_PLAN, IBKR_PLANS["fixe"])
    fill = res["remplissage"]
    s = (STRESS_CENTS if entry >= 1 else entry * STRESS_SUB_DOLLAR) if stress else None
    pnl = 0.0
    for n, e in _legs(q, len(call["targets"]), res[exits_key]):
        price = e["prix"]
        out = (0.0 if e["raison"].startswith("tp") else s) if stress else _half_tick_slip(price)
        pnl += (n * (price - out - fill - (s if stress else _half_tick_slip(fill)))
                - order_commission(n, fill, plan) - order_commission(n, price, plan))
    return round(pnl / (q * risk), 4)


def real_r(call: dict, res: dict | None) -> float | None:
    """Le chiffre qui décide : le R aux frais réels de Simão (`_at_size`), sur le solde joué dans le pire ordre
    des bougies ambiguës. Recalculé à chaque bilan depuis les sorties gardées."""
    return _at_size(call, res)


def favorable_r(call: dict, res: dict | None) -> float | None:
    """Le même R sur le solde joué dans le meilleur ordre des bougies ambiguës : une sensibilité, affichée."""
    return _at_size(call, res, "sorties_fav")


def stress_20e(call: dict, res: dict | None) -> float | None:
    """Le stress de Gemini à la taille de Simão (3 c par côté au lieu du glissement, mêmes brackets et minimums IBKR
    que r_20e) : la condition « avec 3 c » de la lecture finale. Une hypothèse, pas une mesure."""
    return _at_size(call, res, stress=True)


def stressed_r(call: dict, res: dict | None) -> float | None:
    """Stress de Gemini à la commission IBKR par action, sans minimum : 3 cents de plus à l'entrée, 3 cents de moins
    à chaque sortie au marché (stop, stop à l'entrée, clôture), objectifs au prix de l'ordre ; sous 1 $, 1 % du prix
    au lieu de 3 cents. Remplace le glissement du labo (il ne s'y ajoute pas). Gardé pour le labo (PR #43) ; le
    journal du direct lit `stress_20e`, à la taille de Simão."""
    if not res or not res.get("declenche") or not res.get("sorties") or res.get("remplissage") is None:
        return None
    entry, stop = call["entry"], call["stop"]
    s = STRESS_CENTS if entry >= 1 else entry * STRESS_SUB_DOLLAR
    fill = res["remplissage"] + s
    gain = sum(e["part"] * (e["prix"] - (0 if e["raison"].startswith("tp") else s) - fill) for e in res["sorties"])
    return round(gain / (entry - stop) - (SS.ibkr_commission_r(entry, stop) or 0.0), 4)


KEYS = {"r_20e": real_r, "r_3c": stress_20e, "r_fav": favorable_r}


def value(c: dict, key: str = "r_20e") -> float | None:
    """Résultat d'un appel déclenché en R ; `key` : r_20e (frais réels de Simão, le chiffre qui décide), r_fav (le
    même dans le meilleur ordre des bougies ambiguës), r_3c (stress de Gemini à la taille de Simão) ou r_ibkr
    (rejoué comme le labo, frais du labo, pour la comparaison au labo). None sinon."""
    res = c.get("resultat") or {}
    if c.get("insoldable") or not res.get("declenche"):
        return None
    return KEYS[key](c, res) if key in KEYS else res.get(key)


def in_group(book: list[dict], groupe: str) -> list[dict]:
    """Appels d'un groupe tels que notés (le groupe est figé à l'écriture). « temoin_propre » : les témoins sans
    actu positive ni veto ; « temoin_autre » : les autres témoins (actus négatives, résultats dans 24 h…)."""
    if groupe == "temoin_propre":
        return [c for c in book if clean(c)]
    if groupe == "temoin_autre":
        return [c for c in book if _group_of(c) == "temoin_catalyseur" and not clean(c)]
    return [c for c in book if _group_of(c) == groupe]


def first_calls(book: list[dict]) -> list[dict]:
    """Le premier appel de chaque action et séance : comparer les groupes sans compter deux fois une séance."""
    seen, out = set(), []
    for c in sorted(book, key=lambda c: c["at"]):
        if (c["symbol"], c["day"]) not in seen:
            seen.add((c["symbol"], c["day"]))
            out.append(c)
    return out


# Quantiles de Student exacts jusqu'à 8 degrés de liberté (IC 95 % et IC 99,9 % bilatéraux).
T_TABLE = {0.975: (12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306),
           0.9995: (636.62, 31.599, 12.924, 8.610, 6.869, 5.959, 5.408, 5.041)}
HARM_P = 0.9995


def t_quantile(p: float, df: int) -> float:
    """Quantile p de la loi de Student à `df` degrés de liberté : table exacte jusqu'à 8, développement de
    Cornish-Fisher au-delà (écart < 0,01 dès 9 degrés, y compris à 99,95 %)."""
    if df < 1:
        return math.inf
    if df <= len(T_TABLE.get(p, ())):
        return T_TABLE[p][df - 1]
    z = NormalDist().inv_cdf(p)
    return (z + (z ** 3 + z) / (4 * df) + (5 * z ** 5 + 16 * z ** 3 + 3 * z) / (96 * df ** 2)
            + (3 * z ** 7 + 19 * z ** 5 + 17 * z ** 3 - 15 * z) / (384 * df ** 3)
            + (79 * z ** 9 + 776 * z ** 7 + 1482 * z ** 5 - 1920 * z ** 3 - 945 * z) / (92160 * df ** 4))


def mean_t(rows: list[tuple], p: float = 0.975) -> dict:
    """Moyenne en grappes (par jour, ou par la clé donnée : semaine…) comme au labo, avec un IC en t à G − 1
    degrés de liberté, G = grappes : avec 15 à 25 jours, 1,96 donnait un IC 95 % trop étroit de 5 à 9 %."""
    days: dict = defaultdict(lambda: [0.0, 0])
    for day, r in rows:
        days[day][0] += r
        days[day][1] += 1
    if not rows:
        return {"n": 0, "m": None, "se": None, "ci": None, "jours": 0}
    m = sum(r for _, r in rows) / len(rows)
    se = LS.cluster_se(days, m, len(rows))
    q = t_quantile(p, len(days) - 1)
    ci = [round(m - q * se, 4), round(m + q * se, 4)] if se else None
    return {"n": len(rows), "m": round(m, 4), "se": se, "ci": ci, "jours": len(days)}


def _week(day: str) -> tuple:
    return date.fromisoformat(day).isocalendar()[:2]


def stats(book: list[dict], groupe: str = "kill", key: str = "r_20e") -> dict:
    """Bilan d'un groupe du journal : espérance par trade déclenché (frais réels de Simão par défaut ; `key` r_ibkr
    pour les frais du labo, r_3c pour le stress de Gemini) et par appel (0 R quand l'ordre a expiré), IC 95 % en
    grappes par jour en t, et le même en grappes par semaine ; ordres expirés, taux de déclenchement, séances et
    semaines distinctes, appels insoldables."""
    rows = in_group(book, groupe)
    solvable = [c for c in rows if not c.get("insoldable")]
    settled = [c for c in solvable if c.get("resultat")]
    fired = [c for c in settled if c["resultat"]["declenche"]]
    trig = [c for c in fired if value(c, key) is not None]
    per = mean_t([(c["day"], value(c, key)) for c in trig])
    weekly = mean_t([(_week(c["day"]), value(c, key)) for c in trig])
    by_call = mean_t([(c["day"], value(c, key) if c["resultat"]["declenche"] else 0.0) for c in settled
                      if not c["resultat"]["declenche"] or value(c, key) is not None])
    return {"appels": len(rows), "soldes": len(settled), "en_attente": len(solvable) - len(settled),
            "insoldables": len(rows) - len(solvable), "declenches": len(trig), "expires": len(settled) - len(fired),
            "taux": len(fired) / len(settled) if settled else None, "esperance_r": per["m"], "ic95": per["ci"],
            "se": per["se"], "jours": per["jours"], "semaines": weekly["jours"], "ic95_semaines": weekly["ci"],
            "par_appel_r": by_call["m"], "ic95_appel": by_call["ci"], "se_appel": by_call["se"],
            "jours_appel": by_call["jours"],
            "total_r": round(sum(value(c, key) for c in trig), 2) if trig else None,
            "gagnants": sum(value(c, key) > 0 for c in trig),
            "tp1": sum(1 for c in trig if c["resultat"]["tp"][:1] == [True])}


def call_gap(book: list[dict], scan_only: bool = False) -> dict:
    """Écart par appel KILL − témoin propre (0 R pour un ordre expiré), chaque action et séance comptée une fois
    selon son premier appel ; `scan_only` : les appels du scan seulement (même source, même retard, actions que
    Simão n'a pas choisies). Descriptif, il ne décide rien : il faudrait des centaines de séances pour voir 0,3 R.
    C'est la valeur prédictive du filtre catalyseur à règle de prix égale, pas l'effet causal d'une actu ; elle se
    mesure au labo."""
    first = [c for c in first_calls(book) if not scan_only or (c.get("source") or "").startswith("alpaca")]
    k, w = stats(first, "kill"), stats(first, "temoin_propre")
    if k["par_appel_r"] is None or w["par_appel_r"] is None:
        return {"m": None, "ci": None, "n": (k["soldes"], w["soldes"])}
    m = k["par_appel_r"] - w["par_appel_r"]
    if not k["se_appel"] or not w["se_appel"]:
        return {"m": round(m, 4), "ci": None, "n": (k["soldes"], w["soldes"])}
    se = math.hypot(k["se_appel"], w["se_appel"])
    q = t_quantile(0.975, min(k["jours_appel"], w["jours_appel"]) - 1)
    return {"m": round(m, 4), "ci": [round(m - q * se, 4), round(m + q * se, 4)], "n": (k["soldes"], w["soldes"])}


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


def judged(book: list[dict], n: int, key: str = "r_20e") -> list[dict]:
    """Les `n` premiers KILL déclenchés, dans l'ordre des appels : un lot figé. Vide tant qu'il n'est pas complet,
    ou si un KILL plus ancien attend encore son solde (le soldé plus tard décalerait le lot). Les insoldables
    sont comptés à part, hors du lot."""
    out = []
    for c in sorted(in_group(book, "kill"), key=lambda c: c["at"]):
        if c.get("insoldable"):
            continue
        if not c.get("resultat"):
            return []
        if value(c, key) is not None:
            out.append(c)
            if len(out) == n:
                return out
    return []


def harm(book: list[dict], key: str = "r_20e") -> dict | None:
    """Borne de dégât, écrite le 8 octobre avant tout KILL soldé : à chaque bilan dès HARM_MIN KILL déclenchés,
    l'IC 99,9 % de l'espérance par trade (t à 99,95 %) ; renvoyé seulement s'il est entièrement sous 0. Simulée
    par le council : environ 2 % de fausse alerte sur toute la vie du journal si l'espérance vraie est nulle (5,6 %
    avec z 3,29), environ 75 % de détection à −0,4 R. Elle est faite pour une catastrophe ou un bug."""
    trig = [c for c in in_group(book, "kill") if value(c, key) is not None]
    if len(trig) < HARM_MIN:
        return None
    r = mean_t([(c["day"], value(c, key)) for c in trig], HARM_P)
    return r if r["ci"] and r["ci"][1] < 0 else None


def _span(look: int | str, st: dict) -> str:
    ci = st["ic95"]
    return (f"lu sur {look} : {SS._r(st['esperance_r'])} R par trade, IC 95 % "
            f"[{SS._r(ci[0])} ; {SS._r(ci[1])}] sur {_n(st['jours'], 'séance')}")


def reading(book: list[dict], lab: dict | None) -> list[str]:
    """Règle de lecture du journal, écrite avant le premier appel et revue le 8 octobre avant tout KILL soldé
    (avis de GPT et Gemini, council), sur les KILL d'une même version et sur le R aux frais réels de Simão
    (r_20e : 20 € de risque, brackets IBKR, minimum par ordre) :
    - au 30e KILL déclenché, une seule issue : borne haute de l'IC 95 % sous 0 → JARVIS recommande d'éteindre ;
      sinon « pas d'alerte », un audit et jamais une validation (un verdict positif à 30 portait le risque de
      faux positif de 2,5 % à 4,5 %) ;
    - au 100e, la lecture finale, toujours faite : borne haute sous 0 → éteindre ; borne basse au-dessus de 0
      en grappes par jour, par semaine ET avec 3 c de glissement par côté → avantage démontré ; positif sans les
      trois → « pas une validation » ; sinon ce que l'intervalle n'exclut pas et le plus petit écart visible. Si
      l'alerte du 30e a été écartée, le pari se mesure sur les KILL 31 à 100, joués après l'alerte ;
    - la borne de dégât (IC 99,9 %) à chaque bilan dès HARM_MIN KILL déclenchés.
    Les lots sont figés (les premiers KILL dans l'ordre des appels) ; IC en t à G − 1 degrés de liberté. Un appel
    dont la règle refaite sur les bougies SIP ne concorde pas reste dans la lecture : c'est ce que Simão a reçu."""
    n = stats(book, "kill")["declenches"]
    first, final = judged(book, READ_AT), judged(book, READ_MAX)
    s30 = stats(first, "kill") if first else None
    s100 = stats(final, "kill") if final else None
    alert30 = bool(s30 and s30["ic95"] and s30["ic95"][1] < 0)
    out, look = [], None
    if s100 and s100["ic95"]:
        ci, span, look = s100["ic95"], _span(f"les {READ_MAX} premiers KILL", s100), s100
        if ci[1] < 0:
            out.append(f"📉 **Lecture finale : espérance négative même en haut de l'intervalle** ({span}) : "
                       "JARVIS recommande d'éteindre Killmode ; à toi de décider.")
            out += [x for x in (_fav_note(final),) if x]
        elif ci[0] > 0:
            weeks, stress = s100["ic95_semaines"], stats(final, "kill", "r_3c")["ic95"]
            if weeks and weeks[0] > 0 and stress and stress[0] > 0:
                out.append(f"📈 **Lecture finale : espérance positive même en bas de l'intervalle** ({span}), qui "
                           f"tient semaine par semaine ({s100['semaines']} semaines) et avec 3 c de glissement par "
                           "côté : avantage démontré.")
            else:
                why = [f"concentré sur {_n(s100['semaines'], 'semaine')}" if not (weeks and weeks[0] > 0) else None,
                       "ne tient pas avec 3 c de glissement par côté" if not (stress and stress[0] > 0) else None]
                out.append(f"Lecture finale positive ({span}) mais pas une validation : "
                           f"{' ; '.join(w for w in why if w)}.")
        else:
            out.append(f"Lecture finale ({span}) : JARVIS ne voit pas d'avantage démontré. L'intervalle n'exclut ni "
                       f"{SS._r(ci[0])} R ni {SS._r(ci[1])} R par trade ; à cette taille, seul un écart d'au moins "
                       f"≈ {_fr(2.8 * s100['se'])} R se voit. À toi de décider.")
        if alert30:
            bet = stats(final[READ_AT:], "kill")
            bci = bet["ic95"]
            out.append(f"Le pari contre l'avis de JARVIS (KILL {READ_AT + 1} à {READ_MAX}, joués après l'alerte du "
                       f"{READ_AT}e) : {SS._r(bet['esperance_r'])} R par trade"
                       + (f" [{SS._r(bci[0])} ; {SS._r(bci[1])}]" if bci else "") + ".")
    elif s30 and s30["ic95"]:
        span, look = _span(f"les {READ_AT} premiers KILL", s30), s30
        if alert30:
            out.append(f"📉 **Espérance négative même en haut de l'intervalle** ({span}) : JARVIS recommande "
                       f"d'éteindre Killmode ; à toi de décider. Lecture finale à {READ_MAX} KILL "
                       f"({n} pour l'instant).")
            out += [x for x in (_fav_note(first),) if x]
        else:
            out.append(f"Lu au {READ_AT}e KILL ({span}) : pas d'alerte. C'est un audit, pas une validation : lecture "
                       f"finale à {READ_MAX} KILL ({n} pour l'instant).")
    else:
        out.append(f"-# Règle de lecture : rien n'est lu avant {READ_AT} KILL déclenchés et soldés ({n} pour "
                   f"l'instant), l'intervalle affiché d'ici là est indicatif ; lecture finale à {READ_MAX}.")
    h = harm(book)
    if h:
        inc = sum(1 for c in in_group(book, "kill")
                  if value(c) is not None and c["resultat"].get("donnees_incompletes"))
        out.append(f"🛑 **Borne de dégât franchie** : {SS._r(h['m'])} R par trade sur {h['n']} KILL, IC 99,9 % "
                   f"[{SS._r(h['ci'][0])} ; {SS._r(h['ci'][1])}] entièrement sous 0"
                   + (f" ({inc} soldé{'s' if inc > 1 else ''} sur des données incomplètes, à vérifier)" if inc else "")
                   + " : JARVIS recommande d'éteindre Killmode ; à toi de décider.")
        if stats(book, "kill", "r_fav")["declenches"] and harm(book, "r_fav") is None:
            out.append("-# Joué dans le meilleur ordre des minutes ambiguës, la borne de dégât ne serait pas "
                       "franchie : l'alerte dépend de l'ordre des prix dans la minute ; la règle garde le pire.")
    lot = final if look is s100 else first  # le lot lu, aux frais du labo pour la comparaison au labo
    gap = _lab_gap(stats(lot, "kill", "r_ibkr"), lab) if look else None
    if gap:
        out.append(gap)
    return out


def _fav_note(rows: list[dict]) -> str | None:
    """Une alerte (borne haute de l'IC sous 0) qui ne tient plus si les minutes ambiguës sont jouées dans le
    meilleur ordre : dite sous l'alerte, sans rien changer à la règle (le pire ordre décide, demande de GPT)."""
    st = stats(rows, "kill", "r_fav")
    ci = st["ic95"]
    if not st["declenches"] or not ci or ci[1] < 0:
        return None
    return (f"-# Joué dans le meilleur ordre des minutes ambiguës : {SS._r(st['esperance_r'])} R par trade "
            f"[{SS._r(ci[0])} ; {SS._r(ci[1])}], borne haute au-dessus de 0. L'alerte dépend de l'ordre des prix dans "
            "la minute ; la règle garde le pire ordre.")


def ambiguous_line(rows: list[dict]) -> str | None:
    """Ce que pèse l'ordre inconnu des prix dans une minute : KILL dont le pire et le meilleur ordre diffèrent, et
    l'espérance dans les deux (le pire décide)."""
    trig = [c for c in rows if value(c) is not None and value(c, "r_fav") is not None]
    if not trig:
        return None
    moved = [c for c in trig if abs(value(c, "r_fav") - value(c)) > 1e-9]
    worst, best = (sum(value(c, k) for c in trig) / len(trig) for k in ("r_20e", "r_fav"))
    return (f"-# Minutes ambiguës (ordre des prix inconnu) : {len(moved)}/{len(trig)} KILL changent selon l'ordre ; "
            f"{SS._r(worst)} R par trade dans le pire ordre (celui qui compte), {SS._r(best)} R dans le meilleur.")


def errors_line(rows: list[dict]) -> str | None:
    """Appels que le bilan n'a pas pu solder : jamais en silence, retentés au bilan suivant."""
    bad = [c for c in rows if c.get("erreur_solde") and not c.get("resultat")]
    if not bad:
        return None
    return (f"⚠️ {_n(len(bad), 'appel')} pas encore soldé{'s' if len(bad) > 1 else ''} ("
            + " · ".join(f"{c['symbol']} du {c['day']} : {c['erreur_solde']}" for c in bad[:3])
            + ") : retenté au prochain `!kill bilan`. Tant qu'un KILL attend, la lecture du lot ne se fait pas.")


def stress_line(book: list[dict]) -> str | None:
    """Le stress de Gemini (3 c par côté) en un chiffre : une hypothèse, pas une mesure, qui ne décide rien seul."""
    st = stats(book, "kill", "r_3c")
    if not st["declenches"]:
        return None
    return (f"-# Avec 3 c de glissement par côté (hypothèse de Gemini, non mesurée), mêmes brackets et minimums IBKR : "
            f"{SS._r(st['esperance_r'])} R par trade sur {_n(st['declenches'], 'KILL')}.")


def _lab_gap(st: dict, lab: dict | None) -> str | None:
    """Écart labo/direct (aux frais du labo des deux côtés) seulement s'il dépasse le bruit des deux mesures."""
    lab_ci = (lab or {}).get("ic95_ibkr")
    if not lab_ci or not st.get("se") or st.get("esperance_r") is None:
        return None
    m_lab = lab.get("esperance_ibkr_r")
    if m_lab is None:
        m_lab = (lab_ci[0] + lab_ci[1]) / 2
    se = math.hypot((lab_ci[1] - lab_ci[0]) / 3.92, st["se"])
    if not se or abs(st["esperance_r"] - m_lab) / se < 1.96:
        return None
    return (f"-# Écart labo/direct (frais du labo des deux côtés) : le journal fait {SS._r(st['esperance_r'])} R, le "
            f"labo {SS._r(m_lab)} R [{SS._r(lab_ci[0])} ; {SS._r(lab_ci[1])}], au-delà du bruit des deux mesures "
            "(catalyseur, univers ou données différents) : à comprendre avant d'en tirer quoi que ce soit.")


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


def stop_cost(entry: float, stop: float) -> dict:
    """Frais d'un trade stoppé, en R : à la taille de Simão (RISK_EUR, un bracket par objectif, une entrée et un
    stop chacun, minimum par ordre, glissement d'au moins un demi-pas), au labo, et au stress de 3 c de Gemini
    (mêmes brackets et minimums)."""
    risk = entry - stop
    q = shares(risk)
    plan = IBKR_PLANS.get(IBKR_PLAN, IBKR_PLANS["fixe"])
    parts = _brackets(q, len(ST.TARGETS_R))
    fees = sum(order_commission(n, entry, plan) + order_commission(n, stop, plan) for n in parts) / (q * risk)
    s3 = STRESS_CENTS if entry >= 1 else entry * STRESS_SUB_DOLLAR
    return {"actions": q, "brackets": parts, "reel": (_half_tick_slip(entry) + _half_tick_slip(stop)) / risk + fees,
            "labo": SS.expected_cost_r("stock", entry, stop, None) or 0.0, "stress": 2 * s3 / risk + fees}


def bracket_orders(entry: float, stop: float, targets: list[float]) -> list[tuple[int, float]]:
    """Ce que Simão saisit chez IBKR : les actions de chaque bracket et son objectif, comme le journal les solde
    (`_legs` : avec moins d'actions que d'objectifs, l'objectif du milieu de la part du bracket)."""
    q = shares(entry - stop)
    thirds = [{"prix": p, "part": 1 / len(targets), "raison": f"tp{i}"} for i, p in enumerate(targets, start=1)]
    return [(n, e["prix"]) for n, e in _legs(q, len(targets), thirds)]


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
        orders = bracket_orders(plan["entry"], plan["stop"], plan["targets"])
        q = sum(n for n, _ in orders)
        legs = " · ".join(f"{_n(n, 'action')} → TP{plan['targets'].index(p) + 1} {_fr(p)}" for n, p in orders)
        lines.append(f"📋 Chez IBKR, à {RISK_EUR:g} € de risque : **{_n(q, 'action')} en {_n(len(orders), 'bracket')}** "
                     f"(stop d'achat {_fr(plan['entry'])}, SL {_fr(plan['stop'])} et TP liés, ordres du jour) : "
                     f"**{legs}**. Dès que TP1 ({_fr(plan['targets'][0])}) est touché, remonte les SL restants à "
                     f"l'entrée ; ce qui reste, vends-le avant la clôture ({_paris(plan['close_ts'], now)}, "
                     f"{datetime.fromtimestamp(plan['close_ts'], NEW_YORK).hour} h New York). "
                     "C'est la gestion que le journal mesure.")
        cost = stop_cost(plan["entry"], plan["stop"])
        lines.append(f"-# Un trade stoppé coûte ≈ {_fr(cost['reel'])} R de frais à {RISK_EUR:g} € de risque "
                     f"({_n(len(cost['brackets']), 'bracket')} IBKR {IBKR_PLAN}, minimum par ordre compris) ; labo "
                     f"≈ {_fr(cost['labo'])} R ; avec 3 c de glissement par côté (hypothèse de Gemini, non "
                     f"mesurée) ≈ {_fr(cost['stress'])} R.")
        if plan.get("declenche_a") and a["price"] is not None and a["price"] >= plan["entry"]:
            lines.append(f"-# Entrée déjà touchée à {_paris(plan['declenche_a'], now)} : encore à moins de "
                         f"{str(CHASE_R).replace('.', ',')} R, l'ordre part au prix du moment.")
        elif plan.get("declenche_a"):
            lines.append(f"-# Entrée touchée à {_paris(plan['declenche_a'], now)} puis reperdue : l'ordre stop attend "
                         "un nouveau passage à l'entrée.")
        tp = " ".join(f"{p:.2f}" for p in plan["targets"])
        lines.append(f"-# Pour que JARVIS suive le trade avec cette gestion et que le journal compare ton exécution : "
                     f"`!prends {a['symbol']} <ton prix d'exécution> stop {plan['stop']:.2f} kill tp {tp}`.")
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
    rate = f" ({st['taux'] * 100:.0f} %)" if st["taux"] is not None else ""
    return (f"{label} : {_n(st['soldes'], 'soldé')}, {_n(st['declenches'], 'déclenché')}{rate}, "
            f"{_n(st['expires'], 'expiré')} · espérance {SS._r(st['esperance_r'])} R par trade"
            + (f" [{SS._r(ci[0])} ; {SS._r(ci[1])}]" if ci else "")
            + f", {SS._r(st['par_appel_r'])} R par appel · total {SS._r(st['total_r'])} R · "
            f"{_n(st['gagnants'], 'gagnant')} · TP1 {st['tp1']} fois · {_n(st['jours'], 'séance')}, "
            f"{_n(st['semaines'], 'semaine')}")


def _median(xs: list) -> float | None:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = len(xs) // 2
    return xs[k] if len(xs) % 2 else (xs[k - 1] + xs[k]) / 2


def balance(label: str, rows: list[dict]) -> str:
    """Ce que valent les deux groupes avant le résultat (médianes) : un écart de gap, de prix ou de source
    expliquerait l'écart des résultats mieux que le catalyseur. Descriptif : aucun sous-groupe n'en tire de règle."""
    sip = [((c.get("resultat") or {}).get("sip") or {}).get("vol15_adv") for c in rows]
    scan = sum(1 for c in rows if (c.get("source") or "").startswith("alpaca"))
    vals = [("gap", _median([c.get("gap_pct") for c in rows]), lambda x: ST._pct_fr(x) + " %"),
            ("ATR", _median([c.get("atr_pct") for c in rows]), lambda x: _fr(x, 1) + " %"),
            ("risque", _median([(c["entry"] - c["stop"]) / c["entry"] * 100 for c in rows]),
             lambda x: _fr(x, 1) + " %"),
            ("prix", _median([c["entry"] for c in rows]), lambda x: _fr(x) + " $"),
            ("vol15 SIP", _median(sip), lambda x: _fr(x)), ]
    trig = [c for c in rows if value(c) is not None]
    amb = sum(1 for c in trig if (c["resultat"].get("ambigus") or []))
    return (f"{label} ({len(rows)}) : " + ", ".join(f"{k} {f(v)}" for k, v, f in vals if v is not None)
            + f", scan {scan}/{len(rows)}" + (f", minute ambiguë {amb}/{len(trig)}" if trig else ""))


def risk_line(rows: list[dict]) -> str | None:
    """Le risque réel, jugé sur les prix : pire perte, sorties au-delà du stop actif, entrées loin au-dessus de
    l'ordre (le stop d'achat part au marché), soldes sur des données incomplètes (suspension) et insoldables."""
    trig = [c for c in rows if value(c) is not None]
    uns = sum(1 for c in rows if c.get("insoldable"))
    if not trig and not uns:
        return None
    res = [c["resultat"] for c in trig]
    worst = min((value(c) for c in trig), default=None)
    beyond = sum(1 for r in res if (r.get("sous_stop_r") or 0) >= SLIP_ALERT_R)
    high = sum(1 for r in res if (r.get("entree_r") or 0) > CHASE_R)
    inc = sum(1 for r in res if r.get("donnees_incompletes"))
    return (f"-# Risque réel des KILL : pire résultat {SS._r(worst)} R · {beyond} sortie{'s' if beyond > 1 else ''} "
            f"à {_fr(SLIP_ALERT_R)} R ou plus sous le stop actif · {high} entrée{'s' if high > 1 else ''} à plus de "
            f"{_fr(CHASE_R, 1)} R au-dessus de l'ordre · {inc} soldé{'s' if inc > 1 else ''} sur des données "
            f"incomplètes · {uns} insoldable{'s' if uns > 1 else ''}")


def sip_line(rows: list[dict]) -> str | None:
    """Concordance entre le direct et la règle refaite sur les bougies SIP (tout le marché)."""
    seen = [c["resultat"]["sip"] for c in rows if (c.get("resultat") or {}).get("sip")]
    if not seen:
        return None
    sig, same = sum(s["signal"] for s in seen), sum(s["meme_ordre"] for s in seen)
    return (f"-# Bougies SIP (tout le marché) : {sig}/{len(seen)} KILL ont aussi le signal, {same}/{len(seen)} le même "
            "ordre à un pas près ; un KILL qui ne concorde pas reste compté (c'est ce que tu as reçu).")


def fills(rows: list[dict], trades: list[dict]) -> list[tuple[dict, dict]]:
    """Les KILL que Simão a pris et fait suivre (`!prends … kill`, même action, même séance)."""
    out = []
    for c in rows:
        t = next((t for t in trades if t.get("killmode") and t.get("symbol") == c["symbol"] and t.get("created_at")
                  and datetime.fromisoformat(t["created_at"]).astimezone(NEW_YORK).date().isoformat() == c["day"]),
                 None)
        if t:
            out.append((c, t))
    return out


def fills_line(rows: list[dict], trades: list[dict]) -> str | None:
    """Tes vraies exécutions face au journal : glissement d'entrée en R, et R réalisé des trades terminés."""
    pairs = fills(rows, trades)
    if not pairs:
        return None
    slip = [(t["entry"] - c["entry"]) / (c["entry"] - c["stop"]) for c, t in pairs]
    done = [(T.realized_r(t), value(c)) for c, t in pairs if T.realized_r(t) is not None and value(c) is not None]
    out = (f"-# Tes exécutions : {_n(len(pairs), 'KILL pris')}, entrée {SS._r(sum(slip) / len(slip))} R en moyenne "
           "par rapport à l'ordre")
    if done:
        out += (f" · {_n(len(done), 'terminé')} : {SS._r(sum(r for r, _ in done) / len(done))} R réalisés (sans "
                f"frais) contre {SS._r(sum(j for _, j in done) / len(done))} R au journal")
    return out + "."


def _call_gap_line(cur: list[dict]) -> str | None:
    lines = []
    for scan_only, label in ((True, "appels du scan"), (False, "toutes sources")):
        g = call_gap(cur, scan_only)
        if g["m"] is not None:
            ci = f" [{SS._r(g['ci'][0])} ; {SS._r(g['ci'][1])}]" if g["ci"] else ""
            lines.append(f"{label} {SS._r(g['m'])} R{ci} ({g['n'][0]} KILL, {_n(g['n'][1], 'témoin')})")
    if not lines:
        return None
    return ("-# Écart par appel KILL − témoin propre : " + " · ".join(lines) + ". Descriptif, il ne décide rien : "
            "même à 100 KILL seul un écart de plus de ~0,5 R se verrait. C'est la valeur prédictive du filtre "
            "catalyseur, pas l'effet causal d'une actu.")


def bilan_text(book: list[dict], trades: list[dict] | None = None) -> str:
    """Le journal de la version en vigueur : KILL, témoin propre (ni actu positive ni veto) et autres témoins,
    aux frais réels de Simão, avec l'écart par appel, l'équilibre des groupes, le risque réel, la concordance
    SIP et tes vraies exécutions (descriptifs) ; les versions précédentes à part ; la ligne du labo ; la règle
    de lecture."""
    current = _current()
    versions = sorted({_version(c) for c in book})
    cur = [c for c in book if _version(c) == current]
    trades = trades if trades is not None else _read(T.TRADES_PATH, []) or []
    kills, clean_ = in_group(cur, "kill"), in_group(cur, "temoin_propre")
    n_wit = len(in_group(cur, "temoin_catalyseur"))
    pending = sum(1 for c in cur if not c.get("resultat") and not c.get("insoldable"))
    lines = [f"🔪 **Killmode · bilan** · {_n(len(kills), 'KILL proposé')}, {n_wit} témoin{'s' if n_wit > 1 else ''} "
             f"catalyseur dont {len(clean_)} propre{'s' if len(clean_) > 1 else ''}"
             + (f", {pending} en attente de la clôture" if pending else "")
             + (" · version en vigueur" if len(versions) > 1 else "")]
    if not active():
        lines.append("-# Killmode est éteint : le bilan solde et lit les appels déjà notés, rien d'autre.")
    shown = False
    for g, label in (("kill", GROUPS["kill"]), ("temoin_propre", "Témoin propre (ni actu positive ni veto)"),
                     ("temoin_autre", "Autres témoins (actus négatives, résultats dans 24 h, dilution…)")):
        st = stats(cur, g)
        if st["soldes"]:
            lines.append(_group_line(label, st))
            shown = True
    if shown:
        lines.append(f"-# Aux frais réels : {RISK_EUR:g} € de risque, un bracket IBKR par objectif en actions entières "
                     "(minimum par ordre, plan " + IBKR_PLAN + "), glissement du labo d'au moins un demi-pas ; soldé en "
                     "bougies d'1 min, ordre actif une minute pleine après l'appel, minute ambiguë dans le pire ordre. "
                     f"IC 95 % en t, indicatif avant {READ_AT} KILL.")
    for line in (errors_line(cur), _call_gap_line(cur), stress_line(cur), ambiguous_line(kills), risk_line(kills),
                 sip_line(kills), fills_line(kills, trades)):
        if line:
            lines.append(line)
    if kills and clean_:
        lines.append("-# Équilibre, médianes · " + balance("KILL", kills) + " · " + balance("témoin propre", clean_))
    moves = [c for c in kills if c.get("motif") == "mouvement" and value(c) is not None]
    if moves:
        lines.append(f"-# KILL portés seulement par un titre sur le cours (« shares jump ») : {len(moves)}, "
                     f"{SS._r(sum(value(c) for c in moves) / len(moves))} R par trade (descriptif).")
    for v in versions:
        if v == current:
            continue
        sub = [c for c in book if _version(c) == v]
        for g in GROUPS:
            st = stats(sub, g, "r_ibkr")
            if st["declenches"]:
                lines.append(f"-# Ancienne {_version_label(v)}, frais du labo · " + _group_line(GROUPS[g], st))
    for c in [c for c in cur if c.get("resultat")][-8:]:
        res = c["resultat"]
        out = f"{SS._r(value(c))} R ({res['sortie']})" if value(c) is not None else "pas déclenché"
        tag = "" if _group_of(c) == "kill" else " (témoin propre)" if clean(c) else " (témoin)"
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
`!kill bilan`              KILL et témoins catalyseur soldés après la clôture aux frais réels (20 €), la stratégie
                           `killmode` du labo et la règle de lecture (audit au 30e KILL, lecture finale au 100e) ;
                           marche aussi Killmode éteint
`!prends NVDA 132.6 kill`  suivre un KILL pris (ton prix d'exécution ; stop et objectifs du KILL du jour si tu
                           ne les donnes pas) avec sa gestion (SL à l'entrée dès TP1, vendu avant la clôture) :
                           hors de `!desaccords`, comparé au journal
`!kill etat`               allumé ou éteint"""


async def command(args: list[str], now: datetime | None = None) -> str:
    """`!kill …` : interrupteur, analyse, scan, bilan. L'analyse et le scan exigent Killmode allumé ; le bilan,
    l'état et l'aide marchent aussi éteint."""
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
    if sub == "bilan":  # permis éteint : solder et lire les appels déjà notés n'allume rien
        await settle_due(now)
        return bilan_text(calls())
    if not active():
        return "Killmode est éteint. `!kill on` pour l'allumer (toi seul peux le faire)."
    if sub == "scan":
        r = await scan(now)
        for a in r["setups"] + r["dropped"]:  # les écartées peuvent être des témoins catalyseur
            record(a, now)
        log_scan(r)
        return scan_text(r, lab_line(lab_item()) if r["setups"] else None)
    a = await analyze(args[0], now)
    record(a, now)
    return text(a, lab_line(lab_item()) if a["verdict"] in ("kill", "pret") else None)
