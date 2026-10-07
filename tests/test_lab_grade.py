"""Grade de JARVIS (F- à S+) : barème, grade officiel (baisse immédiate, hausse lente et approuvée,
coupe-circuit, council, nouvelle méthode, version morte), risque proposé en direct et routes."""
import json
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import main
from app.lab import grade as GR
from app.lab import method as M
from app.lab.db import LabDB
from app.notify import bot
from app.trading import proposals as PR
from app.trading import service as T
from tests.lab_fixtures import synthetic_trades

KEY = "cle-de-test-grade"
H = {"X-Lab-Key": KEY}
NOW = datetime(2026, 10, 7, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "lab.db"
    monkeypatch.setenv("JARVIS_LAB_DB", str(path))
    monkeypatch.delenv("JARVIS_LAB_GRADE", raising=False)
    monkeypatch.delenv("JARVIS_LAB_CHAMPION", raising=False)
    monkeypatch.setattr(GR, "_cache", {"key": None, "data": None})
    monkeypatch.setattr(GR, "_warned", set())
    return LabDB(str(path))


def state(db, market="stock") -> dict:
    return db.get("grade", market)


def events(db) -> list[str]:
    return [e["texte"] for e in M.events(db.conn, 0, 100)]


# --- Barème -------------------------------------------------------------------------------------

def test_noms_et_rangs():
    assert GR.name(0) == "F-" and GR.name(1) == "F" and GR.name(2) == "F+" and GR.name(20) == "S+"
    assert GR.name(GR.rank_of("C", 1)) == "C" and GR.name(None) == "non noté"
    assert GR.letter_of(GR.D_RANK) == "D" and GR.letter_of(None) is None


@pytest.mark.parametrize("mean, low, expected", [
    (0.45, 0.10, "S"), (0.45, -0.01, "D"), (0.30, 0.02, "A"), (0.20, 0.01, "B"), (0.06, 0.001, "C"),
    (0.04, 0.01, "D"), (0.01, -0.2, "D"), (0.0, -0.1, "E"), (-0.10, -0.3, "E"), (-0.23, -0.30, "F")])
def test_lettre(mean, low, expected):
    assert GR.letter(mean, low) == expected


def test_serie_attendue():
    # 30 % de réussite sur 1 000 trades : ~16 pertes d'affilée attendues ; 74 % de pertes sur 4 308 : ~23.
    assert round(GR.expected_streak(1000, 0.7)) == 16
    assert round(GR.expected_streak(4308, 0.74)) == 23
    assert GR.expected_streak(1, 0.5) is None and GR.expected_streak(100, 0.0) is None


def test_risque_du_grade():
    rank = GR.rank_of
    assert GR.grade_risk(None, 5.0) == 0.0
    assert GR.grade_risk(rank("F", 2), 5.0) == 0.0 and GR.grade_risk(rank("E", 1), 5.0) == 0.0
    assert GR.grade_risk(rank("D", 0), 0.0) == 0.25  # micro-exposition fixe
    assert GR.grade_risk(rank("C", 1), 0.4) == 0.4 and GR.grade_risk(rank("C", 1), 9.0) == 1.0
    assert GR.grade_risk(rank("B", 2), 9.0) == 1.5
    assert GR.grade_risk(rank("A", 0), 9.0) == 3.0 and GR.grade_risk(rank("S", 2), 9.0) == 5.0  # plafond EmperorBTC


def rows(rs, per_day=10, delay=1):
    return [(i, f"2026-01-{1 + i // per_day:02d}", r, int(r > 0), 0, 0, delay if r > 0 else None, None, None)
            for i, r in enumerate(rs)]


def test_evaluation_echantillon_trop_petit():
    m = GR.evaluate(rows([0.5] * 99, per_day=10))
    assert m["rang"] is None and m["trades"] == 99 and "échantillon" in m["raison"]
    m = GR.evaluate(rows([0.5] * 200, per_day=25))  # 8 jours seulement
    assert m["rang"] is None


def test_evaluation_criteres_et_kelly():
    rs = [1.0 if i % 2 else -0.5 for i in range(300)]  # alterné : séries courtes, très régulier
    m = GR.evaluate(rows(rs, per_day=20, delay=1))
    assert m["lettre"] == "A" and m["esperance_r"] == 0.25
    assert m["criteres"]["series_normales"] and m["criteres"]["regulier"]
    assert not m["criteres"]["delais_calibres"]  # 100 % tenus : délais trop prudents
    assert m["grade"].endswith("+")  # 2 critères sur 3
    assert m["kelly_pct"] > 0 and m["reussite"] == 0.5
    perte = GR.evaluate(rows([-1.0] * 120 + [2.0] * 40, per_day=16))
    assert perte["lettre"] == "F" and perte["kelly_pct"] == 0
    assert not perte["criteres"]["series_normales"]  # 120 pertes d'affilée
    assert not perte["criteres"]["regulier"]


# --- Grade officiel (worker) ----------------------------------------------------------------------

def test_premier_grade_negatif_et_fichier(db):
    synthetic_trades(db, n_days=20, per_day=20, base=-0.3, noise=0.8)
    out = GR.step(db, NOW)
    st = state(db)
    assert GR.letter_of(st["rang"]) == "F" and out["stock"] == GR.name(st["rang"])
    assert st["mesure"]["trades"] > 250 and st["morte"]  # IC entièrement sous zéro sur > 150 trades
    data = json.loads(GR.live_path().read_text(encoding="utf-8"))
    g = data["marches"]["stock"]
    assert g["risque_grade"] == 0.0 and g["mode_survie"] and g["morte"]
    assert any("🏅" in t for t in events(db)) and any("☠️" in t for t in events(db))
    hist = GR.history(db.conn)
    assert hist[0]["source"] == "calcul" and hist[0]["raison"] == "premier grade"
    # Rien de neuf : pas de recalcul, pas de nouvel événement.
    n = len(events(db))
    GR.step(db, NOW)
    assert len(events(db)) == n


def test_marche_non_note(db):
    synthetic_trades(db, n_days=5, per_day=10, base=0.5, market="crypto")
    GR.step(db, NOW)
    data = GR.payload(db.conn)
    assert data["marches"]["crypto"]["non_note"] and data["marches"]["crypto"]["risque_grade"] == 0.0
    assert data["marches"]["crypto"]["grade"] == "non noté"


def test_premier_grade_eleve_plafonne_a_d_plus_et_hausse_proposee(db):
    synthetic_trades(db, n_days=30, per_day=20, base=0.7, noise=0.4)
    GR.step(db, NOW)
    st = state(db)
    assert st["rang"] == GR.D_PLUS
    assert st["hausse"] and GR.letter_of(st["hausse"]["vers"]) in ("S", "A")


def test_hausse_lente_approuvee_puis_baisse_immediate(db, tmp_path):
    synthetic_trades(db, n_days=20, per_day=20, base=-0.3, noise=0.8, first=date(2024, 1, 2))
    GR.step(db, NOW)
    assert GR.letter_of(state(db)["rang"]) == "F"
    # 60 nouveaux jours très gagnants : la fenêtre (60 derniers jours) ne voit plus qu'eux.
    synthetic_trades(db, n_days=60, per_day=20, base=0.7, noise=0.4, first=date(2024, 3, 1), seed=2)
    GR.step(db, NOW)
    st = state(db)
    assert GR.letter_of(st["rang"]) == "F"  # une hausse de lettre attend l'accord de Simão
    h = st["hausse"]
    assert h and GR.letter_of(h["vers"]) in ("S", "A") and h["source"] == "calcul"
    # Le serveur en fait une proposition (une seule), envoyée en privé.
    props = PR.Proposals(None)
    new = GR.sync_proposals(props, GR.current())
    assert len(new) == 1 and new[0]["kind"] == "grade" and new[0]["value"] == GR.name(h["vers"])
    assert GR.sync_proposals(props, GR.current()) == []
    assert "le risque proposé par trade augmente" in PR.text(new[0])
    p = props.decide(new[0]["id"], True)
    assert "approuvée" in PR.decision_text(p)
    GR.step(db, NOW)
    st = state(db)
    assert st["rang"] == h["vers"] and st["hausse"] is None
    assert GR.history(db.conn)[0]["source"] == "accord de Simão"
    assert GR.payload(db.conn)["marches"]["stock"]["risque_grade"] > 0
    # Les pertes reviennent : baisse immédiate, sans accord.
    synthetic_trades(db, n_days=60, per_day=20, base=-0.4, noise=0.8, first=date(2024, 6, 3), seed=3)
    GR.step(db, NOW)
    assert GR.letter_of(state(db)["rang"]) == "F"


def test_hausse_refusee_pas_reproposee_sans_nouvelles_preuves(db):
    synthetic_trades(db, n_days=20, per_day=20, base=-0.3, noise=0.8, first=date(2024, 1, 2))
    GR.step(db, NOW)
    synthetic_trades(db, n_days=60, per_day=20, base=0.7, noise=0.4, first=date(2024, 3, 1), seed=2)
    GR.step(db, NOW)
    props = PR.Proposals(None)
    (p,) = GR.sync_proposals(props, GR.current())
    props.decide(p["id"], False)
    GR.step(db, NOW)
    st = state(db)
    assert GR.letter_of(st["rang"]) == "F" and st["hausse"] is None
    db.set("grade_calc", "signature", None)  # force un recalcul : toujours pas de nouvelle hausse
    GR.step(db, NOW)
    assert state(db)["hausse"] is None
    GR.sync_proposals(props, GR.current())
    assert props.get(p["id"])["status"] == "refused"


def test_hausse_dans_la_meme_lettre_sans_accord(db):
    db.conn.execute("INSERT INTO method_versions (version, created_at, settings) VALUES ('v1', '2024-01-01', '{}')")
    db.set("grade", "stock", {"rang": GR.rank_of("F", 0), "version": "v1", "depuis_id": 0, "base_id": 0,
                              "change_le": None, "hausse": None, "morte": False, "seq": 0})
    synthetic_trades(db, n_days=20, per_day=20, base=-0.3, noise=0.8)
    GR.step(db, NOW)
    st = state(db)
    assert GR.letter_of(st["rang"]) == "F" and st["hausse"] is None


def test_coupe_circuit(db):
    synthetic_trades(db, n_days=30, per_day=20, base=0.7, noise=0.4)
    GR.step(db, NOW)
    st = state(db)
    st["rang"], st["hausse"] = GR.rank_of("A", 1), None
    db.set("grade", "stock", st)
    # Une journée noire : 30 stops d'affilée, l'espérance de la fenêtre reste haute.
    synthetic_trades(db, n_days=1, per_day=30, base=-3.0, noise=0.01, first=date(2024, 3, 1), seed=5,
                     trigger_rate=1.0)
    GR.step(db, NOW)
    st = state(db)
    assert st["rang"] == GR.D_RANK
    assert GR.history(db.conn)[0]["source"] == "coupe-circuit"


def test_nouvelle_methode_remet_a_zero(db):
    synthetic_trades(db, n_days=20, per_day=20, base=-0.3, noise=0.8)
    GR.step(db, NOW)
    assert state(db)["version"] == "v1"
    db.conn.execute("INSERT INTO method_versions (version, created_at, settings, base, rules, role, started_ts) "
                    "VALUES ('v2', '2026-10-07', '{}', 'v1', ?, 'champion', ?)",
                    (json.dumps([{"empreinte": "x" * 64, "nom": "r", "kind": "exclude",
                                  "cond": {"field": "context.signal", "op": ">", "value": 0.9}, "markets": None}]),
                     int(NOW.timestamp())))
    GR.step(db, NOW)
    st = state(db)
    assert st["version"] == "v2" and st["rang"] is None  # ses trades d'avant ne comptent pas
    assert st["mesure"]["trades"] == 0
    assert any(h["source"] == "nouvelle méthode" for h in GR.history(db.conn))


# --- Council du soir -------------------------------------------------------------------------------

def avis(adj=-1, marche="stock", **extra):
    return {"marche": marche, "ajustement": adj, "motifs": "La série de 41 pertes montre des trades corrélés."} | extra


@pytest.mark.parametrize("raw, msg", [
    ([], "objet"), (avis(adj=2), "un cran"), (avis(adj=True), "un cran"), (avis(marche="forex"), "marche"),
    (avis(motifs="court"), "motifs"), (avis(voix={"optimiste": "x"}), "voix"), (avis(autre=1), "inconnu")])
def test_avis_illisible(raw, msg):
    with pytest.raises(GR.AvisError, match=msg):
        GR.parse_avis(raw)


def test_avis_du_council(db):
    synthetic_trades(db, n_days=20, per_day=20, base=-0.3, noise=0.8)
    GR.step(db, NOW)
    st = state(db)
    st["rang"] = GR.rank_of("F", 1)
    db.set("grade", "stock", st)
    GR.enqueue_avis(db.path, avis(-1, voix={"sceptique": "Chance ?"}), "2026-10-07")
    GR.step(db, NOW)
    assert state(db)["rang"] == GR.rank_of("F", 0)
    h = GR.history(db.conn)[0]
    assert h["source"] == "council du soir" and h["soiree"] == "2026-10-07"
    # Un seul avis par marché et par soirée.
    GR.enqueue_avis(db.path, avis(-1), "2026-10-07")
    GR.step(db, NOW)
    assert state(db)["rang"] == GR.rank_of("F", 0)
    # Le lendemain, +1 dans la même lettre : appliqué ; marché non noté : ignoré.
    GR.enqueue_avis(db.path, avis(1), "2026-10-08")
    GR.enqueue_avis(db.path, avis(1, marche="crypto"), "2026-10-08")
    GR.step(db, NOW)
    assert state(db)["rang"] == GR.rank_of("F", 1) and state(db)["hausse"] is None
    assert state(db, "crypto") is None or state(db, "crypto")["rang"] is None
    assert not list(GR.queue_dir(db.path).glob("*.json"))


def test_avis_hausse_de_lettre_proposee(db):
    synthetic_trades(db, n_days=20, per_day=20, base=-0.3, noise=0.8)
    GR.step(db, NOW)
    st = state(db)
    st["rang"] = GR.rank_of("F", 2)
    db.set("grade", "stock", st)
    GR.enqueue_avis(db.path, avis(1), "2026-10-07")
    GR.step(db, NOW)
    st = state(db)
    assert st["rang"] == GR.rank_of("F", 2) and st["hausse"]["source"] == "council"


# --- Risque proposé en direct ----------------------------------------------------------------------

DATA = {"marches": {"stock": {"grade": "A", "rang": GR.rank_of("A", 1), "non_note": False, "risque_grade": 3.0,
                              "risque_max": 3.0, "mode_survie": False},
                    "crypto": {"grade": "F-", "rang": 0, "non_note": False, "risque_grade": 0.0, "risque_max": 0.0,
                               "mode_survie": True}}}


def trade(status="open", risk=1.0, stop_current=95.0, hit=0, events=()):
    return {"status": status, "side": "long", "entry": 100.0, "stop": 95.0, "stop_current": stop_current,
            "sizing": {"risk_pct": risk}, "targets": [{"price": 105.0}, {"price": 110.0}, {"price": 115.0}],
            "targets_hit": hit, "events": list(events), "last_price": 100.0}


def test_risque_now():
    g = GR.risk_now("stock", [], NOW, DATA)
    assert g["risque_propose"] == 3.0 and not g["observation"]
    g = GR.risk_now("crypto", [], NOW, DATA)
    assert g["risque_propose"] == 0.0 and g["observation"] and "pas de capital réel" in g["raison"]
    assert GR.risk_now("stock", [], NOW, {"marches": {}})["risque_propose"] == 0.0
    # Risque déjà ouvert : 2 trades à 1,5 % ; le stop remonté à l'entrée ne compte plus.
    opened = [trade(risk=1.5), trade(risk=1.5), trade(risk=3.0, stop_current=100.0, hit=2)]
    assert GR.open_risk(opened) == 3.0
    g = GR.risk_now("stock", opened, NOW, DATA)
    assert g["risque_propose"] == 1.0 and "risque déjà ouvert" in g["raison"]
    # Arrêt de la journée à −3 R (date de Paris de la clôture).
    stopped = [trade(status="closed", events=[{"kind": "stop", "price": 95.0, "at": NOW.isoformat()}])
               for _ in range(3)]
    assert GR.day_r(stopped, NOW) == -3.0
    g = GR.risk_now("stock", stopped, NOW, DATA)
    assert g["risque_propose"] == 0.0 and "arrêt de la journée" in g["raison"]


def plan():
    return {"market": "stock", "entry": 100.0, "stop": 95.0, "sizing": {"risk_pct": 3.0}}


def test_plan_en_direct(monkeypatch):
    monkeypatch.setattr(GR, "current", lambda: DATA)
    monkeypatch.setenv("JARVIS_CAPITAL", "10000")
    p = plan()
    T.apply_grade(p, None, None, NOW, trades=[])
    assert p["sizing"]["risk_pct"] == 3.0 and p["sizing"]["risk_amount"] == 300.0
    assert "🏅 Grade JARVIS **A** · risque proposé **3 %**" in GR.plan_line(p["grade"])
    p = plan() | {"market": "crypto"}
    T.apply_grade(p, None, None, NOW, trades=[])
    assert p["sizing"] is None and "observation" in GR.plan_line(p["grade"])
    p = plan() | {"market": "crypto"}
    T.apply_grade(p, None, 2.0, NOW, trades=[])  # risque demandé : gardé
    assert p["sizing"] == {"risk_pct": 3.0} and p["grade"]["risque_propose"] == 0.0


# --- Routes, brief, Discord -----------------------------------------------------------------------

@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("JARVIS_LAB_API_KEY", KEY)
    return TestClient(main.app)


def test_routes(client, db):
    synthetic_trades(db, n_days=20, per_day=20, base=-0.3, noise=0.8)
    GR.step(db, NOW)
    assert client.get("/api/lab/grade").status_code == 401
    r = client.get("/api/lab/grade", headers=H)
    assert r.status_code == 200 and r.json()["marches"]["stock"]["grade"].startswith("F")
    assert r.json()["historique"][0]["raison"] == "premier grade"
    r = client.post("/api/lab/grade/avis", headers=H, json=avis(-1))
    assert r.status_code == 200 and r.json()["en_file"]
    assert client.post("/api/lab/grade/avis", headers=H, json=avis(3)).status_code == 422
    brief = client.get("/api/lab/brief", headers=H).json()
    assert brief["grade"]["marches"]["stock"]["mode_survie"]
    page = client.get("/cockpit/labo").text
    assert "Grade de JARVIS" in page and "méthode morte" in page


async def test_commande_grade(db):
    synthetic_trades(db, n_days=20, per_day=20, base=-0.3, noise=0.8)
    GR.step(db, NOW)
    text = await bot.handle("!grade")
    assert "Grade JARVIS" in text and "Actions : F" in text and "Méthode déclarée morte" in text
    assert "!grade" in bot.HELP


def test_texte_sans_grade():
    assert "pas encore calculé" in GR.text(None)
