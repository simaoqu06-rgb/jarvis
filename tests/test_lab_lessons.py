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
    assert m["tp1"] > m["tp1_ensemble"] and "trades jamais vus par la leçon (120 j de rejeu, 0 j du direct) :" in L.proof(good)


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


async def test_lecon_confirmee_affichee_sans_toucher_la_note_du_plan(db, tmp_path, monkeypatch):
    write_live(tmp_path, monkeypatch, [live_item(7), live_item(8, "defavorable", SIGNAL),
                                       live_item(9, markets=["crypto"])])
    p = fake_plan("AAA")
    before = dict(p["score"])
    adj = await L.apply(p, daily_bars(80, date(2026, 10, 6)), None, NOW, Settings(None))
    assert adj == {"ajustement": 0.5, "lignes": ["+0.5 #7 Leçon 7"]}  # 8 : champ inconnu ; 9 : autre marché
    assert p["score"]["composite"] == before["composite"] and p["score"]["decision"] == before["decision"]
    assert p["score"]["composite_lecons"] == min(10.0, round(before["composite"] + 0.5, 1)) and p["score"]["lecons"] == adj
    with_lessons = f"{p['score']['composite_lecons']:g}"
    assert (f"Leçons du labo (affichées, sans effet sur le verdict) : +0.5 #7 Leçon 7 · note avec leçons {with_lessons}"
            in "\n".join(T.score_lines(p)))


async def test_ajustement_plafonne_et_verdict_sur_la_note_brute(db, tmp_path, monkeypatch):
    rvol = {"field": "context.rvol", "op": ">", "value": 2}
    write_live(tmp_path, monkeypatch, [live_item(i, "defavorable", c) for i, c in ((1, PIVOT), (2, rvol), (3, PIVOT | {"op": "!="}))]
               + [live_item(4, "defavorable", {"field": "market", "op": "==", "value": "stock"})])
    s = Settings(None)
    p = fake_plan("AAA")
    before = dict(p["score"])
    adj = await L.apply(p, daily_bars(80, date(2026, 10, 6)), None, NOW, s)
    assert adj["ajustement"] == -L.ADJ_MAX and len(adj["lignes"]) == 3
    assert p["score"]["composite"] == before["composite"]
    assert p["score"]["composite_lecons"] == round(before["composite"] - 1, 1)
    assert p["score"]["go"] == before["go"] and p["score"]["reasons"] == before["reasons"]


def test_plan_a_6_5_avec_lecon_favorable_reste_no_go():
    """Les leçons ne décident rien : vetos, tolérance du R/R et verdict restent sur la note brute."""
    from app.trading import score as SC
    from tests.test_score import cat, daily, plan

    s = Settings(None)
    p = plan(ok=3, tp1_r=0.5, expectancy=-0.1)
    raw = SC.evaluate(p, daily(), cat(positive=3), s)
    assert raw["composite"] == 6.5 and raw["decision"] == "NO GO"
    adj = {"ajustement": L.ADJ_MAX, "lignes": ["+0.5 #1 a", "+0.5 #2 b"]}
    out = SC.evaluate(p, daily(), cat(positive=3), s, lessons=adj)
    assert out["decision"] == "NO GO" and out["composite"] == 6.5 and out["composite_lecons"] == 7.5
    assert out["reasons"] == raw["reasons"] and out["lecons"] == adj
    # R/R juste sous le minimum : la tolérance (note ≥ go_min + 0,5) se juge sans les leçons.
    q = plan(rr2=1.9, ok=3)
    assert SC.evaluate(q, daily(), cat(), s)["composite"] == 7.4
    out = SC.evaluate(q, daily(), cat(), s, lessons=adj)
    assert out["decision"] == "NO GO" and out["reasons"] == ["R/R 1.9 sous le minimum de 2"]


# --- Jugement par paliers ----------------------------------------------------------------------

def mesure(z, ecart=0.2, n=60, jours=30, moities=(0.1, 0.1)) -> dict:
    return {"n": n, "jours": jours, "z": z, "ecart_r": ecart, "moities": list(moities), "palier": L.palier(n)}


def test_paliers_et_seuil_qui_depense_l_alpha_de_chaque_lecon():
    assert [L.palier(n) for n in (0, 29, 30, 59, 60, 119, 120, 239, 240, 480)] == [None, None, 0, 0, 1, 1, 2, 2, 3, 4]
    # Leçon n° 1, premier palier : le plancher CONFIRM_Z ; ensuite le seuil monte avec le palier et l'id.
    assert L.confirm_z(1, 0) == L.CONFIRM_Z
    zs = [L.confirm_z(1, p) for p in range(6)]
    assert zs == sorted(zs) and zs[-1] > 3.0
    assert L.confirm_z(20, 0) == pytest.approx(3.91, abs=0.01)
    # En loi de Student à jours − 1 degrés de liberté : plus strict sur peu de jours, la loi normale sur beaucoup.
    assert L.confirm_z(1, 2, 20) > L.confirm_z(1, 2, 30) > L.confirm_z(1, 2) == pytest.approx(2.87, abs=0.01)
    assert L.confirm_z(1, 2, 100_000) == pytest.approx(L.confirm_z(1, 2), abs=1e-3)
    assert L.judge(mesure(3.0), "favorable", essai=1) == "confirmee"  # seuil 2,79 (palier 1, 30 jours)
    assert L.judge(mesure(3.0), "favorable", essai=20) == "a_confirmer"  # seuil 5,03 pour la 20e leçon
    # Jamais deux jugements au même palier, ni sur moins de MIN_DAYS jours.
    assert L.judge(mesure(3.0), "favorable", 1, "a_confirmer", palier_juge=1) == "a_confirmer"
    assert L.judge(mesure(3.0, n=120), "favorable", 1, "a_confirmer", palier_juge=1) == "a_confirmer"  # seuil 3,12
    assert L.judge(mesure(3.2, n=120), "favorable", 1, "a_confirmer", palier_juge=1) == "confirmee"
    assert L.judge(mesure(3.0, jours=L.MIN_DAYS - 1), "favorable", 1) == "a_confirmer"
    assert L.judge(mesure(-1.5), "favorable", 1, "a_confirmer", palier_juge=1) == "a_confirmer"
    assert L.judge(mesure(-1.5), "favorable", 1) == "dementie"


def test_confirmee_par_hysteresis_et_dementie_definitive():
    assert L.judge(mesure(0.4, n=70), "favorable", 1, "confirmee", palier_juge=1) == "confirmee"
    assert L.judge(mesure(-1.0, n=70), "favorable", 1, "confirmee", palier_juge=1) == "dementie"
    assert L.judge(mesure(1.2, n=70), "defavorable", 1, "confirmee", palier_juge=1) == "dementie"
    assert L.judge(mesure(6.0, n=5000), "favorable", 1, "dementie", palier_juge=1) == "dementie"


def _lecon_nulle_confirmee(rnd, essai=1, days=250, per_day=30, share=0.2) -> bool:
    """Leçon sans effet (trades en bracket : −1 R, +0,8, +2, +3), regardée chaque jour comme measure_all."""
    sums, judged, etat, n_hit = {}, None, "a_confirmer", 0
    for d in range(days):
        v = [0.0, 0, 0.0, 0]
        for _ in range(per_day):
            u = rnd.random()
            r = -1.0 if u < 0.55 else 0.8 if u < 0.80 else 2.0 if u < 0.93 else 3.0
            v[0] += r
            v[1] += 1
            if rnd.random() < share:
                v[2] += r
                v[3] += 1
        sums[f"{d:04d}"] = v
        n_hit += v[3]
        level = L.palier(n_hit)
        if level is None or (judged is not None and level <= judged) or d + 1 < L.MIN_DAYS:
            continue  # entre deux paliers, judge ne change rien à une leçon à confirmer
        m = L.summary(sums)
        etat = L.judge(m, "favorable", essai, etat, judged)
        if L.due(m, judged):
            judged = m["palier"]
        if etat != "a_confirmer":
            return etat == "confirmee"
    return False


def test_lecon_nulle_rarement_confirmee_malgre_les_regards_repetes():
    """Vérification du 10 octobre 2026 : 30 trades par jour, 20 % concernés, un regard par jour pendant
    250 jours. Avant les paliers, une leçon nulle était confirmée au moins une fois dans ~10 % des cas."""
    import random
    rnd = random.Random(2026)
    n = 1500
    assert sum(_lecon_nulle_confirmee(rnd) for _ in range(n)) / n <= 0.01


def test_effet_reel_toujours_trouve_par_paliers():
    import random
    rnd = random.Random(7)
    sums, judged, etat = {}, None, "a_confirmer"
    for d in range(120):
        hit = [rnd.gauss(0.25, 1.0) for _ in range(6)]
        rest = [rnd.gauss(0.0, 1.0) for _ in range(24)]
        sums[f"{d:04d}"] = [sum(hit) + sum(rest), 30, sum(hit), 6]
        m = L.summary(sums)
        etat = L.judge(m, "favorable", 3, etat, judged)
        if L.due(m, judged):
            judged = m["palier"]
    assert etat == "confirmee" and judged == 4


def test_dementie_reste_dementie_mais_mesuree(db):
    bad = add(db, lesson(texte="Cassure de pivot : moins bonne que le reste", sens="defavorable"))
    synthetic_trades(db, n_days=60, per_day=20, effect=pivot_effect, first=date(2024, 1, 2))
    L.measure_all(db)
    first = L.get(db.conn, bad["id"])
    assert first["etat"] == "dementie"
    synthetic_trades(db, n_days=120, per_day=20, effect=lambda c, r: -0.6 if c["setup"] == "cassure_pivot" else 0.0,
                     first=date(2024, 4, 1), seed=5)  # la suite lui donnerait raison
    L.measure_all(db)
    again = L.get(db.conn, bad["id"])
    assert again["etat"] == "dementie" and again["mesures"]["n"] > first["mesures"]["n"]


def test_ancienne_confirmee_rejugee_par_paliers(db):
    x = add(db, lesson(texte="Signal fort : meilleure espérance", quand=SIGNAL))
    db.conn.execute("UPDATE lessons SET status = 'confirmee', metrics = ? WHERE id = ?",
                    (json.dumps({"n": 40, "z": 2.6}), x["id"]))  # confirmée avant les paliers, sans effet réel
    synthetic_trades(db, n_days=40, per_day=20, seed=8)
    L.measure_all(db)
    x = L.get(db.conn, x["id"])
    assert x["etat"] != "confirmee" and x["mesures"]["palier_juge"] is not None


def test_invalide_attend_une_place_au_carnet(db, monkeypatch):
    a = add(db, lesson(texte="Leçon A sur le signal", quand=SIGNAL))
    b = add(db, lesson(texte="Leçon B sur le pivot", quand=PIVOT))
    monkeypatch.setattr(L, "MAX_ACTIVE", 1)
    db.conn.execute("UPDATE lessons SET status = 'invalide' WHERE id = ?", (b["id"],))
    db.conn.execute("UPDATE lessons SET status = 'confirmee', metrics = ? WHERE id = ?",
                    (json.dumps({"palier_juge": 0}), a["id"]))
    synthetic_trades(db, n_days=10, per_day=10, effect=lambda c, r: 0.8 if c["signal"] > 0.5 else 0.0)
    L.measure_all(db)
    assert L.get(db.conn, a["id"])["etat"] == "confirmee"
    assert L.get(db.conn, b["id"])["etat"] == "invalide" and L.counts(db.conn)["place"] == 0
    db.conn.execute("UPDATE lessons SET status = 'retiree' WHERE id = ?", (a["id"],))
    L.measure_all(db)
    assert L.get(db.conn, b["id"])["etat"] == "a_confirmer"


def test_lecon_favorable_qui_perd_encore_est_relative(db, tmp_path, monkeypatch):
    x = add(db, lesson())
    db.conn.execute("UPDATE lessons SET status = 'confirmee', metrics = ? WHERE id = ?",
                    (json.dumps({"n": 240, "esperance_r": -0.15, "esperance_ensemble_r": -0.3, "palier_juge": 3}), x["id"]))
    x = L.get(db.conn, x["id"])
    assert L.proof(x).endswith("relative : moins mauvais que la moyenne, perd encore 0,15 R")
    assert "relative" in L.text(db.conn)
    assert L.relative("favorable", 0.1) is None and L.relative("defavorable", -0.2) is None
    L.write_live(db)
    assert json.loads(L.live_path(db.path).read_text())["lecons"][0]["esperance_r"] == -0.15
    adj = L.adjustment([live_item(x["id"]) | {"esperance_r": -0.15}])
    assert adj["lignes"] == [f"+0.5 #{x['id']} Leçon {x['id']} (relative : moins mauvais que la moyenne, perd encore 0,15 R)"]


def test_jours_de_rejeu_et_du_direct_et_etiquette_historique(db, tmp_path, monkeypatch):
    """Avis de GPT du 10 octobre : « jamais vu par la leçon » n'est pas « prospectif ». La mesure compte ses jours
    de rejeu et du direct ; sous MIN_DAYS jours du direct, une confirmée est « (historique) » dans !lecons et dans
    le plan du direct (fichier lab_lessons.json)."""
    x = add(db, lesson())
    synthetic_trades(db, n_days=100, per_day=20, effect=pivot_effect)  # rejeu jamais vu par la leçon
    synthetic_trades(db, n_days=5, per_day=20, effect=pivot_effect, first=date(2026, 10, 5), origin="live", seed=3)
    L.measure_all(db)
    x = L.get(db.conn, x["id"])
    m = x["mesures"]
    assert x["etat"] == "confirmee" and m["jours_rejeu"] == 100 and m["jours_direct"] == 5 and m["jours"] == 105
    assert "trades jamais vus par la leçon (100 j de rejeu, 5 j du direct) :" in L.proof(x)
    assert L.line(x).startswith("✅ (historique) 👍") and "(historique) : confirmée sur le rejeu" in L.text(db.conn)
    L.write_live(db)
    (item,) = json.loads(L.live_path(db.path).read_text())["lecons"]
    assert item["jours_direct"] == 5
    write_live(tmp_path, monkeypatch, [item])
    assert L.current()[0]["jours_direct"] == 5
    assert L.adjustment(L.current())["lignes"] == [f"+0.5 #{x['id']} {x['texte']} (historique)"]
    # Assez de jours du direct : plus d'étiquette.
    synthetic_trades(db, n_days=L.MIN_DAYS, per_day=20, effect=pivot_effect, first=date(2026, 11, 2), origin="live",
                     seed=4)
    L.measure_all(db)
    x = L.get(db.conn, x["id"])
    assert x["mesures"]["jours_direct"] == L.MIN_DAYS + 5 and L.line(x).startswith("✅ 👍")
    assert L.adjustment([live_item(x["id"]) | {"jours_direct": L.MIN_DAYS}])["lignes"] == [f"+0.5 #{x['id']} Leçon {x['id']}"]


def test_lecon_actions_favorable_au_labo_qui_perd_a_20_euros_est_relative(db, monkeypatch):
    """Scénario 3 de GPT : une leçon d'actions à +0,05 R du labo qui perd à 20 € (commission IBKR) n'est pas un
    gain pour le compte de Simão : la plus basse des deux espérances décide, le texte dit « à 20 € »."""
    from app.lab import method as M

    calls = []

    def at_20(market, result, r_net):
        calls.append(market)
        return r_net - 0.3  # commission IBKR à 20 € de risque
    monkeypatch.setattr(M, "compared_r", at_20)
    a = add(db, lesson(markets=["stock"]))
    b = add(db, lesson(texte="Cassure de pivot : même chose, deuxième leçon", quand={"or": [PIVOT, PIVOT]},
                       markets=["stock"]))
    synthetic_trades(db, n_days=120, per_day=20, effect=pivot_effect, base=-0.25)
    L.measure_all(db)
    a, b = L.get(db.conn, a["id"]), L.get(db.conn, b["id"])
    m = a["mesures"]
    assert m["esperance_r"] > 0 > m["esperance_20e_r"] == pytest.approx(m["esperance_r"] - 0.3, abs=1e-3)
    assert len(calls) == m["n"]  # R à 20 € calculé une fois par trade concerné, partagé entre les deux leçons
    lost = f"{abs(m['esperance_20e_r']):.2f}".replace(".", ",")
    assert L.proof(a).endswith(f"relative : moins mauvais que la moyenne, perd encore {lost} R à 20 €")
    assert f"(à 20 € : {m['esperance_20e_r']:+.2f} R)".replace(".", ",") in L.proof(a)
    adj = L.adjustment([live_item(a["id"]) | {k: m[k] for k in L.LIVE_METRICS}])
    assert adj["lignes"][0].endswith(f"(historique ; relative : moins mauvais que la moyenne, perd encore {lost} R à 20 €)")
    # Crypto : pas de R à 20 € (frais OKX déjà dans le R du labo).
    assert L.relative("favorable", 0.05, None) is None
    assert L.relative("favorable", -0.1, -0.4) == "relative : moins mauvais que la moyenne, perd encore 0,40 R à 20 €"
    assert L.relative("favorable", -0.2, 0.1) == "relative : moins mauvais que la moyenne, perd encore 0,20 R"
    assert L.relative("favorable", 0.2, 0.0) == "relative : moins mauvais que la moyenne, ne gagne rien à 20 €"


def test_lecon_a_confirmer_favorable_qui_perd_encore_est_relative(db):
    x = add(db, lesson())
    db.conn.execute("UPDATE lessons SET metrics = ? WHERE id = ?",
                    (json.dumps({"n": 40, "esperance_r": -0.1, "esperance_ensemble_r": -0.3, "jours_rejeu": 12,
                                 "jours_direct": 0, "vu_jusqu": {}}), x["id"]))
    x = L.get(db.conn, x["id"])
    assert x["etat"] == "a_confirmer"
    assert L.proof(x).endswith("relative : moins mauvais que la moyenne, perd encore 0,10 R")
    assert L.line(x).startswith("🔎 👍")  # « (historique) » ne vise que les confirmées


def test_lecon_favorable_qui_fait_pire_que_l_ensemble_n_est_pas_moins_mauvaise(db, tmp_path, monkeypatch):
    """Relecture de la fusion (L1) : une favorable « à confirmer » (ou une confirmée retombée) dont les trades
    concernés font PIRE que l'ensemble ne dit pas « moins mauvais que la moyenne » : elle dit « pas mieux que la
    moyenne » et garde « perd encore X R », dans !lecons, dans la preuve de la routine et dans le plan du direct."""
    x = add(db, lesson())
    synthetic_trades(db, n_days=12, per_day=20, effect=lambda c, r: -0.15 if c["setup"] == "cassure_pivot" else 0.0,
                     base=-0.2, seed=4)
    L.measure_all(db)
    x = L.get(db.conn, x["id"])
    m = x["mesures"]
    assert x["etat"] == "a_confirmer" and m["esperance_r"] < m["esperance_ensemble_r"] < 0 and m["ecart_r"] < 0
    lost = f"{abs(m['esperance_r']):.2f}".replace(".", ",")
    assert L.proof(x).endswith(f" · pas mieux que la moyenne, perd encore {lost} R")
    assert "moins mauvais" not in L.text(db.conn) and "moins mauvais" not in json.dumps(L.brief(db.conn))
    # Mesure sur un seul jour (écart du z inconnu) : l'écart vient des deux espérances.
    one_day = {"n": 40, "esperance_r": -0.3, "esperance_ensemble_r": -0.1, "ecart_r": None, "jours_rejeu": 1,
               "jours_direct": 0, "vu_jusqu": {}}
    assert L.proof(x | {"mesures": one_day}).endswith(" · pas mieux que la moyenne, perd encore 0,30 R")
    # Confirmée dont l'écart est retombé sous 0 (hystérésis) : le fichier du direct porte l'écart, le plan le dit.
    db.conn.execute("UPDATE lessons SET status = 'confirmee', metrics = ? WHERE id = ?",
                    (json.dumps(m | {"palier_juge": 0}), x["id"]))
    L.write_live(db)
    (item,) = json.loads(L.live_path(db.path).read_text())["lecons"]
    assert item["ecart_r"] == m["ecart_r"]
    write_live(tmp_path, monkeypatch, [item])
    (line,) = L.adjustment(L.current())["lignes"]
    assert line.endswith(f"(historique ; pas mieux que la moyenne, perd encore {lost} R)")
    # Le texte « moins mauvais » reste quand l'écart est positif (ou inconnu : fichier d'avant l'écart).
    assert L.relative("favorable", -0.2, None, 0.05) == "relative : moins mauvais que la moyenne, perd encore 0,20 R"
    assert L.relative("favorable", -0.2, None, None) == "relative : moins mauvais que la moyenne, perd encore 0,20 R"
    assert L.relative("favorable", -0.2, -0.4, 0.0) == "pas mieux que la moyenne, perd encore 0,40 R à 20 €"
    assert L.relative("favorable", 0.1, None, -0.2) is None and L.relative("defavorable", -0.2, None, -0.1) is None


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
    assert "1 confirmées" in text and "1 démenties" in text and "✅ (historique) 👍 **#1**" in text and "❌ 👎 **#2**" in text


async def test_commande_lecons_sans_base(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_LAB_DB", str(tmp_path / "absente.db"))
    assert "absente" in await bot.handle("!leçons")
