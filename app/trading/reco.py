"""Journal des recommandations : « si j'avais suivi JARVIS » (espace Papier du cockpit, choix de Simão du 28/10/2026).

Chaque recommandation **actionnable** que JARVIS envoie sur Discord, prise ou non, est suivie comme un trade virtuel
**exactement telle qu'envoyée** :

- GO **confirmé** (scheduler.tick_confirm, ligne « Ordre : stop d'achat … » de confirm.event_text) : ordre stop
  d'achat au déclencheur, actif à la fin de la bougie 5 min qui confirme, annulé à 11 h 30 New York s'il n'est pas
  touché ; stop du plan ; taille du message (actions entières pour le risque de la règle unique, ou aucune taille) ;
  sortie unique sur TP2 (ibkr.single_target ; un bracket par objectif avec JARVIS_IBKR_BRACKETS=3), stop à l'entrée
  après TP2, aucune sortie au temps (la gestion de `!prends … go`).
- 🔪 KILL (killmode.record, groupe « kill » de data/killmode_calls.json, envoyé en réponse à `!kill`) : ordre stop
  d'achat actif une minute pleine après l'envoi (killmode.activation), valable jusqu'à 11 h New York ; un bracket
  par objectif (1, 2, 3 R) à la taille du journal de Killmode (killmode.shares, au moins une action) ; stop à
  l'entrée dès TP1 ; vendu à la dernière bougie de la séance.

Exclus : NO GO, GO annulés ou expirés avant confirmation (jamais actionnables), ordres non déclenchés (comptés à
part : « pas déclenché »), réponses à une demande (`!avis`, `!plan`), témoins de Killmode, challenger Gemini et
potentiel +10 (mesurés au labo).

Le suivi rejoue les bougies avec le même moteur que les trades de Simão (service.apply_bars : objectifs franchis
d'un pas, stop au pire du stop et de l'ouverture sur un gap) ; aucun message n'est envoyé. Bougies de 5 min lues
une fois par bougie close et par action suivie (1 crédit Twelve Data chacune, Alpaca en secours), pendant la
séance seulement, plus une lecture après la clôture. Mise en service : la date `depuis` du fichier ; un KILL noté
avant n'est pas repris. Résultat : R brut du moteur, puis dollars et euros à la taille du message, commissions IBKR
du barème de Simão déduites (comme fees.py, à la quantité envoyée). Sans taille dans le message : R seulement,
euros inconnus (jamais 0).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from app.trading import calendar as CAL
from app.trading import ibkr as IBKR
from app.trading import service as T

log = logging.getLogger(__name__)

RECO_PATH = os.getenv("JARVIS_RECO_PATH", "data/reco.json")
NEW_YORK = CAL.NEW_YORK
GO_UNTIL = time(11, 30)   # confirm.LAST_CHECK : l'ordre du GO confirmé est annulé à 11 h 30 New York
BAR = 5 * 60              # bougies du suivi
ACTIVE = ("attente", "ouvert")
KINDS = {"go_confirme": "GO confirmé", "kill": "KILL"}
STATUS_FR = {"attente": "en attente du déclenchement", "ouvert": "déclenchée", "clos": "close",
             "pas_declenche": "pas déclenchée"}


class RecoBook:
    """data/reco.json : {"depuis": ISO, "recos": [...]} ; écrit par le processus web sous scheduler._lock."""

    def __init__(self, path: str | None = RECO_PATH):
        self.path = Path(path) if path else None
        self.since: str | None = None
        self.recos: list[dict] = []
        if self.path and self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.since, self.recos = raw.get("depuis"), raw.get("recos") or []
            except (OSError, ValueError, AttributeError):
                log.warning("Journal des recommandations illisible, on repart de zéro")
        if not self.since:
            self.since = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"depuis": self.since, "recos": self.recos}, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    def get(self, rid: str) -> dict | None:
        return next((r for r in self.recos if r["id"] == rid), None)

    def add(self, reco: dict) -> dict | None:
        """Ajoute une recommandation (une seule fois par id) ; None si elle est déjà là."""
        if self.get(reco["id"]):
            return None
        self.recos.append(reco)
        self.save()
        return reco

    def active(self) -> list[dict]:
        return [r for r in self.recos if r["status"] in ACTIVE]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _targets(entry: float, stop: float, prices: list[float], timing: list[dict] | None, start: datetime) -> list[dict]:
    """Objectifs au format du moteur (service.apply_bars) ; délais estimés repartis de l'heure du déclenchement
    quand le plan du scanner les avait mesurés (minutes de cotation), sinon inconnus."""
    risk = abs(entry - stop)
    out = []
    for i, price in enumerate(prices):
        t = timing[i] if timing and i < len(timing) else None
        item = {"name": f"TP{i + 1}", "price": price, "pct": round((price - entry) / entry * 100, 2),
                "r_multiple": round(abs(price - entry) / risk, 2) if risk else None,
                "minutes_median": None, "minutes_p75": None, "eta_median": None, "eta_latest": None,
                "eta_text": "délai non estimé", "reached_by_p75": None, "reached_by_p75_given_prev": None,
                "still_after_p75": None}
        if t and t.get("minutes_median") is not None:
            item |= {k: t.get(k) for k in ("minutes_median", "minutes_p75", "reached_by_p75",
                                            "reached_by_p75_given_prev", "still_after_p75")}
            try:
                med = T.eta_at("stock", start, t["minutes_median"])
                late = T.eta_at("stock", start, t["minutes_p75"] or t["minutes_median"])
                item |= {"eta_median": med.isoformat(timespec="minutes"), "eta_latest": late.isoformat(timespec="minutes"),
                         "eta_text": f"~{T.duration_fr(t['minutes_median'])} de cotation, vers {T.time_fr(med, start)}"}
            except T.PlanError:
                pass
        out.append(item)
    return out


# --- Entrées : ce qui a été envoyé ------------------------------------------------------------

def go_from_confirm(symbol: str, go: dict, ev: dict, day: str, sent: datetime, qty: int | None,
                    risk_eur: float | None, usd_per_eur: float | None, size_note: str | None = None) -> dict:
    """La recommandation d'un GO confirmé, telle que le MP de confirmation l'a donnée (confirm.event_text)."""
    until = datetime.combine(datetime.fromisoformat(day).date(), GO_UNTIL, NEW_YORK)
    plan = {"side": "long", "market": "stock", "targets": [{"price": p} for p in go["targets"]]}
    single = IBKR.single_target(plan)
    return {
        "id": f"go-{day}-{symbol}", "kind": "go_confirme", "symbol": symbol, "market": "stock", "side": "long",
        "day": day, "sent_at": sent.isoformat(timespec="seconds"),
        "message": {"entry": ev["trigger"], "stop": go["stop"], "targets": list(go["targets"]),
                    "qty": qty or None, "risk_eur": risk_eur if qty else None, "eurusd": usd_per_eur,
                    "size_note": size_note if not qty else None, "sortie_unique": single,
                    "valid_until": until.astimezone(timezone.utc).isoformat(timespec="seconds"),
                    "composite": go.get("composite")},
        "timing": go.get("plan_targets"),
        "activation_ts": int(ev["t"]) + BAR,  # le message part à la fin de la bougie qui confirme
        "status": "attente", "trade": None,
    }


def kill_from_call(call: dict, usd_per_eur: float | None) -> dict:
    """La recommandation d'un 🔪 KILL du journal de Killmode, à la taille de son journal (killmode.shares)."""
    from app.trading import killmode as K  # import tardif : killmode importe service

    rate = usd_per_eur or K.EURUSD_DEFAULT
    qty = K.shares(call["entry"] - call["stop"], rate)
    return {
        "id": f"kill-{call['id']}", "kind": "kill", "symbol": call["symbol"], "market": "stock", "side": "long",
        "day": call["day"], "sent_at": _iso(call.get("envoye_a", call["at"])),
        "message": {"entry": call["entry"], "stop": call["stop"], "targets": list(call["targets"]), "qty": qty,
                    "risk_eur": round(min(K.RISK_EUR, qty * (call["entry"] - call["stop"]) / rate), 2),
                    "eurusd": rate, "size_note": None, "sortie_unique": None,
                    "valid_until": _iso(call["until"]), "close_ts": call.get("close_ts"),
                    "brackets": [[n, p] for n, p in K.bracket_orders(call["entry"], call["stop"], call["targets"], rate)]},
        "timing": None, "activation_ts": K.activation(call),
        "status": "attente", "trade": None,
    }


def sync_kills(book: RecoBook, usd_per_eur: float | None) -> list[dict]:
    """Reprend les 🔪 KILL notés depuis la mise en service du journal (jamais les témoins)."""
    from app.trading import killmode as K  # import tardif : killmode importe service

    since = datetime.fromisoformat(book.since).timestamp()
    added = []
    for c in K.calls():
        if K._group_of(c) != "kill" or c.get("at", 0) < since or book.get(f"kill-{c['id']}"):
            continue
        try:
            r = book.add(kill_from_call(c, usd_per_eur))
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
            log.warning("KILL %s pas repris au journal des recommandations (%s: %s)", c.get("id"), type(exc).__name__, exc)
            continue
        if r:
            added.append(r)
    return added


# --- Suivi --------------------------------------------------------------------------------------

def _fill(reco: dict, bars: list[dict], step: int) -> tuple[dict, float] | None:
    """Bougie qui déclenche l'ordre stop d'achat (plus haut ≥ entrée) entre l'activation et la fin de validité, et
    le prix retenu : le pire de l'entrée et de l'ouverture (gap au-dessus de l'ordre)."""
    entry = reco["message"]["entry"]
    until = datetime.fromisoformat(reco["message"]["valid_until"]).timestamp()
    for b in bars:
        if b["t"] + step <= reco["activation_ts"] or b["t"] >= until:
            continue
        if b["h"] >= entry:
            return b, max(entry, b["o"])
    return None


def _open_trade(reco: dict, bar: dict, price: float) -> dict:
    m = reco["message"]
    start = datetime.fromtimestamp(bar["t"], timezone.utc)
    trade = {
        "id": reco["id"], "symbol": reco["symbol"], "market": reco["market"], "side": reco["side"], "status": "open",
        "entry": price, "stop": m["stop"], "stop_current": m["stop"], "stop_pct": round(-abs(price - m["stop"]) / price * 100, 2),
        "targets": _targets(price, m["stop"], m["targets"], reco.get("timing"), start), "targets_hit": 0,
        "events": [], "last_price": price, "last_check": bar["t"], "late_warned": [], "exit_signals": [],
        "created_at": start.isoformat(timespec="seconds"), "sortie_unique": m.get("sortie_unique"),
        "sizing": {"quantity": m.get("qty"), "risk_amount_eur": m.get("risk_eur"), "eurusd": m.get("eurusd")},
        "filled_at": start.isoformat(timespec="seconds"), "fill_ref": m["entry"],
    }
    if reco["kind"] == "kill":
        trade["killmode"] = True
    return trade


def _close(trade: dict, price: float, at: int, reason: str) -> None:
    trade["status"] = "closed"
    trade["last_price"] = price
    trade["events"].append({"kind": "close", "price": price, "reason": reason, "at": _iso(at)})


def step(reco: dict, bars: list[dict], now: datetime, step_s: int = BAR) -> bool:
    """Fait avancer une recommandation sur les bougies reçues ; vrai si son état a changé."""
    before = json.dumps([reco["status"], reco.get("trade")], sort_keys=True, default=str)
    if reco["status"] == "attente":
        hit = _fill(reco, bars, step_s)
        if hit:
            reco["trade"] = _open_trade(reco, *hit)
            reco["status"] = "ouvert"
        elif now.timestamp() >= datetime.fromisoformat(reco["message"]["valid_until"]).timestamp():
            reco["status"] = "pas_declenche"
    if reco["status"] == "ouvert":
        trade = reco["trade"]
        T.apply_bars(trade, bars, now)  # mêmes règles que les trades de Simão ; messages jetés (rien n'est envoyé)
        close_ts = reco["message"].get("close_ts")
        if reco["kind"] == "kill" and trade["status"] == "open" and close_ts and now.timestamp() >= close_ts:
            last = [b for b in bars if b["t"] + step_s <= close_ts]
            if last:  # vendu à la dernière bougie de la séance, comme le journal de Killmode
                _close(trade, last[-1]["c"], close_ts, "vendu à la clôture (règle du KILL)")
        if trade["status"] == "closed":
            reco["status"] = "clos"
            reco["result"] = result(reco)
    return json.dumps([reco["status"], reco.get("trade")], sort_keys=True, default=str) != before


def _ny_session(now: datetime) -> tuple[datetime, datetime] | None:
    return CAL.session_bounds(now.astimezone(NEW_YORK).date())


_last_read: dict[str, int] = {}     # action -> fin de la dernière bougie 5 min close lue
_after_close: dict[str, str] = {}   # action -> séance déjà relue après la clôture


def due(symbol: str, recos: list[dict], now: datetime) -> bool:
    """Une lecture par bougie 5 min close pendant la séance, une seule après la clôture (fin de séance)."""
    bounds = _ny_session(now)
    if bounds and bounds[0] < now <= bounds[1] + timedelta(minutes=2):
        boundary = int(now.timestamp()) // BAR * BAR
        if _last_read.get(symbol, 0) >= boundary:
            return False
        _last_read[symbol] = boundary
        return True
    prev = CAL.previous_session(now)
    day = prev[1].date().isoformat() if prev else None
    if not prev or _after_close.get(symbol) == day:
        return False
    if not any(r["day"] <= prev[1].astimezone(NEW_YORK).date().isoformat() for r in recos):
        return False
    _after_close[symbol] = day
    return True


async def read_bars(symbol: str) -> list[dict] | None:
    """Bougies 5 min de la séance : Twelve Data (temps réel, 1 crédit), Alpaca en secours (service.candles)."""
    try:
        return await T.candles(symbol, "stock", 5, max_age=0, outputsize=120)
    except T.PlanError as exc:
        log.warning("Suivi des recommandations : %s", exc)
        return None


async def tick(book: RecoBook, now: datetime | None = None, bars_fn=None, lock=None,
               usd_per_eur: float | None = None) -> int:
    """Reprend les KILL, puis fait avancer chaque recommandation active. Lectures hors du verrou, écriture dessous.
    Renvoie le nombre de recommandations qui ont changé."""
    now = now or datetime.now(timezone.utc)
    bars_fn = bars_fn or read_bars
    changed = 0
    if book.path and not book.path.exists():
        book.save()  # la mise en service (`depuis`) est gardée dès le premier passage
    from contextlib import nullcontext

    guard = lock or nullcontext()
    try:
        async with guard:
            changed += len(sync_kills(book, usd_per_eur))
    except Exception:
        log.exception("Journal des recommandations : reprise des KILL en échec")
    groups: dict[str, list[dict]] = {}
    for r in book.active():
        groups.setdefault(r["symbol"], []).append(r)
    for symbol, recos in groups.items():
        if not due(symbol, recos, now):
            # Fin de validité passée sans lecture : l'ordre est annulé quand même.
            if any(r["status"] == "attente" for r in recos):
                async with guard:
                    for r in recos:
                        if r["status"] == "attente" and step(r, [], now):
                            changed += 1
            continue
        bars = await bars_fn(symbol)
        if bars is None:
            continue
        async with guard:
            for r in recos:
                try:
                    changed += step(r, bars, now)
                except Exception:
                    log.exception("Recommandation %s : suivi en échec", r["id"])
    for d in (_last_read, _after_close):
        for k in [k for k in d if k not in groups]:
            d.pop(k, None)
    if changed:
        async with guard:
            book.save()
    return changed


# --- Résultats ------------------------------------------------------------------------------------

def _commission(n: float, price: float) -> float:
    from app.trading import killmode as K  # import tardif : killmode importe service

    return K.order_commission(n, price, K.IBKR_PLANS.get(K.IBKR_PLAN, K.IBKR_PLANS["fixe"]))


def result(reco: dict, qty: int | None = None) -> dict | None:
    """Résultat d'une recommandation close : R brut (service.realized_r), puis, à la quantité du message, dollars bruts,
    commissions IBKR (une entrée et une sortie par bracket ; le reste vendu à la clôture en un seul ordre), net en
    dollars, en euros (change du message) et en R du risque prévu à cette taille. Sans taille : R brut et R net
    des frais à la règle (service.realized_r_net), euros inconnus. `qty` remplace la taille du message
    (portefeuille fictif)."""
    from app.trading import fees as FEES  # import tardif : fees lit killmode, qui importe service
    from app.trading import killmode as K

    trade = reco.get("trade")
    if not trade or trade["status"] != "closed":
        return None
    r = T.realized_r(trade)
    if r is None:
        return None
    m = reco["message"]
    qty, rate = qty or m.get("qty"), m.get("eurusd") or K.EURUSD_DEFAULT
    exit_ev = next((e for e in reversed(trade["events"]) if e["kind"] in ("stop", "close")), None)
    if exit_ev:  # stop (après un objectif ou non) ou vente à la clôture d'un KILL
        reason = exit_ev.get("reason") or exit_ev["kind"]
    else:  # sortie unique ou dernier objectif touché
        reason = trade["targets"][max(trade["targets_hit"], 1) - 1]["name"]
    out = {"r": r, "raison": reason, "closed_at": trade["events"][-1]["at"] if trade["events"] else None}
    if not qty:
        return out | {"r_net": T.realized_r_net(trade), "usd": None, "eur": None, "fees_usd": None,
                      "taille": None}
    sign = 1 if trade["side"] == "long" else -1
    risk = abs(trade["entry"] - trade["stop"])
    brackets = 1 if trade.get("sortie_unique") is not None else len(trade["targets"])
    legs = K._legs(qty, brackets, FEES._exits(trade))
    gross = sum(n * sign * (e["prix"] - trade["entry"]) for n, e in legs)
    fees = sum(_commission(n, trade["entry"]) for n, _ in legs)
    fees += sum(_commission(n, e["prix"]) for n, e in legs if e["raison"] != "cloture")
    closing = [(n, e["prix"]) for n, e in legs if e["raison"] == "cloture"]
    if closing:
        fees += _commission(sum(n for n, _ in closing), closing[0][1])
    net = gross - fees
    return out | {"usd_brut": round(gross, 2), "fees_usd": round(fees, 2), "usd": round(net, 2),
                  "eur": round(net / rate, 2), "r_net": round(net / (qty * risk), 3) if risk else None,
                  "taille": qty}


def linked_trade(reco: dict, trades: list[dict]) -> dict | None:
    """Le trade de « Mes trades » pris sur cette recommandation : même action, même jour de New York, pris sur un
    GO confirmé (`!prends … go`) ou un KILL (`!prends … kill`). Le plus ancien s'il y en a plusieurs."""
    want = "killmode" if reco["kind"] == "kill" else "go_confirme"
    for t in trades:
        if t.get("symbol") != reco["symbol"] or not t.get("created_at"):
            continue
        day = datetime.fromisoformat(t["created_at"]).astimezone(NEW_YORK).date().isoformat()
        if day != reco["day"]:
            continue
        if (want == "killmode" and t.get("killmode")) or (want == "go_confirme" and t.get("policy") == "go_confirme"):
            return t
    return None


def unrealized(reco: dict, qty: int | None = None) -> dict | None:
    """Latent brut d'une recommandation déclenchée, au dernier prix lu par le suivi : R et, à la taille du message,
    dollars et euros (sans frais)."""
    trade = reco.get("trade")
    if reco["status"] != "ouvert" or not trade:
        return None
    sign = 1 if trade["side"] == "long" else -1
    risk = abs(trade["entry"] - trade["stop"])
    move = sign * (trade["last_price"] - trade["entry"])
    qty = qty or reco["message"].get("qty")
    rate = reco["message"].get("eurusd")
    usd = round(qty * move, 2) if qty else None
    return {"r": round(move / risk, 2) if risk else None, "usd": usd,
            "eur": round(usd / rate, 2) if usd is not None and rate else None}


PORTFOLIO_START_EUR = 1000.0  # choix de Simão du 29/10 : 1 000 € au départ, 20 € de risque fixe par trade


def fixed_qty(reco: dict) -> int | None:
    """Actions pour 20 € de risque (JARVIS_RISK_EUR) au stop du message, au change du message : la règle de Simão,
    quel que soit le grade (au moins une action, comme Killmode)."""
    from app.trading import killmode as K  # import tardif : killmode importe service

    m = reco["message"]
    risk = abs(m["entry"] - m["stop"]) if m.get("entry") is not None and m.get("stop") is not None else 0
    return K.shares(risk, m.get("eurusd")) if risk > 0 else None


def portfolio(book: RecoBook, start: float = PORTFOLIO_START_EUR) -> dict:
    """Portefeuille fictif : chaque recommandation déclenchée (GO confirmés et KILL) prise à 20 € de risque fixe,
    entrée, stop et sortie du message, frais IBKR déduits. Capital = départ + résultats nets des closes ; latent brut
    des ouvertes au dernier prix du suivi."""
    from app.trading import killmode as K  # import tardif : killmode importe service

    capital, points, nets, wins, unknown = start, [], [], 0, 0
    closed = sorted((r for r in book.recos if r["status"] == "clos"),
                    key=lambda r: (r.get("result") or {}).get("closed_at") or r["sent_at"])
    for r in closed:
        res = result(r, fixed_qty(r))
        if not res or res.get("eur") is None:
            unknown += 1
            continue
        capital += res["eur"]
        nets.append(res["r_net"])
        wins += res["eur"] > 0
        points.append([res["closed_at"], round(capital - start, 2)])
    latent = [u for u in (unrealized(r, fixed_qty(r)) for r in book.recos if r["status"] == "ouvert") if u]
    latent_eur = [u["eur"] for u in latent if u["eur"] is not None]
    return {"depart_eur": start, "risque_par_trade_eur": K.RISK_EUR, "capital_eur": round(capital, 2),
            "resultat_eur": round(capital - start, 2), "rendement_pct": round((capital - start) / start * 100, 2),
            "closes": len(points), "gagnantes": wins, "sans_resultat": unknown,
            "r_net": round(sum(x for x in nets if x is not None), 3),
            "ouvertes": len(latent), "latent_eur": round(sum(latent_eur), 2) if latent_eur else None,
            "points": points}


def bilan(book: RecoBook, capital_eur: float | None = None) -> dict:
    """« Si j'avais tout suivi depuis le … » : recommandations par état, R et euros nets des closes."""
    recos = book.recos
    closed = [r for r in recos if r["status"] == "clos" and r.get("result")]
    nets = [r["result"]["r_net"] for r in closed if r["result"].get("r_net") is not None]
    eur = [r["result"]["eur"] for r in closed if r["result"].get("eur") is not None]
    open_ = [r for r in recos if r["status"] == "ouvert"]
    latent = [u for u in (unrealized(r) for r in open_) if u]
    return {
        "depuis": book.since, "total": len(recos),
        "par_etat": {s: sum(r["status"] == s for r in recos) for s in STATUS_FR},
        "par_type": {k: sum(r["kind"] == k for r in recos) for k in KINDS},
        "closes": len(closed), "gagnantes": sum(x > 0 for x in nets),
        "taux_reussite": round(sum(x > 0 for x in nets) / len(nets), 3) if nets else None,
        "r_net": round(sum(nets), 3), "eur_net": round(sum(eur), 2) if eur else None,
        "eur_sans_taille": len(closed) - len(eur),
        "latent_r": round(sum(u["r"] for u in latent if u["r"] is not None), 2) if latent else None,
        "latent_eur": round(sum(u["eur"] for u in latent if u["eur"] is not None), 2)
        if any(u["eur"] is not None for u in latent) else None,
        "capital_declare_eur": capital_eur,
        "equite_eur": round(capital_eur + sum(eur), 2) if capital_eur else None,
    }
