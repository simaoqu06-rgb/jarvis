"""Scanner (mesures et filtres), forward-test (répartition, issue, bilan) et commandes Discord, hors ligne."""
import json
import random
from datetime import date, datetime, timedelta, timezone

import pytest

from app.notify import bot
from app.sources import alpaca
from app.trading import calendar as CAL
from app.trading import confirm as CONFIRM
from app.trading import paper, scanner
from app.trading import proposals as PR
from app.trading.settings import Settings
from tests.test_score import daily


@pytest.fixture
def s():
    return Settings(None)


def test_mesures_du_scanner_ignorent_la_seance_du_jour():
    rows = daily(80)
    rows[-1]["v"] = 9e9  # bougie « d'aujourd'hui » : ne doit pas compter dans le volume de la veille
    today = datetime.fromtimestamp(rows[-1]["t"], alpaca.NEW_YORK).date().isoformat()
    f = scanner.features(rows, price=rows[-2]["c"] * 1.03, prev_close=None, today=today)
    assert f["gap_pct"] == pytest.approx(3.0, abs=0.01)
    assert f["rvol"] == pytest.approx(1.0)


def test_filtres_du_scanner(s):
    f = {"price": 50.0, "gap_pct": 3.0, "rvol": 1.0, "dollar_volume": 5e7, "atr_pct": 4.0, "rsi": 60.0,
         "ma20": 45.0, "ma50": 40.0}
    assert scanner.filter_reasons(f, s) == []
    assert "pas en jeu" in scanner.filter_reasons(f | {"gap_pct": 0.5}, s)
    assert set(scanner.filter_reasons(f | {"price": 3.0, "rsi": 80.0, "ma50": 60.0}, s)) >= {"prix", "RSI", "sous la MM50"}


def test_repartition_des_parts_sur_les_tp():
    tps = [{"name": "TP1"}, {"name": "TP2"}, {"name": "TP3"}]
    assert [q for q, _ in paper.legs_for(10, tps)] == [4, 3, 3]
    assert [t["name"] for _, t in paper.legs_for(2, tps)] == ["TP1", "TP2"]
    assert paper.legs_for(0, tps) == []


# Séances de juin-juillet 2026 (heure d'été : 13 h 40 UTC = 9 h 40 New York) : un plan par jour de grappe.
DAYS = [d for d in (date(2026, 6, 1) + timedelta(days=i) for i in range(60)) if CAL.is_trading_day(d)]
FULL = {"price": 100.0, "atr_pct": 4.0, "ma20": 95.0, "ma50": 90.0, "dollar_volume": 5e7}  # passe les autres filtres


def entry(decision="GO", composite=7.5, rsi=60, exits=("tp", "stop", "tp"), day=None, rvol=2.0, gap=3.0, full=True):
    d = (DAYS[day] if day is not None else date(2026, 10, 6)).isoformat()
    e = {"id": "x", "origin": "scanner", "created_at": f"{d}T13:10:00+00:00", "symbol": "ACME", "side": "long",
         "entry": 100.0, "stop": 98.0, "features": {"rsi": rsi, "rvol": rvol, "gap_pct": gap} | (FULL if full else {}),
         "decision": decision, "composite": composite, "parts": {"technique": 8, "catalyst": 6, "risk": 7},
         "status": "open", "legs": [], "result_r": None, "outcome": None}
    for name, ex, price in zip(("TP1", "TP2", "TP3"), exits, (102.0, 104.0, 106.0)):
        e["legs"].append({"name": name, "qty": 1, "target": price, "predicted_minutes": 60, "order_id": name,
                          "filled_at": f"{d}T13:40:00Z", "fill_price": 100.0, "exit": ex,
                          "exit_price": price if ex == "tp" else 98.0, "exit_at": f"{d}T14:40:00Z"})
    return e


def done(r, day, **kw):
    """Plan terminé au résultat `r` (R), sans passer par les jambes."""
    e = entry(day=day, **kw)
    e.update(status="done", result_r=round(r, 4), outcome="TP1 touché" if r > 0 else "stop")
    return e


def noisy(n, mean, days, seed=1, **kw):
    """`n` plans de moyenne `mean` (écart-type 1) répartis sur `days` séances."""
    rnd = random.Random(seed)
    xs = [rnd.gauss(0, 1) for _ in range(n)]
    shift = mean - sum(xs) / n
    return [done(x + shift, i % days, **kw) for i, x in enumerate(xs)]


def test_ordre_alpaca_lu_et_issue_en_r():
    leg = {"filled_at": None, "fill_price": None, "exit": None, "exit_price": None, "exit_at": None}
    paper.apply_order(leg, {"filled_at": "2026-10-06T13:40:00Z", "filled_avg_price": "100", "legs": [
        {"id": "a", "type": "limit", "status": "filled", "filled_avg_price": "102", "filled_at": "2026-10-06T14:00:00Z"},
        {"id": "b", "type": "stop", "status": "canceled"}]})
    assert leg["exit"] == "tp" and leg["exit_price"] == 102.0 and leg["child_ids"] == ["a", "b"]
    e = entry()
    paper.finish(e)
    assert e["result_r"] == pytest.approx((1 - 1 + 3) / 3, abs=0.01) and e["outcome"] == "TP3 touché"


def test_bilan_et_proposition_de_reglage(s):
    # X2.3 : la preuve est en grappes par jour, il faut au moins MIN_SAMPLES plans sur MIN_DAYS séances.
    book = paper.PaperBook(None)
    for i in range(40):
        bad, good = entry(rsi=70, exits=("stop", "stop", "stop"), day=i % 20), entry(rsi=60, day=i % 20)
        paper.finish(bad)
        paper.finish(good)
        book.entries += [bad, good]
    st = paper.stats(book, s, confirmed=[])
    assert st["by"]["rsi"]["groups"]["≥ 65"]["avg_r"] == -1.0
    assert st["by"]["rsi"]["groups"]["≥ 65"]["days"] == 20
    assert any(sg["key"] == "rsi_max" for sg in st["suggestions"])
    assert st["timing"]["median_ratio"] == 1.0
    assert "propositions de réglages suivent" in paper.report_text(st)
    # Tous ces plans le même jour : une seule grappe, pas d'erreur type, donc pas de proposition.
    for e in book.entries:
        e["created_at"] = "2026-06-01T13:10:00+00:00"
    assert not any(sg["key"] for sg in paper.stats(book, s, confirmed=[])["suggestions"])


def test_seance_reguliere_seulement():
    open_ = int(datetime(2026, 10, 6, 13, 30, tzinfo=timezone.utc).timestamp())  # 9 h 30 à New York
    rows = [{"t": open_ - 600}, {"t": open_}, {"t": open_ + 390 * 60}]
    assert alpaca.regular_hours(rows) == [{"t": open_}]


def test_commande_discord_lit_le_ticker():
    assert bot.parse_trade(["nvda"]) == {"symbol": "NVDA", "market": "stock", "side": "long",
                                           "entry": None, "stop": None, "targets": None}
    assert bot.parse_trade(["BTCUSD", "short"]) == {"symbol": "BTC", "market": "crypto", "side": "short",
                                                          "entry": None, "stop": None, "targets": None}
    assert bot.parse_trade(["coin", "crypto"])["market"] == "crypto"
    with pytest.raises(ValueError):
        bot.parse_trade([])


def test_propositions_approuvees_ou_refusees(tmp_path):
    st = Settings(str(tmp_path / "s.json"))
    pr = PR.Proposals(str(tmp_path / "p.json"), st)
    assert pr.propose("rsi_max", 75.0, "rien ne change") is None
    p = pr.propose("rsi_max", 65, "RSI ≥ 65 : -0.40 R")
    assert p["current"] == 75.0 and st["rsi_max"] == 75.0  # rien n'est appliqué avant l'accord
    assert pr.propose("rsi_max", 65, "doublon") is None
    pr.decide(p["id"], approve=True)
    assert st["rsi_max"] == 65
    with pytest.raises(ValueError):
        pr.decide(p["id"], approve=False)
    q = pr.propose("go_min", 6.5, "notes 6-7 positives")
    pr.decide(q["id"], approve=False)
    assert st["go_min"] == 7.0
    assert pr.propose("go_min", 6.5, "encore") is None  # refusée : pas reproposée


async def test_commandes_de_proposition(monkeypatch, tmp_path):
    st = Settings(str(tmp_path / "s.json"))
    pr = PR.Proposals(str(tmp_path / "p.json"), st)
    monkeypatch.setattr(PR, "proposals", pr)
    monkeypatch.setattr(bot, "settings", st)
    assert await bot.handle("!propositions") == "Aucune proposition en attente."
    p = pr.propose("rsi_max", 65, "test")
    assert "Proposition 1" in await bot.handle("!propositions")
    assert "approuvée" in await bot.handle(f"!oui {p['id']}")
    assert st["rsi_max"] == 65
    assert "déjà" in await bot.handle(f"!non {p['id']}")
    assert "numéro" in await bot.handle("!oui")
    assert "rsi_max" in await bot.handle("!regle")
    assert await bot.handle("bonjour") is None
    assert "!oui" in await bot.handle("!aide")


def test_bot_reserve_au_proprietaire(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x")
    monkeypatch.delenv("JARVIS_DISCORD_OWNER_ID", raising=False)
    assert not bot.enabled()
    monkeypatch.setenv("JARVIS_DISCORD_OWNER_ID", "561632751028142111")
    assert bot.enabled() and bot.owners() == {561632751028142111}


def test_scanner_a_l_heure_sans_rattrapage(monkeypatch):
    from app import scheduler
    monkeypatch.setattr(scheduler.alpaca, "configured", lambda: True)
    monkeypatch.setattr(scheduler, "state", type("S", (), {"data": {}})())
    at = lambda h, m: datetime(2026, 10, 6, h, m, tzinfo=alpaca.NEW_YORK)  # noqa: E731
    assert not scheduler.scan_due(at(9, 5))
    assert scheduler.scan_due(at(9, 10)) and scheduler.scan_due(at(9, 39))
    assert not scheduler.scan_due(at(11, 0))
    scheduler.state.data["scan_date"] = "2026-10-06"
    assert not scheduler.scan_due(at(9, 12))


# --- Propositions sur preuve (X2.3) --------------------------------------------------------

def keys(st):
    return [(sg["key"], sg["value"]) for sg in st["suggestions"] if sg["key"]]


def _book(entries):
    book = paper.PaperBook(None)
    book.entries = entries
    return book


def test_pas_de_proposition_sur_du_bruit(s):
    book = paper.PaperBook(None)
    book.entries = noisy(15, -0.1, 8, composite=7.2)  # (a) 15 plans à −0,1 R sur 8 jours
    assert keys(paper.stats(book, s, confirmed=[])) == []
    book.entries = noisy(40, -0.1, 20, composite=7.2)  # assez de plans, mais l'intervalle couvre 0
    st = paper.stats(book, s, confirmed=[])
    assert st["by"]["composite"]["groups"]["7-8"]["ci_low"] < 0 < st["by"]["composite"]["groups"]["7-8"]["ci_high"]
    assert keys(st) == []


def test_go_min_sur_la_tranche_relative_sans_cliquet(tmp_path):
    st_ = Settings(str(tmp_path / "s.json"))
    pr = PR.Proposals(str(tmp_path / "p.json"), st_)
    book = paper.PaperBook(None)
    book.entries = noisy(40, -0.6, 20, composite=7.2)  # (b) 40 plans à −0,6 R sur 20 jours, notes 7-7,5
    st = paper.stats(book, st_, confirmed=[])
    assert keys(st) == [("go_min", 7.5)]
    sg = st["suggestions"][0]
    assert "notes 7-7.5" in sg["why"] and "20 jours" in sg["why"] and "IC" in sg["why"]
    p = pr.propose(sg["key"], sg["value"], sg["why"])
    pr.decide(p["id"], approve=True)
    assert st_["go_min"] == 7.5
    assert keys(paper.stats(book, st_, confirmed=[])) == []  # (c) mêmes plans : pas de 8,0 derrière


def test_go_min_plafonne(s):
    s.update({"go_min": 8.2})
    book = paper.PaperBook(None)
    book.entries = noisy(40, -0.6, 20, composite=8.3)
    assert keys(paper.stats(book, s, confirmed=[])) == [("go_min", paper.GO_MIN_CAP)]  # (d) 8,5 et pas 8,7
    s.update({"go_min": 8.5})
    book.entries = noisy(40, -0.6, 20, composite=8.7)
    assert keys(paper.stats(book, s, confirmed=[])) == []


def test_go_min_abaisse_seulement_sur_un_ic_net(s):
    book = paper.PaperBook(None)
    book.entries = noisy(40, 0.8, 20, composite=6.5, decision="NO GO")
    assert keys(paper.stats(book, s, confirmed=[])) == [("go_min", 6.5)]
    book.entries = noisy(40, 0.25, 20, composite=6.5, decision="NO GO")  # positif, mais borne basse sous 0,2
    assert keys(paper.stats(book, s, confirmed=[])) == []


async def test_instantane_des_reglages_dans_chaque_plan(monkeypatch, s):
    monkeypatch.setattr(alpaca, "configured", lambda: True)
    monkeypatch.setattr(paper.LAB, "current", lambda: {"version": "v3", "regles": []})

    async def account():
        return {"equity": "100000"}

    async def submit(symbol, qty, side, entry, tp, stop, client_order_id):
        return {"id": client_order_id}

    monkeypatch.setattr(alpaca, "account", account)
    monkeypatch.setattr(alpaca, "submit_bracket", submit)
    s.update({"rsi_max": 70})
    plan = {"market": "stock", "symbol": "ACME", "side": "long", "entry": 100.0, "stop": 98.0,
            "created_at": "2026-10-06T13:10:00+00:00",
            "targets": [{"name": f"TP{i}", "price": 100.0 + 2 * i, "minutes_median": 60} for i in (1, 2, 3)],
            "score": {"decision": "GO", "composite": 7.5, "parts": {"technique": {"score": 7}}}}

    book = paper.PaperBook(None)
    e = await paper.submit(book, plan, {"rsi": 60}, s=s)
    manual = await paper.submit(book, plan | {"symbol": "BETA"}, origin="manuel", s=s)
    assert e["settings"] == {"go_min": 7.0, "rsi_max": 70, "scan_rvol_min": 1.5, "scan_gap_min_pct": 2.0,
                             "scan_gap_rvol_min": 0.0}
    assert e["methode"] == "v3" and manual["methode"] is None


# --- Le bon levier pour le volume (X2.4) ---------------------------------------------------

def test_en_jeu_par_le_gap_avec_un_volume_minimal(s):
    f = {"price": 50.0, "gap_pct": 3.0, "rvol": 1.2, "atr_pct": 4.0, "rsi": 60.0, "ma20": 45.0, "ma50": 40.0}
    assert scanner.filter_reasons(f, s) == []  # défaut 0 : rien ne change
    assert scanner.filter_reasons(f, s.all() | {"scan_gap_rvol_min": 1.5}) == ["pas en jeu"]
    g = f | {"rvol": 1.7, "gap_pct": 1.0}
    assert scanner.filter_reasons(g, s) == [] == scanner.filter_reasons(g, s.all() | {"scan_gap_rvol_min": 1.5})
    assert scanner.filter_reasons(f | {"rvol": None}, s.all() | {"scan_gap_rvol_min": 1.5}) == ["pas en jeu"]


def test_volume_faible_propose_scan_gap_rvol_min(s):
    book = paper.PaperBook(None)
    book.entries = noisy(40, -0.6, 20, rvol=1.2, gap=3.0, composite=6.0, decision="NO GO")
    st = paper.stats(book, s, confirmed=[])
    assert keys(st) == [("scan_gap_rvol_min", 1.5)]  # et plus scan_rvol_min = 2,0, qui ne retirerait rien
    assert "en jeu par le gap seul" in st["suggestions"][0]["why"]
    s.update({"scan_gap_rvol_min": 1.5})
    assert all(scanner.filter_reasons(e["features"], s) == ["pas en jeu"] for e in book.entries)
    assert keys(paper.stats(book, s, confirmed=[])) == []


def test_population_retiree(s):
    book = noisy(40, -0.6, 20, rvol=1.2, gap=3.0)
    manual = entry(day=0) | {"origin": "manuel", "features": {}}
    bare = entry(day=0) | {"features": {}}
    assert paper.removed(book + [manual, bare], s, "scan_rvol_min", 2.0) == []  # (c) rien à retirer, pas de plantage
    assert len(paper.removed(book + [manual, bare], s, "scan_gap_rvol_min", 1.5)) == 40
    # Mesures incomplètes (ancien format) : la population retirée ne se mesure pas, pas de proposition.
    old = noisy(40, -0.6, 20, rvol=1.2, gap=3.0, full=False, composite=6.0, decision="NO GO")
    assert keys(paper.stats(_book(old), s, confirmed=[])) == []


# --- Délais en minutes de cotation (X2.5) ---------------------------------------------------

def _tp_after_night(filled_at, exit_at):
    e = entry(day=0) | {"status": "done", "result_r": 1.0, "created_at": "2026-10-06T13:10:00+00:00"}
    e["legs"] = [{"name": "TP1", "qty": 1, "target": 102.0, "predicted_minutes": 90, "order_id": "a",
                  "filled_at": filled_at, "fill_price": 100.0, "exit": "tp", "exit_price": 102.0, "exit_at": exit_at}]
    return e


def test_delais_en_minutes_de_cotation(s):
    night = _book([_tp_after_night("2026-10-06T19:00:00Z", "2026-10-07T14:00:00Z")])  # 15 h → 10 h le lendemain
    assert paper.stats(night, s, confirmed=[])["timing"]["median_ratio"] == 1.0
    weekend = _book([_tp_after_night("2026-10-09T19:00:00Z", "2026-10-12T14:00:00Z")])  # vendredi → lundi
    assert paper.stats(weekend, s, confirmed=[])["timing"]["median_ratio"] == 1.0
    same = _book([_tp_after_night("2026-10-06T14:00:00Z", "2026-10-06T15:30:00Z")])
    assert paper.stats(same, s, confirmed=[])["timing"]["median_ratio"] == 1.0
    assert "TP coupés par l'horizon sont exclus" in paper.report_text(paper.stats(night, s, confirmed=[]))


# --- Ce que mesure le compte fictif (X2.1) -------------------------------------------------

def rsi_book(go_bad=True):
    """RSI ≥ 65 perdant (GO), 55-65 gagnant, sur 20 séances."""
    return (noisy(40, -0.6 if go_bad else 0.6, 20, rsi=70, seed=2) + noisy(40, 0.6, 20, rsi=60, seed=3))


def conf_rows(r, n, features=True):
    return [{"day": DAYS[i].isoformat(), "symbol": f"S{i}", "status": "confirmé", "composite": 7.5, "entry": 10.0,
             "stop": 9.5, "targets": [10.5, 11.0, 11.5], "trigger": 10.1, "brut_r": 0.1, "confirme_r": r,
             "rempli": 10.1} | ({"features": {"rsi": 70.0, "rvol": 2.0, "gap_pct": 3.0}} if features else {})
            for i in range(n)]


def test_titre_et_plans_manuels_a_part(s):
    book = _book(rsi_book() + [done(5.0, 0) | {"origin": "manuel"}])
    st = paper.stats(book, s, confirmed=[])
    assert st["traded"] == 80 and st["manual"]["n"] == 1 and st["overall"]["avg_r"] == 0.0
    text = paper.report_text(st)
    assert ("plans bruts du scanner (GO + NO GO, entrée limite au prix de pré-ouverture, stop fixe, 2 séances, "
            "avant frais)") in text
    assert "Plans manuels (à part, hors propositions) : 1" in text
    assert "Volume moyen 20 j en $" in text and "10-100 M$" in text
    assert not any(sg["key"] == "dollar_volume" for sg in st["suggestions"])


def test_proposition_sur_les_go_seuls(s):
    # Tous plans confondus, RSI ≥ 65 gagne (NO GO très gagnants) ; sur les GO seuls, il perd.
    book = _book(rsi_book() + noisy(60, 1.5, 20, rsi=70, decision="NO GO", seed=4))
    st = paper.stats(book, s, confirmed=[])
    assert st["by"]["rsi"]["groups"]["≥ 65"]["avg_r"] > 0
    (sg,) = [sg for sg in st["suggestions"] if sg["key"]]
    assert (sg["key"], sg["value"], sg["population"]) == ("rsi_max", 65, "GO seuls")


def test_go_confirmes_contraires_bloquent_la_proposition(s):
    book = _book(rsi_book())
    assert keys(paper.stats(book, s, confirmed=conf_rows(0.5, 12))) == []  # les GO confirmés disent le contraire
    st = paper.stats(book, s, confirmed=conf_rows(-0.5, 12))
    assert keys(st) == [("rsi_max", 65)] and "GO confirmés : -0.50 R sur 12" in st["suggestions"][0]["why"]
    few = paper.stats(book, s, confirmed=conf_rows(0.5, 3))  # trop peu pour trancher : les deux chiffres montrés
    assert keys(few) == [("rsi_max", 65)] and "sur 3, trop peu pour trancher" in few["suggestions"][0]["why"]
    bare = paper.stats(book, s, confirmed=conf_rows(0.5, 12, features=False))  # lignes sans mesures
    assert keys(bare) == [("rsi_max", 65)] and "pas encore mesurés" in bare["suggestions"][0]["why"]
    net = conf_rows(0.5, 12)
    for row in net:
        row["confirme_r20"] = -0.2  # R net à 20 € (frais compris) préféré au R avant frais
    assert keys(paper.stats(book, s, confirmed=net)) == [("rsi_max", 65)]


def test_bilan_relit_les_confirmations(monkeypatch, tmp_path, s):
    log = tmp_path / "confirmations.jsonl"
    log.write_text("".join(json.dumps(r) + "\n" for r in conf_rows(0.5, 12)), encoding="utf-8")
    monkeypatch.setattr(CONFIRM, "LOG_PATH", str(log))
    assert keys(paper.stats(_book(rsi_book()), s)) == []


async def test_reglages_du_scanner_enregistres(monkeypatch):
    async def fake(value):
        return value

    monkeypatch.setattr(scanner.alpaca, "configured", lambda: True)
    monkeypatch.setattr(scanner, "universe", lambda s: fake([]))
    monkeypatch.setattr(scanner.alpaca, "snapshots", lambda symbols: fake({}))
    monkeypatch.setattr(scanner.alpaca, "bars", lambda *a, **k: fake({}))
    monkeypatch.setattr(scanner.LAB, "current", lambda: {"version": None, "regles": []})
    result = await scanner.scan(datetime(2026, 10, 6, 13, 10, tzinfo=timezone.utc), s=Settings(None), save=False)
    assert result["reglages"] == {"scan_rvol_min": 1.5, "scan_gap_min_pct": 2.0, "scan_gap_rvol_min": 0.0}


# --- Anciens fichiers : rien ne plante -------------------------------------------------------

def test_confirmations_illisibles_ou_anciennes(monkeypatch, tmp_path, s):
    rows = conf_rows(0.5, 12)
    old = [{k: v for k, v in r.items() if k != "features"} for r in conf_rows(0.5, 3)]  # avant L5b : ni features ni parts
    new = [r | {"features": {"rsi": 70.0, "rvol": None, "gap_pct": 3.0}, "parts": {"technique": 7, "catalyst": None},
                "confirme_r20": -0.1, "brut_r_be2": 0.2, "confirme_r_be2": 0.1} for r in conf_rows(0.5, 2)]
    odd = [{"status": "confirmé", "day": "2026-06-01", "composite": None, "confirme_r": 1.0},
           {"status": "confirmé", "day": None, "composite": 7.5, "confirme_r": 1.0},
           {"status": "confirmé", "day": "2026-06-01", "composite": 7.5, "confirme_r": "1,0", "features": [1, 2]},
           {"status": "annulé", "day": "2026-06-01", "composite": 7.5, "confirme_r": None}]
    log = tmp_path / "confirmations.jsonl"
    log.write_text("".join(json.dumps(r) + "\n" for r in rows + old + new + odd) + '{"day": "2026-06-0\n[1, 2]\n',
                   encoding="utf-8")
    monkeypatch.setattr(CONFIRM, "LOG_PATH", str(log))
    assert len(paper.confirm_log()) == len(rows + old + new + odd)  # ligne coupée et ligne non objet sautées
    conf = paper._confirmed(paper.confirm_log())
    assert len(conf) == 17 and all(isinstance(c["features"], dict) for c in conf)
    assert "rvol" not in conf[-1]["features"] and conf[-1]["result_r"] == -0.1 and conf[-1]["parts"] == {"technique": 7}
    st = paper.stats(_book(rsi_book()), s)  # relu sur le disque : les GO confirmés à RSI ≥ 65 gagnent en moyenne
    assert keys(st) == []
    monkeypatch.setattr(CONFIRM, "LOG_PATH", str(tmp_path / "absent.jsonl"))
    assert paper.confirm_log() == [] and keys(paper.stats(_book(rsi_book()), s)) == [("rsi_max", 65)]


def test_ancien_fichier_du_compte_fictif(tmp_path, s):
    """Entrées d'avant les réglages enregistrés (ni settings ni methode), plan manuel sans mesures, jambe TP sans
    heure de sortie : bilan et texte sans erreur."""
    old = [entry(day=i % 12) for i in range(14)]
    for e in old:
        paper.finish(e)
        e.pop("origin")
    manual = entry(day=0) | {"origin": "manuel", "features": {}, "parts": {}}
    paper.finish(manual)
    odd = entry(day=1)
    odd["legs"][0]["exit_at"] = None
    paper.finish(odd)
    pending = entry(day=2) | {"status": "pending", "legs": []}
    path = tmp_path / "paper.json"
    path.write_text(json.dumps(old + [manual, odd, pending]), encoding="utf-8")
    book = paper.PaperBook(str(path))
    st = paper.stats(book, s, confirmed=[])
    assert st["traded"] == 15 and st["manual"]["n"] == 1 and st["total"] == 16
    assert st["timing"]["n"] == 14 * 2 + 1  # la jambe sans heure de sortie ne compte pas dans les délais
    assert "Bilan du forward-test" in paper.report_text(st) and keys(st) == []
