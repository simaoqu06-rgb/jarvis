"""Worker du labo : `python -m app.lab.worker`, service docker séparé (jamais dans le serveur web).

Toutes les ~10 min :
1. en direct : chaque séance US terminée depuis 30 min est journalisée (origin = live), et les
   créneaux crypto dont l'horizon de 24 h est passé ;
2. rejeu historique, par morceaux : gros morceaux la nuit (22 h - 8 h à Paris) et le week-end,
   un seul jour à la fois pendant la séance US pour que le serveur reste réactif.
3. Labo 2, chaque étape isolée (une erreur n'arrête pas les suivantes), battement entre deux :
   régimes de marché, mouvements du jour et fiches catalyseurs (base seule), actus Finnhub des fiches
   (jamais pendant la séance US), file des règles soumises puis validation, statistiques en cache,
   résumé du soir (après 22 h 30 à Paris). Budgets par période dans LAB2_BUDGET : gros la nuit et
   le week-end, minimes pendant la séance.
4. Labo 3 (étape « methode ») : décisions de Simão et types de catalyseurs déposés par le serveur,
   challenger, regards fixes (promotion proposée à Simão) ou retrait, fichier du champion lu par JARVIS en direct.
5. Carnet des leçons (étape « lecons », lessons.py) : leçons déposées par la routine du soir,
   re-mesurées quand les trades changent, fichier des leçons confirmées lu par JARVIS en direct.
6. Grade de JARVIS (étape « grade », grade.py) : avis du council et décisions de Simão, grade de
   chaque marché recalculé quand les trades changent, fichier lab_grade.json lu en direct. Aussi
   en tête de chaque passage : le grade n'attend pas une collecte longue du rejeu.
7. Labo v2 (strategies.py) : les stratégies à déclencheur sont calculées dans le même passage que v1
   (runner.run_day) ; les jours traités avant elles sont rattrapés hors séance (rattrapage.py, bougies
   relues et gardées en mémoire) ; leur tableau (strat_stats.py) est mis en cache à l'étape « strategies ».
8. Lot B : « potentiel » (potentiel.py, part des séances qui touchent +8 % selon le gap, fichier
   lab_potentiel.json lu par le scanner) et « derives » (derivs.py, funding et open interest Bybit, lus
   par le tableau des stratégies crypto), dans le budget derivs_calls de la période.
9. Déclaration n° 6 : « couverture » (universe.coverage_step), part des actions ouvertes en gap ≥ 4 % avec un
   ATR ≥ 4 % présentes dans l'univers du labo, quelques séances par passage (coverage_days).
10. Déclaration n° 8, D1 : « archive » (archive.py), actus Finnhub d'avant 9 h 45 des actions en gap, calendrier
   des résultats et profils, la séance la plus ancienne d'abord (Finnhub gratuit perd une séance par jour),
   archivés sans être lus, hors séance US, archive_seconds et archive_days par passage.
11. Déclaration n° 8 : « cout » (cout.py), R net des trades v1 d'actions recalculé une fois au coût du demi-pas
   (D3) ; « killmode_complet » (kill_complet.py), séances dont l'archive est complète, bougies Alpaca relues et
   gardées en mémoire, hors séance US, kill_complet_seconds par passage.
12. Déclaration n° 9 : « potentiel_plus10 » (plus10.py), listes S+10 et Rebond sur toute la base, bougies de 5 min
   non ajustées relues et gardées en mémoire, hors séance US, plus10_seconds par passage.
13. « autopsie » (autopsie.py) : où les trades échouent (MFE/MAE contre le hasard) et grade indicatif des GO
   confirmés, recalculés quand les trades changent, au plus toutes les stats_age secondes.
14. « filtres » (filtres.py) : ce que coûte chaque filtre du scanner de 15 h 10 sur les trades v1 d'actions,
   recalculé quand les trades ou les réglages changent, au plus toutes les stats_age secondes, hors séance US.
15. Garde du Labo 1 (deuxième tour d'avis du 10 octobre 2026) : au démarrage, l'empreinte du comportement v1
   (empreinte.v1_guard, ≈ 1 s) est comparée à V1_FINGERPRINT. Écart ou calcul en échec : rejeu et direct du Labo 1
   en pause (aucune ligne v1 écrite, ni le recalcul des coûts), journal d'erreur, événement du labo et alerte dans
   !labo ; le Labo 2, le rattrapage et le grade continuent. Un calcul en échec est retenté à chaque passage.
16. « recalcul » (recalcul_v1) : séances du direct calculées avant une rupture de comportement (empreinte.V1_RECALCULS,
   la séance d'actions du 06/10/2026), recalculées une fois avec le code actuel en tête de passage, avant le grade.
Le battement (lab.heartbeat) porte l'étape en cours, affichée par /api/lab/status et !labo.
Le rejeu couvre JARVIS_LAB_REPLAY_DAYS séances (504 ≈ 2 ans) avant le premier lancement du worker ;
le direct prend le relais à partir de ce jour-là. JARVIS_LAB_ENABLED=off met le worker en veille.
"""
import asyncio
import logging
import os
import time as clock
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.core.http import SourceError
from app.lab import archive as ARC
from app.lab import catalysts as CAT
from app.lab import collector as CO
from app.lab import cout as COUT
from app.lab import derivs as DER
from app.lab import empreinte as E
from app.lab import filtres as FIL
from app.lab import generator as G
from app.lab import grade as GRADE
from app.lab import heartbeat
from app.lab import kill_complet as KC
from app.lab import lessons as L
from app.lab import plus10 as P10
from app.lab import method as M
from app.lab import potentiel as PO
from app.lab import autopsie as AUT
from app.lab import rattrapage as RAT
from app.lab import regime as REG
from app.lab import runner as R
from app.lab import stats as STATS
from app.lab import strat_stats as SS
from app.lab import strategies as ST
from app.lab import summary as SUM
from app.lab import universe as U
from app.lab import validator as V
from app.lab.db import LabDB, now_iso
from app.sources import alpaca

log = logging.getLogger("app.lab.worker")
PARIS = ZoneInfo("Europe/Paris")
TICK_SECONDS = 600
CHUNK_DAYS = {"nuit": 20, "jour": 4, "seance": 1}
# Durée maximale du rejeu par marché et par passage (secondes) : un jour d'actions coûte 2 à 5 min ;
# sans borne, 20 jours la nuit retenaient la crypto et le Labo 2 plus d'une heure, et une mise à
# jour du serveur coupait le passage avant qu'ils ne tournent.
REPLAY_SECONDS = {"nuit": 900, "jour": 300, "seance": 120}
# Labo v2 : rattrapage des stratégies sur les jours traités avant lui (rattrapage.py), bougies relues à la
# source et gardées en mémoire ; jamais pendant la séance US.
RATTRAPAGE_SECONDS = {"nuit": 600, "jour": 120, "seance": 0}
MAX_LIVE_DAYS_PER_TICK = 5
KEY_WARNING_SECONDS = 3600
# Labo 2 : jours de mouvements calculés, appels Finnhub, règles validées et âge maximal des
# statistiques en cache (secondes), par période.
LAB2_BUDGET = {
    "nuit": {"moves_days": 30, "news_calls": 40, "rules": 3, "stats_age": 3600, "derivs_calls": 300,
             "coverage_days": 60, "archive_seconds": 1200, "archive_days": 40, "kill_complet_seconds": 600,
             "plus10_seconds": 600},
    "jour": {"moves_days": 5, "news_calls": 10, "rules": 1, "stats_age": 3 * 3600, "derivs_calls": 100,
             "coverage_days": 10, "archive_seconds": 600, "archive_days": 10, "kill_complet_seconds": 120,
             "plus10_seconds": 120},
    "seance": {"moves_days": 2, "news_calls": 0, "rules": 0, "stats_age": 6 * 3600, "derivs_calls": 30,
               "coverage_days": 2, "archive_seconds": 0, "archive_days": 2, "kill_complet_seconds": 0,
               "plus10_seconds": 0},
}
_key_warned_at: float | None = None


def configure_logging() -> None:
    """Comme app/main.py : INFO avec l'heure, httpx et httpcore en WARNING (leurs URL contiennent des clés)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s : %(message)s")
    logging.getLogger().setLevel(logging.INFO)
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def enabled() -> bool:
    return os.getenv("JARVIS_LAB_ENABLED", "on").strip().lower() not in ("0", "off", "false", "non", "faux")


def replay_sessions() -> int:
    try:
        return max(1, int(os.getenv("JARVIS_LAB_REPLAY_DAYS", "504")))
    except ValueError:
        return 504


# --- Décisions (fonctions pures) -----------------------------------------------------------

def period(now: datetime) -> str:
    """« nuit » : 22 h - 8 h à Paris ou week-end ; « seance » : Bourse de New York ouverte ; sinon « jour »."""
    local = now.astimezone(PARIS)
    if local.weekday() >= 5 or local.hour >= 22 or local.hour < 8:
        return "nuit"
    if CO.CAL.session_open(now):
        return "seance"
    return "jour"


def replay_seconds(now: datetime, summary_last_day: str | None) -> float:
    """Durée permise au rejeu d'un marché à ce passage : REPLAY_SECONDS de la période, sans déborder
    sur l'heure du résumé du soir (22 h 30 à Paris) tant qu'il n'est pas fait, pour qu'il soit prêt
    avant la routine du soir (22 h 52)."""
    box = float(REPLAY_SECONDS[period(now)])
    local = now.astimezone(PARIS)
    target = datetime.combine(local.date(), SUM.SUMMARY_AT, PARIS)
    if summary_last_day != local.date().isoformat() and local < target:
        box = min(box, (target - local).total_seconds())
    return box


def rattrapage_seconds(now: datetime, summary_last_day: str | None) -> float:
    """Durée permise au rattrapage du labo v2 d'un marché à ce passage : RATTRAPAGE_SECONDS de la période, sans déborder
    sur l'heure du résumé du soir tant qu'il n'est pas fait, comme le rejeu (replay_seconds). Depuis la n° 10, il a des
    centaines de jours devant lui et prend tout son budget à chaque passage : 2 marchés × 10 min juste avant 22 h 30
    repoussaient le résumé après la routine du soir."""
    return min(float(RATTRAPAGE_SECONDS[period(now)]), replay_seconds(now, summary_last_day))


def replay_budget(now: datetime) -> int:
    """Nombre de jours de rejeu par passage et par marché."""
    return CHUNK_DAYS[period(now)]


def replay_bounds(live_start: date, sessions: int) -> tuple[date, date]:
    """Du jour de la `sessions`-ième séance avant le premier lancement à la veille de ce lancement."""
    return CO.sessions_back(live_start, sessions), live_start - timedelta(days=1)


def live_days_due(market: str, last_done: date | None, live_start: date, now: datetime) -> list[date]:
    """Jours à journaliser en direct : actions, chaque séance close depuis 30 min ; crypto, chaque
    jour dont au moins un créneau a atteint son horizon (le jour en cours sera complété plus tard)."""
    day = last_done + timedelta(days=1) if last_done else live_start
    out = []
    while day <= now.date() and len(out) < MAX_LIVE_DAYS_PER_TICK:
        if market == "stock":
            if CO.session_ts(day):
                if not R.day_ready("stock", day, now):
                    break
                out.append(day)
        else:
            if G.crypto_moments(day)[0] + 86400 > now.timestamp():
                break
            out.append(day)
        day += timedelta(days=1)
    return out


def stock_pause(now: datetime, collect: bool = True) -> str | None:
    """Raison de laisser les actions de côté à ce passage (None = on y va).

    Pendant la séance régulière, la limite Alpaca (200 requêtes/min) sert à JARVIS en direct
    (scanner, forward-test) : le labo n'y touche pas, la crypto continue. Sans clés Alpaca, la
    crypto tourne seule."""
    if CO.CAL.session_open(now):
        return "séance US en cours"
    if collect and not alpaca.configured():
        return "clés Alpaca absentes"
    return None


def warn_missing_keys(now_mono: float | None = None) -> bool:
    """Une seule ligne d'avertissement par heure (pas de pile d'appels). True si elle est écrite."""
    global _key_warned_at
    now_mono = clock.monotonic() if now_mono is None else now_mono
    if _key_warned_at is not None and now_mono - _key_warned_at < KEY_WARNING_SECONDS:
        return False
    _key_warned_at = now_mono
    log.warning("Labo : clés Alpaca absentes (ALPACA_API_KEY / ALPACA_SECRET_KEY), actions en pause ; la crypto continue")
    return True


def crypto_windows_due(day: date, now: datetime, done_until: int | None) -> bool:
    """Un nouveau créneau crypto du jour a-t-il atteint son horizon depuis le dernier passage ? Un jour complet
    encore à journaliser (son calcul complet a échoué) l'est toujours : nouvel essai, puis sauté au 3e (I4)."""
    due = [m for m in G.crypto_moments(day) if m + 86400 <= now.timestamp()]
    return bool(due) and (done_until is None or due[-1] > done_until or R.day_ready("crypto", day, now))


def _history_start(market: str, day: date, stock_sessions: int, crypto_days: int) -> int:
    if market == "stock":
        return CO.session_ts(CO.sessions_back(day, stock_sessions))[0]
    return CO.utc_midnight(day) - crypto_days * 86400


def prune_cutoff(db: LabDB, market: str, now: datetime) -> int | None:
    """Plus ancienne bougie de 5 min encore utile : fenêtre du prochain jour rejoué (120 séances ou
    40 jours), fenêtre du direct, et historique des jours dont les mouvements restent à calculer.
    None : rien à effacer de sûr."""
    today = now.astimezone(CO.NEW_YORK).date() if market == "stock" else now.astimezone(timezone.utc).date()
    cuts = [_history_start(market, today, CO.STOCK_HISTORY_SESSIONS + 2, CO.CRYPTO_HISTORY_DAYS + 2)]
    job = f"replay:{market}"
    start, end, last = db.get(job, "start"), db.get(job, "end"), db.get(job, "last_day")
    if start and not (last and last >= end):
        cursor = date.fromisoformat(last) if last else date.fromisoformat(start)
        cuts.append(_history_start(market, cursor, CO.STOCK_HISTORY_SESSIONS + 1, CO.CRYPTO_HISTORY_DAYS + 1))
    row = db.conn.execute(
        "SELECT MIN(u.day) FROM universe u WHERE u.market = ? AND NOT EXISTS "
        "(SELECT 1 FROM progress p WHERE p.job = ? AND p.key = u.day)", (market, f"moves:{market}")).fetchone()
    if row and row[0]:
        cuts.append(_history_start(market, date.fromisoformat(row[0]), CAT.RVOL_SESSIONS + 2, CAT.RVOL_SESSIONS + 2))
    return min(cuts)


def summary_first(now: datetime, summary_last_day: str | None) -> bool:
    """Le résumé du soir est attendu à ce passage : le Labo 2 passe avant le rejeu."""
    return SUM.due(now, summary_last_day) is not None


def market_order(db: LabDB, markets: list[str]) -> list[str]:
    """Le rejeu alterne le marché qui passe en premier : un marché lent (journalier de tout l'univers
    au premier passage, collecte) n'affame plus l'autre."""
    if len(markets) < 2:
        return markets
    first = db.get("replay", "first")
    order = markets[1:] + markets[:1] if first == markets[0] else list(markets)
    db.set("replay", "first", order[0])
    return order


# --- Garde du Labo 1 (deuxième tour d'avis du 10 octobre 2026) --------------------------------

# État de la garde de ce processus (empreinte.v1_guard), posé par guard_v1 au démarrage du worker ; None (tests qui
# appellent tick directement) : pas de garde, le Labo 1 écrit.
_v1_guard: dict | None = None


def guard_v1(db: LabDB, now: datetime | None = None) -> dict:
    """Calcule l'empreinte du comportement v1 du code qui tourne et la compare à V1_FINGERPRINT (≈ 1 s).

    Écart (« change ») ou calcul en échec (« erreur ») : le Labo 1 se met en pause (v1_paused) au lieu d'écrire des lignes
    d'un autre comportement sous la même version ; le calcul en échec aussi, car une pause ne perd rien (le direct et le
    rejeu reprennent les jours en attente, les bougies restent chez la source) alors qu'une ligne v1 d'un comportement
    non vérifié reste dans les statistiques et le grade. Il est retenté à chaque passage (tick) : une erreur passagère
    lève la pause d'elle-même. L'état est gardé dans progress (« lab », « garde_v1 ») pour !labo et /api/lab/status,
    l'empreinte de la révision du code dans « empreintes_v1 » (stats.code_revisions) ; un changement d'état est un
    événement du labo, relayé sur Discord."""
    global _v1_guard
    now = now or datetime.now(timezone.utc)
    state = E.v1_guard() | {"le": now.isoformat(timespec="seconds"), "code_rev": E.code_rev("v1")}
    _v1_guard = state  # avant toute écriture : la pause tient même si la base refuse l'état
    prev = db.get("lab", "garde_v1")
    key = lambda s: (s.get("etat"), s.get("calculee"), s.get("erreur"))  # noqa: E731
    with db.transaction():
        db.set("lab", "garde_v1", state)
        if state["calculee"]:
            db.set("empreintes_v1", state["code_rev"], state["calculee"])
        alert = E.guard_text(state)
        if alert and (not prev or key(prev) != key(state)):
            M.event(db, "labo1_pause", None, alert + " : rejeu et direct du Labo 1 arrêtés, aucune ligne v1 écrite ; "
                    "Labo 2, rattrapage et grade continuent. Revenir en arrière, ou ouvrir une nouvelle version de "
                    "méthode avec sa nouvelle empreinte.", state, now)
        elif not alert and prev and prev.get("etat") != "ok":
            M.event(db, "labo1_reprise", None, f"✅ Labo 1 repris : empreinte v1 {state['calculee']} conforme, rejeu et "
                    "direct relancés.", state, now)
    if alert:
        log.error("Labo : %s ; rejeu et direct du Labo 1 en pause", alert.removeprefix("⚠️ "))
    else:
        log.info("Labo : empreinte v1 %s conforme", state["calculee"])
    return state


def v1_paused(db: LabDB | None = None, now: datetime | None = None) -> str | None:
    """Raison de la pause du Labo 1 (None : il écrit). Avec `db`, un calcul d'empreinte en échec est retenté."""
    if db is not None and _v1_guard and _v1_guard.get("etat") == "erreur":
        try:
            guard_v1(db, now)
        except Exception as exc:  # la base refuse l'état : la pause calculée tient
            log.warning("Labo : garde du Labo 1 non enregistrée : %s: %s", type(exc).__name__, exc)
    return E.guard_text(_v1_guard)


# --- Recalcul ponctuel des séances du direct d'avant une rupture (deuxième tour d'avis) --------

RECALCUL_JOB = "recalcul"


def recalcul_key(market: str, avant: str, day: str) -> str:
    return f"v1:{avant}:{market}:live:{day}"


def _live_rows(db: LabDB, market: str, day: str) -> tuple[int, int]:
    """Trades du direct du jour : nombre, et nombre sans révision du code (calculés avant le suivi)."""
    return db.conn.execute("SELECT COUNT(*), COUNT(*) - COUNT(code_rev) FROM trades WHERE origin = 'live' "
                           "AND market = ? AND day = ?", (market, day)).fetchone()


async def recalcul_v1(db: LabDB, now: datetime, transport=None) -> dict:
    """Recalcule une fois, avec le code actuel, chaque séance du direct calculée avant une rupture de comportement
    (empreinte.V1_RECALCULS). Idempotent : l'état final (fait, sans objet, non recalculable) est gardé dans progress
    (job « recalcul », clé « v1:<révision>:<marché>:live:<jour> ») et le jour n'est plus jamais relu. Actions seulement
    (la seule rupture listée). Renvoie l'état de chaque séance traitée à ce passage."""
    out = {}
    for market, avant, day in E.V1_RECALCULS:
        key = recalcul_key(market, avant, day)
        if db.get(RECALCUL_JOB, key) or market != "stock":
            continue
        heartbeat.beat(f"recalcul v1 {market} {day}")
        out[key] = await _recalcul_day(db, market, avant, date.fromisoformat(day), now, transport)
        heartbeat.beat()
    return out


async def _recalcul_day(db: LabDB, market: str, avant: str, day: date, now: datetime, transport=None) -> dict:
    """Une séance d'actions du direct, comme la calcule le direct (bougies de 5 min de la base complétées à la source,
    runner.run_day sur l'univers du jour gardé), sans toucher son avancement (last_day).

    - Plus aucune ligne sans révision (déjà recalculée, ou pas encore journalisée : le direct la fera avec ce code) :
      sans objet.
    - Bougies de 5 min manquantes (purgées puis refusées par la source, univers absent) : rien n'est effacé, nouvel
      essai au passage suivant ; au 3e échec (runner.MAX_DAY_ATTEMPTS), « non recalculable », dit dans !labo.
    - replace_trades réécrit les trades en place : mêmes id pour un même symbole (leçons, règles et grade bornent ce
      qu'ils ont vu par jour, le jour reste « vu »), révision du code et heure du calcul neuves. run_day efface aussi
      les lignes du labo v2 du jour (n° 10 : le générateur n'en écrit pas pour les actions) : si le rattrapage de la
      déclaration en vigueur a déjà passé ce jour du direct, ses lignes sont refaites depuis les nouveaux plans, dans
      la même transaction (rattrapage.stock_rows, prix négociés : réseau) ; sinon le rattrapage les bâtira."""
    iso = day.isoformat()
    key = recalcul_key(market, avant, iso)
    total, unknown = _live_rows(db, market, iso)
    base = {"marche": market, "avant": avant, "jour": iso, "le": now.isoformat(timespec="seconds")}
    if not unknown:
        state = base | {"etat": "sans_objet", "trades": total}
        db.set(RECALCUL_JOB, key, state)
        log.info("Labo : recalcul v1 %s %s sans objet (%d trades, tous du code suivi)", market, iso, total)
        return state
    tries = (db.get(RECALCUL_JOB, f"attempts:{key}", 0) or 0) + 1
    last = tries >= R.MAX_DAY_ATTEMPTS
    try:
        universe = db.universe(market, iso)
        if not universe:
            raise SourceError("univers du jour absent")
        names = [u["symbol"] for u in universe]
        failed: set = set()
        await CO.ensure_intraday(db, market, names, *CO.stock_window(day), now, transport, failed_out=failed)
        if failed & set(names):
            lost = sorted(failed & set(names))
            raise SourceError(f"{len(lost)} symboles sans leurs bougies de 5 min : {', '.join(lost[:5])}")
        job_v2 = ST.progress_job(market)
        cursor = db.get(job_v2, "rattrapage:live")
        with db.transaction():
            n = R.run_day(db, market, day, "live", universe, now=now)
            v2 = "laissées au rattrapage"
            if cursor and cursor >= iso:
                v2 = await _redo_v2(db, market, day, job_v2, transport, last)
            state = base | {"etat": "fait", "trades": n, "trades_avant": total, "sans_revision_avant": unknown,
                            "code_rev": E.code_rev("v1"), "labo_v2": v2}
            db.set(RECALCUL_JOB, key, state)
    except Exception as exc:  # données manquantes, source en panne… : 3 essais, rien n'est effacé
        reason = f"{type(exc).__name__}: {exc}"[:300]
        with db.transaction():
            db.set(RECALCUL_JOB, f"attempts:{key}", tries)
            if last:
                state = base | {"etat": "non_recalculable", "raison": reason, "essais": tries}
                db.set(RECALCUL_JOB, key, state)
        if last:
            log.error("Labo : recalcul v1 %s %s non recalculable après %d essais : %s", market, iso, tries, reason)
            return state
        log.warning("Labo : recalcul v1 %s %s en échec (essai %d/%d) : %s", market, iso, tries,
                    R.MAX_DAY_ATTEMPTS, reason)
        return base | {"etat": "en_echec", "raison": reason, "essais": tries}
    log.info("Labo : recalcul v1 %s %s fait, %d trades (%d sans révision avant), labo v2 : %s", market, iso, n, unknown, v2)
    return state


async def _redo_v2(db: LabDB, market: str, day: date, job: str, transport, last: bool) -> str:
    """Lignes du labo v2 d'un jour du direct déjà rattrapé, refaites comme rattrapage.step le fait pour un jour (dans la
    transaction de l'appelant) : au dernier essai, sans les lots en échec à la source, et sautées si la source est en
    panne (lignes effacées, raison gardée sous skipped:live:<jour>, comme le rattrapage)."""
    iso = day.isoformat()
    missing = set() if last else None
    try:
        rows = await RAT.stock_rows(db, "live", day, transport, missing)
    except Exception as exc:
        if not last:
            raise  # tout est annulé, nouvel essai au passage suivant
        db.replace_strat("live", market, iso, [])
        db.set(job, f"skipped:live:{iso}", f"{type(exc).__name__}: {exc}"[:300])
        return "sautées (source en panne)"
    db.replace_strat("live", market, iso, rows or [])
    db.conn.execute("DELETE FROM progress WHERE job = ? AND key IN (?, ?)", (job, f"skipped:live:{iso}", f"missing:live:{iso}"))
    if missing:
        db.set(job, f"missing:live:{iso}", {"missing": len(missing), "missing_symbols": sorted(missing)[:10]})
    return f"refaites ({len(rows or [])} lignes)"


# --- Boucle -------------------------------------------------------------------------------

async def tick(db: LabDB, now: datetime | None = None, collect: bool = True, transport=None) -> dict:
    now = now or datetime.now(timezone.utc)
    live_start = db.get("lab", "live_start")
    if not live_start:
        live_start = now.astimezone(CO.NEW_YORK).date().isoformat()
        db.set("lab", "live_start", live_start)
    live_start = date.fromisoformat(live_start)
    R.method(db)
    report: dict = {"live": {}, "replay": {}, "paused": {}}
    pause = stock_pause(now, collect)
    if pause:
        report["paused"]["stock"] = pause
        if pause.startswith("clés"):
            warn_missing_keys()
    markets = [m for m in R.MARKETS if not (m == "stock" and pause)]
    # Garde du Labo 1 : comportement v1 changé ou non vérifié, ni direct ni rejeu (aucune ligne v1 écrite).
    paused_v1 = v1_paused(db, now)
    if paused_v1:
        report["paused"]["labo1"] = paused_v1
    elif collect and not pause and E.V1_RECALCULS:
        # Séances du direct d'avant une rupture, recalculées une fois avant le grade (bougies complétées à la source :
        # jamais pendant la séance US).
        try:
            report["recalcul"] = await recalcul_v1(db, now, transport)
        except Exception as exc:
            log.warning("Labo : recalcul v1 en échec : %s: %s", type(exc).__name__, exc)
    v1_markets = [] if paused_v1 else markets
    # Grade en tête de passage (lecture de la base, sans réseau, < 1 s) : il ne doit pas attendre derrière
    # une collecte longue du rejeu (journalier de tout l'univers crypto au premier passage…).
    heartbeat.beat("grade")
    try:
        report["grade"] = GRADE.step(db, now)
    except Exception as exc:
        log.warning("Labo : grade en échec : %s: %s", type(exc).__name__, exc)
    for market in v1_markets:
        heartbeat.beat(f"direct {market}")
        try:
            last = db.get(f"live:{market}", "last_day")
            for day in live_days_due(market, date.fromisoformat(last) if last else None, live_start, now):
                if market == "crypto":
                    key = day.isoformat()
                    if not crypto_windows_due(day, now, db.get("live:crypto", f"windows:{key}")):
                        continue
                n = await R.run_live_day(day, db, (market,), collect, now, transport)
                if market == "crypto":
                    due = [m for m in G.crypto_moments(day) if m + 86400 <= now.timestamp()]
                    db.set("live:crypto", f"windows:{day.isoformat()}", due[-1])
                report["live"][f"{market}:{day}"] = n.get(market, 0)
                heartbeat.beat()
        except Exception as exc:
            log.warning("Labo : direct %s en échec : %s: %s", market, type(exc).__name__, exc)
    start, end = replay_bounds(live_start, replay_sessions())
    budget = replay_budget(now)
    started = clock.monotonic()

    def current() -> datetime:  # `now` avancé du temps déjà passé dans ce passage
        return now + timedelta(seconds=clock.monotonic() - started)

    # Résumé du soir attendu : le Labo 2 (qui le construit) passe avant le rejeu, sinon 2 × 15 min de
    # rejeu le repoussaient après la routine de 22 h 52, voire après minuit (résumé perdu).
    if summary_first(now, db.get("summary", "last_day")):
        report["labo2"] = await lab2(db, current(), collect)
    for market in market_order(db, v1_markets):
        heartbeat.beat(f"rejeu {market}")
        try:
            report["replay"][market] = await R.replay(
                start, end, market, db, collect, budget, now, transport,
                max_seconds=replay_seconds(current(), db.get("summary", "last_day")))
        except Exception as exc:
            log.warning("Labo : rejeu %s en échec : %s: %s", market, type(exc).__name__, exc)
        heartbeat.beat()
    if "labo2" not in report:
        report["labo2"] = await lab2(db, current(), collect)
    if collect and RATTRAPAGE_SECONDS[period(current())]:
        report["rattrapage"] = {}
        for market in markets:
            seconds = rattrapage_seconds(current(), db.get("summary", "last_day"))
            if not seconds:
                continue
            heartbeat.beat(f"labo v2 : rattrapage {market}")
            try:
                report["rattrapage"][market] = await RAT.step(db, market, seconds, transport)
            except Exception as exc:
                log.warning("Labo v2 : rattrapage %s en échec : %s: %s", market, type(exc).__name__, exc)
    report["menage"] = {}
    heartbeat.beat("ménage")
    for market in R.MARKETS:
        try:
            cutoff = prune_cutoff(db, market, current())
            if cutoff is not None:
                report["menage"][market] = CO.prune_intraday(db, market, cutoff)
        except Exception as exc:
            log.warning("Labo : ménage %s en échec : %s: %s", market, type(exc).__name__, exc)
    heartbeat.beat()
    return report


def trades_signature(db: LabDB) -> list:
    """COUNT et MAX(id) des trades, compteur rev:trades (un trade réécrit sur place) et empreinte du code des stats."""
    row = db.conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM trades").fetchone()
    return [row[0], row[1], *E.sig(db.conn, "trades")]


def refresh_stats(db: LabDB, now: datetime, max_age: float, force: bool = False) -> dict | None:
    """Recalcule les statistiques de la version en cours si les trades ont changé et que le cache a
    plus de `max_age` secondes (ou `force`, ou un autre code : déploiement). Renvoie les statistiques à jour,
    ou None si inchangées."""
    sig = trades_signature(db)
    last = db.get("stats", "signature")
    at = db.get("stats", "at", 0) or 0
    if not force and (sig == last or (now.timestamp() - at < max_age and not E.code_changed(last))):
        return None
    version = V.current_version(db.conn)
    data = STATS.compute(db.conn, version=version) if version else STATS.compute(db.conn)
    with db.transaction():
        STATS.store(db.conn, "courant", data, sig[0], now_iso())
        db.set("stats", "signature", sig)
        db.set("stats", "at", now.timestamp())
    return data


def refresh_strategies(db: LabDB, now: datetime, max_age: float) -> int | None:
    """Labo v2 : tableau des stratégies et diagnostic v1 (strat_stats), recalculés quand les lignes ont
    changé et que le cache a plus de `max_age` secondes (tout de suite après un déploiement). Renvoie le nombre
    de lignes, ou None."""
    sig = SS.signature(db.conn)
    last = db.get("strats", "signature")
    at = db.get("strats", "at", 0) or 0
    if sig == last or (SS.cached(db.conn) and now.timestamp() - at < max_age and not E.code_changed(last)):
        return None
    data = SS.report(db.conn)
    with db.transaction():
        STATS.store(db.conn, SS.CACHE_KEY, data, sig[0], now_iso())
        db.set("strats", "signature", sig)
        db.set("strats", "at", now.timestamp())
    return sig[0]


async def lab2(db: LabDB, now: datetime, collect: bool = True) -> dict:
    """Étapes du Labo 2, chacune isolée, dans le budget de la période."""
    budget = LAB2_BUDGET[period(now)]
    report: dict = {}
    fresh: dict = {}

    async def step(name: str, fn):
        heartbeat.beat(f"labo 2 : {name}")
        try:
            result = fn()
            if asyncio.iscoroutine(result):
                result = await result
            report[name] = result if not isinstance(result, (list, dict)) or name != "stats" else "à jour"
            return result
        except Exception as exc:
            log.warning("Labo : étape %s en échec : %s: %s", name, type(exc).__name__, exc)
            report[name] = f"échec : {type(exc).__name__}"
            return None
        finally:
            heartbeat.beat()

    await step("regimes", lambda: sum(REG.refresh(db, m) for m in R.MARKETS))
    await step("mouvements", lambda: CAT.backfill_moves(db, now, budget["moves_days"]))
    await step("potentiel", lambda: PO.step(db))
    await step("couverture", lambda: U.coverage_step(db, budget["coverage_days"]))
    await step("archive", lambda: ARC.step(db, now, budget["archive_seconds"], collect,
                                                budget["archive_days"]))
    # Recalcul des coûts v1 (n° 8) : il réécrit des lignes v1, en pause avec le Labo 1 (garde).
    await step("cout", lambda: "en pause (garde du Labo 1)" if v1_paused() else COUT.step(db))
    if collect:
        await step("killmode_complet", lambda: KC.step(db, budget["kill_complet_seconds"]))
        await step("potentiel_plus10", lambda: P10.step(db, now, budget["plus10_seconds"]))
        await step("derives", lambda: DER.collect(db, now, budget["derivs_calls"]))
    if collect and not CO.CAL.session_open(now):
        await step("actus", lambda: CAT.fetch_news(db, now, budget["news_calls"]))
    await step("file_regles", lambda: V.ingest_queue(db))
    await step("regles", lambda: len(V.process_pending(db, budget["rules"])))
    await step("methode", lambda: M.step(db, now))
    await step("lecons", lambda: L.step(db, now, budget["stats_age"]))
    await step("grade", lambda: GRADE.step(db, now))
    await step("strategies", lambda: refresh_strategies(db, now, budget["stats_age"]))
    await step("autopsie", lambda: AUT.step(db, now, budget["stats_age"]))
    if not CO.CAL.session_open(now):  # un journalier relu par action : pas pendant la séance US
        await step("filtres", lambda: FIL.step(db, now, budget["stats_age"]))
    summary_day = SUM.due(now, db.get("summary", "last_day"))
    stats = await step("stats", lambda: refresh_stats(db, now, budget["stats_age"], force=bool(summary_day)))
    if stats:
        fresh["stats"] = stats
    if summary_day:
        def build_summary():
            data = SUM.build(db.conn, summary_day, fresh.get("stats") or STATS.cached(db.conn))
            with db.transaction():
                SUM.store(db, data, SUM.text(data))
                db.set("summary", "last_day", summary_day)
            return SUM.size(data)
        await step("resume", build_summary)
    return report


async def loop() -> None:
    """Boucle principale ; le battement n'est rafraîchi qu'après chaque étape terminée (lot de
    bougies, jour rejoué, passage complet) : une boucle bloquée cesse de battre."""
    db = LabDB()
    heartbeat.enabled = True
    heartbeat.beat()
    log.info("Labo démarré : base %s, rejeu de %d séances", db.path, replay_sessions())
    try:  # une fois par processus (≈ 1 s), avant toute écriture du Labo 1
        guard_v1(db)
    except Exception as exc:  # la base refuse l'état : la garde calculée tient pour ce processus (pause comprise)
        log.error("Labo : garde du Labo 1 impossible : %s: %s", type(exc).__name__, exc)
    while True:
        started = clock.monotonic()
        try:
            report = await tick(db)
            log.info("Labo : passage terminé en %.0f s (%s)", clock.monotonic() - started, report)
        except Exception as exc:
            log.warning("Labo : passage en échec : %s: %s", type(exc).__name__, exc)
        heartbeat.beat("attente du prochain passage")
        await asyncio.sleep(max(60.0, TICK_SECONDS - (clock.monotonic() - started)))


def main() -> None:
    configure_logging()
    if not enabled():
        log.info("Labo désactivé (JARVIS_LAB_ENABLED=off) : en veille")
        heartbeat.enabled = True
        while True:
            heartbeat.beat()
            clock.sleep(TICK_SECONDS)
    asyncio.run(loop())


if __name__ == "__main__":
    main()
