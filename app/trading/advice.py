"""Avis de JARVIS sur un actif choisi par Simão, et conseil sur ses positions ouvertes, en prix concrets.

Demande de Simão (2026-10-08) : pas de moyennes, des prix. Pour chaque position, les meilleurs prix de sortie et
de renfort ; pour un actif, les meilleurs prix d'entrée s'il y en a. Ils viennent de la carte des niveaux
(app/trading/levels.py : swings 15 min et journaliers, veille, 52 semaines, gaps, chacun cité) et de la formation
EmperorBTC (stop sous la structure, ventes sur les résistances, renfort sur un retest ou une cassure de swing).

`!avis NVDA` : pour chaque côté, l'entrée maintenant (stop du plan au prix actuel), sur repli vers un support
(ordre limite) et sur cassure d'une résistance (ordre stop), avec stop, objectifs et R/R. JARVIS entre maintenant
si le plan est GO et que le R/R sur le 2e objectif tient (rr_min, avec la tolérance) ; sinon il donne les ordres
conditionnels dont le R/R tient ; sinon il attend. Un côté contre la tendance de fond n'est jamais proposé
(EmperorBTC : tradez dans le sens du marché), ni aucune entrée avant des résultats trimestriels proches.
Sous l'avis, le grade de JARVIS (risque réel proposé, 0 % tant que la méthode n'a rien prouvé) et, pour les
entrées sur repli et sur cassure, ce que le labo a mesuré sur les stratégies les plus proches face à un témoin
aléatoire (lab_entries) : avis externe de GPT du 2026-10-08, ces prix sont ceux de la méthode, pas des prédictions.

`!positions`, `!position NVDA 132.5` et le message 30 min avant la clôture US : pour chaque position, les ordres
à poser (une vente par part sur les prochaines résistances, stop suiveur sous la structure, renforts si la
position gagne) et la décision du moment :
  1. le prix a passé le stop → SORTIR ;
  2. résultats trimestriels avant la fin de la prochaine séance → SORTIR (pari binaire hors plan) ;
  3. augmentation de capital dans les actus → SORTIR ;
  4. objectif en retard (heure passée à laquelle 3 sur 4 des cas qui l'ont touché l'avaient fait, soit
     reached_by_p75 de tous les cas) et position qui ne gagne pas → SORTIR (le retard ne décide que si rien de
     mesurable ne dit de garder : la mesure « garder jusqu'au prochain objectif » est retirée, voir plus bas) ;
  5. signe de retournement sur une position gagnante → ALLÉGER (la moitié tout de suite) ;
  6. gap de nuit plus grand que la distance au stop dans au moins 10 % des nuits passées → ALLÉGER (la moitié
     avant la nuit) ;
  7. sinon GARDER, avec ces ordres.
La règle « garder jusqu'au prochain objectif perd en moyenne → SORTIR » de la première version est retirée
(2026-10-08) : elle ne comparait que « tout garder avec le stop à l'entrée » et « tout vendre », sans vente
partielle ni stop suiveur, et parlait en moyennes au lieu de donner des prix.
Un renfort se juge sur toute la position : au stop qui le suit, ce que perd chaque part ajoutée et ce que devient
chaque part déjà détenue. Le labo ne teste pas les renforts ; la note qui les suit dit ce qu'il a mesuré sur les
entrées du même type.
Ces règles ne sont pas validées par le labo : chaque conseil donné sur un trade suivi est gardé (un par trade et
par jour) et comparé à ce que la position a fait ensuite (`!positions` dit combien étaient justes).
"""
import asyncio
import logging
import sqlite3
from datetime import date, datetime, timezone

from app.config import env_float
from app.trading import calendar as CAL
from app.trading import catalyst as C
from app.trading import levels as L
from app.trading import plan as P
from app.trading import service as S
from app.trading import signals as SIG
from app.trading.settings import settings

log = logging.getLogger(__name__)
GAP_LIMIT = env_float("JARVIS_GAP_LIMIT", 0.10)  # part des nuits où le gap aurait sauté le stop
CLOSE_ADVICE_MINUTES = env_float("JARVIS_CLOSE_ADVICE_MINUTES", 30)  # message auto, minutes avant la clôture
GAP_DAYS = 250  # nuits regardées pour le risque de gap
MIN_GAPS = 30
ICON = {"SORTIR": "🚪", "ALLÉGER": "✂️", "GARDER": "✊", "ATTENDRE": "⏸️"}
MARKET_FR = {"stock": "action", "crypto": "crypto"}
KIND_FR = {"maintenant": "maintenant", "repli": "sur repli", "cassure": "sur cassure"}
ORDER_FR = {("long", "maintenant"): "achat au marché", ("long", "repli"): "achat limite", ("long", "cassure"): "achat stop",
            ("short", "maintenant"): "vente au marché", ("short", "repli"): "vente limite",
            ("short", "cassure"): "vente stop"}
# Stratégies du labo v2 les plus proches des entrées et renforts proposés (app/lab/strategies.py) : achat sur repli
# vers un support (rebond confirmé, pullback) et achat sur cassure (range d'ouverture par ordre stop, cassure d'une
# zone ou du plus haut de la veille puis retest). Leur écart au témoin aléatoire (même stop, mêmes objectifs, entrée
# au hasard) dit si ce type d'entrée a un avantage. Avis externe de GPT (2026-10-08) : sans cet avantage, ce sont
# les prix de la méthode, pas des prédictions, et JARVIS doit dire ce que le labo a mesuré.
LAB_KIN = {"repli": ("rebond_confirme", "pullback"),
           "cassure": ("orb15", "orb30", "cassure_zone_retest", "cassure_zone_retest_15m", "cassure_zone_retest_1h",
                       "cassure_retest")}


def _fr(x: float, digits: int = 2) -> str:
    return f"{x:+.{digits}f}"  # point décimal, comme les prix affichés dans le même message


def _date_fr(iso: str) -> str:
    d = date.fromisoformat(iso[:10])
    return f"{'1er' if d.day == 1 else d.day} {L.MONTHS[d.month - 1]}"


def _side(side: str) -> str:
    return "long" if side == "long" else "short"


def rr_floor() -> float:
    """R/R minimal sur le 2e objectif : la règle souple de Simão (minimum moins la tolérance)."""
    return settings["rr_min"] - settings["rr_tolerance"]


def company_line(cat: dict | None, now: datetime) -> str | None:
    """Ce que le catalyseur sait de la société, en une ligne (résultats, actus, augmentation de capital)."""
    if not cat or not cat.get("available"):
        return None
    parts = []
    nxt = cat.get("next_earnings")
    if nxt:
        days = (date.fromisoformat(nxt["date"]) - now.date()).days
        moment = {"bmo": " avant l'ouverture", "amc": " après la clôture"}.get(nxt.get("hour") or "", "")
        parts.append(f"résultats le {_date_fr(nxt['date'])}{moment} (dans {days} j)")
    elif cat.get("earnings_known"):
        parts.append("pas de résultats annoncés dans les 30 jours")
    pos, neg = cat.get("positive", 0), cat.get("negative", 0)
    parts.append(f"actus 7 j : {pos} positive{'s' if pos > 1 else ''}, {neg} négative{'s' if neg > 1 else ''}")
    parts.append("⚠️ augmentation de capital dans les actus" if cat.get("dilution") else "pas d'augmentation de capital")
    text = " · ".join(parts)
    return "🏢 " + text[0].upper() + text[1:]


# --- Avis sur un actif ------------------------------------------------------------------

def _trend(p: dict) -> dict:
    return next((i for i in p["checklist"]["items"] if i["key"] == "trend_daily"), {"ok": None, "detail": ""})


def against_trend(p: dict) -> bool:
    """Côté contre la tendance de fond (checklist EmperorBTC : MM 50 et 200 jours)."""
    return _trend(p)["ok"] is False


def zone_words(e: dict) -> str:
    """D'où vient le prix d'une entrée : la zone, ses bornes et ses sources."""
    z = e["zone"]
    if z is None:
        return ""
    if e["kind"] == "repli":
        word = "support" if e["side"] == "long" else "résistance"
    else:
        word = "au-dessus de" if e["side"] == "long" else "sous"
    return f"{word} {z.label()} : {z.why()}"


def entry_line(e: dict) -> str:
    """Une entrée en prix : ordre, d'où vient le prix, stop, objectifs et R/R."""
    where = f" ({zone_words(e)})" if e["zone"] is not None else ""
    targets = " / ".join(S._num(t["price"]) for t in e["targets"])
    rr = " / ".join(f"{x:g}" for x in e["rr"])
    return (f"{KIND_FR[e['kind']].capitalize()} : {ORDER_FR[(e['side'], e['kind'])]} **{S._num(e['entry'])}**{where} · "
            f"stop {S._num(e['stop'])} · objectifs {targets} · R/R {rr}")


def take_command(symbol: str, e: dict, with_entry: bool = True) -> str:
    """Commande Discord qui fait suivre ces prix par JARVIS (entrée, stop et objectifs)."""
    entry = f" {e['entry']:g}" if with_entry else ""
    side = " short" if e["side"] == "short" else ""
    tps = " ".join(f"{t['price']:g}" for t in e["targets"])
    return f"`!prends {symbol}{entry}{side} stop {e['stop']:g} tp {tps}`"


def decide(plans: list[dict]) -> dict:
    """ENTRER maintenant, des ordres conditionnels (repli ou cassure) ou ATTENDRE, à partir des entrées en prix
    de chaque plan (`entries`, calculées sur la carte des niveaux). Renvoie la décision, le plan retenu, les
    ordres (entrées) à poser et les raisons. Un plan que la méthode championne écarte (grade.ecarte, apply_grade)
    n'a pas d'entrée : le scanner ne le proposerait pas et le grade ne mesure pas ces plans."""
    floor = rr_floor()
    ranked = sorted(plans, key=lambda p: p["score"]["composite"], reverse=True)
    reasons = [f"{_side(p['side'])} : contre la tendance de fond ({_trend(p)['detail']})"
               for p in ranked if against_trend(p)]
    cat = next((p.get("catalyst") for p in plans if p.get("catalyst")), None) or {}
    hours = cat.get("hours_to_earnings")
    veto = settings["earnings_veto_hours"]
    if plans[0]["market"] == "stock" and hours is not None and veto and hours <= veto:
        return {"decision": "ATTENDRE", "plan": None, "orders": [], "reasons": reasons + [
            f"résultats trimestriels dans {hours:.0f} h : aucune entrée avant, le gap qui suit est un pari que le "
            "plan ne couvre pas"]}
    allowed = []
    for p in ranked:
        if against_trend(p):
            continue
        if p["side"] == "long" and cat.get("dilution"):
            reasons.append("long : augmentation de capital dans les actus (de nouvelles actions arrivent)")
            continue
        if (p.get("grade") or {}).get("ecarte"):  # hors de la méthode championne : le grade ne mesure pas ce plan
            reasons.append(f"{_side(p['side'])} : {p['grade']['raison']} : le scanner ne le proposerait pas")
            continue
        allowed.append(p)
    for p in allowed:
        now = next((e for e in p["entries"] if e["kind"] == "maintenant"), None)
        if now and now["ok"] and p["score"]["go"]:
            return {"decision": f"ENTRER {p['side'].upper()}", "plan": p, "orders": [now], "reasons": reasons + [
                f"{p['checklist']['verdict']} (checklist EmperorBTC {p['checklist']['score']}/{p['checklist']['max_score']}), "
                f"note {p['score']['composite']:g}/10 GO, R/R {now['rr'][1]:g} sur le 2e objectif"]}
    for p in allowed:
        now = next((e for e in p["entries"] if e["kind"] == "maintenant"), None)
        if now and not now["ok"]:
            reasons.append(f"{_side(p['side'])} maintenant : R/R {now['rr'][1]:g} sur le 2e objectif, trop court "
                           f"(minimum {floor:g})")
        elif now:
            reasons.append(f"{_side(p['side'])} maintenant : plan NO GO "
                           f"({' ; '.join(p['score']['reasons']) or 'note trop basse'})")
    for p in allowed:
        orders = [e for e in p["entries"] if e["kind"] != "maintenant" and e["ok"]]
        if orders:
            kinds = " OU ".join(KIND_FR[e["kind"]].split()[-1].upper() for e in orders)
            return {"decision": f"{p['side'].upper()} SUR {kinds}", "plan": p, "orders": orders, "reasons": reasons}
    watch = [(p, e) for p in allowed for e in p["entries"] if e["kind"] != "maintenant"]
    if watch:
        p, e = max(watch, key=lambda pe: pe[1]["rr"][1])
        reasons.append(f"aucune entrée à R/R {floor:g} ou plus sur le 2e objectif ; la meilleure : {_side(p['side'])} "
                       f"{KIND_FR[e['kind']]} à {S._num(e['entry'])}, R/R {e['rr'][1]:g}")
    return {"decision": "ATTENDRE", "plan": None, "orders": [], "reasons": reasons}


def decision_icon(decision: str) -> str:
    if decision.startswith(("ENTRER LONG", "LONG")):
        return "🟢"
    if decision.startswith(("ENTRER SHORT", "SHORT")):
        return "🔴"
    return ICON.get(decision, "")


def lab_measure(market: str, symbol: str, path: str | None = None) -> dict | None:
    """Ce que le labo a mesuré sur la méthode de génération en cours (plans longs rejoués : origine « replay »,
    hors bloc scellé ; ni le direct ni les autres versions n'y entrent) : sur ce symbole et sur tout le
    marché, résultat des plans GO et NO GO déclenchés, en R du labo. Deux AVG SQL, pas de calcul par trade
    (commande interactive). None sans base."""
    from app.lab import db as lab_db  # import tardif : le labo importe le moteur de plans

    conn = lab_db.open_readonly(path) if path else lab_db.open_readonly()
    if conn is None:
        return None
    try:
        try:
            version = lab_db.generation_version(conn)
        except sqlite3.Error:  # base sans table des versions (d'avant le Labo 3) : une seule version
            version = None
        scope, scope_args = "origin = 'replay' AND side = 'long'", ()
        if version is not None:
            scope, scope_args = scope + " AND method_version = ?", (version,)
        out = {"version": version}
        for key, where, args in (("symbole", "market = ? AND symbol = ?", (market, symbol)),
                                 ("marche", "market = ?", (market,))):
            rows = conn.execute(
                f"SELECT decision, COUNT(*), AVG(r_net), SUM(r_net > 0) FROM trades WHERE {where} AND {scope} "
                "AND triggered = 1 AND r_net IS NOT NULL GROUP BY decision", args + scope_args).fetchall()
            out[key] = {d or "?": {"n": n, "r": round(r, 2), "wins": w or 0} for d, n, r, w in rows}
        return out
    except sqlite3.Error as exc:
        log.warning("Mesures du labo illisibles : %s", exc)
        return None
    finally:
        conn.close()


def note_proven(m: dict | None) -> bool:
    """Vrai si le labo a mesuré que les plans GO font nettement mieux que les NO GO sur ce marché."""
    market = (m or {}).get("marche", {})
    go, nogo = market.get("GO"), market.get("NO GO")
    return bool(go and nogo and go["n"] >= 30 and nogo["n"] >= 30 and go["r"] > 0 and go["r"] - nogo["r"] >= 0.1)


def note_line(m: dict | None) -> str:
    """Ce que vaut la note GO/NO GO d'après le labo, sans parler de moyennes."""
    if m is None:
        return "-# Labo indisponible : rien ne dit encore si la note GO/NO GO prédit quelque chose."
    if note_proven(m):
        return "-# Le labo a mesuré que les plans GO font mieux que les NO GO sur ce marché."
    return ("-# La note GO/NO GO n'a pas encore prouvé au labo qu'elle gagne : elle décrit la méthode, "
            "elle ne prédit rien.")


def _judged(it: dict) -> dict | None:
    """Les mesures qui jugent une stratégie, comme le tableau du labo : une stratégie choisie après avoir lu des
    jours (Killmode, n° 4, n° 11 : « jours_vus_jusqu_au ») n'est jugée que sur les jours suivants (None tant qu'il
    n'y en a pas) ; les autres, sur tous leurs jours."""
    return it.get("jours_suivants") if "jours_vus_jusqu_au" in it else it


def _proven(it: dict) -> bool:
    """✅ de `!strategies` : candidate_compte (n° 10), ou candidate seule dans un tableau d'avant la n° 10."""
    return bool(it.get("candidate_compte", it.get("candidate")))


def lab_entries(market: str, path: str | None = None) -> dict | None:
    """Pour chaque type d'entrée (repli, cassure), la version la plus favorable parmi les stratégies proches que le
    labo v2 a mesurées sur ce marché (tableau en cache, lecture seule, sans le bloc scellé) : une version prouvée
    s'il y en a une, sinon celle qui bat le plus son témoin aléatoire. Lue comme `!strategies` : prouvée (✅) si
    elle est aussi positive à la mise de Simão (candidate_compte, n° 10 ; « candidate » seule : avantage, perd à
    20 €), seuil en t des jours de la stratégie (seuil_t, n° 8), jours suivants (_judged). None sans base ou sans
    tableau."""
    from app.lab import db as lab_db  # import tardif : le labo importe le moteur de plans
    from app.lab import strat_stats as STRAT

    conn = lab_db.open_readonly(path) if path else lab_db.open_readonly()
    if conn is None:
        return None
    try:
        rep = STRAT.cached(conn)
    finally:
        conn.close()
    if not rep:
        return None
    items = (rep.get("strategies") or {}).get(market) or {}
    out: dict = {"seuil": (rep.get("declaration") or {}).get("z_candidate")}
    for kind, names in LAB_KIN.items():
        found = []
        for name, it in items.items():
            j = _judged(it) if name in names else None
            if j and (j.get("temoin") or {}).get("z") is not None:
                found.append(j)
        if not found:
            continue
        best = max(found, key=lambda it: (_proven(it), bool(it.get("candidate")), it["temoin"]["z"]))
        ci = best.get("ic95_ibkr") or best.get("ic95")
        out[kind] = {"versions": len(found), "nom": best["nom"], "z": best["temoin"]["z"], "jours": best.get("jours"),
                     "prouvee": _proven(best), "avantage": bool(best.get("candidate")), "seuil": best.get("seuil_t"),
                     "perd": bool(ci and ci[1] < 0)}
    return out


def entries_note(lab: dict | None, kinds: set[str], market: str, adds: bool = False) -> str:
    """Ce que valent les entrées (ou les renforts) sur repli et sur cassure : les prix de la méthode, et pour
    chaque type la version la plus favorable testée au labo face au hasard (comparaison, pas de moyenne)."""
    head = ("-# Renforts : prix de la méthode EmperorBTC, jamais testés au labo comme renforts" if adds else
            "-# Entrées : prix de la méthode EmperorBTC, pas une prédiction")
    if lab is None:
        return head + " ; labo indisponible, aucune mesure face au hasard."
    parts, days = [], []
    for kind in ("repli", "cassure"):
        if kind not in kinds:
            continue
        k = lab.get(kind)
        if not k:
            parts.append(f"{KIND_FR[kind]} : pas encore mesuré")
            continue
        days.append(k["jours"] or 0)
        if k["prouvee"]:
            parts.append(f"{KIND_FR[kind]} : « {k['nom']} » est prouvée ✅")
            continue
        if k.get("avantage"):  # n° 10 : bat le hasard, mais pas à la mise de Simão
            parts.append(f"{KIND_FR[kind]} : « {k['nom']} » bat le hasard mais perd à 20 € ☑️")
            continue
        seuil = k.get("seuil") or lab.get("seuil")
        if k["z"] <= -2:
            verdict = "pire que le hasard"
        elif k["z"] < 2:
            verdict = "pas mieux que le hasard"
        else:
            verdict = "mieux que le hasard, sans preuve" + (f" (il faut z {seuil:g})" if seuil else "")
        best = f"meilleure des {k['versions']} versions testées" if k["versions"] > 1 else "version testée"
        parts.append(f"{KIND_FR[kind]} : {verdict}{', et perd après frais' if k['perd'] else ''} "
                     f"({best}, z {k['z']:+.1f})")
    where = f"{'actions' if market == 'stock' else 'crypto'}" + (f", {max(days)} j rejoués" if days else "")
    return (f"{head}. {'Comme entrées, au' if adds else 'Au'} labo ({where}), " + " ; ".join(parts)
            + ". Détail : `!strategies`.")


async def opinion(symbol: str, market: str, now: datetime | None = None) -> dict:
    """Calcule les deux plans (long et short), la carte des niveaux et les entrées en prix de chaque côté.
    PlanError si aucun plan n'est possible."""
    now = now or datetime.now(timezone.utc)
    plans, errors, cat = [], [], None
    for side in ("long", "short"):
        try:
            p = await S.build_plan(symbol, market, side, now=now, cat=cat)
        except S.PlanError as exc:
            errors.append(f"{side} : {exc}")
            continue
        cat = p.get("catalyst")  # lu une fois pour les deux côtés
        plans.append(p)
    if not plans:
        raise S.PlanError(" ; ".join(errors))
    sym, mkt = plans[0]["symbol"], plans[0]["market"]
    bars = await S.candles(sym, mkt, S.SETTINGS[mkt]["interval"])  # en cache : celles du plan
    daily = await S._optional_candles(sym, mkt, 1440)
    m = L.build(bars, daily, mkt, now)
    price = m["bars"][-1]["c"]
    for p in plans:
        p["entries"] = L.entries(m, p["side"], price, p["stop"], rr_floor())
    view = decide(plans)
    lab = await asyncio.to_thread(lab_measure, mkt, sym)
    kin = await asyncio.to_thread(lab_entries, mkt)
    return view | {"symbol": sym, "market": mkt, "price": price, "plans": plans, "errors": errors, "lab": lab,
                   "lab_entries": kin, "catalyst": cat, "created_at": now.isoformat(timespec="seconds")}


def opinion_text(o: dict) -> str:
    sym, orders = o["symbol"], o["orders"]
    lines = [f"🧭 **Avis de JARVIS sur {sym}** ({MARKET_FR[o['market']]}) · {S._num(o['price'])}"]
    head = f"**{decision_icon(o['decision'])} {o['decision']}**"
    if orders:
        head += " : " + " ou ".join(f"{ORDER_FR[(e['side'], e['kind'])]} {S._num(e['entry'])} (stop {S._num(e['stop'])})"
                                    for e in orders)
        if len(orders) > 1:
            head += (" ; chez IBKR, en groupe OCA" if o["market"] == "stock" else " ; en OCO") + \
                " : le premier exécuté annule l'autre"
    lines.append(head)
    lines += [f"• {r}" for r in o["reasons"]]
    kinds = set()
    for p in o["plans"]:
        if not against_trend(p):
            side = "Long" if p["side"] == "long" else "Short"
            lines += [f"{side} · {entry_line(x)}{'' if x['ok'] else ' (R/R trop court)'}" for x in p["entries"]]
            kinds |= {x["kind"] for x in p["entries"] if x["kind"] != "maintenant"}
    grade = next((p["grade"] for p in o["plans"] if p.get("grade")), None)
    if grade:  # le risque réel que le grade permet, comme sous un plan (0 % tant que la méthode n'a rien prouvé)
        from app.lab import grade as GRADE  # import tardif : le labo importe le moteur de plans

        lines.append(GRADE.plan_line(grade))
    for p in o["plans"]:
        lines.append(f"-# Checklist EmperorBTC {_side(p['side'])} : {p['checklist']['verdict']} "
                     f"({p['checklist']['score']}/{p['checklist']['max_score']}) · note {p['score']['composite']:g}/10 "
                     f"{p['score']['decision']}")
    lines += [f"-# Plan impossible côté {err}" for err in o["errors"]]
    company = company_line(o.get("catalyst"), datetime.fromisoformat(o["created_at"]))
    if company:
        lines.append(company)
    lines.append(note_line(o["lab"]))
    if orders:
        now_entry = orders[0]["kind"] == "maintenant"
        lines.append(f"-# {'Pour le suivre' if now_entry else 'Une fois exécuté'} : "
                     + " ou ".join(take_command(sym, e, with_entry=not now_entry) for e in orders))
    if kinds:
        lines.append(entries_note(o.get("lab_entries"), kinds, o["market"]))
    if o["market"] == "stock":
        lines.append(f"-# Tout sur la société : `!societe {sym}`")
    return "\n".join(lines)


# --- Positions ouvertes : ordres en prix, et sortir, alléger ou garder ----------------------

def _sign(trade: dict) -> int:
    return 1 if trade["side"] == "long" else -1


def r_of(trade: dict, price: float) -> float:
    risk = abs(trade["entry"] - trade["stop"])
    return _sign(trade) * (price - trade["entry"]) / risk if risk else 0.0


def exit_r(trade: dict, price: float) -> float:
    """Gain en R si tout ce qui reste est vendu à `price` (un tiers par objectif, comme realized_r)."""
    legs = len(trade["targets"]) or 1
    hit = min(trade["targets_hit"], legs)
    done = sum(r_of(trade, t["price"]) for t in trade["targets"][:hit])
    return round((done + (legs - hit) * r_of(trade, price)) / legs, 2)


def overnight_gaps(daily: list[dict] | None, side: str, price: float, stop: float) -> dict | None:
    """Gaps d'ouverture des nuits passées (ouverture du jour contre clôture de la veille) : nuits où le gap
    aurait sauté le stop depuis le prix actuel, et gap contre la position du pire dixième des nuits."""
    if not daily or len(daily) < MIN_GAPS + 1:
        return None
    sign = 1 if side == "long" else -1
    gaps = [sign * (b["o"] - a["c"]) / a["c"] for a, b in zip(daily, daily[1:]) if a["c"]][-GAP_DAYS:]
    if len(gaps) < MIN_GAPS:
        return None
    room = sign * (price - stop) / price
    adverse = sorted(-g for g in gaps)
    worst = max(adverse[int(0.9 * len(adverse))], 0.0)
    through = sum(g <= -room for g in gaps)
    return {"nights": len(gaps), "through": through, "through_stop": round(through / len(gaps), 3),
            "room_pct": round(room * 100, 2), "p90_adverse_pct": round(worst * 100, 2),
            "p90_open": L.tick(price * (1 - sign * worst), "stock")}


def earnings_soon(cat: dict | None, now: datetime) -> float | None:
    """Heures avant des résultats publiés d'ici la fin de la prochaine séance (None sinon)."""
    hours = (cat or {}).get("hours_to_earnings")
    if hours is None:
        return None
    end = CAL.nth_session_close(now, 2 if CAL.session_open(now) else 1)
    return hours if hours * 3600 <= (end - now).total_seconds() else None


def _parts(legs: int) -> list[str]:
    """Part de ce qui reste vendue à chaque prix."""
    if legs == 1:
        return ["tout"]
    if legs == 2:
        return ["la moitié", "le reste"]
    if legs == 3:
        return ["1/3", "1/3", "le reste"]
    return [f"1/{legs}"] * (legs - 1) + ["le reste"]


def _sell(side: str) -> str:
    return "vends" if side == "long" else "rachète"


def sales_text(a: dict) -> str:
    parts = zip(_parts(len(a["sales"])), a["sales"])
    return f"{_sell(a['side']).capitalize()} " + ", ".join(f"{part} à **{S._num(s['price'])}**" for part, s in parts)


def orders_summary(a: dict) -> str:
    """Les ordres en une ligne (gardée sur le trade avec le conseil, affichée dans le cockpit)."""
    sell = _sell(a["side"])
    if a["decision"] == "SORTIR":
        return f"{sell} tout ({a['why']})"
    head = f"{sell} la moitié maintenant, puis " if a["decision"] == "ALLÉGER" else f"{sell} "
    sold = ", ".join(f"{part} à {S._num(s['price'])}" for part, s in zip(_parts(len(a["sales"])), a["sales"]))
    return f"{head}{sold} ; stop {S._num(a['stop'])}"


def advise(trade: dict, price: float, bars: list[dict], daily: list[dict] | None, cat: dict | None,
           now: datetime, m: dict | None = None) -> dict:
    """Ordres en prix et décision pour une position ouverte, sans réseau (règles dans l'en-tête du module).

    `trade` : entry, stop (stop d'origine, pour compter en R), stop_current (None : à placer sur la structure),
    targets et targets_hit (une part par objectif ; sans objectifs, trois parts). `m` : carte des niveaux déjà
    calculée sur ces bougies."""
    market, side, sign = trade["market"], trade["side"], _sign(trade)
    m = m or L.build(bars, daily, market, now)
    current = trade.get("stop_current")
    stop, stop_why = L.trailing_stop(m, side, price, current)
    if stop is None:  # ni stop donné ni creux utilisable : le stop du plan sur le prix d'entrée
        stop, stop_why = trade["stop"], "stop du plan sous la structure de ton entrée"
    raised = current is not None and stop_why is not None
    targets = trade.get("targets") or []
    legs = max(len(targets) - trade["targets_hit"], 1) if targets else 3
    sales = L.sales(m, side, price, legs)
    winning = (price - trade["entry"]) * sign > 0
    gaps = overnight_gaps(daily, side, price, stop) if market == "stock" else None
    earnings = earnings_soon(cat, now) if market == "stock" else None
    dilution = bool((cat or {}).get("dilution"))
    interval = S.SETTINGS[market]["interval"]
    closed = [b for b in bars if b["t"] + interval * 60 <= now.timestamp()]
    signals = SIG.exit_signals(closed, side) if winning and len(closed) >= 20 else []
    nxt = targets[trade["targets_hit"]] if trade["targets_hit"] < len(targets) else None
    late = bool(nxt and nxt.get("eta_latest") and now >= datetime.fromisoformat(nxt["eta_latest"]))
    sym = trade["symbol"]
    if (price - stop) * sign <= 0:
        decision, why = "SORTIR", f"le prix ({S._num(price)}) a passé le stop ({S._num(stop)}) : la structure est cassée"
    elif earnings is not None:
        decision, why = "SORTIR", (f"résultats trimestriels dans {earnings:.0f} h : vends tout avant, le gap qui suit "
                                   "est un pari que le plan ne couvre pas")
    elif dilution and side == "long":
        decision, why = "SORTIR", "augmentation de capital dans les actus : de nouvelles actions arrivent sur le marché"
    elif late and not winning:
        decision, why = "SORTIR", f"{nxt['name']} en retard et la position ne gagne pas : le scénario ne se déroule pas"
    elif signals:
        decision, why = "ALLÉGER", f"{signals[0][1]} : {_sell(side)} la moitié maintenant (vers {S._num(price)})"
    elif gaps and gaps["through_stop"] >= GAP_LIMIT and CAL.session_open(now):  # marché fermé : la nuit est déjà là
        decision, why = "ALLÉGER", (f"{sym} a ouvert au-delà de ce stop {gaps['through']} nuits sur {gaps['nights']} : "
                                    f"ne garde que la moitié pendant la nuit ({_sell(side)} l'autre en séance, "
                                    f"vers {S._num(price)})")
    else:
        decision, why = "GARDER", "rien ne dit de sortir : pose ces ordres"
    # Renforcer seulement une position gagnante qu'on garde entière (pas pendant qu'on allège ou qu'on sort).
    adds = L.adds(m, side, price, stop, rr_floor(), trade["entry"]) if winning and decision == "GARDER" else []
    facts = []
    if gaps:
        beyond = "plus bas" if side == "long" else "plus haut"
        line = (f"Nuit : une ouverture à {_fr(-sign * gaps['room_pct'])} % ou {beyond} saute ce stop ; "
                f"{sym} l'a fait {gaps['through']} nuits sur {gaps['nights']}")
        if gaps["p90_adverse_pct"] > 0:
            line += (f", et 1 nuit sur 10 il ouvre à {_fr(-sign * gaps['p90_adverse_pct'], 1)} % ou pire "
                     f"(vers {S._num(gaps['p90_open'])})")
        facts.append(line + ". Par défaut, IBKR ne déclenche pas un stop hors séance : un gap est exécuté "
                     "à l'ouverture.")
    company = company_line(cat, now) if market == "stock" else None
    if company:
        facts.append(company.removeprefix("🏢 "))
    left = targets[trade["targets_hit"]:]
    if left and any(abs(t["price"] - s["price"]) > 0.5 * m["atr"] for t, s in zip(left, sales)):
        facts.append("Objectifs suivis par JARVIS : " + ", ".join(f"{t['name']} {S._num(t['price'])}" for t in left)
                     + " ; les ventes ci-dessus viennent de la carte des niveaux d'aujourd'hui.")
    a = {"id": trade.get("id"), "symbol": sym, "market": market, "side": side, "entry": trade["entry"],
         "decision": decision, "why": why, "price": price, "r_now": round(r_of(trade, price), 2),
         "exit_r": exit_r(trade, price), "stop": stop, "stop_why": stop_why, "stop_was": current,
         "stop_raised": raised, "sales": sales, "adds": adds, "gaps": gaps, "earnings_hours": earnings,
         "dilution": dilution, "signals": [k for k, _ in signals], "late": late, "facts": facts,
         "at": now.isoformat(timespec="seconds")}
    a["orders"] = orders_summary(a)
    return a


def _signed(x: float) -> str:
    return "0" if abs(x) < 1e-9 else f"{'+' if x > 0 else '-'}{S._num(abs(x))}"  # bruit de calcul : zéro


def add_line(c: dict, opened: float, market: str) -> str:
    """Un renfort en prix, jugé sur toute la position (avis de GPT, 2026-10-08) : au stop qui suivra le renfort,
    ce que perd chaque part ajoutée et ce que devient chaque part déjà détenue."""
    buy = "achat" if c["side"] == "long" else "vente"
    unit = "action" if market == "stock" else "unité"
    held = (c["stop"] - opened) * (1 if c["side"] == "long" else -1)  # par part déjà détenue, au stop
    risk = (f" · au stop : {_signed(-abs(c['entry'] - c['stop']))} par {unit} ajoutée, {_signed(held)} par {unit} "
            "déjà détenue")
    if c["kind"] == "repli":
        word = "support" if c["side"] == "long" else "résistance"
        text = f"sur repli : {buy} limite **{S._num(c['entry'])}** ({word} {c['zone'].label()}), même stop"
    else:
        text = (f"sur cassure : {buy} stop **{S._num(c['entry'])}** ({'au-dessus de' if c['side'] == 'long' else 'sous'} "
                f"{c['zone'].label()})")
        if c.get("new_stop") is not None:
            text += f", puis stop de toute la position à {S._num(c['new_stop'])} (sous la zone cassée)"
    return f"• Renforcer {text} · R/R {' / '.join(f'{x:g}' for x in c['rr'])}{risk}"


def advice_text(a: dict) -> str:
    arrow = "🟢" if a["side"] == "long" else "🔴"
    lines = [f"{arrow} **{a['symbol']}** · {S._num(a['price'])} ({_fr(a['price'] - a['entry'])} depuis "
             f"{S._num(a['entry'])}) · **{ICON[a['decision']]} {a['decision']}** : {a['why']}"]
    if a["decision"] != "SORTIR":
        sales = sales_text(a)
        if a["decision"] == "ALLÉGER":
            sales = "Puis, sur le reste : " + sales[0].lower() + sales[1:]
        lines.append("• " + sales)
        lines.append("-# " + " · ".join(f"{S._num(s['price'])} {L.sale_source(s, a['side'])}" for s in a["sales"]))
        stop = f"• Stop **{S._num(a['stop'])}** sur tout ce qui reste"
        if a["stop_raised"]:
            stop += f", remonté depuis {S._num(a['stop_was'])} : {a['stop_why']}"
            if a["id"] is not None:
                stop += " (JARVIS suit ce nouveau stop)"
        elif a["stop_was"] is None:
            stop += f" : {a['stop_why']}"
        else:
            stop += " : garde-le, la structure ne le remonte pas encore"
        lines.append(stop + " ; ne l'éloigne jamais.")
        lines += [add_line(c, a["entry"], a["market"]) for c in a["adds"]]
        if a["adds"]:
            lines.append(entries_note(a.get("lab_entries"), {c["kind"] for c in a["adds"]}, a["market"], adds=True))
    lines += [f"-# {f}" for f in a["facts"]]
    return "\n".join(lines)


def record(trade: dict, a: dict, now: datetime) -> None:
    """Garde le conseil sur le trade (un par jour de New York, le dernier remplace) pour le mesurer. Un stop
    remonté par le conseil devient le stop suivi (comme la remontée à l'entrée après le TP2) : les alertes
    et le bilan suivent le stop que JARVIS a dit de poser."""
    day = now.astimezone(CAL.NEW_YORK).date().isoformat()
    kept = [c for c in trade.get("conseils", []) if c.get("day") != day]
    kept.append({"day": day, "at": a["at"], "decision": a["decision"], "price": a["price"],
                 "exit_r": a["exit_r"], "why": a["why"], "orders": a.get("orders")})
    trade["conseils"] = kept
    sign = _sign(trade)
    if (a.get("stop_raised") and trade.get("status") == "open"
            and (a["stop"] - trade["stop_current"]) * sign > 0 and (a["price"] - a["stop"]) * sign > 0):
        trade["stop_current"] = a["stop"]
        trade["stop_moved_at"] = int(now.timestamp())  # la bougie en cours ne peut pas toucher ce nouveau stop


def advice_stats(trades: list[dict]) -> dict:
    """Pour chaque conseil donné sur un trade depuis terminé : ce que garder a rapporté de plus (ou de
    moins) que sortir au moment du conseil. Juste : SORTIR quand garder a perdu, GARDER quand garder a payé."""
    out: dict = {}
    for t in trades:
        final = S.realized_r(t)
        if final is None:
            continue
        for c in t.get("conseils", []):
            g = out.setdefault(c["decision"], {"n": 0, "right": 0, "deltas": []})
            delta = final - c["exit_r"]
            g["n"] += 1
            g["deltas"].append(delta)
            g["right"] += (delta < 0) if c["decision"] == "SORTIR" else (delta >= 0) if c["decision"] == "GARDER" else 0
    for g in out.values():
        g["avg_delta"] = round(sum(g.pop("deltas")) / g["n"], 2)
    return out


def stats_line(st: dict) -> str:
    """Combien de conseils se sont révélés justes, compté conseil par conseil."""
    if not st:
        return "-# Bilan des conseils : aucun trade conseillé n'est encore terminé."
    parts = []
    for d in ("SORTIR", "ALLÉGER", "GARDER"):
        if d in st:
            g = st[d]
            parts.append(f"{d} juste {g['right']}/{g['n']}" if d != "ALLÉGER" else f"{d} ×{g['n']}")
    return "-# Bilan des conseils : " + " · ".join(parts)


async def _inputs(symbol: str, market: str, now: datetime) -> tuple[list[dict], list[dict] | None, dict | None]:
    bars = await S.candles(symbol, market, S.SETTINGS[market]["interval"])
    daily = await S._optional_candles(symbol, market, 1440)
    cat = await C.safe_check(symbol, now) if market == "stock" else None
    return bars, daily, cat


async def advise_trade(trade: dict, now: datetime | None = None) -> dict:
    """Conseil sur un trade suivi (prix : le dernier lu par le suivi des TP/SL)."""
    now = now or datetime.now(timezone.utc)
    bars, daily, cat = await _inputs(trade["symbol"], trade["market"], now)
    return await _with_lab(advise(trade, trade["last_price"], bars, daily, cat, now))


async def _with_lab(a: dict) -> dict:
    """Ce que le labo dit des entrées proches, lu seulement quand le conseil propose des renforts."""
    if a["adds"]:
        a["lab_entries"] = await asyncio.to_thread(lab_entries, a["market"])
    return a


async def advise_book(trades: list[dict], now: datetime | None = None) -> list[dict]:
    """Conseil pour chaque trade ouvert ; un trade en échec donne une ligne d'erreur, pas une exception."""
    now = now or datetime.now(timezone.utc)
    out = []
    for t in trades:
        try:
            out.append(await advise_trade(t, now))
        except Exception as exc:
            log.warning("Conseil %s impossible : %s", t["symbol"], exc)
            out.append({"id": t["id"], "symbol": t["symbol"], "error": str(exc)})
    return out


async def advise_position(symbol: str, market: str, side: str, entry: float, stop: float | None = None,
                          now: datetime | None = None) -> dict:
    """Position prise hors de JARVIS (ex. sur IBKR), entière (aucune part vendue) : ordres en prix au cours
    actuel. Sans stop donné, le stop vient de la structure ; les R se comptent depuis le stop du plan sur l'entrée."""
    now = now or datetime.now(timezone.utc)
    symbol, market, side = symbol.strip().upper(), S._market(market), side.lower()
    if side not in ("long", "short"):
        raise S.PlanError("side doit valoir long ou short")
    bars, daily, cat = await _inputs(symbol, market, now)
    m = L.build(bars, daily, market, now)
    if m["atr"] <= 0:
        raise S.PlanError(f"{symbol} : pas assez de bougies pour placer des niveaux")
    price = m["bars"][-1]["c"]
    if (entry - (stop if stop is not None else entry)) * (1 if side == "long" else -1) < 0:
        raise S.PlanError("le stop doit être sous l'entrée pour un long, au-dessus pour un short")
    ref = stop if stop is not None else P._invalidation(m["bars"][:-1], side, entry, m["atr"])[0]
    trade = {"symbol": symbol, "market": market, "side": side, "entry": entry, "stop": ref,
             "stop_current": stop, "targets": [], "targets_hit": 0}
    return await _with_lab(advise(trade, price, m["bars"], daily, cat, now, m))


def closing_due(now: datetime, last_day: str | None) -> bool:
    """Message automatique : séance US ouverte, à CLOSE_ADVICE_MINUTES de la clôture ou moins, une fois par jour."""
    if not CAL.session_open(now):
        return False
    bounds = CAL.session_bounds(now.astimezone(CAL.NEW_YORK).date())
    left = (bounds[1] - now).total_seconds() / 60
    return left <= CLOSE_ADVICE_MINUTES and last_day != now.astimezone(CAL.NEW_YORK).date().isoformat()


def closing_text(advices: list[dict], stats: dict, minutes_left: float | None = None,
                 title: str = "🔔 **Avant la clôture US**") -> str:
    head = title + (f" (dans {minutes_left:.0f} min)" if minutes_left else "")
    lines = [head + " : les ordres à poser sur chaque position, en prix"]
    for a in advices:
        lines.append(f"⚠️ **{a['symbol']}** : conseil impossible ({a['error']})" if "error" in a else advice_text(a))
    lines.append(stats_line(stats))
    lines.append("-# Règles fixes, pas encore validées par le labo : chaque conseil est gardé et mesuré.")
    return "\n".join(lines)
