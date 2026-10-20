"""Grade de JARVIS (F- à S+) : barème, grade officiel (baisse immédiate, hausse lente et approuvée,
coupe-circuit, council, nouvelle méthode, version morte), risque proposé en direct et routes."""
import json
import random
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


def test_ic_en_student_c_devient_d():
    # 10 jours, espérance +0,10 R = 2 erreurs types : prouvée avec 1,96 (C), plus en t à 9 degrés (2,262).
    m = GR.evaluate(rows([0.25 if (i // 10) % 2 else -0.05 for i in range(100)], per_day=10))
    assert m["esperance_r"] == 0.1 and m["ic95"][0] < 0 < 0.1 - 1.96 * 0.05
    assert m["lettre"] == "D"


def test_kelly_corrige_de_la_correlation_du_jour():
    # Tous les trades d'un jour ont le même R (ρ ≈ 1) : un A (3 %) laisse 2 positions sous le budget de 4 %,
    # le Kelly est divisé par ≈ 2.
    days = [0.8, -0.2, 0.6, 0.0]
    m = GR.evaluate(rows([days[(i // 10) % 4] for i in range(400)], per_day=10))
    assert m["lettre"] == "A" and m["positions_max"] == 2 and m["rho_jour"] > 0.95
    assert m["kelly_pct"] == pytest.approx(m["kelly_brut_pct"] / (1 + m["rho_jour"]), abs=1e-3)
    assert m["kelly_pct"] < 0.52 * m["kelly_brut_pct"]
    # Trades indépendants (ρ ≈ 0) : Kelly presque inchangé.
    rnd = random.Random(3)
    m = GR.evaluate(rows([round(rnd.gauss(0.3, 1.0), 4) for _ in range(600)], per_day=20))
    assert m["lettre"] == "B" and m["positions_max"] == 3 and m["rho_jour"] < 0.05
    assert m["kelly_pct"] > 0.9 * m["kelly_brut_pct"] > 0


# --- Ce que coûte vraiment un trade (R5) et fenêtre réelle (I5) -------------------------------------

def sim_result(price, reason, entry=10.0, stop=9.7):
    """Résultat du simulateur (trades.result) : une seule sortie, rempli à l'entrée."""
    return json.dumps({"triggered": True, "fill_price": entry, "exits": [{"part": 1.0, "price": price, "reason": reason}],
                       "order_prices": {"entry": entry, "stop": stop, "targets": [10.3, 10.6, 10.9]}})


def test_r_du_grade_apres_commission_ibkr():
    # Stoppé à 9,70 pour une entrée à 10 : à 20 € de risque (76 actions, 3 brackets, minimums de 1 $), -1,30 R.
    assert GR.cost_r("stock", -1.0, sim_result(9.7, "stop")) == pytest.approx(-1.30, abs=0.01)
    assert GR.cost_r("crypto", -1.0, sim_result(9.7, "stop")) == -1.0  # frais OKX déjà dans r_net
    assert GR.cost_r("stock", -1.0, "{}") == -1.0 and GR.cost_r("stock", -1.0, None) == -1.0  # sans détail : r_net
    assert GR.cost_r("stock", 0.2, '{"triggered": true, "exits": [{"part": 1}]}') == 0.2  # détail illisible


def test_grade_sur_les_r_a_20_euros(db):
    synthetic_trades(db, n_days=15, per_day=12, base=0.2, noise=0.3)
    synthetic_trades(db, n_days=15, per_day=12, base=0.2, noise=0.3, market="crypto")
    rows_, fen = GR.window(db.conn, "stock")
    assert GR.evaluate(rows_)["lettre"] in ("B", "A") and not fen.get("discontinue")
    # Les mêmes trades avec leur détail : +0,2 R au labo, mais -0,016 R une fois payés les ordres IBKR à 20 €.
    db.conn.execute("UPDATE trades SET result = ? WHERE triggered = 1", (sim_result(10.06, "cloture"),))
    rows_, _ = GR.window(db.conn, "stock")
    assert {r[2] for r in rows_} == {-0.0156}
    m = GR.evaluate(rows_)
    assert m["lettre"] == "E" and m["kelly_pct"] == 0
    crypto, _ = GR.window(db.conn, "crypto")  # crypto : r_net du labo, inchangé
    assert GR.evaluate(crypto)["lettre"] in ("B", "A")
    assert GR.since(db.conn, "stock", 0)["baisse_r"] > 1.0  # le coupe-circuit lit les mêmes R


def test_signature_du_calcul(db):
    synthetic_trades(db, n_days=2, per_day=2)
    assert GR.signature(db)[3] == GR.GRADE_CALC == 2  # version du calcul : changer de calcul force un recalcul


def test_fenetre_reelle_et_trou(db):
    assert GR.span([], set())["jours"] == 0
    days = ["2026-04-01", "2026-04-02", "2026-10-06", "2026-10-07"]
    assert GR.span(days, {"2026-10-06", "2026-10-07"}) == {
        "du": "2026-04-01", "au": "2026-10-07", "jours": 4, "jours_direct": 2, "trou_max_jours": 187,
        "rejeu_du": "2026-04-01", "rejeu_au": "2026-04-02", "direct_du": "2026-10-06"}
    assert GR.last_segment(days) == ["2026-10-06", "2026-10-07"]
    assert GR.last_segment(["2026-01-02", "2026-01-05", "2026-01-12"]) == ["2026-01-02", "2026-01-05", "2026-01-12"]


def test_fenetre_discontinue_sans_risque_puis_coupee(db):
    # Rejeu de janvier à mars 2026, puis 4 jours de direct en octobre : fenêtre mixte, segment récent trop petit.
    synthetic_trades(db, n_days=60, per_day=10, base=0.5, noise=0.5, first=date(2026, 1, 5))
    synthetic_trades(db, n_days=4, per_day=15, base=0.5, noise=0.5, first=date(2026, 10, 6), origin="live", seed=2)
    GR.step(db, NOW)
    st = state(db)
    fen = st["mesure"]["fenetre"]
    assert fen["jours"] == 60 and fen["jours_direct"] == 4 and fen["trou_max_jours"] >= 180 and fen["discontinue"]
    assert fen["direct_du"] == "2026-10-06" and fen["rejeu_au"] == "2026-03-27"
    assert st["rang"] == GR.D_PLUS and st["hausse"] is None  # lettre mesurée gardée, pas de hausse proposée
    data = GR.current()
    g = data["marches"]["stock"]
    assert g["discontinue"] and g["risque_grade"] == 0.0
    r = GR.risk_now("stock", [], NOW, data)
    assert r["risque_propose"] == 0.0 and r["raison"].startswith(
        "fenêtre discontinue (rejeu jusqu'au 27/03/2026, direct depuis le 06/10/2026)")
    text = GR.text(data, now=NOW)
    assert "fenêtre : rejeu du " in text and "direct depuis le 06/10/2026 (60 jours, dont 4 en direct" in text
    assert "fenêtre discontinue" in text
    # Le direct atteint 16 jours et plus de 100 trades : le grade est calculé sur lui seul.
    synthetic_trades(db, n_days=12, per_day=15, base=0.5, noise=0.5, first=date(2026, 10, 12), origin="live", seed=3)
    rows_, fen = GR.window(db.conn, "stock")
    assert fen["coupee_au"] == "2026-10-06" and not fen.get("discontinue")
    assert min(r[1] for r in rows_) == "2026-10-06" and len(rows_) >= 100  # le rejeu d'avant le trou est écarté
    GR.step(db, NOW)
    assert not GR.current()["marches"]["stock"]["discontinue"]
    assert "grade calculé sur les jours depuis le 06/10/2026 seulement" in GR.text(GR.current(), now=NOW)


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
    assert any("⬇️" in t and "→ **F" in t for t in events(db))  # changement de lettre : message privé


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
    # Le cran change (F- → F ou F+) : gardé dans l'historique, sans message privé (risque inchangé).
    assert st["rang"] != GR.rank_of("F", 0) and GR.history(db.conn)[0]["grade"] == GR.name(st["rang"])
    assert not any("Grade JARVIS" in t for t in events(db))


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
                               "mode_survie": True}},
        "verifie_le": NOW.isoformat()}  # grade revérifié par le labo à l'instant : pas périmé


def full_data(stock: dict) -> dict:
    """lab_grade.json complet pour `!grade` (mesure d'une fenêtre notée A), vérifié à NOW."""
    m = GR.evaluate(rows([1.0 if i % 2 else -0.5 for i in range(300)], per_day=20, delay=1))
    return {"marches": {"stock": {"version": "v1", "mesure": m} | stock}, "verifie_le": NOW.isoformat()}


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
    assert g["risque_propose"] == 1.0
    assert "plafonné par le budget de 4 % ouvert au total (3 % déjà ouverts)" in g["raison"]
    # Arrêt de la journée à −3 R (date de Paris de la clôture).
    stopped = [trade(status="closed", events=[{"kind": "stop", "price": 95.0, "at": NOW.isoformat()}])
               for _ in range(3)]
    assert GR.day_r(stopped, NOW) == pytest.approx(3 * (-1.0 - 5.9 / 20), abs=0.01)  # net : 5,90 $ pour 20 $ de risque
    # Net des frais estimés (4 actions en 3 brackets IBKR), les frais avancent l'arrêt : −2,2 R bruts (deux stops et
    # une sortie à −0,2 R), mais −3,09 R nets.
    mixed = stopped[:2] + [trade(status="closed", events=[{"kind": "stop", "price": 99.0, "at": NOW.isoformat()}])]
    assert GR.day_r(mixed, NOW) == pytest.approx(-3.09, abs=0.01)
    assert "arrêt de la journée" in GR.risk_now("stock", mixed, NOW, DATA)["raison"]
    g = GR.risk_now("stock", stopped, NOW, DATA)
    assert g["risque_propose"] == 0.0 and "arrêt de la journée" in g["raison"]


def plan(entry=100.0, stop=95.0, market="stock"):
    return {"market": market, "entry": entry, "stop": stop, "sizing": {"risk_pct": 3.0}}


def test_plan_en_direct(monkeypatch):
    """Une seule règle de taille : min(capital × % du grade, 20 €), au change du jour, en actions entières."""
    monkeypatch.setattr(GR, "current", lambda: DATA)
    monkeypatch.setenv("JARVIS_CAPITAL", "10000")  # en euros
    p = plan(10.0, 9.5)
    T.apply_grade(p, None, None, NOW, trades=[], usd_per_eur=1.15)
    s = p["sizing"]
    assert s["risk_amount_eur"] == 20.0 and s["risk_amount"] == 23.0 and s["eurusd"] == 1.15
    assert s["quantity"] == 46 and isinstance(s["quantity"], int) and s["risk_pct"] == 0.2
    assert "sizing_regle" not in p
    assert T.sizing_lines(p) == ["💰 Risque 20 € (23,0 $) : 46 actions pour 460 $"]
    assert "🏅 Grade JARVIS **A** · risque proposé **3 %**" in GR.plan_line(p["grade"])
    # Petit capital : le % du grade décide (500 € × 3 % = 15 €).
    p = plan(10.0, 9.5)
    T.apply_grade(p, 500.0, None, NOW, trades=[], usd_per_eur=1.15)
    assert p["sizing"]["risk_amount_eur"] == 15.0 and p["sizing"]["quantity"] == 34  # 17,25 $ / 0,5
    # Capital inconnu : 20 € dès que le grade propose plus de 0 %.
    monkeypatch.delenv("JARVIS_CAPITAL")
    p = plan(10.0, 9.5)
    T.apply_grade(p, None, None, NOW, trades=[], usd_per_eur=1.15)
    assert p["sizing"]["risk_amount_eur"] == 20.0 and p["sizing"]["capital"] is None
    # Une action au-delà de 20 € : pas d'ordre, dit en clair.
    p = plan(100.0, 70.0)
    T.apply_grade(p, None, None, NOW, trades=[], usd_per_eur=1.15)
    assert p["sizing"]["quantity"] == 0
    assert "pas d'ordre, 1 action = 26,09 € de risque, au-delà de 20 €" in T.sizing_lines(p)[0]
    # Crypto : quantité fractionnaire, comme avant.
    data = DATA | {"marches": DATA["marches"] | {"crypto": DATA["marches"]["stock"]}}
    monkeypatch.setattr(GR, "current", lambda: data)
    p = plan(60000.0, 59000.0, "crypto")
    T.apply_grade(p, None, None, NOW, trades=[], usd_per_eur=1.15)
    assert p["sizing"]["quantity"] == 0.023 and "taille 0.023" in T.sizing_lines(p)[0]


def test_plan_grade_f_taille_a_la_regle_hors_avis(monkeypatch):
    """Grade à 0 % : JARVIS ne recommande rien ; la règle de 20 € de Simão est à part, marquée hors grade."""
    data = DATA | {"marches": {"stock": DATA["marches"]["crypto"]}}
    monkeypatch.setattr(GR, "current", lambda: data)
    monkeypatch.setenv("JARVIS_CAPITAL", "10000")
    p = plan(10.0, 9.5)
    T.apply_grade(p, None, None, NOW, trades=[], usd_per_eur=1.15)
    assert p["sizing"] is None and p["grade"]["risque_propose"] == 0.0
    rule = p["sizing_regle"]
    assert rule["hors_grade"] and rule["quantity"] == 46 and isinstance(rule["quantity"], int)
    assert T.sizing_lines(p) == ["💰 JARVIS ne propose pas de taille (grade F- : 0 %). Ta règle de 20 € : 46 actions "
                                 "pour 460 $ (exception, hors avis de JARVIS)"]
    stopped = p | {"grade": {"grade": "A", "risque_grade": 3.0, "risque_propose": 0.0,
                             "raison": "arrêt de la journée à −3 R atteint"}}
    assert T.sizing_lines(stopped)[0].startswith("💰 JARVIS ne propose pas de taille (0 % : arrêt de la journée")
    text = T.plan_text(p | {"symbol": "AAA", "side": "long", "stop_pct": -5.0, "stop_source": "creux",
                            "targets": [], "checklist": {"verdict": "Feu vert", "score": 3, "max_score": 5,
                                                         "items": []}, "method": "m"})
    assert "Ta règle de 20 €" in text and "plan en observation" in text
    # Risque demandé explicitement : gardé, signalé hors budget (JARVIS ne propose rien).
    p = plan(10.0, 9.5)
    T.apply_grade(p, None, 5.0, NOW, trades=[], usd_per_eur=1.15)
    assert p["sizing"]["risk_amount_eur"] == 20.0 and "sizing_regle" not in p
    assert p["grade"]["hors_budget"] and p["grade"]["risque_demande"] == 5.0
    assert "⚠️ **Hors budget** : risque demandé 5 % (20 €), alors que JARVIS ne propose aucun risque" in \
        GR.plan_line(p["grade"])


def test_risque_demande_hors_budget(monkeypatch):
    monkeypatch.setattr(GR, "current", lambda: DATA)
    monkeypatch.setenv("JARVIS_CAPITAL", "1000")
    # Grade A, 3 % de 1 000 € = 20 € : 5 % demandés donnent les mêmes 20 € (plafond), rien de plus.
    p = plan(10.0, 9.5)
    T.apply_grade(p, None, 5.0, NOW, trades=[], usd_per_eur=1.15)
    assert p["sizing"]["risk_amount_eur"] == 20.0 and not p["grade"].get("hors_budget")
    # Budget déjà pris (3 trades à 1 % ouverts) : JARVIS propose 1 % = 10 € ; 3 % demandés = 20 €, hors budget.
    opened = [trade(risk=1.0) for _ in range(3)]
    p = plan(10.0, 9.5)
    T.apply_grade(p, None, 3.0, NOW, trades=opened, usd_per_eur=1.15)
    assert p["grade"]["risque_propose"] == 1.0 and p["grade"]["hors_budget"]
    assert "risque demandé 3 % (20 €), au-delà des 10 € que propose JARVIS" in GR.plan_line(p["grade"])
    # Arrêt de la journée atteint : tout risque demandé est hors budget.
    stopped = [trade(status="closed", events=[{"kind": "stop", "price": 95.0, "at": NOW.isoformat()}])
               for _ in range(3)]
    p = plan(10.0, 9.5)
    T.apply_grade(p, None, 0.5, NOW, trades=stopped, usd_per_eur=1.15)
    assert p["grade"]["hors_budget"] and p["sizing"]["risk_amount_eur"] == 5.0
    # 0 % demandé : pas de taille du tout (ni règle à part).
    p = plan(10.0, 9.5)
    T.apply_grade(p, None, 0.0, NOW, trades=[], usd_per_eur=1.15)
    assert p["sizing"] is None and "sizing_regle" not in p and not p["grade"].get("hors_budget")


# --- Fail-closed et péremption (R1) ----------------------------------------------------------------

@pytest.mark.parametrize("data", [
    {"marches": {"stock": {"grade": "A"}}, "verifie_le": NOW.isoformat()},  # schéma différent : KeyError
    [1, 2],                                                                # JSON qui n'est pas un objet
])
def test_grade_illisible_fail_closed(monkeypatch, data):
    monkeypatch.setattr(GR, "current", lambda: data)
    monkeypatch.setenv("JARVIS_CAPITAL", "10000")
    p = plan()
    T.apply_grade(p, None, None, NOW, trades=[], usd_per_eur=1.15)
    assert p["sizing"] is None and p["grade"]["risque_propose"] == 0.0 and p["grade"]["observation"]
    assert p["grade"]["grade"] == "non noté" and "grade illisible" in p["grade"]["raison"]
    assert p["sizing_regle"]["hors_grade"]  # la règle de Simão reste affichée, à part


def test_grade_exception_fail_closed(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("schéma")

    monkeypatch.setattr(GR, "risk_now", boom)
    monkeypatch.setenv("JARVIS_CAPITAL", "10000")
    p = plan()
    T.apply_grade(p, None, None, NOW, trades=[])
    assert p["sizing"] is None and p["grade"]["risque_propose"] == 0.0
    assert "grade illisible (RuntimeError)" in GR.plan_line(p["grade"])
    p = plan()
    T.apply_grade(p, None, 1.0, NOW, trades=[])  # risque explicite : gardé, hors budget
    assert p["sizing"]["risk_amount_eur"] == 20.0 and p["grade"]["hors_budget"]


def test_trade_mal_forme_ne_fait_pas_sauter_le_budget(monkeypatch, caplog):
    monkeypatch.setattr(GR, "current", lambda: DATA)
    bad = {"status": "open", "sizing": {"risk_pct": 1}, "entry": 10, "side": "long"}  # sans stop
    assert GR.open_risk([bad, "pas un trade"]) == 1.0  # compté pour tout son risque, sans exception
    assert "mal formé" in caplog.text
    g = GR.risk_now("stock", [bad], NOW)
    assert g["risque_propose"] == 3.0 and "grade illisible" not in g["raison"]


def test_risque_zero_explicite(monkeypatch):
    assert T.sizing(10.0, 9.5, 1000.0, 0.0) is None and T.sizing(10.0, 9.5, 1000.0, -1.0) is None
    assert T.sizing(10.0, 9.5, 1000.0, 3.0)["risk_amount"] == 30.0


def test_grade_perime(monkeypatch):
    old = DATA | {"verifie_le": "2026-10-07T13:00:00+00:00"}  # 7 h avant NOW
    g = GR.risk_now("stock", [], NOW, old)
    assert g["risque_propose"] == 0.0 and g["observation"] and g["perime"]
    assert g["raison"] == "grade périmé (dernier passage du labo il y a 7 h)"
    g = GR.risk_now("stock", [], NOW, {k: v for k, v in DATA.items() if k != "verifie_le"})
    assert g["risque_propose"] == 0.0 and "grade périmé (aucun passage du labo daté)" == g["raison"]
    assert GR.risk_now("stock", [], NOW, DATA | {"verifie_le": "2026-10-07T15:00:00+00:00"})["risque_propose"] == 3.0
    monkeypatch.setattr(GR, "MAX_AGE_H", 4.0)
    assert GR.risk_now("stock", [], NOW, DATA | {"verifie_le": "2026-10-07T15:00:00+00:00"})["perime"]
    text = GR.text(full_data(old["marches"]["stock"]) | {"verifie_le": old["verifie_le"]}, now=NOW)
    assert "⚠️ **Grade périmé** : dernier passage du labo il y a 7 h" in text


def test_verifie_le_a_chaque_passage_reussi(db, monkeypatch):
    synthetic_trades(db, n_days=20, per_day=20, base=-0.3, noise=0.8)
    GR.step(db, NOW)
    first = json.loads(GR.live_path().read_text(encoding="utf-8"))
    assert first["verifie_le"] == NOW.isoformat()
    later = datetime(2026, 10, 7, 20, 10, tzinfo=timezone.utc)
    assert GR.step(db, later)["fichier"] is False  # rien de changé…
    second = json.loads(GR.live_path().read_text(encoding="utf-8"))
    assert second["verifie_le"] == later.isoformat() and second["ecrit_le"] == first["ecrit_le"]  # … mais vérifié
    assert second["marches"] == first["marches"]

    def boom(self, market, force=False):
        raise RuntimeError("base")

    db.set("grade_calc", "signature", None)  # force un recalcul, qui échoue : le fichier n'est pas revérifié
    monkeypatch.setattr(GR.Grader, "grade_market", boom)
    with pytest.raises(RuntimeError):
        GR.step(db, datetime(2026, 10, 8, 2, 0, tzinfo=timezone.utc))
    assert json.loads(GR.live_path().read_text(encoding="utf-8"))["verifie_le"] == later.isoformat()


def test_page_du_labo_fenetre_et_peremption():
    from app.lab import page as PG

    data = full_data({"grade": "S", "rang": GR.rank_of("S", 1), "non_note": False, "risque_grade": 5.0,
                      "risque_max": 5.0, "mode_survie": False})
    data["marches"]["stock"]["mesure"]["fenetre"] = GR.span(["2026-04-01", "2026-10-06"], {"2026-10-06"}) | {
        "discontinue": True}
    html = PG._grade_card(data)  # vérifié à NOW, il y a plus de 6 h
    assert "⚠️ Grade périmé (dernier passage du labo il y a" in html and "5 % permis, 4 % en pratique" in html
    assert "risque 5 % permis, 4 % en pratique" in html and "permis 5 % permis" not in html
    assert "fenêtre : rejeu du 01/04/2026 au 01/04/2026, direct depuis le 06/10/2026" in html
    fresh = data | {"verifie_le": datetime.now(timezone.utc).isoformat()}
    assert "périmé" not in PG._grade_card(fresh)


# --- Budget de risque ouvert (R4) -------------------------------------------------------------------

def test_plafond_s_et_budget_total():
    data = {"marches": {"stock": DATA["marches"]["stock"] | {"grade": "S", "risque_grade": 5.0, "risque_max": 5.0}},
            "verifie_le": NOW.isoformat()}
    g = GR.risk_now("stock", [], NOW, data)
    assert g["risque_propose"] == 4.0 and "budget de 4 % ouvert au total" in g["raison"] and "déjà" not in g["raison"]
    assert GR.practical(5.0) == "5 % permis, 4 % en pratique (budget total)" and GR.practical(3.0) == "3 %"
    text = GR.text(full_data(data["marches"]["stock"]), now=NOW)
    assert "risque 5 % permis, 4 % en pratique (budget total) (plafond 5 %)" in text and "périmé" not in text
    assert "permis 5 % permis" not in text  # relecture d'intégration : « permis » n'est plus doublé
    assert GR.allowed(3.0) == "risque permis 3 %"
    assert "espérance +0.250 R par trade après commission IBKR à 20 €" in text


def test_risque_ouvert_en_euros(monkeypatch):
    monkeypatch.setenv("JARVIS_CAPITAL", "1000")
    t = trade() | {"sizing": {"risk_pct": None, "risk_amount_eur": 20.0, "capital": None}}
    assert GR.open_risk([t]) == 2.0  # 20 € sur 1 000 €
    t = trade() | {"sizing": {"risk_pct": 0.5, "risk_amount_eur": 20.0, "capital": 4000.0}}
    assert GR.open_risk([t]) == 0.5  # le capital du moment où le trade a été pris
    taken_f = trade() | {"sizing": None, "sizing_regle": {"risk_amount_eur": 20.0, "capital": None}}
    assert GR.open_risk([taken_f]) == 2.0  # pris sous grade F : compte quand même
    monkeypatch.delenv("JARVIS_CAPITAL")
    assert GR.open_risk([t | {"sizing": {"risk_amount_eur": 20.0}}]) == 1.0  # ni capital ni % : 1 % (plafond de C)
    assert GR.open_risk([trade() | {"sizing": None}]) == 1.0  # jamais 0 pour un trade réellement pris


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


def test_api_risque_explicite(monkeypatch):
    seen = []

    async def fake_build(*args, **kwargs):
        seen.append(kwargs)
        raise T.PlanError("plan de test")

    monkeypatch.setattr(main.trading, "build_plan", fake_build)
    client = TestClient(main.app)
    body = {"symbol": "AAA", "market": "stock", "capital": 10000}
    for bad in (-1, 101):
        assert client.post("/api/trading/plan", json=body | {"risk_pct": bad}).status_code == 422
    assert not seen
    r = client.post("/api/trading/plan", json=body | {"risk_pct": 0})
    assert r.status_code == 422 and r.json()["detail"] == "plan de test"
    assert seen[0]["risk_pct"] == 0.0 and seen[0]["capital"] == 10000.0  # 0 transmis tel quel (plus de « or 3 % »)
