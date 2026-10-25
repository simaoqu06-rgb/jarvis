"""Export des trades pour un recalcul externe (3e tour des avis de GPT et Gemini, 25/10/2026)."""
import csv
import hashlib
import io

import pytest

from app.lab import export as EX
from app.lab import strat_stats as SS
from app.lab.db import LabDB


def _db():
    db = LabDB(":memory:")
    rows = []
    for k in range(6):
        day = f"2025-01-{6 + k:02d}"
        for j in range(3):
            r = 0.2 * ((j + k) % 3 - 1)
            rows.append(("replay", "stock", f"S{j}", day, "go_confirme_v2", 0, 1, 10.0, 9.8, 2.0, r, r + 0.05,
                         r - 0.1 * ((j * 2 + k) % 3 - 1) - 0.03 * (k % 2) * j, 1, r - 0.1, 1_736_172_000 + 300 * j))
    rows.append(("replay", "stock", "S9", "2025-01-06", "go_confirme_v2", 0, 0, 10.0, 9.8, 2.0, None, None, None,
                 0, None, None))
    rows.append(("replay", "stock", "S0", "2025-01-06", "orb15", 0, 1, 10.0, 9.8, 2.0, 1.0, 1.0, 0.0, 1, 0.9, None))
    db.conn.executemany(
        "INSERT INTO strat_trades (origin, market, symbol, day, strategy, slot, triggered, entry, stop, risk_pct, "
        "r_net, r_sans_be, temoin_r, temoin_triggered, r_20e_1s, filled_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?)", rows)
    return db


def test_export_csv_et_manifeste():
    db = _db()
    out = EX.export(db.conn, ("go_confirme_v2",))
    lines = list(csv.DictReader(io.StringIO(out["csv"])))
    assert len(lines) == 19 and {x["strategy"] for x in lines} == {"go_confirme_v2"}   # orb15 non demandée
    assert lines[0]["filled_at_utc"].startswith("2025-01-06T") and lines[0]["sortie_ref"] == "r_sans_be"
    m = out["manifest"]
    assert m["lignes"] == 19 and m["declenchees"] == 18 and m["jours"] == 6
    assert m["sha256_csv"] == hashlib.sha256(out["csv"].encode("utf-8")).hexdigest()
    # 5e tour : empreinte du coût des lignes du labo v2 (pas celle de v1) et révisions présentes dans les lignes
    from app.lab import empreinte as E
    assert m["cout_rev_actuel"] == E.strat_cost_rev("stock") and "cout_rev" not in m
    assert m["revisions_lignes"] == [{"code_rev": "inconnue", "cout_rev": "inconnue", "lignes": 19,
                                      "du": "2025-01-06", "au": "2025-01-11"}]
    assert {x["temoin_absent"] for x in lines} == {""}
    first = next(x for x in lines if x["triggered"] == "1")
    comm = SS.ibkr_commission_r(10.0, 9.8)
    assert float(first["commission_ibkr_fixe_r"]) == pytest.approx(comm)
    assert float(first["r_ref_ibkr"]) == pytest.approx(float(first["r_ref"]) - comm)
    assert float(first["r_20e_deg_1s"]) == pytest.approx(SS.one_bracket_degressif(10.0, 9.8,
                                                                                  float(first["r_20e_fixe_1s"])))
    assert float(first["frais_3s_moins_1s_deg_r"]) > 0  # trois brackets : quatre ordres de plus qu'un seul


def test_le_controle_se_retrouve_depuis_le_csv():
    """Un tiers qui applique la recette au CSV retrouve les chiffres du tableau."""
    db = _db()
    out = EX.export(db.conn, ("go_confirme_v2",))
    lines = [x for x in csv.DictReader(io.StringIO(out["csv"])) if x["triggered"] == "1"]
    esp, gap = SS.Mean(), SS.Mean()
    for x in lines:
        esp.add(x["day"], float(x["r_ref_ibkr"]))
        gap.add(x["day"], float(x["r_ref"]) - float(x["temoin_r"]))
    ctl = out["manifest"]["controle"]["go_confirme_v2"]
    assert ctl["esperance_ibkr"]["esperance_r"] == esp.result()["esperance_r"]
    assert ctl["ecart_temoin"]["t"] == gap.result()["z"] and ctl["ecart_temoin"]["n"] == 18
    assert ctl["ecart_temoin"]["ic95"] == gap.result()["ic95"]
    assert ctl["par_origine"]["replay"]["esperance_ibkr"]["esperance_r"] == esp.result()["esperance_r"]
    # Sous la nulle imposée : u_g = somme des écarts du jour g, sans retirer m × n_g
    sums: dict = {}
    for x in lines:
        sums[x["day"]] = sums.get(x["day"], 0.0) + float(x["r_ref"]) - float(x["temoin_r"])
    n, g = len(lines), len(sums)
    se0 = (sum(v * v for v in sums.values()) * g / (g - 1)) ** 0.5 / n
    assert ctl["ecart_temoin"]["t_nulle_imposee"] == pytest.approx(sum(sums.values()) / n / se0, abs=0.01)
    table = SS.report(db.conn)["strategies"]["stock"]["go_confirme_v2"]
    assert table["temoin"]["ecart_r"] == ctl["ecart_temoin"]["esperance_r"]


def test_route_export(monkeypatch):
    from fastapi.testclient import TestClient

    from app import main

    db = _db()
    monkeypatch.setattr(main, "_lab_conn", lambda: db.conn)
    client = TestClient(main.app)
    monkeypatch.setenv("JARVIS_LAB_API_KEY", "k")
    r = client.get("/api/lab/export/trades", params={"strategies": "go_confirme_v2"}, headers={"X-Lab-Key": "k"})
    assert r.status_code == 200 and r.json()["manifest"]["lignes"] == 19
