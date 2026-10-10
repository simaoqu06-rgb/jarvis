"""Avis de JARVIS sur un actif choisi par Simão, et conseil sur les positions ouvertes avant la clôture.

`!avis NVDA` : JARVIS calcule le plan long ET le plan short, dit lequel il prendrait (ou s'il attend)
et pourquoi, puis affiche ce que le labo a mesuré. La note /10 n'a pas encore prouvé qu'elle prédit
quoi que ce soit (labo v1 : GO ≈ NO GO) : l'avis le dit au lieu de faire croire à une fiabilité.

`!positions` (et un message automatique 30 min avant la clôture US) : pour chaque position ouverte
dont ni le TP ni le SL n'est touché, SORTIR, ALLÉGER ou GARDER, avec les faits qui décident :
  1. résultats trimestriels avant la fin de la prochaine séance → SORTIR (pari binaire hors plan) ;
  2. garder jusqu'au prochain objectif a une espérance négative, rejouée sur l'historique de l'actif
     depuis le prix actuel avec le stop suivi → SORTIR ;
  3. objectif en retard (heure passée à laquelle 3 sur 4 des cas qui l'ont touché l'avaient fait, soit
     reached_by_p75 de tous les cas) et position qui ne gagne pas, seulement si garder n'est pas mesurable
     (sinon la règle 2 a déjà jugé depuis le prix actuel, et le retard reste un simple fait) → SORTIR ;
  4. signe de retournement sur une position gagnante → ALLÉGER (moitié dehors, stop à l'entrée) ;
  5. gap de nuit plus grand que la distance au stop dans au moins 10 % des nuits passées → ALLÉGER ;
  6. sinon GARDER, avec le stop à laisser et l'heure estimée du prochain objectif.
Ces règles sont fixes et pas encore validées : chaque conseil donné sur un trade suivi est gardé
(un par trade et par jour) et comparé à ce que la position a fait ensuite (`!positions` en fait le bilan).
"""
import asyncio
import logging
import sqlite3
from datetime import datetime, timezone

from app.config import env_float
from app.trading import calendar as CAL
from app.trading import catalyst as C
from app.trading import plan as P
from app.trading import service as S
from app.trading import signals as SIG

log = logging.getLogger(__name__)
GAP_LIMIT = env_float("JARVIS_GAP_LIMIT", 0.10)  # part des nuits où le gap aurait sauté le stop
CLOSE_ADVICE_MINUTES = env_float("JARVIS_CLOSE_ADVICE_MINUTES", 30)  # message auto, minutes avant la clôture
HOLD_HORIZON = {"stock": 26, "crypto": 24}  # bougies du plan : une séance (15 min) ou 24 h (1 h)
GAP_DAYS = 250  # nuits regardées pour le risque de gap
MIN_GAPS = 30
ICON = {"SORTIR": "🚪", "ALLÉGER": "✂️", "GARDER": "✊", "ENTRER LONG": "🟢", "ENTRER SHORT": "🔴", "ATTENDRE": "⏸️"}
MARKET_FR = {"stock": "action", "crypto": "crypto"}


def _fr(x: float, digits: int = 2) -> str:
    return f"{x:+.{digits}f}"  # point décimal, comme plan_text affiché dans le même message


# --- Avis sur un actif ------------------------------------------------------------------

def best_target(p: dict) -> dict:
    """Objectif à la meilleure espérance rejouée (vendre tout à ce TP)."""
    return max(p["targets"], key=lambda t: t["expectancy_r"])


def side_line(p: dict) -> str:
    sc, b = p["score"], best_target(p)
    why = f" ({' ; '.join(sc['reasons'])})" if sc["reasons"] and not sc["go"] else ""
    return (f"{'Long' if p['side'] == 'long' else 'Short'} : {sc['decision']} {sc['composite']:g}/10{why} · "
            f"meilleur objectif {b['name']} {_fr(b['expectancy_r'])} R (touché avant le stop {b['hit_rate']:.0%})")


def decide(plans: list[dict]) -> dict:
    """ENTRER LONG / ENTRER SHORT / ATTENDRE à partir des deux plans (un seul si l'autre est impossible).

    Un côté GO passe ; s'il y en a deux, la meilleure note. Un GO dont aucun objectif n'a une espérance
    rejouée positive sur l'historique de l'actif est ramené à ATTENDRE : la note seule n'est pas prouvée.
    """
    go = sorted((p for p in plans if p["score"]["go"]), key=lambda p: p["score"]["composite"], reverse=True)
    best = max(plans, key=lambda p: p["score"]["composite"])
    if go:
        p = go[0]
        b = best_target(p)
        reasons = [f"{p['checklist']['verdict']} (checklist EmperorBTC {p['checklist']['score']}/{p['checklist']['max_score']}), "
                   f"note {p['score']['composite']:g}/10"]
        if b["expectancy_r"] <= 0:
            return {"decision": "ATTENDRE", "plan": p, "reasons": reasons + [
                f"mais rejoué sur l'historique de {p['symbol']}, aucun objectif ne gagne (meilleur : {b['name']} "
                f"{_fr(b['expectancy_r'])} R) : je ne le prendrais pas sur la note seule"],
                "unlock": "un objectif à espérance positive (prix plus près du stop, ou meilleur niveau d'entrée)"}
        reasons.append(f"{b['name']} touché avant le stop dans {b['hit_rate']:.0%} des cas passés, espérance "
                       f"{_fr(b['expectancy_r'])} R si tout est vendu là")
        return {"decision": "ENTRER " + p["side"].upper(), "plan": p, "reasons": reasons,
                "unlock": f"stop {S._num(p['stop'])} ({p['stop_source']}) : au-delà, la thèse est fausse"}
    reasons = []
    for p in plans:
        reasons.append(f"{'long' if p['side'] == 'long' else 'short'} NO GO : {' ; '.join(p['score']['reasons'])}")
    missing = [i["label"] for i in best["checklist"]["items"] if i["ok"] is False]
    unlock = (f"côté {best['side']} : {', '.join(missing[:4])}" if missing else
              f"côté {best['side']} : {best['checklist']['verdict'].lower()}")
    return {"decision": "ATTENDRE", "plan": None, "reasons": reasons, "unlock": unlock}


def lab_measure(market: str, symbol: str, path: str | None = None) -> dict | None:
    """Ce que le labo a mesuré sur la méthode de génération en cours (plans longs rejoués : origine « replay »,
    hors bloc scellé ; ni le direct ni les autres versions n'y entrent, comme l'annonce le texte) : sur ce
    symbole et sur tout le marché, espérance des plans GO et NO GO déclenchés, en R du labo. Deux AVG SQL, pas
    de calcul par trade (commande interactive). None sans base."""
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


def _group_text(g: dict) -> str:
    parts = []
    for d in ("GO", "NO GO"):
        if d in g:
            parts.append(f"{d} {_fr(g[d]['r'])} R sur {g[d]['n']}")
    return " · ".join(parts) or "aucun plan déclenché"


def _unit(market: str | None) -> str:
    """Unité des espérances de !avis : le R du labo ; en actions, il ne compte pas encore la commission IBKR à la
    mise de Simão (20 €), qui peut faire perdre un GO à peine positif."""
    return {"stock": "R du labo, avant commission IBKR à 20 €", "crypto": "R du labo, frais OKX compris"}.get(
        market, "R du labo")


def measure_lines(m: dict | None, symbol: str, market: str | None = None) -> list[str]:
    """La fiabilité affichée telle qu'elle est mesurée, sans l'embellir : l'écart GO − NO GO est relatif, dit avec
    l'espérance absolue du GO et son unité (scénario 3 de GPT : un GO moins mauvais que le NO GO n'est pas un
    gain)."""
    if m is None:
        return ["📏 Labo indisponible : aucune mesure à montrer, l'avis suit la méthode sans preuve chiffrée."]
    unit = _unit(market)
    lines = [f"📏 **Mesuré par le labo** (méthode {m.get('version') or 'v1'}, plans longs rejoués, {unit}) : "
             f"{symbol} {_group_text(m['symbole'])} ; tout le marché {_group_text(m['marche'])}."]
    go, nogo = m["marche"].get("GO"), m["marche"].get("NO GO")
    if go and nogo and go["n"] >= 30 and nogo["n"] >= 30 and go["r"] - nogo["r"] >= 0.1:
        gap = f"Le GO fait mieux que le NO GO de {_fr(go['r'] - nogo['r'])} R par trade dans le labo (écart relatif)"
        if go["r"] > 0:
            lines.append(f"-# {gap} ; le GO gagne {_fr(go['r'])} R en moyenne ({unit}) : la note départage sur ce "
                         "marché.")
        else:
            lines.append(f"-# {gap}, mais le GO perd encore {_fr(go['r'])} R en moyenne ({unit}) : moins mauvais, "
                         "pas gagnant.")
    else:
        lines.append("-# La note GO/NO GO n'a pas encore prouvé qu'elle gagne (GO pas nettement meilleur que NO GO) : "
                     "prends cet avis comme une lecture de la méthode, pas comme une prédiction fiable.")
    return lines


async def opinion(symbol: str, market: str, now: datetime | None = None) -> dict:
    """Calcule les deux plans (long et short) et l'avis. PlanError si aucun plan n'est possible."""
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
    view = decide(plans)
    m = await asyncio.to_thread(lab_measure, plans[0]["market"], plans[0]["symbol"])
    return view | {"symbol": plans[0]["symbol"], "market": plans[0]["market"], "plans": plans,
                   "errors": errors, "lab": m, "created_at": now.isoformat(timespec="seconds")}


def opinion_text(o: dict) -> str:
    price = o["plans"][0]["entry"]
    lines = [f"🧭 **Avis de JARVIS sur {o['symbol']}** ({MARKET_FR[o['market']]}) · {S._num(price)}",
             f"**{ICON[o['decision']]} {o['decision']}**"]
    lines += [f"• {r}" for r in o["reasons"]]
    cat = o["plans"][0].get("catalyst") or {}
    if o["market"] == "stock" and cat.get("hours_to_earnings") is not None:
        lines.append(f"• résultats trimestriels dans {cat['hours_to_earnings']:.0f} h")
    lines.append(("Ce qui invalide : " if o["plan"] else "Ce qui débloquerait : ") + o["unlock"])
    lines += [side_line(p) for p in o["plans"]]
    lines += [f"-# Plan impossible côté {e}" for e in o["errors"]]
    lines += measure_lines(o["lab"], o["symbol"], o["market"])
    if o["plan"]:
        lines.append("")
        lines.append(S.plan_text(o["plan"]))
    else:
        lines.append(f"-# Plans complets : `!plan {o['symbol']}` et `!plan {o['symbol']} short`")
    return "\n".join(lines)


# --- Positions ouvertes : sortir, alléger ou garder ----------------------------------------

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


def hold_odds(bars: list[dict], trade: dict, price: float, horizon: int) -> dict | None:
    """Garder jusqu'au prochain objectif, rejoué depuis chaque bougie de l'historique de l'actif avec
    les mêmes écarts que maintenant (prix → objectif, prix → stop suivi). Espérance en R du plan d'origine,
    par unité de position restante ; les cas ni objectif ni stop dans l'horizon comptent pour 0."""
    sign, stop = _sign(trade), trade["stop_current"]
    if (price - stop) * sign <= 0:
        return None
    nxt = next((t for t in trade["targets"][trade["targets_hit"]:] if (t["price"] - price) * sign > 0), None)
    if nxt is None:
        return None
    lv = P.Levels(trade["side"], price, stop, [P.Target(nxt["price"], nxt["name"])])
    try:
        t = P.historical_timing(bars, lv, horizon)["targets"][0]
    except ValueError:
        return None
    risk = abs(trade["entry"] - trade["stop"])
    up, down = abs(nxt["price"] - price) / risk, abs(price - stop) / risk
    return {"target": nxt["name"], "price": nxt["price"], "hit_rate": t["hit_rate"], "stop_rate": t["stop_rate"],
            "up_r": round(up, 2), "down_r": round(down, 2),
            "expectancy_r": round(t["hit_rate"] * up - t["stop_rate"] * down, 2)}


def overnight_gaps(daily: list[dict] | None, trade: dict, price: float) -> dict | None:
    """Gaps d'ouverture des nuits passées (ouverture du jour contre clôture de la veille) : part des nuits
    où le gap aurait sauté le stop suivi depuis le prix actuel, et gap défavorable du pire dixième."""
    if not daily or len(daily) < MIN_GAPS + 1:
        return None
    sign = _sign(trade)
    gaps = [sign * (b["o"] - a["c"]) / a["c"] for a, b in zip(daily, daily[1:]) if a["c"]][-GAP_DAYS:]
    if len(gaps) < MIN_GAPS:
        return None
    room = sign * (price - trade["stop_current"]) / price
    adverse = sorted(-g for g in gaps)
    return {"nights": len(gaps), "through_stop": round(sum(g <= -room for g in gaps) / len(gaps), 3),
            "room_pct": round(room * 100, 2), "p90_adverse_pct": round(adverse[int(0.9 * len(adverse))] * 100, 2)}


def earnings_soon(cat: dict | None, now: datetime) -> float | None:
    """Heures avant des résultats publiés d'ici la fin de la prochaine séance (None sinon)."""
    hours = (cat or {}).get("hours_to_earnings")
    if hours is None:
        return None
    end = CAL.nth_session_close(now, 2 if CAL.session_open(now) else 1)
    return hours if hours * 3600 <= (end - now).total_seconds() else None


def advise(trade: dict, price: float, bars: list[dict], daily: list[dict] | None, cat: dict | None,
           now: datetime) -> dict:
    """SORTIR / ALLÉGER / GARDER pour une position ouverte, sans réseau (règles dans l'en-tête du module)."""
    market = trade["market"]
    interval = S.SETTINGS[market]["interval"]
    r_now = round(r_of(trade, price), 2)
    hold = hold_odds(bars, trade, price, HOLD_HORIZON[market])
    gaps = overnight_gaps(daily, trade, price) if market == "stock" else None
    earnings = earnings_soon(cat, now) if market == "stock" else None
    closed = [b for b in bars if b["t"] + interval * 60 <= now.timestamp()]
    signals = SIG.exit_signals(closed, trade["side"]) if r_now > 0 and len(closed) >= 20 else []
    nxt = trade["targets"][trade["targets_hit"]] if trade["targets_hit"] < len(trade["targets"]) else None
    late = bool(nxt and nxt.get("eta_latest") and now >= datetime.fromisoformat(nxt["eta_latest"]))
    facts = [f"{S._num(price)} ({_fr(r_now)} R) · stop suivi {S._num(trade['stop_current'])} · "
             f"{trade['targets_hit']}/{len(trade['targets'])} TP"]
    if hold:
        facts.append(f"Garder jusqu'à {hold['target']} ({S._num(hold['price'])}, +{hold['up_r']:g} R) : touché avant le stop "
                     f"{hold['hit_rate']:.0%}, stop d'abord {hold['stop_rate']:.0%} sur l'historique · espérance "
                     f"{_fr(hold['expectancy_r'])} R")
    if gaps:
        facts.append(f"Nuit : gap qui saute le stop ({gaps['room_pct']:g} % de marge) {gaps['through_stop']:.0%} des "
                     f"{gaps['nights']} dernières nuits · pire dixième des gaps contre toi ≥ {gaps['p90_adverse_pct']:g} %")
    if nxt and nxt.get("eta_text"):
        facts.append(f"Prochain objectif {nxt['name']} {S._num(nxt['price'])} : prévu {nxt['eta_text']}"
                     + (" (en retard)" if late else ""))
    if earnings is not None:
        decision, why = "SORTIR", f"résultats trimestriels dans {earnings:.0f} h : gap binaire que le plan ne couvre pas"
    elif hold and hold["expectancy_r"] < 0:
        decision, why = "SORTIR", (f"garder jusqu'à {hold['target']} perd en moyenne ({_fr(hold['expectancy_r'])} R) : "
                                   "le stop vient plus souvent que l'objectif depuis ce prix")
    elif late and r_now <= 0 and hold is None:
        decision, why = "SORTIR", (f"{nxt['name']} en retard, la position ne gagne pas et garder n'est pas mesurable : "
                                   "le scénario ne se déroule pas")
    elif signals:
        decision, why = "ALLÉGER", (f"{signals[0][1]} : sors la moitié, remonte le stop à l'entrée "
                                    f"({S._num(trade['entry'])}) et laisse courir le reste")
    elif gaps and gaps["through_stop"] >= GAP_LIMIT:
        decision, why = "ALLÉGER", (f"une nuit sur {round(1 / gaps['through_stop'])} environ le gap saute ton stop : "
                                    "allège avant la clôture pour ne garder qu'un risque que tu acceptes de perdre en entier")
    else:
        decision = "GARDER"
        why = f"garde avec le stop à {S._num(trade['stop_current'])}"
        if hold:
            why += f" : garder jusqu'à {hold['target']} rapporte {_fr(hold['expectancy_r'])} R en moyenne sur l'historique"
        else:
            why += " (rien de mesurable ne dit de sortir)"
    if hold is None:
        facts.append("Garder jusqu'au prochain objectif : pas mesurable (historique trop court ou objectif déjà dépassé).")
    return {"id": trade.get("id"), "symbol": trade["symbol"], "market": market, "side": trade["side"],
            "decision": decision, "why": why, "facts": facts, "price": price, "r_now": r_now,
            "exit_r": exit_r(trade, price), "hold": hold, "gaps": gaps, "earnings_hours": earnings,
            "signals": [k for k, _ in signals], "late": late, "at": now.isoformat(timespec="seconds")}


def advice_text(a: dict) -> str:
    arrow = "🟢" if a["side"] == "long" else "🔴"
    lines = [f"{arrow} **{a['symbol']}** · **{ICON[a['decision']]} {a['decision']}** : {a['why']}"]
    lines += [f"-# {f}" for f in a["facts"]]
    return "\n".join(lines)


def record(trade: dict, a: dict, now: datetime) -> None:
    """Garde le conseil sur le trade (un par jour de New York, le dernier remplace) pour le mesurer."""
    day = now.astimezone(CAL.NEW_YORK).date().isoformat()
    kept = [c for c in trade.get("conseils", []) if c.get("day") != day]
    kept.append({"day": day, "at": a["at"], "decision": a["decision"], "price": a["price"],
                 "exit_r": a["exit_r"], "why": a["why"]})
    trade["conseils"] = kept


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
    if not st:
        return "-# Bilan des conseils : aucun trade conseillé n'est encore terminé."
    parts = []
    for d in ("SORTIR", "ALLÉGER", "GARDER"):
        if d in st:
            g = st[d]
            judged = f", juste {g['right']}/{g['n']}" if d != "ALLÉGER" else ""
            parts.append(f"{d} ×{g['n']} (garder a fait {_fr(g['avg_delta'])} R de plus que sortir{judged})")
    return "-# Bilan des conseils : " + " · ".join(parts)


async def _inputs(symbol: str, market: str, now: datetime) -> tuple[list[dict], list[dict] | None, dict | None]:
    bars = await S.candles(symbol, market, S.SETTINGS[market]["interval"])
    daily = await S._optional_candles(symbol, market, 1440) if market == "stock" else None
    cat = await C.safe_check(symbol, now) if market == "stock" else None
    return bars, daily, cat


async def advise_trade(trade: dict, now: datetime | None = None) -> dict:
    """Conseil sur un trade suivi (prix : le dernier lu par le suivi des TP/SL)."""
    now = now or datetime.now(timezone.utc)
    bars, daily, cat = await _inputs(trade["symbol"], trade["market"], now)
    return advise(trade, trade["last_price"], bars, daily, cat, now)


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
    """Position prise hors de JARVIS (ex. sur IBKR) : plan recalculé sur son prix d'entrée, puis conseil
    au prix actuel. Les objectifs déjà dépassés comptent comme touchés."""
    now = now or datetime.now(timezone.utc)
    plan = await S.build_plan(symbol, market, side, entry=entry, stop=stop, now=now)
    bars, daily, cat = await _inputs(plan["symbol"], plan["market"], now)
    price = bars[-1]["c"]
    sign = 1 if plan["side"] == "long" else -1
    hit = sum((price - t["price"]) * sign >= 0 for t in plan["targets"])
    trade = plan | {"stop_current": plan["stop"] if hit < 2 else plan["entry"], "targets_hit": hit, "last_price": price}
    a = advise(trade, price, bars, daily, cat, now)
    if (price - trade["stop_current"]) * sign <= 0:
        a |= {"decision": "SORTIR", "why": f"le prix ({S._num(price)}) a déjà passé le stop du plan ({S._num(trade['stop_current'])})"}
    return a | {"plan": plan}


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
    lines = [head + " : sortir, alléger ou garder les positions dont ni le TP ni le SL n'est touché"]
    for a in advices:
        lines.append(f"⚠️ **{a['symbol']}** : conseil impossible ({a['error']})" if "error" in a else advice_text(a))
    lines.append(stats_line(stats))
    lines.append("-# Règles fixes, pas encore validées par le labo : chaque conseil est gardé et mesuré.")
    return "\n".join(lines)
