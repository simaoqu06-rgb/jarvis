"""Carnet des leçons du labo : format, file, mesure chaque nuit (confirmée / démentie), carnet court,
ajustement de la note des plans en direct, routes de la routine du soir et commande !lecons."""
import json
import logging
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import main
from app.lab import brief as B
from app.lab import lessons as L
from app.lab import live as LIVE
from app.lab.db import LabDB
from app.notify import bot
from app.trading import service as T
from app.trading.settings import Settings
from tests.lab_fixtures import synthetic_trades
from tests.test_lab3_live import NOW, daily_bars, fake_plan

KEY = "cle-de-test-lecons"
H = {"X-Lab-Key": KEY}
PIVOT = {"field": "context.setup", "op": "==", "value": "cassure_pivot"}
SIGNAL = {"field": "context.signal", "op": ">", "value": 0.5}


def lesson(texte="Cassure de pivot : meilleure espérance", quand=PIVOT, sens="favorable", **extra) -> dict:
    return {"texte": texte, "quand": quand, "sens": sens} | extra


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "lab.db"
    monkeypatch.setenv("JARVIS_LAB_DB", str(path))
    monkeypatch.delenv("JARVIS_LAB_LESSONS", raising=False)
    monkeypatch.delenv("JARVIS_LAB_CHAMPION", raising=False)
    monkeypatch.setattr(L, "_cache", {"key": None, "data": []})
    monkeypatch.setattr(L, "_warned", set())
    monkeypatch.setattr(L, "_index", {"day": None, "bars": None})
    monkeypatch.setattr(LIVE, "_cache", {"key": None, "data": None})
    return LabDB(str(path))


def pivot_effect(ctx, rnd):
    return 0.4 if ctx["setup"] == "cassure_pivot" else 0.0


def add(db, raw) -> dict:
    return L.add(db, L.parse(raw))


# --- Format -----------------------------------------------------------------------------------

def test_lecon_canonique():
    c = L.parse(lesson(sens="défavorable", markets=["stock"], pourquoi="  n = 480, +0,4 R  "))
    assert c == {"texte": "Cassure de pivot : meilleure espérance", "cond": PIVOT, "markets": ["stock"],
                 "sens": "defavorable", "pourquoi": "n = 480, +0,4 R"}
    # Le texte et le pourquoi ne changent pas l'empreinte ; le sens, si.
    assert L.lesson_hash(c) == L.lesson_hash(L.parse(lesson(texte="Autre formulation", sens="defavorable",
                                                            markets=["stock"])))
    assert L.lesson_hash(c) != L.lesson_hash(L.parse(lesson(markets=["stock"])))


@pytest.mark.parametrize("raw, words", [
    (lesson(quand={"field": "r_net", "op": ">", "value": 0}), "regard vers l'avenir"),
    (lesson(quand={"field": "context.dollar_volume", "op": ">", "value": 1e6}), "liquidité"),
    (lesson(quand={"field": "context.rvol", "op": ">", "value": "auto"}), "auto"),
    (lesson(sens="peut-être"), "favorable ou defavorable"),
    (lesson(texte="court"), "texte"),
    (lesson(regle="x"), "clé inconnue"),
    ({"texte": "Une leçon sans condition du tout"}, "quand"),
    (lesson(markets=["forex"]), "markets"),
])
def test_lecons_refusees(raw, words):
    with pytest.raises(L.LessonError, match=words):
        L.parse(raw)


# --- File et carnet ----------------------------------------------------------------------------

def test_file_puis_base_sans_doublon_et_retrait(db):
    out = L.enqueue(db.path, lesson(pourquoi="vu sur 480 trades"), "2026-10-07")
    assert out["etat"] == "en_file"
    L.enqueue(db.path, lesson(texte="La même, reformulée"), "2026-10-07")  # même empreinte : la première compte
    assert L.ingest(db) == 1 and L.ingest(db) == 0
    (x,) = L.list_lessons(db.conn)
    assert x["etat"] == "a_confirmer" and x["soiree"] == "2026-10-07" and x["pourquoi"] == "vu sur 480 trades"
    L.enqueue(db.path, {"retirer": x["id"], "raison": "doublon de la 7"})
    assert L.ingest(db) == 1
    x = L.get(db.conn, x["id"])
    assert x["etat"] == "retiree" and x["raison_retrait"] == "doublon de la 7"
    with pytest.raises(L.LessonError):
        L.enqueue(db.path, {"retirer": "7"})


def test_fichier_illisible_ecarte(db, caplog):
    q = L.queue_dir(db.path)
    q.mkdir(parents=True)
    (q / "abc.json").write_text("{pas du json")
    assert L.ingest(db) == 0 and not list(q.glob("*.json"))
    assert "illisible" in caplog.text


def test_carnet_court_la_plus_faible_laisse_sa_place(db, monkeypatch):
    monkeypatch.setattr(L, "MAX_ACTIVE", 2)
    a = add(db, lesson(texte="Leçon A sur le signal", quand={"field": "context.signal", "op": ">", "value": 0.1}))
    b = add(db, lesson(texte="Leçon B sur le signal", quand={"field": "context.signal", "op": ">", "value": 0.2}))
    db.conn.execute("UPDATE lessons SET metrics = ? WHERE id = ?", (json.dumps({"n": 80}), a["id"]))
    db.conn.execute("UPDATE lessons SET metrics = ? WHERE id = ?", (json.dumps({"n": 10}), b["id"]))
    c = add(db, lesson(texte="Leçon C sur le signal", quand={"field": "context.signal", "op": ">", "value": 0.3}))
    assert c and L.get(db.conn, b["id"])["etat"] == "retiree" and L.get(db.conn, a["id"])["etat"] == "a_confirmer"
    # Carnet plein de confirmées : la nouvelle est écartée.
    db.conn.execute("UPDATE lessons SET status = 'confirmee' WHERE status = 'a_confirmer'")
    assert add(db, lesson(texte="Leçon D sur le signal", quand={"field": "context.signal", "op": ">", "value": 0.4})) is None
    assert L.counts(db.conn)["place"] == 0


# --- Mesure ------------------------------------------------------------------------------------

def test_effet_reel_confirme_et_sens_contraire_dementi(db):
    good = add(db, lesson())
    bad = add(db, lesson(texte="Cassure de pivot : moins bonne que le reste", sens="defavorable"))
    synthetic_trades(db, n_days=120, per_day=20, effect=pivot_effect)  # trades arrivés après les leçons
    assert L.measure_all(db) == 2
    good, bad = L.get(db.conn, good["id"]), L.get(db.conn, bad["id"])
    m = good["mesures"]
    assert good["etat"] == "confirmee" and bad["etat"] == "dementie"
    assert m["n"] > 300 and m["ecart_r"] > 0.2 and m["z"] > L.CONFIRM_Z and all(h > 0 for h in m["moities"])
    assert m["tp1"] > m["tp1_ensemble"] and "trades depuis la leçon :" in L.proof(good)


def test_trades_d_avant_la_lecon_ne_la_confirment_pas(db):
    synthetic_trades(db, n_days=120, per_day=20, effect=pivot_effect)  # ceux qui ont inspiré la leçon
    seen = add(db, lesson())
    L.measure_all(db)
    seen = L.get(db.conn, seen["id"])
    assert seen["etat"] == "a_confirmer" and seen["mesures"]["n"] == 0
    assert seen["apres_trade"] == db.conn.execute("SELECT MAX(id) FROM trades").fetchone()[0]


def _reecrit(db, where: str = "1") -> None:
    """Supprime puis réinsère des trades : mêmes trades, id neufs (backfill, recalcul, ancien direct crypto)."""
    cols = [r[1] for r in db.conn.execute("PRAGMA table_info(trades)") if r[1] != "id"]
    rows = db.conn.execute(f"SELECT {', '.join(cols)} FROM trades WHERE {where}").fetchall()
    db.conn.execute(f"DELETE FROM trades WHERE {where}")
    db.conn.executemany(f"INSERT INTO trades ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", rows)


def test_jours_vus_reecrits_apres_la_lecon_ne_la_confirment_pas(db):
    synthetic_trades(db, n_days=120, per_day=20, effect=pivot_effect)  # rejeu vu par la leçon
    synthetic_trades(db, n_days=10, per_day=10, effect=pivot_effect, market="crypto", origin="live",
                     first=date(2026, 9, 1))  # direct vu, dont le jour en cours
    seen = add(db, lesson())
    assert seen["vu_jusqu"] == {"replay:stock": "2024-06-17", "live:crypto": "2026-09-10"}
    _reecrit(db)  # tout reçoit un id plus grand que la base de la leçon
    synthetic_trades(db, n_days=120, per_day=20, effect=pivot_effect, first=date(2024, 6, 18), seed=2)  # rejeu suivant
    L.measure_all(db)
    m = L.get(db.conn, seen["id"])["mesures"]
    assert m["n_total"] == db.conn.execute("SELECT COUNT(*) FROM trades WHERE origin = 'replay' AND day >= '2024-06-18' "
                                           "AND triggered = 1").fetchone()[0]
    assert m["vu_jusqu"] == seen["vu_jusqu"]


def test_lecon_d_avant_la_frontiere_exclut_le_direct_reecrit(db):
    synthetic_trades(db, n_days=10, per_day=10, effect=pivot_effect, market="crypto", origin="live",
                     first=date(2026, 9, 1))
    old = add(db, lesson())
    db.conn.execute("UPDATE lessons SET seen_until = NULL WHERE id = ?", (old["id"],))  # leçon d'avant la colonne
    _reecrit(db)  # ancien direct crypto : jour réécrit, id neufs au-dessus de base_trade_id
    synthetic_trades(db, n_days=120, per_day=20, effect=pivot_effect)  # rejeu jamais vu
    L.measure_all(db)
    m = L.get(db.conn, old["id"])["mesures"]
    assert m["n_total"] == db.conn.execute("SELECT COUNT(*) FROM trades WHERE origin = 'replay' "
                                           "AND triggered = 1").fetchone()[0]
    assert m["vu_jusqu"]["live:crypto"] >= "2026-09-10"


def test_sans_effet_dementie_apres_assez_de_trades_et_petit_echantillon_a_confirmer(db):
    null = add(db, lesson(texte="Signal fort : meilleure espérance", quand=SIGNAL))
    rare = add(db, lesson(texte="Un seul symbole : meilleure espérance", quand={"field": "symbol", "op": "==", "value": "S001"},
                          markets=["stock"]))
    rare_day = add(db, lesson(texte="Signal extrême : meilleure espérance",
                              quand={"field": "context.signal", "op": ">", "value": 0.995}))
    synthetic_trades(db, n_days=120, per_day=20, effect=pivot_effect)
    L.measure_all(db)
    assert L.get(db.conn, null["id"])["etat"] == "dementie"
    assert L.get(db.conn, rare_day["id"])["etat"] == "a_confirmer"
    assert L.get(db.conn, rare["id"])["mesures"]["n"] > 0


def test_champ_absent_invalide(db):
    synthetic_trades(db, n_days=30, per_day=5)
    x = add(db, lesson(texte="Secteur tech : meilleure espérance", quand={"field": "context.sector", "op": "==", "value": "tech"}))
    L.measure_all(db)
    x = L.get(db.conn, x["id"])
    assert x["etat"] == "invalide" and "absent" in L.proof(x)


def test_passage_du_worker_et_fichier_du_direct(db):
    synthetic_trades(db, n_days=1, per_day=1, first=date(2023, 6, 1))
    L.enqueue(db.path, lesson(markets=["stock"]))
    now = datetime(2026, 10, 7, 22, tzinfo=timezone.utc)
    out = L.step(db, now, 3600)
    assert out["file"] == 1 and out["nouvelles"] == 1
    synthetic_trades(db, n_days=120, per_day=20, effect=pivot_effect)  # les trades des nuits suivantes
    out = L.step(db, datetime(2026, 10, 8, 22, tzinfo=timezone.utc), 3600)
    assert out["mesurees"] == 1 and out["fichier"] is True
    data = json.loads(L.live_path(db.path).read_text())
    assert [x["texte"] for x in data["lecons"]] == ["Cassure de pivot : meilleure espérance"]
    assert L.step(db, datetime(2026, 10, 8, 22, tzinfo=timezone.utc), 3600)["fichier"] is False  # pas réécrit


# --- Direct ------------------------------------------------------------------------------------

def write_live(tmp_path, monkeypatch, items):
    path = tmp_path / "lab_lessons.json"
    path.write_text(json.dumps({"lecons": items}), encoding="utf-8")
    monkeypatch.setenv("JARVIS_LAB_LESSONS", str(path))
    return path


def live_item(i=7, sens="favorable", cond=PIVOT, markets=None):
    return {"id": i, "texte": f"Leçon {i}", "sens": sens, "cond": cond, "markets": markets}


async def test_lecon_confirmee_ajuste_la_note_du_plan(db, tmp_path, monkeypatch):
    write_live(tmp_path, monkeypatch, [live_item(7), live_item(8, "defavorable", SIGNAL),
                                       live_item(9, markets=["crypto"])])
    p = fake_plan("AAA")
    before = p["score"]["composite"]
    adj = await L.apply(p, daily_bars(80, date(2026, 10, 6)), None, NOW, Settings(None))
    assert adj == {"ajustement": 0.5, "lignes": ["+0.5 #7 Leçon 7"]}  # 8 : champ inconnu ; 9 : autre marché
    assert p["score"]["composite"] == min(10.0, round(before + 0.5, 1)) and p["score"]["lecons"] == adj
    assert "Leçons du labo (+0.5 sur la note) : +0.5 #7 Leçon 7" in "\n".join(T.score_lines(p))


async def test_ajustement_plafonne_et_verdict_recalcule(db, tmp_path, monkeypatch):
    rvol = {"field": "context.rvol", "op": ">", "value": 2}
    write_live(tmp_path, monkeypatch, [live_item(i, "defavorable", c) for i, c in ((1, PIVOT), (2, rvol), (3, PIVOT | {"op": "!="}))]
               + [live_item(4, "defavorable", {"field": "market", "op": "==", "value": "stock"})])
    s = Settings(None)
    p = fake_plan("AAA")
    before = p["score"]["composite"]
    adj = await L.apply(p, daily_bars(80, date(2026, 10, 6)), None, NOW, s)
    assert adj["ajustement"] == -L.ADJ_MAX and len(adj["lignes"]) == 3
    assert p["score"]["composite"] == round(before - 1, 1)
    assert p["score"]["go"] == (p["score"]["composite"] >= s["go_min"] and not p["score"]["reasons"])


async def test_sans_lecon_ou_en_erreur_plan_inchange(db, tmp_path, monkeypatch, caplog):
    p = fake_plan("AAA")
    score = json.dumps(p["score"])
    assert await L.apply(p, None, None, NOW) is None  # pas de fichier
    write_live(tmp_path, monkeypatch, [live_item()])
    monkeypatch.setattr(L, "match", lambda lessons, row: 1 / 0)
    caplog.set_level(logging.WARNING)
    assert await L.apply(p, None, None, NOW) is None and json.dumps(p["score"]) == score
    assert "non appliquées" in caplog.text


async def test_regime_indisponible_lecon_sur_le_regime_ignoree(db, tmp_path, monkeypatch):
    write_live(tmp_path, monkeypatch, [live_item(1, cond={"not": {"field": "regime_trend", "op": "==", "value": "haussier"}})])

    async def no_index(now):
        return None
    from app.trading import scanner
    monkeypatch.setattr(scanner, "_index_daily", no_index)
    assert await L.apply(fake_plan("AAA"), None, None, NOW) is None


async def test_plan_crypto(db, tmp_path, monkeypatch):
    write_live(tmp_path, monkeypatch, [live_item(5, cond=PIVOT, markets=["crypto"])])
    p = fake_plan("BTC") | {"market": "crypto"}
    adj = await L.apply(p, None, None, NOW)
    assert adj["ajustement"] == 0.5


# --- Routes, brief, Discord --------------------------------------------------------------------

def test_routes_de_la_routine_et_brief(db, monkeypatch):
    monkeypatch.setenv("JARVIS_LAB_API_KEY", KEY)
    client = TestClient(main.app)
    assert client.post("/api/lab/lessons", json=lesson()).status_code == 401
    r = client.post("/api/lab/lessons", headers=H, json=lesson())
    assert r.status_code == 200 and r.json()["etat"] == "en_file"
    assert client.post("/api/lab/lessons", headers=H, json=lesson(quand={"field": "tp1_hit", "op": "==", "value": 1})).status_code == 422
    assert L.ingest(db) == 1
    got = client.get("/api/lab/lessons", headers=H).json()
    assert got["compteurs"]["a_confirmer"] == 1 and got["lecons"][0]["texte"] == lesson()["texte"]
    assert got["lecons"][0]["soiree"] == B.soiree(datetime.now(timezone.utc))
    brief = client.get("/api/lab/brief", headers=H).json()
    assert brief["lecons"]["actives"][0]["etat"] == "a_confirmer" and brief["lecons"]["compteurs"]["place"] == L.MAX_ACTIVE - 1


async def test_commande_lecons(db):
    add(db, lesson())
    add(db, lesson(texte="Cassure de pivot : moins bonne", sens="defavorable"))
    synthetic_trades(db, n_days=120, per_day=20, effect=pivot_effect)
    L.measure_all(db)
    text = await bot.handle("!lecons")
    assert "1 confirmées" in text and "1 démenties" in text and "✅ 👍 **#1**" in text and "❌ 👎 **#2**" in text


async def test_commande_lecons_sans_base(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_LAB_DB", str(tmp_path / "absente.db"))
    assert "absente" in await bot.handle("!leçons")
