"""Actions du nouveau Trading Cockpit : POST /api/cockpit/actions (PR 2 de la refonte, docs/CARTE-INTERFACE.md §4).

Liste blanche fermée. Chaque action appelle **la même fonction** que la route ou la commande Discord existante, sous
`scheduler._lock` quand elle écrit ; aucune ne passe d'ordre (ni IBKR, ni Alpaca) ni ne touche une clé. La réponse est
le résultat réel, jamais « en attente ». Chaque appel est journalisé (heure, action, arguments, résultat).

Gardes : jeton CSRF (cookie `jarvis_csrf` posé par GET /cockpit, renvoyé dans `X-CSRF-Token`, comparé à temps constant)
en plus du contrôle de l'Origin (`reject_cross_site`) ; les actions qui écrivent dans « Mes trades » exigent
`"space": "trades"` et `"confirm": "MES_TRADES"`, que l'interface n'ajoute qu'après l'appui maintenu sur la bascule.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app import scheduler
from app.trading import service as T
from app.web.cockpit_api import SPACES, _symbol

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/cockpit", include_in_schema=False)

CSRF_COOKIE, CSRF_HEADER = "jarvis_csrf", "x-csrf-token"
MES_TRADES = "MES_TRADES"
ACTIONS_LOG = os.getenv("JARVIS_COCKPIT_ACTIONS_PATH", "data/cockpit_actions.jsonl")
ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{32,128}$")  # forme de secrets.token_urlsafe(32)

# action → (écrit dans « Mes trades », protégée par appui maintenu côté interface)
ACTIONS = {
    "plan": (False, False), "take": (True, False), "exit": (True, True), "close": (True, True),
    "delete": (True, True), "reopen": (True, True), "advice": (True, False), "scan_now": (False, False),
    "proposal_approve": (False, True), "proposal_refuse": (False, False), "veto": (False, True),
    "killmode_on": (False, True), "killmode_off": (False, True),
    # Lecture à la demande (§12 : touche A et inspecteur), absentes de la liste du §9.4 : mêmes fonctions que `!avis`
    # et `!societe`. Écart noté dans la carte.
    "opinion": (False, False), "company": (False, False),
}


class Action(BaseModel):
    action: str
    space: str = "paper"
    confirm: str | None = None
    args: dict = Field(default_factory=dict)


def csrf_ok(request: Request) -> None:
    cookie = request.cookies.get(CSRF_COOKIE) or ""
    header = request.headers.get(CSRF_HEADER) or ""
    if not cookie or not header or not hmac.compare_digest(cookie.encode(), header.encode()):
        raise HTTPException(403, "Jeton CSRF absent ou invalide : recharge la page du cockpit")


# --- Lecture et contrôle des arguments ------------------------------------------------------------

def _num(args: dict, key: str, required: bool = False) -> float | None:
    v = args.get(key)
    if v in (None, ""):
        if required:
            raise HTTPException(400, f"{key} manquant")
        return None
    try:
        x = float(str(v).replace(",", "."))
    except ValueError:
        raise HTTPException(400, f"{key} illisible : nombre attendu")
    if not x > 0 or x != x or x == float("inf"):
        raise HTTPException(400, f"{key} doit être un nombre positif")
    return x


def _id(args: dict) -> str:
    v = str(args.get("id") or "")
    if not ID_RE.match(v):
        raise HTTPException(400, "id du trade invalide")
    return v


def _pid(args: dict) -> int:
    try:
        return int(args.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(400, "id de la proposition invalide")


def _market(args: dict, symbol: str) -> str:
    m = args.get("market")
    if m is None:
        from app.notify import bot  # import tardif : bot importe scheduler

        return bot.parse_trade([symbol])["market"]
    if m not in ("stock", "crypto"):
        raise HTTPException(400, "market doit valoir stock ou crypto")
    return m


def _request(args: dict, with_note: bool = False):
    """Les champs d'un plan (TradeRequest de la route /api/trading/plan) contrôlés."""
    from app import main as M  # import tardif : main inclut ce routeur

    symbol = _symbol(str(args.get("symbol") or ""))
    side = args.get("side") or "long"
    if side not in ("long", "short"):
        raise HTTPException(400, "side doit valoir long ou short")
    raw = args.get("targets") or []
    if not isinstance(raw, list) or len(raw) > 5:
        raise HTTPException(400, "targets : une liste de 5 prix au plus")
    targets = [_num({"tp": t}, "tp", True) for t in raw]
    risk = args.get("risk_pct")
    if risk not in (None, ""):
        try:
            risk = float(risk)
        except (TypeError, ValueError):
            raise HTTPException(400, "risk_pct illisible")
        if not 0 <= risk <= 100:
            raise HTTPException(400, "risk_pct doit être entre 0 et 100")
    else:
        risk = None
    note = args.get("note") if with_note else None
    if note is not None and (not isinstance(note, str) or len(note) > 300):
        raise HTTPException(400, "note : texte de 300 caractères au plus")
    return M.TradeRequest(symbol=symbol, market=_market(args, symbol), side=side, entry=_num(args, "entry"),
                          stop=_num(args, "stop"), targets=targets or None, capital=_num(args, "capital"),
                          risk_pct=risk, note=note or None)


# --- Les actions (mêmes fonctions que les routes et commandes existantes) ---------------------------------

async def _plan(args: dict) -> dict:
    from app import main as M

    plan = await M.trading_plan(_request(args))  # POST /api/trading/plan (sans envoi Discord)
    return {"text": plan["text"], "plan": plan}


async def _take(args: dict) -> dict:
    from app import main as M

    trade = await M.trading_open(_request(args, with_note=True))  # POST /api/trading/trades
    return {"text": f"📌 {trade['symbol']} suivi dans Mes trades (aucun ordre envoyé à IBKR)", "trade": trade}


async def _close(args: dict) -> dict:
    from app import main as M

    trade = await M.trading_close(_id(args), _num(args, "price"))  # POST /api/trading/trades/{id}/close
    net = trade.get("realized_r_net")
    return {"text": f"🏁 {trade['symbol']} clôturé à {T._num(trade['events'][-1]['price'])}"
                    + (f" : {net:+.2f} R nets de frais estimés" if net is not None else ""), "trade": trade}


async def _delete(args: dict) -> dict:
    from app import main as M

    out = await M.trading_delete(_id(args))  # DELETE /api/trading/trades/{id}
    return {"text": "Trade supprimé du journal", **out}


async def _exit(args: dict) -> dict:
    symbol, price = _symbol(str(args.get("symbol") or "")), _num(args, "price", True)
    async with scheduler._lock:  # comme `!sortie`
        res = T.record_exit(scheduler.book, symbol, price)
    if res is None:
        raise HTTPException(404, f"Aucun trade {symbol} suivi : rien à clôturer ni à corriger")
    return {"text": T.exit_text(res), "trade": T.summary(res["trade"]), "corrige": res["corrige"]}


async def _reopen(args: dict) -> dict:
    req = _request(args)
    if req.entry is None:
        raise HTTPException(400, "entry manquant : ton prix d'entrée")
    try:  # comme `!rouvre`
        plan = await T.build_plan(req.symbol, req.market, req.side, req.entry, req.stop, req.targets)
    except T.PlanError as exc:
        raise HTTPException(422, str(exc))
    async with scheduler._lock:
        old = T.reopen(scheduler.book, req.symbol, plan)
    if old is None:
        raise HTTPException(404, f"Aucun trade {req.symbol} terminé par le stop à rouvrir")
    return {"text": f"♻️ Trade {req.symbol} rouvert : le stop {T._num(old['stop_current'])} n'a pas été exécuté chez "
                    "ton courtier, l'ancienne sortie est effacée du journal", "ancien": old["id"]}


async def _advice(args: dict) -> dict:
    from app import main as M

    out = await M.trading_advice()  # GET /api/trading/advice (garde les conseils sur les trades)
    n = sum("error" not in a for a in out["advices"])
    return {"text": f"🧭 {n} conseil(s) en prix" if n else "Aucun trade ouvert à conseiller (les KILL n'en ont pas)",
            **out}


async def _scan(args: dict) -> dict:
    from app import main as M

    out = await M.trading_scan()  # GET /api/trading/scan (rien enregistré, aucun ordre fictif)
    return {"text": out["text"], "picks": len(out.get("picks") or [])}


async def _proposal(args: dict, approve: bool) -> dict:
    from app import main as M
    from app.trading import proposals as PR

    p = await M.trading_proposal_decide(_pid(args), "approve" if approve else "refuse")
    return {"text": PR.decision_text(p), "proposal": p}


async def _veto(args: dict) -> dict:
    from app.lab import db as lab_db
    from app.lab import method as lab_method

    await asyncio.to_thread(lab_method.enqueue_veto, lab_db.db_path())  # comme `!veto`
    return {"text": "🧪 Veto déposé : le labo annule la dernière décision du council à son prochain passage"}


async def _killmode(args: dict, on: bool) -> dict:
    from app.trading import killmode as K  # import tardif : killmode tire le labo

    async with scheduler._lock:
        st = K.set_active(on)  # comme `!kill on` / `!kill off`
    return {"text": "🔪 Killmode allumé" if on else "Killmode éteint", "killmode": st}


async def _opinion(args: dict) -> dict:
    from app import main as M

    symbol = _symbol(str(args.get("symbol") or ""))
    out = await M.trading_opinion(symbol, _market(args, symbol))  # GET /api/trading/opinion = `!avis`
    return out


async def _company(args: dict) -> dict:
    from app import main as M

    return await M.trading_company(_symbol(str(args.get("symbol") or "")))  # GET /api/trading/company = `!societe`


HANDLERS = {
    "plan": _plan, "take": _take, "close": _close, "delete": _delete, "exit": _exit, "reopen": _reopen,
    "advice": _advice, "scan_now": _scan, "veto": _veto, "opinion": _opinion, "company": _company,
    "proposal_approve": lambda a: _proposal(a, True), "proposal_refuse": lambda a: _proposal(a, False),
    "killmode_on": lambda a: _killmode(a, True), "killmode_off": lambda a: _killmode(a, False),
}


def _journal(entry: dict) -> None:
    if not ACTIONS_LOG:
        return
    try:
        path = Path(ACTIONS_LOG)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:  # le journal ne bloque jamais l'action
        log.warning("Cockpit : journal des actions illisible (%s)", exc)


def recent_actions(limit: int = 20) -> list[dict]:
    if not ACTIONS_LOG or not Path(ACTIONS_LOG).exists():
        return []
    try:
        lines = Path(ACTIONS_LOG).read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


@router.post("/actions", dependencies=[Depends(csrf_ok)])
async def actions(body: Action):
    """Liste blanche (§9.4) : chaque action appelle la fonction de sa route ou commande Discord existante."""
    if body.action not in ACTIONS:
        raise HTTPException(400, f"Action inconnue : {body.action[:40]}")
    if body.space not in SPACES:
        raise HTTPException(400, "space doit valoir trades ou paper")
    writes_trades, _ = ACTIONS[body.action]
    if writes_trades and (body.space != "trades" or body.confirm != MES_TRADES):
        raise HTTPException(403, "Espace « Mes trades » verrouillé : ouvre-le par appui maintenu sur la bascule")
    now = datetime.now(timezone.utc)
    args = {k: (v[:120] if isinstance(v, str) else v) for k, v in body.args.items()}
    entry = {"at": now.isoformat(timespec="seconds"), "action": body.action, "space": body.space, "args": args}
    try:
        out = await HANDLERS[body.action](body.args)
    except HTTPException as exc:
        _journal(entry | {"ok": False, "status": exc.status_code, "resultat": str(exc.detail)[:300]})
        raise
    except Exception as exc:
        log.exception("Cockpit : action %s en échec", body.action)
        _journal(entry | {"ok": False, "status": 500, "resultat": type(exc).__name__})
        raise HTTPException(500, f"Action {body.action} en échec : {type(exc).__name__}")
    _journal(entry | {"ok": True, "status": 200, "resultat": str(out.get("text") or "")[:300]})
    return {"ok": True, "action": body.action, "at": entry["at"], **out}
