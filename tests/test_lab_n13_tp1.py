"""Déclaration n° 13 : la confirmation des GO avec une autre annulation sur le TP1 (quart d'heure de grâce, puis
clôture au niveau du TP1 ou au-dessus), rejouée sur les deux stops de la n° 7 ; le direct garde la règle actuelle."""
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.lab import audit as AUD
from app.lab import empreinte as E
from app.lab import simulator as SIM
from app.lab import strat_stats as SS
from app.lab import strategies as ST
from app.lab.db import LabDB
from app.trading import confirm as CF
from tests.test_killmode import daily, hist
from tests.test_lab_n7_confirmation import CONFIRMED, PLAN, at, by_name, d_close, make, session
from tests.test_lab_strategies import DAY, OPEN, bars_from

TARGETS = [103.0, 105.0, 107.0]
# Façon AEYE (28/10) : ouverture collée au TP1, mèche au-dessus à la 1re bougie qui clôture sous l'entrée (repli),
# puis reprise du VWAP sur la bougie de 9 h 45 (finie à 9 h 50), déclenchement à 9 h 50, TP1 à 9 h 55.
WICK = [(103.0, 103.4, 99.6, 99.8), (99.8, 100.2, 99.5, 100.0), (100.0, 100.3, 99.7, 99.9),
        (99.9, 100.6, 99.8, 100.5), (100.5, 101.0, 100.4, 100.9), (100.9, 103.2, 100.8, 103.0)]
# Sans repli : la bougie de 9 h 45 monte au TP1, en mèche (WICK_LATE) ou en clôture (CLOSE_LATE).
CALM = [(101.0, 101.5, 100.6, 101.2), (101.2, 101.6, 100.9, 101.4), (101.4, 102.0, 101.3, 101.9)]
WICK_LATE = CALM + [(101.9, 103.5, 101.8, 102.6)]
CLOSE_LATE = CALM + [(101.9, 103.5, 101.8, 103.2)]


def go():
    return {"status": "attente", "entry": 100.0, "stop": 98.0, "targets": list(TARGETS), "last_bar": None}


def events(rows):
    bars, opened = bars_from(OPEN, rows), datetime.fromtimestamp(OPEN, timezone.utc)
    return CF.evaluate(go(), bars, opened), CF.evaluate(go(), bars, opened, tp1="cloture")


# --- La règle (confirm.evaluate) ---------------------------------------------------------------

def test_meche_au_tp1_pendant_le_quart_d_heure_ne_tue_plus_le_go():
    old, new = events(WICK)
    assert old == {"kind": "annulé", "t": OPEN, "why": "TP1 103 touché avant la confirmation"}   # règle du direct
    assert new["kind"] == "confirmé" and new["t"] == OPEN + 15 * 60 and new["trigger"] == 100.61


def test_cloture_au_tp1_pendant_le_quart_d_heure_ne_tue_pas_non_plus():
    rows = [(101.0, 103.6, 100.9, 103.3), (103.3, 103.4, 101.0, 101.5), (101.5, 101.6, 99.9, 100.2),
            (100.2, 102.0, 100.1, 101.9)]                                # repli à 9 h 40, VWAP repris à 9 h 45
    old, new = events(rows)
    assert old["kind"] == "annulé" and old["t"] == OPEN
    assert new["kind"] == "confirmé" and new["t"] == OPEN + 15 * 60 and new["trigger"] == 102.01


def test_apres_9h45_seule_une_cloture_au_tp1_annule():
    old, new = events(WICK_LATE)
    assert old["kind"] == "annulé" and old["t"] == OPEN + 15 * 60
    assert new is None                                                   # mèche : le GO reste en vie
    old, new = events(CLOSE_LATE)
    assert old["t"] == new["t"] == OPEN + 15 * 60 and new["kind"] == "annulé"
    assert new["why"] == "clôture 5 min à 103.2 au TP1 103 ou au-dessus avant la confirmation"
    old, new = events(CALM + [(101.9, 103.5, 101.8, 103.0)])           # clôture au niveau du TP1 : annulé aussi
    assert new["kind"] == "annulé" and new["t"] == OPEN + 15 * 60


def test_sous_le_stop_annule_toujours_des_la_premiere_bougie():
    rows = [(101.0, 101.2, 97.0, 97.5)] + CONFIRMED[1:]
    old, new = events(rows)
    assert old == new and new["kind"] == "annulé" and new["t"] == OPEN and "sous le stop" in new["why"]


def test_le_direct_garde_la_regle_actuelle():
    bars, opened = bars_from(OPEN, WICK), datetime.fromtimestamp(OPEN, timezone.utc)
    assert CF.evaluate(go(), bars, opened) == CF.evaluate(go(), bars, opened, tp1="meche")
    assert CF.TP1_RULES == ("meche", "cloture")
    with pytest.raises(ValueError):
        CF.evaluate(go(), bars, opened, tp1="clôture")


def test_la_n13_n_annule_jamais_plus_tot_et_ne_change_que_les_go_tombes_sur_le_tp1():
    """Contrôle du protocole : un GO confirmé, expiré ou tombé sous le stop avec la règle actuelle a exactement le même
    sort avec la n° 13 ; seuls les GO annulés sur le TP1 changent, jamais plus tôt."""
    rng = random.Random(13)
    opened = datetime.fromtimestamp(OPEN, timezone.utc)
    changed = rescued = 0
    for _ in range(600):
        p, rows = 100.0 + rng.uniform(-1.0, 4.0), []
        for _ in range(23):  # 9 h 30 → 11 h 20 : les bougies finies avant 11 h 30
            c = p + rng.gauss(0, 0.6)
            rows.append((p, max(p, c) + abs(rng.gauss(0, 0.5)), min(p, c) - abs(rng.gauss(0, 0.5)), c))
            p = c
        bars = bars_from(OPEN, rows)
        plan = {"status": "attente", "entry": 100.0, "stop": 98.5, "targets": [101.5, 103.0, 104.5], "last_bar": None}
        old = CF.evaluate(dict(plan), bars, opened)
        new = CF.evaluate(dict(plan), bars, opened, tp1="cloture")
        if old is None or old["kind"] == "confirmé" or "sous le stop" in old["why"]:
            assert new == old
        else:
            assert "TP1" in old["why"] and (new is None or new["t"] >= old["t"])
            changed += 1
            rescued += bool(new and new["kind"] == "confirmé")
    assert changed > 100 and rescued > 10       # le cas se présente, et la n° 13 en garde une partie


# --- Les bras au labo (go_rows) ----------------------------------------------------------------

def test_bras_tp1_cloture_au_stop_d_origine():
    d = make(WICK)
    rows = by_name(ST.go_rows(d, "AAA", {"origine": PLAN}, 3, 1.0))
    conf, tp1 = rows["go_confirme"], rows["go_confirme_tp1"]
    assert conf["confirmation"] == "annule" and conf["triggered"] == 0 and conf["signal_at"] == OPEN + 5 * 60
    assert tp1["confirmation"] == "confirme" and tp1["triggered"] == 1 and tp1["entry"] == 100.61
    assert tp1["signal_at"] == tp1["filled_at"] == OPEN + 20 * 60 and tp1["entry_mode"] == "stop"
    assert tp1["r_net"] is None and tp1["r_sans_be"] is not None and tp1["temoin_triggered"] == 1
    assert all(r["confirmation"] == "annule" for n, r in rows.items() if n.startswith("go_confirme_v"))  # n° 8
    ref = ST._sim(d, ST._plan(100.61, 98.0, TARGETS), OPEN + 20 * 60, at(11, 30), d.close_ts,
                  SIM.costs("stock", 100.61, 3), "stop", 100.5, exit_mode="sans_be")
    assert tp1["r_sans_be"] == ref["r_net"]                              # même ordre et même sortie que go_confirme


def test_meme_trade_que_go_confirme_quand_rien_ne_touche_le_tp1():
    rows = by_name(ST.go_rows(make(CONFIRMED), "AAA", {"origine": PLAN}, 3, 1.0))
    keys = ("confirmation", "triggered", "entry", "stop", "signal_at", "filled_at", "r_sans_be", "r_gross",
            "exit_reason", "r_glisse_x2", "r_retard", "r_20e")
    assert {k: rows["go_confirme_tp1"][k] for k in keys} == {k: rows["go_confirme"][k] for k in keys}


def test_bras_tp1_cloture_au_plancher_et_dans_la_journee():
    floor = {"entry": 100.0, "stop": 97.0, "targets": [{"price": 103.0}, {"price": 106.0}, {"price": 108.0}]}
    rows = by_name(ST.go_rows(make(WICK), "AAA", {"plancher": floor}))
    assert set(rows) == {"go_brut_plancher", "go_confirme_plancher", "go_confirme_tp1_plancher"}
    assert rows["go_confirme_plancher"]["confirmation"] == "annule"
    assert rows["go_confirme_tp1_plancher"]["confirmation"] == "confirme" and rows["go_confirme_tp1_plancher"]["stop"] == 97.0
    v1 = {"plan": PLAN, "decided_at": OPEN - 20 * 60, "entry_until": d_close(), "last_price": 100.0,
          "cost": SIM.costs("stock", 100.0), "go": True, "plancher": None}
    names = {r["strategy"] for r in ST.stock_day(DAY, "AAA", hist(), bars_from(OPEN, session(WICK)), daily(), 3, v1,
                                                 traded=True)}
    assert {"go_confirme", "go_confirme_tp1"} <= names and "go_confirme_tp1_plancher" not in names


# --- Déclaration, essais, jugement sur les séances neuves --------------------------------------

def test_declaration_et_essais():
    assert ST.DECLARATION == ST.LATEST_DECLARATION == 13 and ST.CRYPTO_DECLARATION == 11
    assert ST.REGISTERED_ON == "2026-10-29" and ST.progress_job("stock") == "strats:stock:d13"
    assert ST.N13_NAMES == ("go_confirme_tp1", "go_confirme_tp1_plancher") and set(ST.N13_NAMES) <= set(ST.GO_STRATEGIES)
    assert ST.GO_TP1 == {"origine": ("confirme_tp1", "cloture"), "plancher": ("confirme_tp1", "cloture")}
    assert set(ST.N13_NAMES).isdisjoint(ST.GO_FILTERS.values())
    t = ST.trials()
    assert len(t) == 165 and [x for x in t if x["strategy"] in ST.N13_NAMES] == [
        {"market": "stock", "strategy": n, "exit": "r_sans_be"} for n in ST.N13_NAMES]
    assert SS.candidate_z(len(t)) == 3.43
    assert ST.seen_until("go_confirme_tp1_plancher", "stock") == ST.SEEN_N13["stock"] == "2026-10-28"
    assert ST.seen_until("go_confirme", "stock") is None
    assert "TP1 sur clôture" in AUD.NOTES[13]
    assert ("trading/confirm.py", ("BAR", "WAIT", "LAST_CHECK", "TICK", "TP1_RULES", "PENDING", "evaluate")) \
        in E.STRAT_CODE and E.missing() == []
    doc = (Path(__file__).resolve().parents[1] / "docs" / "labo-strategies.md").read_text(encoding="utf-8")
    section = doc.split("## Déclaration n° 13")[1].split("## Bloc scellé")[0]
    for text in ("**165**", "protocole-n13-fige-2026-10-29.md", "**28 octobre 2026**", "`go_confirme_tp1_plancher`",
                 "**Le direct ne change pas**", "Paris de l'analyste"):
        assert text in section


SEEN = date(2026, 10, 28)


def _rows(db, days_before=8, days_after=10, odd=False):
    """Trois GO par séance (entrée 10, stop 9 : 0,01 R de commission IBKR) : S0 confirmé par les deux règles (même
    trade), S1 annulé sur le TP1 et repris par la n° 13 (+1 R ±0,1 ; 0,8 R ±0,1 à 20 € en sortie TP2), S2 annulé par
    les deux. `odd` : le 1er jour, S1 expiré par la règle actuelle puis confirmé par la n° 13 (impossible : défaut)."""
    out = []
    days = [SEEN - timedelta(days=days_before - k) for k in range(days_before)]
    days += [SEEN + timedelta(days=1 + k) for k in range(days_after)]
    for k, day in enumerate(x.isoformat() for x in days):
        wobble = 0.1 * (k % 3 - 1)
        for sym, old, new, r_old, r_new in (("S0", "confirme", "confirme", 0.5, 0.5),
                                            ("S1", "annule", "confirme", None, 1.0 + wobble),
                                            ("S2", "annule", "annule", None, None)):
            out += [(sym, day, "go_brut", -0.2, None, "confirme" if sym == "S0" else "annule"),
                    (sym, day, "go_confirme", r_old, None if r_old is None else 0.8 + wobble, old),
                    (sym, day, "go_confirme_tp1", r_new, None if r_new is None else 0.8 + wobble, new)]
        if odd and k == 0:
            out[-5] = ("S1", day, "go_confirme", None, None, "expire")
    db.conn.executemany(
        "INSERT INTO strat_trades (origin, market, symbol, day, strategy, slot, triggered, entry, stop, r_sans_be, "
        "r_gross, r_glisse_x2, r_glisse_x3, r_retard, r_manque, r_3c, r_20e, r_20e_1s, temoin_r, temoin_triggered, "
        "confirmation) VALUES ('replay', 'stock', ?, ?, ?, 0, ?, 10.0, 9.0, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0.0, 1, ?)",
        [(s, d, n, int(r is not None), r, r, r, r, r, r, r, r, r20, st) for s, d, n, r, r20, st in out])


def test_tableau_de_la_n13_juge_les_seules_seances_neuves():
    db = LabDB(":memory:")
    _rows(db)
    rep = SS.report(db.conn)
    n13 = rep["confirmation_go"]["origine"]["tp1_cloture"]
    assert n13["vu_jusqu_au"] == "2026-10-28" and n13["anormaux"] == 0
    new, old = n13["jours_neufs"], n13["jours_vus"]
    assert new["ecart"]["n"] == 30 and new["ecart"]["jours"] == 10 and old["ecart"]["n"] == 24
    # S1 : +1 R ± 0,1 (moyenne +0,01 sur ces 10 séances) − 0,01 R de commission, sur 3 GO par séance
    assert new["ecart"]["esperance_r"] == pytest.approx(1 / 3, abs=1e-3) and new["ecart"]["aide"]
    assert new["ecart"]["seuil_t"] == SS.candidate_t(10) and "aide" not in old["ecart"]   # l'historique : descriptif
    assert 0.8 / 3 < new["ecart_20e"]["esperance_r"] < 0.9 / 3        # r_20e_1s recompté au Dégressif, un peu mieux
    moves = {(p["de"], p["vers"]): p for p in n13["passages"]}
    assert set(moves) == {("confirmé", "confirmé"), ("annulé", "confirmé"), ("annulé", "annulé")}
    assert moves[("annulé", "confirmé")]["go"] == 18 and moves[("annulé", "confirmé")]["actuelle"]["n"] == 0
    assert moves[("annulé", "confirmé")]["n13"]["esperance_r"] == pytest.approx(0.99, abs=1e-3)
    item = rep["strategies"]["stock"]["go_confirme_tp1"]
    assert item["jours_vus_jusqu_au"] == "2026-10-28" and item["jours_suivants"]["declenches"] == 20
    assert item["declenches"] == 36 and not item["candidate"]
    assert "plancher" not in rep["confirmation_go"]
    line = next(x for x in SS.text(rep).splitlines() if x.startswith("TP1 sur clôture (n° 13, stop d'origine)"))
    assert "après le 28/10/2026 : n° 13 − confirmé +0,33 R par GO" in line and ", ↗ aide (relatif)" in line
    assert "30 GO, 10 séances" in line and "jours vus (descriptif) +0,33 R par GO" in line
    assert line.endswith(" · passages annulé → confirmé 18 (n° 13 +0,99 R sur 18 trades)")   # sorts inchangés : tus


def test_tableau_de_la_n13_sans_seance_neuve_et_defaut_signale():
    db = LabDB(":memory:")
    _rows(db, days_after=0, odd=True)
    rep = SS.report(db.conn)
    n13 = rep["confirmation_go"]["origine"]["tp1_cloture"]
    assert n13["jours_neufs"]["ecart"]["n"] == 0 and not n13["jours_neufs"]["ecart"]["aide"]
    assert n13["anormaux"] == 1                                          # expiré → confirmé : impossible
    line = next(x for x in SS.text(rep).splitlines() if x.startswith("TP1 sur clôture (n° 13"))
    assert "après le 28/10/2026 : pas encore de séance" in line and "⚠️ 1 GO non annulés" in line
