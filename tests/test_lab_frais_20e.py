"""`!frais` (20/10/2026, demande de Simão) : sa mise de 20 € selon le barème IBKR et le nombre de brackets, en écart
apparié sur les mêmes trades du labo. Mesure descriptive : aucun essai, aucune « candidate »."""
from datetime import date, timedelta

import pytest

from app.lab import brief as BRIEF
from app.lab import strat_stats as SS
from app.lab.db import LabDB
from app.notify import bot


def _day(k):
    return (date(2025, 1, 6) + timedelta(days=k)).isoformat()   # lundi 6 janvier 2025


def _insert(db, rows):
    cols = sorted({c for r in rows for c in r})
    db.conn.executemany(f"INSERT INTO strat_trades (origin, {', '.join(cols)}) VALUES ('replay', "
                        f"{', '.join('?' * len(cols))})", [[r.get(c) for c in cols] for r in rows])


def _row(market, k, j, three=True):
    r = -0.1
    row = {"market": market, "symbol": f"S{j}", "day": _day(k), "strategy": "pullback", "slot": j, "triggered": 1,
           "r_net": r, "r_sans_be": r, "r_glisse_x2": r, "r_glisse_x3": r, "r_retard": r, "r_manque": r,
           "temoin_r": -0.2, "temoin_triggered": 1, "entry": 10.0, "stop": 9.0}
    if market == "stock":   # variantes à 20 € : actions seulement, un ou deux brackets sur les plans à trois objectifs
        base, noise = -0.3 + 0.02 * j, 0.01 * (k % 3 - 1)    # bruit par jour, de moyenne nulle sur les 30 jours
        row |= {"r_3c": r, "r_20e": base, "r_20e_deg": base + 0.05 + noise,
                "r_20e_1s": base + 0.2 + noise if three else None, "r_20e_2s": base + 0.1 + noise if three else None}
    return row


@pytest.fixture
def rep():
    db = LabDB(":memory:")
    _insert(db, [_row(m, k, j, three=j < 2) for m in ("stock", "crypto") for k in range(30) for j in range(3)])
    return SS.report(db.conn)


def test_ecart_apparie_sur_les_memes_trades(rep):
    item = rep["strategies"]["stock"]["pullback"]
    costs = item["couts_20e"]
    assert item["mise_20e"]["n"] == 90 and costs["degressif"]["ecart"]["n"] == 90
    assert costs["degressif"]["ecart"]["esperance_r"] == pytest.approx(0.05, abs=1e-4)
    # Un ou deux brackets : seulement les 60 trades à trois objectifs, comparés à r_20e sur ces mêmes trades.
    assert costs["un_objectif_tp2"]["ecart"]["n"] == costs["deux_objectifs_tp1_tp3"]["ecart"]["n"] == 60
    assert costs["un_objectif_tp2"]["ecart"]["esperance_r"] == pytest.approx(0.2, abs=1e-4)
    assert costs["deux_objectifs_tp1_tp3"]["ecart"]["esperance_r"] == pytest.approx(0.1, abs=1e-4)
    assert costs["un_objectif_tp2"]["variante"]["esperance_r"] == pytest.approx(-0.09, abs=1e-4)
    assert rep["strategies"]["crypto"]["pullback"]["couts_20e"] is None
    assert not item["candidate"]          # descriptif : ne change ni « candidate » ni le nombre d'essais


def test_texte_de_la_commande_frais(rep):
    text = SS.costs_text(rep)
    nom = rep["strategies"]["stock"]["pullback"]["nom"]
    assert text.startswith("💶 **Ta mise de 20 € chez IBKR** (mesure, pas un essai)")
    assert f"\n{nom} : trois brackets Fixe -0,28 R (90 trades) · Dégressif -0,23 R (écart +0,05, t " in text
    assert "un bracket (TP2) -0,09 R (écart +0,20, t " in text and ", 60 trades)" in text
    assert "deux brackets (TP1 et TP3)" in text and "Crypto" not in text
    assert "pas encore de trade" in SS.costs_text(None)
    old = {"strategies": {"stock": {"pullback": {"nom": nom, "mise_20e": {"n": 5, "esperance_r": -0.3}}}}}
    assert "Variantes : au prochain calcul" in SS.costs_text(old)      # tableau calculé avant cette mesure
    part = rep | {"partiel": {"stock": {"declaration": 11, "jours_rattrapage": 513, "lignes_exclues": 1}}}
    assert "⏳ Tableau partiel : 513 jours en rattrapage (n° 11 seule en attendant)" in SS.costs_text(part)


def test_brief_et_commande_discord(rep, monkeypatch, tmp_path):
    monkeypatch.setattr(SS, "cached", lambda conn: rep)
    got = BRIEF._strategies(None)["marches"]["stock"]["pullback"]["couts_20e_ecart_r"]
    assert got == pytest.approx({"degressif": 0.05, "un_objectif_tp2": 0.2, "deux_objectifs_tp1_tp3": 0.1}, abs=1e-4)
    assert BRIEF._strategies(None)["marches"]["crypto"]["pullback"]["couts_20e_ecart_r"] is None
    assert "!frais" in bot.HELP


async def test_commande_frais(rep, monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_LAB_DB", str(tmp_path / "absent.db"))
    assert (await bot.handle("!frais")).startswith("💶 Base du labo absente")
    (tmp_path / "lab.db").touch()
    monkeypatch.setenv("JARVIS_LAB_DB", str(tmp_path / "lab.db"))
    monkeypatch.setattr(SS, "cached", lambda conn: rep)
    assert (await bot.handle("!mise")).startswith("💶 **Ta mise de 20 € chez IBKR**")


# --- 25/10/2026 : un seul bracket au barème Dégressif (audits de GPT et Gemini, décision n° 1) -----------------

def test_un_bracket_au_degressif_egal_au_calcul_direct():
    """La variante déduite de r_20e_1s (Fixe) en changeant la commission de ses deux ordres donne le même R que
    killmode.real_r rejoué au Dégressif sur le même trade à un bracket."""
    from app.trading import killmode as K

    call = {"entry": 10.0, "stop": 9.0, "targets": [11.5]}
    for exits in ([{"part": 1.0, "prix": 11.5, "raison": "tp2"}], [{"part": 1.0, "prix": 9.0, "raison": "stop"}],
                  [{"part": 1.0, "prix": 10.37, "raison": "cloture"}]):
        done = {"declenche": True, "remplissage": 10.0, "eurusd": K.EURUSD_DEFAULT, "plan_ibkr": "fixe",
                "sorties": exits}
        fixe = K.real_r(call, done)
        deg = K.real_r(call, done | {"plan_ibkr": "degressif"})
        assert SS.one_bracket_degressif(10.0, 9.0, fixe) == pytest.approx(deg, abs=2e-4)
    # 23 actions à 1 $ de risque : deux ordres à 1 $ (Fixe) contre 0,35 $ + 0,0032 $ par action (Dégressif).
    assert SS.one_bracket_degressif(10.0, 9.0, -0.1) == pytest.approx(-0.1 + 2 * (1 - 0.35 - 0.0032 * 23) / 23,
                                                                       abs=1e-4)
    assert SS.one_bracket_degressif(10.0, 9.0, None) is None and SS.one_bracket_degressif(9.0, 9.0, 0.1) is None


def test_un_bracket_contre_trois_au_degressif(rep):
    item = rep["strategies"]["stock"]["pullback"]
    deg = item["couts_20e_degressif"]["un_objectif_tp2"]
    gain = 2 * (1 - 0.35 - 0.0032 * 23) / 23   # entrée 10 $, stop 9 $ : 23 actions
    # Apparié à trois brackets au Dégressif sur les mêmes 60 trades : (r_20e_1s + gain) − r_20e_deg = 0,15 + gain.
    assert deg["ecart"]["n"] == 60 and deg["ecart"]["esperance_r"] == pytest.approx(0.15 + gain, abs=1e-4)
    assert rep["strategies"]["crypto"]["pullback"]["couts_20e_degressif"] is None
    assert "un bracket (TP2) au Dégressif -0,04 R (écart à trois brackets au Dégressif +0,20, t " in SS.costs_text(rep)
    assert "au moins +0,02 R" in SS.costs_text(rep)
    assert not item["candidate"]


def test_brief_un_bracket_au_degressif(rep, monkeypatch):
    monkeypatch.setattr(SS, "cached", lambda conn: rep)
    got = BRIEF._strategies(None)["marches"]["stock"]["pullback"]["un_bracket_degressif"]
    assert got["n"] == 60 and got["ecart_r"] == pytest.approx(0.2001, abs=1e-3)
    assert BRIEF._strategies(None)["marches"]["crypto"]["pullback"]["un_bracket_degressif"] is None


def test_brief_ic_de_la_meme_esperance(rep, monkeypatch):
    """Soirée du 27/10/2026 : en actions, l'espérance du brief est après commission ; son IC doit l'être aussi."""
    monkeypatch.setattr(SS, "cached", lambda conn: rep)
    got = BRIEF._strategies(None)["marches"]
    stock, crypto = rep["strategies"]["stock"]["pullback"], rep["strategies"]["crypto"]["pullback"]
    assert stock["ic95_ibkr"] and got["stock"]["pullback"]["ic95"] == stock["ic95_ibkr"]
    assert got["stock"]["pullback"]["esperance_r"] == stock["esperance_ibkr_r"]
    assert got["crypto"]["pullback"]["ic95"] == crypto["ic95"]       # crypto : pas de commission


def test_par_rang_a_20_euros_au_degressif():
    """4e échange avec GPT (Q3) : par tranche de rang, l'espérance après toutes les commissions à 20 €."""
    db = LabDB(":memory:")
    rows = []
    for k in range(4):
        day = f"2025-02-{3 + k:02d}"
        rows += [("replay", "stock", "A", day, "orb15", 0, 1, 0.1, 0.1, 0.0, 1, 10, -0.2),
                 ("replay", "stock", "B", day, "orb15", 0, 1, 0.1, 0.1, 0.0, 1, 200, 0.05 * k)]
    db.conn.executemany("INSERT INTO strat_trades (origin, market, symbol, day, strategy, slot, triggered, r_net, "
                        "r_sans_be, temoin_r, temoin_triggered, rank, r_20e_deg) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                        "?, ?, ?)", rows)
    item = SS.report(db.conn)["strategies"]["stock"]["orb15"]
    tiers = item["par_rang_20e_degressif"]
    assert tiers["rang 1-50"]["esperance_r"] == -0.2 and tiers["rang 1-50"]["n"] == 4
    assert tiers["rang 151+"]["esperance_r"] == pytest.approx(0.075)
    assert "rang 51-150" not in tiers
