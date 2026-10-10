"""Grade de JARVIS (F- à S+) : la preuve, mesurée par le code, que la méthode en place gagne.

Barème v2 validé par Simão le 2026-10-07 (après avis de GPT et Gemini) :

- Lettre = rentabilité : espérance nette en R par trade sur la fenêtre (FENETRE_JOURS derniers jours
  de bourse du marché, trades déclenchés de la méthode championne, hors échantillon), avec son
  intervalle de confiance à 95 % en grappes par jour (stats.mean_ci) :
    S ≥ +0,40 R, A ≥ +0,25, B ≥ +0,15, C ≥ +0,05 (borne basse de l'IC > 0 dès C),
    D positive mais pas prouvée, E entre −0,10 et 0, F < −0,10.
- − / neutre / + = stabilité et calibration (2 ou 3 critères : +, 1 : neutre, 0 : −) :
    séries de pertes normales (pire série ≤ 1,25 × celle attendue pour des trades indépendants),
    régularité (écart ≤ 0,15 R entre les deux moitiés chronologiques de la fenêtre),
    délais calibrés (40 à 65 % des TP touchés au plus tard au délai médian prévu).
  Réussite et % de TP sont affichés à côté, sans effet sur la lettre ni sur le risque (un taux de
  réussite se gonfle en rapprochant le TP1).
- Moins de MIN_TRADES trades sur MIN_JOURS jours : « non noté », risque 0 %.

Le grade donne le DROIT de risquer ; la taille se calcule à part (¼ de Kelly sur la borne basse de
l'espérance, variance des R), plafonnée par la lettre : F, E, non noté 0 % (plans en observation,
forward-test papier seulement), D 0,25 %, C 1 %, B 1,5 %, A 3 %, S 5 % (plafond EmperorBTC choisi par
Simão ; JARVIS recommandait 2 %, pari noté). En direct s'y ajoutent le risque ouvert total (≤ 4 %)
et l'arrêt de la journée à −3 R sur les trades pris (risk_now).

Changements du grade officiel (worker, chaque passage, zéro jeton) :
- baisse : immédiate ;
- hausse : au moins HAUSSE_TRADES nouveaux trades sur HAUSSE_JOURS nouveaux jours depuis le dernier
  changement (deux mesures sur des fenêtres qui se chevauchent ne prouvent rien) ; une hausse de
  lettre (qui change le risque) attend en plus l'accord de Simão (proposition Approuver/Refuser) ;
- coupe-circuit : à partir de C, une baisse de COUPE_CIRCUIT_R depuis le sommet (trades journalisés
  depuis le dernier changement) ramène tout de suite en D ;
- council du soir : un avis par marché et par soirée (POST /api/lab/grade/avis), un cran au plus ;
  une baisse s'applique, une hausse de lettre passe par Approuver/Refuser ;
- version morte : au moins MORT_TRADES trades et borne haute de l'IC sous 0 : annoncée, mode survie.

Le serveur web n'écrit jamais dans la base : avis et décisions passent par data/lab_queue/grade/ ; le
worker écrit lab_grade.json, lu par JARVIS en direct (plans, !grade, cockpit, propositions).
"""
import json
import logging
import math
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.lab import stats as STATS
from app.lab import validator as V
from app.lab.db import LabDB, db_path, now_iso

log = logging.getLogger(__name__)
PARIS = ZoneInfo("Europe/Paris")
GRADE_FILE = "lab_grade.json"
MARKETS = ("stock", "crypto")
MARKET_FR = {"stock": "actions", "crypto": "crypto"}
LETTERS = "FEDCBAS"
MODS = ("-", "", "+")
FENETRE_JOURS = 60
MIN_TRADES, MIN_JOURS = 100, 10
HAUSSE_TRADES, HAUSSE_JOURS = 150, 5
MORT_TRADES = 150
COUPE_CIRCUIT_R = 20.0
SEUILS = (("S", 0.40), ("A", 0.25), ("B", 0.15), ("C", 0.05))
E_PLANCHER = -0.10
SERIE_TOLERANCE = 1.25
REGULARITE_R = 0.15
DELAIS = (0.40, 0.65)
# Plafond de risque réel par trade (% du capital) selon la lettre ; D : micro-exposition fixe.
RISQUE_MAX = {"F": 0.0, "E": 0.0, "D": 0.25, "C": 1.0, "B": 1.5, "A": 3.0, "S": 5.0}
KELLY_PART = 0.25
RISQUE_OUVERT_MAX = 4.0
ARRET_JOUR_R = -3.0
MOTIFS_MAX, VOIX_MAX = 1500, 1000
SOURCES_FR = {"calcul": "calcul", "council": "council du soir", "simao": "accord de Simão",
              "coupe_circuit": "coupe-circuit", "nouvelle_version": "nouvelle méthode"}

class AvisError(ValueError):
    """Avis du council illisible (message en français, montré tel quel)."""


# --- Barème (fonctions pures) ------------------------------------------------------------------

def rank_of(letter: str, mod: int) -> int:
    """Rang de 0 (F-) à 20 (S+) ; mod : 0 = −, 1 = neutre, 2 = +."""
    return LETTERS.index(letter) * 3 + mod


def name(rank: int | None) -> str:
    if rank is None:
        return "non noté"
    return LETTERS[rank // 3] + MODS[rank % 3]


def letter_of(rank: int | None) -> str | None:
    return None if rank is None else LETTERS[rank // 3]


D_RANK = rank_of("D", 1)
D_PLUS = rank_of("D", 2)
C_MINUS = rank_of("C", 0)


def letter(mean: float, low: float | None) -> str:
    """Lettre d'après l'espérance et la borne basse de son IC à 95 %."""
    proven = low is not None and low > 0
    for lt, floor in SEUILS:
        if mean >= floor and proven:
            return lt
    if mean > 0:
        return "D"
    return "E" if mean >= E_PLANCHER else "F"


def expected_streak(n: int, loss_share: float) -> float | None:
    """Plus longue série de pertes attendue sur n trades indépendants (≈ ln(n·p) / ln(1/q))."""
    q = loss_share
    if n < 2 or not 0 < q < 1:
        return None
    return max(1.0, math.log(n * (1 - q)) / math.log(1 / q))


def evaluate(rows: list[tuple]) -> dict:
    """Mesures et grade d'une fenêtre. Ligne : (id, jour, r_net, tp1, tp2, tp3, délai_ok_1, délai_ok_2,
    délai_ok_3), dans l'ordre des plans ; délai_ok vaut 1, 0 ou None (stats.delay_ok)."""
    day_sums: dict[str, list] = {}
    curve = STATS.Curve()
    n = wins = losses = 0
    tp = [0, 0, 0]
    dl_ok = dl_n = 0
    s = s2 = 0.0
    for _id, day, r, tp1, tp2, tp3, d1, d2, d3 in rows:
        n += 1
        s += r
        s2 += r * r
        wins += r > 0
        losses += r < 0
        d = day_sums.setdefault(day, [0.0, 0])
        d[0] += r
        d[1] += 1
        curve.add(r)
        for i, hit in enumerate((tp1, tp2, tp3)):
            tp[i] += bool(hit)
        for ok in (d1, d2, d3):
            if ok is not None:
                dl_n += 1
                dl_ok += ok
    out = {"trades": n, "jours": len(day_sums)}
    if n < MIN_TRADES or len(day_sums) < MIN_JOURS:
        return out | {"rang": None, "grade": name(None),
                      "raison": f"échantillon trop petit (au moins {MIN_TRADES} trades sur {MIN_JOURS} jours)"}
    mean, se, ci = STATS.mean_ci(day_sums, s, n)
    var = max(0.0, (s2 - n * mean * mean) / (n - 1))
    curve_res = curve.result()
    streak = expected_streak(n, losses / n)
    days = sorted(day_sums)
    halves = []
    for part in (days[:len(days) // 2], days[len(days) // 2:]):
        k = sum(day_sums[d][1] for d in part)
        halves.append(sum(day_sums[d][0] for d in part) / k if k else None)
    delays = dl_ok / dl_n if dl_n else None
    criteria = {
        "series_normales": streak is not None and curve_res["pire_serie_pertes"] <= SERIE_TOLERANCE * streak,
        "regulier": None not in halves and abs(halves[0] - halves[1]) <= REGULARITE_R,
        "delais_calibres": delays is not None and DELAIS[0] <= delays <= DELAIS[1],
    }
    met = sum(criteria.values())
    lt = letter(mean, ci[0] if ci else None)
    rank = rank_of(lt, 2 if met >= 2 else met)
    low = ci[0] if ci else None
    kelly = KELLY_PART * low / var * 100 if low is not None and low > 0 and var > 0 else 0.0
    return out | {
        "rang": rank, "grade": name(rank), "lettre": lt,
        "esperance_r": round(mean, 4), "ic95": ci, "variance_r": round(var, 4), "total_r": round(s, 2),
        "kelly_pct": round(kelly, 3),
        "criteres": criteria,
        "serie_pertes": curve_res["pire_serie_pertes"], "serie_attendue": round(streak, 1) if streak else None,
        "baisse_max_r": curve_res["baisse_max_r"],
        "moities_r": [round(h, 4) if h is not None else None for h in halves],
        "delais_tenus": round(delays, 4) if delays is not None else None,
        "reussite": round(wins / n, 4), "tp1": round(tp[0] / n, 4), "tp2": round(tp[1] / n, 4),
        "tp3": round(tp[2] / n, 4),
    }


def grade_risk(rank: int | None, kelly_pct: float | None) -> float:
    """Risque proposé par trade (% du capital) avant le budget du portefeuille."""
    lt = letter_of(rank)
    if lt is None or RISQUE_MAX[lt] == 0:
        return 0.0
    if lt == "D":
        return RISQUE_MAX["D"]
    return round(min(RISQUE_MAX[lt], kelly_pct or 0.0), 2)


def drawdown(rs) -> float:
    curve = STATS.Curve()
    for r in rs:
        curve.add(r)
    return curve.max_dd


# --- Lecture de la base (worker) ----------------------------------------------------------------

def _scope(conn, market: str, base_id: int | None) -> tuple[str, list, str | None]:
    """Trades déclenchés de la version de génération gardés par le champion, sur le marché ; pour un
    champion filtré, seulement ce qu'il n'a pas vu (journalisés depuis sa mise en essai : hors
    échantillon)."""
    from app.lab import method as M

    version = V.current_version(conn)
    champ = M.champion(conn)
    kept, kparams = V.champion_filter(conn)
    sql = (f"t.method_version = ? AND t.market = ? AND t.triggered = 1 AND t.r_net IS NOT NULL AND {kept}")
    params = [version, market, *kparams]
    if champ["regles"]:
        start = champ.get("debut_ts") or 0
        sql += " AND (t.planned_at >= ? OR t.id > ?)"
        params += [start, base_id or 0]
    return sql, params, champ["version"]


def window_rows(conn, market: str, base_id: int | None = None) -> tuple[list[tuple], str | None]:
    sql, params, version = _scope(conn, market, base_id)
    join = "FROM trades t LEFT JOIN regimes r ON r.market = t.market AND r.day = t.day"
    row = conn.execute(f"SELECT DISTINCT t.day {join} WHERE {sql} ORDER BY t.day DESC LIMIT 1 OFFSET ?",
                       (*params, FENETRE_JOURS - 1)).fetchone()
    cutoff = row[0] if row else ""
    cur = conn.execute(
        f"""SELECT t.id, t.day, t.r_net, t.tp1_hit, t.tp2_hit, t.tp3_hit,
                   t.minutes_to_tp1, t.minutes_to_tp2, t.minutes_to_tp3,
                   json_extract(t.plan, '$.targets[0].minutes_median'),
                   json_extract(t.plan, '$.targets[1].minutes_median'),
                   json_extract(t.plan, '$.targets[2].minutes_median')
            {join} WHERE {sql} AND t.day >= ? ORDER BY t.planned_at, t.id""", (*params, cutoff))
    rows = [(i, day, r, a, b, c, STATS.delay_ok(a, m1, e1), STATS.delay_ok(b, m2, e2), STATS.delay_ok(c, m3, e3))
            for i, day, r, a, b, c, m1, m2, m3, e1, e2, e3 in cur]
    return rows, version


def since(conn, market: str, after_id: int, base_id: int | None = None) -> dict:
    """Trades journalisés depuis le dernier changement : nombre, jours, baisse depuis le sommet."""
    sql, params, _ = _scope(conn, market, base_id)
    rs, days = [], set()
    for day, r in conn.execute(f"SELECT t.day, t.r_net FROM trades t LEFT JOIN regimes r ON r.market = t.market "
                               f"AND r.day = t.day WHERE {sql} AND t.id > ? ORDER BY t.planned_at, t.id",
                               (*params, after_id)):
        rs.append(r)
        days.add(day)
    return {"trades": len(rs), "jours": len(days), "baisse_r": round(drawdown(rs), 2)}


def max_trade_id(conn) -> int:
    return conn.execute("SELECT COALESCE(MAX(id), 0) FROM trades").fetchone()[0]


# --- File : avis du council et décisions de Simão -------------------------------------------------

def queue_dir(db_file: str | None = None) -> Path:
    return V.queue_dir(db_file or db_path()) / "grade"


def _write_atomic(target: Path, data) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".{target.name}.{uuid.uuid4().hex[:6]}.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)


def parse_avis(raw) -> dict:
    """Avis du council : {"marche": "stock", "ajustement": -1 | 0 | 1, "motifs": "…",
    "voix": {"sceptique": "…", "pragmatique": "…", "critique": "…"}} (voix facultatives)."""
    if not isinstance(raw, dict):
        raise AvisError("objet JSON attendu")
    unknown = set(raw) - {"marche", "ajustement", "motifs", "voix"}
    if unknown:
        raise AvisError(f"champ inconnu : {', '.join(sorted(unknown))}")
    market = raw.get("marche")
    if market not in MARKETS:
        raise AvisError("« marche » : stock ou crypto")
    adj = raw.get("ajustement")
    if isinstance(adj, bool) or adj not in (-1, 0, 1):
        raise AvisError("« ajustement » : -1, 0 ou 1 (un cran au plus)")
    motifs = raw.get("motifs")
    if not isinstance(motifs, str) or not 10 <= len(motifs.strip()) <= MOTIFS_MAX:
        raise AvisError(f"« motifs » : texte de 10 à {MOTIFS_MAX} caractères (obligatoire)")
    voix = raw.get("voix") or {}
    if not isinstance(voix, dict) or set(voix) - {"sceptique", "pragmatique", "critique"} or any(
            not isinstance(v, str) or len(v) > VOIX_MAX for v in voix.values()):
        raise AvisError(f"« voix » : sceptique, pragmatique, critique ; textes de {VOIX_MAX} caractères au plus")
    return {"marche": market, "ajustement": adj, "motifs": motifs.strip(),
            "voix": {k: v.strip() for k, v in voix.items()}}


def enqueue_avis(db_file: str, raw, soiree: str) -> dict:
    """Serveur web : dépose l'avis du council (lu par le worker ; un seul par marché et par soirée,
    le dernier déposé remplace le précédent tant que le worker ne l'a pas lu)."""
    avis = parse_avis(raw) | {"soiree": soiree, "le": now_iso()}
    _write_atomic(queue_dir(db_file) / f"avis-{avis['marche']}-{soiree}.json", avis)
    return {"en_file": True, "marche": avis["marche"], "soiree": soiree}


def enqueue_decision(db_file: str, hausse_id: str, approve: bool, proposal_id: int | None = None) -> Path:
    """Serveur web : décision de Simão sur une hausse de lettre proposée."""
    if not re.fullmatch(r"(stock|crypto)-\d+", hausse_id or ""):
        raise ValueError("hausse illisible")
    target = queue_dir(db_file) / f"decision-{hausse_id}.json"
    _write_atomic(target, {"hausse": hausse_id, "decision": "approuvee" if approve else "refusee",
                           "proposition": proposal_id, "le": now_iso()})
    return target


# --- Worker : grade officiel ---------------------------------------------------------------------

class Grader:
    """État par marché (table progress, job « grade ») et historique (table grades)."""

    def __init__(self, db: LabDB, now: datetime):
        self.db, self.now = db, now

    def state(self, market: str) -> dict:
        return self.db.get("grade", market) or {"rang": None, "version": None, "depuis_id": 0, "base_id": None,
                                                 "change_le": None, "hausse": None, "morte": False, "seq": 0}

    def save(self, market: str, st: dict) -> None:
        self.db.set("grade", market, st)

    def history(self, market: str, st: dict, rank, measured, source: str, reason: str, metrics=None,
                soiree: str | None = None) -> None:
        self.db.conn.execute(
            "INSERT INTO grades (created_at, market, version, rank, measured, source, reason, metrics, soiree) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.now.isoformat(timespec="seconds"), market, st.get("version"), rank, measured, source, reason,
             json.dumps(metrics or {}, ensure_ascii=False), soiree))

    def event(self, st: dict, text: str, data: dict | None = None) -> None:
        from app.lab import method as M

        M.event(self.db, "grade", st.get("version"), text, data, self.now)

    def set_rank(self, market: str, st: dict, rank: int, source: str, reason: str, measured=None,
                 metrics=None, soiree: str | None = None) -> None:
        old = st["rang"]
        st.update(rang=rank, depuis_id=max_trade_id(self.db.conn), change_le=self.now.isoformat(timespec="seconds"),
                  hausse=None)
        self.history(market, st, rank, measured, source, reason, metrics, soiree)
        if old == rank:
            return
        if old is not None and letter_of(old) == letter_of(rank):
            # − / + dans la même lettre : le risque proposé ne change pas. Gardé dans l'historique
            # (!grade) sans message privé, sinon le rejeu (des milliers de trades par passage) fait
            # osciller F / F+ et envoie un message toutes les 40 minutes.
            log.info("Labo : grade %s %s → %s (%s)", market, name(old), name(rank), reason)
            return
        arrow = "⬆️" if old is not None and rank > old else "⬇️" if old is not None else "🏅"
        risk = grade_risk(rank, (metrics or {}).get("kelly_pct"))
        text = (f"{arrow} Grade JARVIS {MARKET_FR[market]} : {name(old) + ' → ' if old is not None else ''}"
                f"**{name(rank)}** ({SOURCES_FR.get(source, source)} : {reason}). Risque proposé par trade : "
                f"{risk:g} %{' (plans en observation)' if risk == 0 else ''}.")
        self.event(st, text, {"marche": market, "de": old, "vers": rank, "source": source})

    def propose(self, market: str, st: dict, target: int, source: str, reason: str,
                soiree: str | None = None) -> None:
        """Hausse de lettre : attend l'accord de Simão (proposition créée par le serveur)."""
        if st.get("hausse") and st["hausse"]["vers"] == target:
            return
        st["seq"] = st.get("seq", 0) + 1
        st["hausse"] = {"id": f"{market}-{st['seq']}", "de": st["rang"], "vers": target, "source": source,
                        "raison": reason, "le": self.now.isoformat(timespec="seconds")}
        self.history(market, st, st["rang"], target, source, f"hausse proposée vers {name(target)} : {reason}",
                     soiree=soiree)
        log.info("Labo : grade %s, hausse proposée %s → %s (%s)", market, name(st["rang"]), name(target), reason)

    def raise_to(self, market: str, st: dict, target: int, source: str, reason: str, measured, metrics,
                 soiree: str | None = None) -> None:
        """Hausse : immédiate dans la même lettre (le risque ne change pas), sinon proposée."""
        if letter_of(target) == letter_of(st["rang"]):
            self.set_rank(market, st, target, source, reason, measured, metrics, soiree)
        else:
            self.propose(market, st, target, source, reason, soiree)

    # -- passage --
    def grade_market(self, market: str, force: bool = False) -> dict:
        conn = self.db.conn
        st = self.state(market)
        from app.lab import method as M

        champ_version = M.champion(conn)["version"]
        if st.get("version") != champ_version:
            if st.get("version") is not None:
                self.history(market, st, None, None, "nouvelle_version",
                             f"méthode {champ_version} en place : grade remis à zéro (hors échantillon)")
            st = {"rang": None, "version": champ_version, "depuis_id": max_trade_id(conn), "base_id": max_trade_id(conn),
                  "change_le": None, "hausse": None, "morte": False, "seq": st.get("seq", 0)}
        rows, _ = window_rows(conn, market, st.get("base_id"))
        m = evaluate(rows)
        st["mesure"] = m
        measured = m.get("rang")
        cur = st["rang"]
        if measured is not None:
            if cur is None:
                if measured <= D_PLUS:
                    self.set_rank(market, st, measured, "calcul", "premier grade", measured, m)
                else:
                    self.set_rank(market, st, D_PLUS, "calcul", f"premier grade, mesuré {name(measured)}", measured, m)
                    self.propose(market, st, measured, "calcul", f"mesuré {name(measured)} dès le premier calcul")
            else:
                new = since(conn, market, st["depuis_id"], st.get("base_id"))
                st["nouveaux"] = new
                if cur >= C_MINUS and new["baisse_r"] >= COUPE_CIRCUIT_R:
                    self.set_rank(market, st, min(D_RANK, measured), "coupe_circuit",
                                  f"baisse de {new['baisse_r']:g} R depuis le sommet", measured, m)
                elif measured < cur:
                    self.set_rank(market, st, measured, "calcul",
                                  f"espérance {m['esperance_r']:+.3f} R sur {m['trades']} trades", measured, m)
                elif measured > cur and new["trades"] >= HAUSSE_TRADES and new["jours"] >= HAUSSE_JOURS:
                    self.raise_to(market, st, measured, "calcul",
                                  f"espérance {m['esperance_r']:+.3f} R sur {m['trades']} trades, dont "
                                  f"{new['trades']} nouveaux sur {new['jours']} jours", measured, m)
        ci = m.get("ic95")
        dead = m["trades"] >= MORT_TRADES and ci is not None and ci[1] < 0
        if dead and not st.get("morte"):
            self.event(st, f"☠️ Méthode {champ_version} déclarée morte sur les {MARKET_FR[market]} : "
                           f"{m['esperance_r']:+.3f} R par trade sur {m['trades']} trades, IC95 [{ci[0]:+.3f} ; "
                           f"{ci[1]:+.3f}] entièrement sous zéro. Mode survie : il faut un challenger.",
                       {"marche": market, "ic95": ci})
        st["morte"] = dead
        self.save(market, st)
        return st

    def apply_avis(self, avis: dict) -> str:
        market = avis["marche"]
        st = self.state(market)
        if st["rang"] is None:
            return "ignoré (marché non noté)"
        done = self.db.conn.execute("SELECT 1 FROM grades WHERE market = ? AND source = 'council' AND soiree = ?",
                                    (market, avis["soiree"])).fetchone()
        if done:
            return "ignoré (déjà un avis ce soir)"
        adj, cur = avis["ajustement"], st["rang"]
        reason = avis["motifs"][:300]
        metrics = {"voix": avis.get("voix")} | (st.get("mesure") or {})
        target = max(0, min(20, cur + adj))
        if adj < 0 and target < cur:
            self.set_rank(market, st, target, "council", reason, (st.get("mesure") or {}).get("rang"), metrics,
                          avis["soiree"])
        elif adj > 0 and target > cur:
            self.raise_to(market, st, target, "council", reason, (st.get("mesure") or {}).get("rang"), metrics,
                          avis["soiree"])
        else:
            self.history(market, st, cur, (st.get("mesure") or {}).get("rang"), "council", f"grade confirmé : {reason}",
                         {"voix": avis.get("voix")}, avis["soiree"])
        self.save(market, st)
        return "appliqué"

    def apply_decision(self, data: dict) -> str:
        market = data["hausse"].split("-")[0]
        st = self.state(market)
        h = st.get("hausse")
        if not h or h["id"] != data["hausse"]:
            return "ignorée (hausse dépassée)"
        if data["decision"] == "approuvee":
            measured = (st.get("mesure") or {}).get("rang")
            target = h["vers"]
            if h["source"] == "calcul" and measured is not None:
                target = min(target, measured)  # la mesure a pu baisser depuis la proposition
            if target <= st["rang"]:
                st["hausse"] = None
                self.save(market, st)
                return "ignorée (la mesure a baissé depuis)"
            self.set_rank(market, st, target, "simao", f"hausse approuvée ({h['raison'][:200]})", measured,
                          st.get("mesure"))
        else:
            st["hausse"] = None
            st["depuis_id"] = max_trade_id(self.db.conn)  # pas reproposée sans nouvelles preuves
            self.history(market, st, st["rang"], h["vers"], "simao", f"hausse vers {name(h['vers'])} refusée")
        self.save(market, st)
        return data["decision"]

    def ingest(self) -> int:
        q = queue_dir(self.db.path)
        if not q.exists():
            return 0
        n = 0
        for f in sorted(q.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                with self.db.transaction():
                    if f.name.startswith("avis-"):
                        avis = parse_avis({k: data.get(k) for k in ("marche", "ajustement", "motifs", "voix")})
                        out = self.apply_avis(avis | {"soiree": data.get("soiree")})
                    else:
                        out = self.apply_decision(data)
                log.info("Labo : grade, %s : %s", f.name, out)
                n += 1
            except (OSError, ValueError, KeyError, TypeError) as exc:
                log.warning("Labo : grade, fichier %s illisible, écarté (%s)", f.name, exc)
            f.unlink(missing_ok=True)
        return n


def signature(db: LabDB) -> list:
    from app.lab import method as M

    row = db.conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM trades").fetchone()
    return [row[0], row[1], M.champion(db.conn)["version"]]


def step(db: LabDB, now: datetime | None = None) -> dict:
    """Passage du worker : file (avis, décisions), grade de chaque marché si les trades ont changé,
    fichier lab_grade.json pour le direct."""
    now = now or datetime.now(timezone.utc)
    g = Grader(db, now)
    out = {"file": g.ingest()}
    sig = signature(db)
    if sig != db.get("grade_calc", "signature") or out["file"]:
        for market in MARKETS:
            with db.transaction():
                st = g.grade_market(market)
            out[market] = name(st["rang"])
        db.set("grade_calc", "signature", sig)
    out["fichier"] = write_live(db)
    return out


# --- Lecture (serveur, routine, cockpit) ---------------------------------------------------------

def payload(conn) -> dict:
    """Ce que le direct lit : grade officiel, mesure, risque du grade, hausse en attente, par marché."""
    markets = {}
    for market in MARKETS:
        row = conn.execute("SELECT value FROM progress WHERE job = 'grade' AND key = ?", (market,)).fetchone()
        st = json.loads(row[0]) if row else None
        if not st:
            continue
        m = st.get("mesure") or {}
        non_note = m.get("rang") is None
        lt = letter_of(st.get("rang"))
        markets[market] = {
            "grade": name(st.get("rang")), "rang": st.get("rang"), "version": st.get("version"),
            "non_note": non_note, "mesure": m, "change_le": st.get("change_le"),
            "risque_grade": 0.0 if non_note else grade_risk(st.get("rang"), m.get("kelly_pct")),
            "risque_max": RISQUE_MAX[lt] if lt else 0.0,
            "mode_survie": non_note is False and (lt in ("E", "F") or bool(st.get("morte"))),
            "morte": bool(st.get("morte")), "hausse": st.get("hausse"), "nouveaux": st.get("nouveaux")}
    return {"marches": markets}


def history(conn, market: str | None = None, limit: int = 20) -> list[dict]:
    try:
        sql = "SELECT created_at, market, version, rank, measured, source, reason, soiree FROM grades"
        args: list = []
        if market:
            sql += " WHERE market = ?"
            args.append(market)
        rows = conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"le": c, "marche": m, "version": v, "grade": name(r), "mesure": name(me) if me is not None else None,
             "source": SOURCES_FR.get(s, s), "raison": why, "soiree": so} for c, m, v, r, me, s, why, so in rows]


def live_path(db_file: str | None = None) -> Path:
    override = os.getenv("JARVIS_LAB_GRADE", "").strip()
    return Path(override) if override else Path(db_file or db_path()).parent / GRADE_FILE


def write_live(db: LabDB) -> bool:
    if db.path == ":memory:":
        return False
    data = payload(db.conn)
    path = live_path(db.path)
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("marches") == data["marches"]:
            return False
    except (OSError, ValueError):
        pass
    _write_atomic(path, data | {"ecrit_le": now_iso()})
    return True


_cache: dict = {"key": None, "data": None}
_warned: set[str] = set()


def current() -> dict | None:
    """lab_grade.json pour le direct ; None si absent ou illisible (risque 0 % : pas de preuve)."""
    try:
        path = live_path()
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
        if _cache["key"] != key:
            _cache.update(key=key, data=json.loads(path.read_text(encoding="utf-8")))
            _warned.discard("lecture")
        return _cache["data"]
    except FileNotFoundError:
        return None
    except Exception as exc:
        if "lecture" not in _warned:
            _warned.add("lecture")
            log.warning("Grade du labo illisible (%s: %s) : risque proposé 0 %%", type(exc).__name__, exc)
    return None


def market_grade(market: str, data: dict | None = None) -> dict | None:
    data = current() if data is None else data
    return ((data or {}).get("marches") or {}).get(market)


def open_risk(trades: list[dict]) -> float:
    """Risque encore en jeu sur les trades ouverts (% du capital) : risque pris × part de la position
    restante × part du stop encore sous l'entrée (0 une fois le stop remonté à l'entrée)."""
    total = 0.0
    for t in trades:
        if t.get("status") != "open":
            continue
        pct = (t.get("sizing") or {}).get("risk_pct")
        if pct is None:
            pct = (t.get("grade") or {}).get("risque_propose") or 0.0
        risk = abs(t["entry"] - t["stop"])
        if not pct or not risk:
            continue
        sign = 1 if t["side"] == "long" else -1
        left = max(0.0, sign * (t["entry"] - t.get("stop_current", t["stop"]))) / risk
        legs = len(t.get("targets") or []) or 1
        total += pct * left * (legs - min(t.get("targets_hit", 0), legs)) / legs
    return round(total, 3)


def day_r(trades: list[dict], now: datetime) -> float:
    """R réalisé aujourd'hui (date de Paris de la clôture) sur les trades pris."""
    from app.trading import service as T

    today = now.astimezone(PARIS).date()
    total = 0.0
    for t in trades:
        r = T.realized_r(t)
        if r is None:
            continue
        ends = [e["at"] for e in t.get("events", []) if e["kind"] in ("stop", "close", "tp3", "tp2", "tp1")]
        if ends and datetime.fromisoformat(ends[-1]).astimezone(PARIS).date() == today:
            total += r
    return round(total, 2)


def risk_now(market: str, trades: list[dict], now: datetime | None = None, data: dict | None = None) -> dict:
    """Risque proposé pour un nouveau plan : grade, puis budget du portefeuille et arrêt de la journée."""
    now = now or datetime.now(timezone.utc)
    g = market_grade(market, data)
    if not g:
        return {"grade": "non noté", "risque_grade": 0.0, "risque_propose": 0.0, "raison": "grade pas encore calculé",
                "observation": True}
    base = g["risque_grade"]
    out = {"grade": g["grade"], "risque_grade": base, "risque_max": g["risque_max"], "mode_survie": g["mode_survie"],
           "non_note": g["non_note"]}
    if base <= 0:
        why = ("échantillon trop petit" if g["non_note"] else
               f"grade {g['grade']} : pas de capital réel tant que la méthode n'est pas au moins D")
        return out | {"risque_propose": 0.0, "raison": why, "observation": True}
    opened = open_risk(trades)
    today = day_r(trades, now)
    out |= {"risque_ouvert": opened, "r_du_jour": today}
    if today <= ARRET_JOUR_R:
        return out | {"risque_propose": 0.0, "raison": f"arrêt de la journée ({today:+g} R sur les trades pris)",
                      "observation": True}
    room = max(0.0, RISQUE_OUVERT_MAX - opened)
    proposed = round(min(base, room), 2)
    why = f"grade {g['grade']}" + (f", plafonné par le risque déjà ouvert ({opened:g} %)" if proposed < base else "")
    return out | {"risque_propose": proposed, "raison": why, "observation": proposed <= 0}


def plan_line(g: dict) -> str:
    """Une ligne pour le plan sur Discord."""
    head = f"🏅 Grade JARVIS **{g['grade']}** · risque proposé **{g['risque_propose']:g} %**"
    if g.get("observation"):
        return head + f" : plan en observation ({g['raison']})"
    return head + f" ({g['raison']})"


def _pct(x) -> str:
    return "n/d" if x is None else f"{x:.0%}"


def text(data: dict | None, hist: list[dict] | None = None) -> str:
    """`!grade` : grade de chaque marché, de quoi il est fait, et ce qu'il change."""
    marches = (data or {}).get("marches") or {}
    if not marches:
        return "🏅 Grade JARVIS pas encore calculé (le worker du labo le calcule à son prochain passage)."
    lines = ["🏅 **Grade JARVIS** (F- à S+ : la preuve que la méthode gagne, mesurée sans jeton)"]
    for market in MARKETS:
        g = marches.get(market)
        if not g:
            continue
        m = g["mesure"] or {}
        lines.append(f"\n**{MARKET_FR[market].capitalize()} : {g['grade']}** · méthode {g['version']} · "
                     f"risque permis {g['risque_grade']:g} % (plafond {g['risque_max']:g} %)")
        if g["non_note"]:
            lines.append(f"-# {m.get('raison', 'échantillon trop petit')} : {m.get('trades', 0)} trades sur "
                         f"{m.get('jours', 0)} jours")
        else:
            ci = m.get("ic95") or [None, None]
            crit = m.get("criteres") or {}
            ok = {True: "✅", False: "❌"}
            lines.append(f"Mesuré **{m['grade']}** : {m['esperance_r']:+.3f} R par trade (IC95 {ci[0]:+.3f} ; "
                         f"{ci[1]:+.3f}) sur {m['trades']} trades et {m['jours']} jours")
            lines.append(f"{ok[crit.get('series_normales', False)]} séries de pertes ({m['serie_pertes']} d'affilée, "
                         f"~{m['serie_attendue']} attendues) · {ok[crit.get('regulier', False)]} régularité "
                         f"({' / '.join(f'{h:+.2f}' for h in m['moities_r'] if h is not None)} R) · "
                         f"{ok[crit.get('delais_calibres', False)]} délais tenus {_pct(m['delais_tenus'])}")
            lines.append(f"-# Réussite {_pct(m['reussite'])} · TP1 {_pct(m['tp1'])} · TP2 {_pct(m['tp2'])} · "
                         f"TP3 {_pct(m['tp3'])} · baisse max {m['baisse_max_r']:g} R")
        if g.get("morte"):
            lines.append("☠️ Méthode déclarée morte (IC95 entièrement sous zéro) : il faut un challenger.")
        elif g.get("mode_survie"):
            lines.append("🆘 Mode survie : la routine du soir travaille à remonter le grade.")
        if g.get("hausse"):
            h = g["hausse"]
            lines.append(f"⏳ Hausse vers {name(h['vers'])} en attente de ton accord ({h['raison']})")
    if hist:
        lines.append("\n**Derniers changements**")
        for h in hist[:5]:
            lines.append(f"-# {h['le'][:16].replace('T', ' ')} · {MARKET_FR.get(h['marche'], h['marche'])} · "
                         f"{h['grade']} · {h['source']} : {h['raison']}")
    return "\n".join(lines)


def reels(trades: list[dict]) -> dict:
    """Troisième population (avis de GPT, 10 oct.) : trades réellement pris et suivis par JARVIS (`!prends`),
    terminés, hors Killmode (mesuré par son propre journal), en R réalisé. Indicatif : aucun effet."""
    from app.trading import service as T

    out = {}
    for market in MARKETS:
        rs = [r for r in (T.realized_r(t) for t in trades if t.get("market", "stock") == market
                          and not t.get("killmode")) if r is not None]
        out[market] = {"termines": len(rs), "esperance_r": round(sum(rs) / len(rs), 3) if rs else None,
                       "total_r": round(sum(rs), 2), "gagnants": sum(r > 0 for r in rs)}
    return out


def populations_text(indicatif: list[str], real: dict | None) -> str:
    """Lignes ajoutées à `!grade` : GO confirmés (labo) et trades réels, à côté du grade officiel de v1."""
    lines = []
    if indicatif:
        lines.append("\n**Ce que tu trades vraiment** (indicatif : ne change ni le grade ni le risque)")
        lines += indicatif
    for market, g in (real or {}).items():
        if g["termines"]:
            lines.append(f"-# Tes trades réels {MARKET_FR[market]} : {g['termines']} terminés, {g['gagnants']} "
                         f"gagnants, {g['esperance_r']:+.2f} R en moyenne avant frais ({g['total_r']:+.2f} R au total)"
                         + (" · trop peu pour conclure" if g["termines"] < 30 else ""))
    return "\n".join(lines)


def sync_proposals(proposals, data: dict | None = None) -> list[dict]:
    """Serveur : une proposition par hausse de lettre en attente ; les hausses dépassées sont remplacées.
    Renvoie les nouvelles propositions (à envoyer en privé)."""
    data = current() if data is None else data
    if data is None:
        return []
    new, live = [], set()
    for market, g in (data.get("marches") or {}).items():
        h = g.get("hausse")
        if not h:
            continue
        live.add(h["id"])
        p = proposals.propose_grade(h, MARKET_FR.get(market, market), name(h.get("de")), name(h["vers"]))
        if p:
            new.append(p)
    proposals.retire_grade(live)
    return new


def brief(conn) -> dict:
    """Pour la routine du soir (GET /api/lab/brief) : de quoi tenir le council du grade."""
    data = payload(conn)
    out = {}
    for market, g in data["marches"].items():
        m = g["mesure"] or {}
        out[market] = {k: g[k] for k in ("grade", "non_note", "risque_grade", "mode_survie", "morte", "hausse")} | {
            "mesure": {k: m.get(k) for k in ("grade", "trades", "jours", "esperance_r", "ic95", "criteres",
                                             "serie_pertes", "serie_attendue", "moities_r", "delais_tenus",
                                             "reussite", "tp1", "baisse_max_r", "raison")}}
    return {"marches": out, "historique": history(conn, limit=6),
            "avis": "POST /api/lab/grade/avis {marche, ajustement: -1|0|1, motifs, voix} : un avis par marché et par soir"}
