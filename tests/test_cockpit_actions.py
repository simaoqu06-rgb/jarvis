"""Page et actions du nouveau cockpit (GET /cockpit, POST /api/cockpit/actions) : CSRF, verrou « Mes trades », liste
blanche, mêmes fonctions que les routes et commandes Discord, journal des actions, en-têtes de sécurité."""
import json
import re

import pytest
from fastapi.testclient import TestClient

from app import scheduler
from app.trading import proposals as PR
from app.trading import reco as R
from app.trading import service as T
from app.web import cockpit_actions as A

from tests.test_cockpit_api import trade

URL = "/api/cockpit/actions"


@pytest.fixture
def client(monkeypatch, tmp_path):
    from app import main

    monkeypatch.setattr(scheduler, "book", T.Book(None))
    monkeypatch.setattr(scheduler, "reco_book", R.RecoBook(None))
    monkeypatch.setattr(A, "ACTIONS_LOG", str(tmp_path / "actions.jsonl"))
    return TestClient(main.app, base_url="https://testserver")  # le cookie CSRF est Secure


def token(client) -> str:
    r = client.get("/cockpit")
    assert r.status_code == 200
    return r.cookies.get("jarvis_csrf") or client.cookies.get("jarvis_csrf")


def post(client, body, tok=None):
    tok = token(client) if tok is None else tok
    return client.post(URL, json=body, headers={"X-CSRF-Token": tok})


def journal() -> list[dict]:
    return [json.loads(line) for line in open(A.ACTIONS_LOG, encoding="utf-8")]


# --- Page ----------------------------------------------------------------------------------------------

def test_page_pose_le_jeton_csrf_et_version_les_fichiers(client):
    r = client.get("/cockpit")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert re.search(r'/cockpit/app\.js\?v=[0-9a-f]{12}"', r.text) and re.search(r'/cockpit/app\.css\?v=[0-9a-f]{12}"', r.text)
    assert "{{" not in r.text
    cookie = r.headers["set-cookie"].lower()
    assert "jarvis_csrf=" in cookie and "samesite=strict" in cookie and "secure" in cookie and "httponly" not in cookie
    first = client.cookies.get("jarvis_csrf")
    assert len(first) >= 32
    client.get("/cockpit")
    assert client.cookies.get("jarvis_csrf") == first  # même jeton tant que le cookie vit
    fresh = TestClient(client.app, base_url="https://testserver")
    r = fresh.get("/cockpit", headers={"Cookie": "jarvis_csrf=court<script>"})
    assert len(r.cookies.get("jarvis_csrf")) >= 32  # jeton d'une autre forme : remplacé


def test_fichiers_servis_en_local_et_ancien_cockpit(client):
    for url in ("/cockpit/app.js", "/cockpit/app.css",
                "/cockpit-static/vendor/lightweight-charts-5.0.8.standalone.production.js",
                "/cockpit-static/fonts/geist-latin-400.woff2"):
        r = client.get(url)
        assert r.status_code == 200, url
        assert r.headers["content-security-policy"].startswith("default-src 'self'"), url
    assert client.get("/cockpit/app.txt").status_code == 404
    old = client.get("/cockpit/ancien")
    assert old.status_code == 200 and "content-security-policy" not in old.headers


def test_la_page_ne_charge_rien_hors_du_serveur():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "app" / "web" / "cockpit"
    html, js, css = ((root / n).read_text(encoding="utf-8") for n in ("index.html", "app.js", "app.css"))
    assert not re.search(r"(src|href)=\"https?://", html) and "url(http" not in css
    assert " style=\"" not in html and "<script>" not in html  # CSP : ni style ni script en ligne
    assert ".innerHTML" not in js and "insertAdjacentHTML" not in js and "eval(" not in js


def test_vue_a_faire_branchee_en_deuxieme_position():
    from pathlib import Path

    from app.web import attention, cockpit_actions

    root = Path(__file__).resolve().parent.parent / "app" / "web" / "cockpit"
    html, js = ((root / n).read_text(encoding="utf-8") for n in ("index.html", "app.js"))
    views = re.findall(r"^  \['(\w+)', '", js.split("const VIEWS = [", 1)[1].split("];", 1)[0], re.M)
    assert views[:3] == ["cockpit", "afaire", "marches"] and len(views) == 9  # touches 1 à 9
    assert re.findall(r'data-view="(\w+)"', html) == views  # une section par vue, dans l'ordre du rail
    assert "/^[1-9]$/" in js and "t: 'afaire'" in js
    # les boutons d'action de la page À faire : ceux que le moteur peut proposer, tous connus du serveur
    todo_act = re.findall(r"^  (\w+): ", js.split("const TODO_ACT = {", 1)[1].split("};", 1)[0], re.M)
    assert set(todo_act) == set(attention.STEP_ACTIONS) <= set(cockpit_actions.ACTIONS)


# --- Gardes ---------------------------------------------------------------------------------------------

def test_csrf_absent_ou_faux(client):
    token(client)
    assert client.post(URL, json={"action": "plan"}).status_code == 403
    r = client.post(URL, json={"action": "plan"}, headers={"X-CSRF-Token": "x" * 43})
    assert r.status_code == 403 and "CSRF" in r.json()["detail"]
    fresh = TestClient(client.app, base_url="https://testserver")  # sans cookie
    assert fresh.post(URL, json={"action": "plan"}, headers={"X-CSRF-Token": "x" * 43}).status_code == 403


def test_autre_site_refuse_meme_avec_le_jeton(client):
    tok = token(client)
    r = client.post(URL, json={"action": "veto"}, headers={"X-CSRF-Token": tok, "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_action_et_espace_inconnus(client):
    assert post(client, {"action": "buy_now"}).status_code == 400
    assert post(client, {"action": "plan", "space": "live"}).status_code == 400
    assert post(client, {"action": "close", "space": "trades", "confirm": "MES_TRADES", "args": {"id": "a b"}}).status_code == 400


@pytest.mark.parametrize("action", ["take", "exit", "close", "delete", "reopen", "advice"])
def test_mes_trades_verrouille_sans_appui_maintenu(client, action):
    scheduler.book.trades.append(trade())
    for body in ({"action": action, "space": "paper", "confirm": "MES_TRADES"},
                 {"action": action, "space": "trades"},
                 {"action": action, "space": "trades", "confirm": "mes_trades"}):
        r = post(client, body | {"args": {"id": "t1", "symbol": "NVDA", "price": 180}})
        assert r.status_code == 403 and "verrouillé" in r.json()["detail"], body
    assert scheduler.book.trades[0]["status"] == "open"


def test_toutes_les_actions_ont_un_gestionnaire():
    assert set(A.ACTIONS) == set(A.HANDLERS)
    protected = {a for a, (_, hold) in A.ACTIONS.items() if hold}
    assert protected == {"exit", "close", "delete", "reopen", "proposal_approve", "veto", "killmode_on", "killmode_off"}


# --- Mêmes fonctions que les routes et commandes ---------------------------------------------------------

LIVE = {"space": "trades", "confirm": "MES_TRADES"}


def test_cloturer_et_supprimer_comme_les_routes(client):
    scheduler.book.trades += [trade(), trade(id="t9", symbol="AMD")]
    r = post(client, {"action": "close", **LIVE, "args": {"id": "t1", "price": "181,5"}})
    assert r.status_code == 200, r.text
    t = scheduler.book.trades[0]
    assert t["status"] == "closed" and t["events"][-1]["price"] == 181.5 and "181.5" in r.json()["text"]
    assert post(client, {"action": "close", **LIVE, "args": {"id": "inconnu"}}).status_code == 404
    assert post(client, {"action": "delete", **LIVE, "args": {"id": "t9"}}).json()["deleted"] == "t9"
    assert [x["id"] for x in scheduler.book.trades] == ["t1"]
    rows = journal()
    assert [(e["action"], e["ok"], e["status"]) for e in rows] == [("close", True, 200), ("close", False, 404),
                                                                   ("delete", True, 200)]
    assert rows[0]["space"] == "trades" and rows[0]["args"] == {"id": "t1", "price": "181,5"}


def test_sortie_reelle_comme_sortie_sur_discord(client, monkeypatch):
    calls = []
    real = T.record_exit

    def spy(book, symbol, price):
        calls.append((symbol, price))
        return real(book, symbol, price)

    monkeypatch.setattr(T, "record_exit", spy)
    scheduler.book.trades.append(trade())
    r = post(client, {"action": "exit", **LIVE, "args": {"symbol": "nvda", "price": 183.2}})  # mis en majuscules
    assert r.status_code == 200 and calls == [("NVDA", 183.2)]
    assert scheduler.book.trades[0]["status"] == "closed" and r.json()["text"].startswith("🏁 **NVDA** clôturé à 183.2")
    assert post(client, {"action": "exit", **LIVE, "args": {"symbol": "AMD", "price": 1}}).status_code == 404
    assert post(client, {"action": "exit", **LIVE, "args": {"symbol": "NVDA", "price": -3}}).status_code == 400
    assert post(client, {"action": "exit", **LIVE, "args": {"symbol": "NV$DA", "price": 3}}).status_code == 400


def test_rouvrir_comme_rouvre(client, monkeypatch):
    async def plan(symbol, market, side, entry, stop, targets):
        return {"symbol": symbol, "market": market, "side": side, "entry": entry, "stop": stop, "targets": targets}

    seen = []
    monkeypatch.setattr(T, "build_plan", plan)
    monkeypatch.setattr(T, "reopen", lambda book, symbol, p: seen.append((symbol, p)) or {"id": "old", "stop_current": 9.5})
    r = post(client, {"action": "reopen", **LIVE, "args": {"symbol": "MRVL", "market": "stock", "entry": 281,
                                                            "stop": 270, "targets": [290, 300]}})
    assert r.status_code == 200 and r.json()["ancien"] == "old"
    assert seen == [("MRVL", {"symbol": "MRVL", "market": "stock", "side": "long", "entry": 281.0, "stop": 270.0,
                              "targets": [290.0, 300.0]})]
    assert post(client, {"action": "reopen", **LIVE, "args": {"symbol": "MRVL", "market": "stock"}}).status_code == 400


def test_proposition_approuvee_et_refusee(client, monkeypatch):
    from app.lab import method as lab_method

    store = PR.Proposals(None)
    queued = []
    monkeypatch.setattr(PR, "proposals", store)
    monkeypatch.setattr(lab_method, "enqueue_decision", lambda db, h, choice, pid: queued.append((h, choice, pid)))
    a = store.propose_lab("abc", "rebond", None, "preuve", None, "actions")
    b = store.propose_lab("def", "cassure", None, "preuve", None, "actions")
    r = post(client, {"action": "proposal_approve", "args": {"id": a["id"]}})
    assert r.status_code == 200 and store.get(a["id"])["status"] == "approved" and r.json()["text"] == PR.decision_text(store.get(a["id"]))
    assert post(client, {"action": "proposal_refuse", "args": {"id": b["id"]}}).json()["proposal"]["status"] == "refused"
    assert queued == [("abc", "approuvee", a["id"]), ("def", "refusee", b["id"])]
    assert post(client, {"action": "proposal_approve", "args": {"id": a["id"]}}).status_code == 409
    assert post(client, {"action": "proposal_approve", "args": {"id": "x"}}).status_code == 400


def test_veto_et_killmode_comme_discord(client, monkeypatch):
    from app.lab import method as lab_method
    from app.trading import killmode as K

    vetos, states = [], []
    monkeypatch.setattr(lab_method, "enqueue_veto", lambda db: vetos.append(db))
    monkeypatch.setattr(K, "set_active", lambda on: states.append(on) or {"actif": on})
    assert post(client, {"action": "veto"}).status_code == 200 and len(vetos) == 1
    assert post(client, {"action": "killmode_on"}).json()["killmode"] == {"actif": True}
    assert post(client, {"action": "killmode_off"}).json()["text"] == "Killmode éteint"
    assert states == [True, False]


def test_erreur_inattendue_journalisee_sans_detail(client, monkeypatch):
    from app.trading import killmode as K

    def boom(on):
        raise RuntimeError("chemin secret /root/.env")

    monkeypatch.setattr(K, "set_active", boom)
    r = post(client, {"action": "killmode_on"})
    assert r.status_code == 500 and "secret" not in r.text and r.json()["detail"] == "Action killmode_on en échec : RuntimeError"
    assert journal()[-1] == journal()[-1] | {"action": "killmode_on", "ok": False, "status": 500, "resultat": "RuntimeError"}


def test_plan_et_prise_par_les_routes_existantes(client, monkeypatch):
    from app import main as M

    async def fake_plan(req):
        return {"symbol": req.symbol, "market": req.market, "side": req.side, "entry": req.entry or 100.0,
                "stop": req.stop or 98.0, "targets": [{"name": "TP1", "price": 101.0}], "note": req.note}

    monkeypatch.setattr(M, "_plan", fake_plan)
    monkeypatch.setattr(M.trading, "plan_text", lambda p: f"plan {p['symbol']}")
    monkeypatch.setattr(scheduler.book, "add", lambda plan, note: {**plan, "id": "n1", "status": "open", "note": note,
                                                                   "last_price": plan["entry"], "events": []})
    r = post(client, {"action": "plan", "args": {"symbol": "NVDA", "market": "stock", "entry": "176,9", "targets": [180]}})
    assert r.status_code == 200 and r.json()["text"] == "plan NVDA" and r.json()["plan"]["entry"] == 176.9
    r = post(client, {"action": "take", **LIVE, "args": {"symbol": "NVDA", "market": "stock", "note": "cassure"}})
    assert r.status_code == 200 and r.json()["trade"]["note"] == "cassure" and "aucun ordre" in r.json()["text"]
    assert post(client, {"action": "plan", "args": {"symbol": "NVDA", "side": "achat"}}).status_code == 400
    assert post(client, {"action": "plan", "args": {"symbol": "NVDA", "targets": [1, 2, 3, 4, 5, 6]}}).status_code == 400
    assert post(client, {"action": "take", **LIVE, "args": {"symbol": "NVDA", "note": "x" * 301}}).status_code == 400
