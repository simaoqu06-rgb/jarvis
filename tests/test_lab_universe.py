"""Univers du jour : seulement des données de la veille et d'avant."""
import random
from datetime import date, timedelta

from app.lab import collector as CO
from app.lab import universe as U
from app.lab.db import LabDB

DAY = date(2026, 10, 6)


def stock_daily(n_symbols: int = 400, days: int = 30, seed: int = 3) -> dict[str, list[dict]]:
    rnd = random.Random(seed)
    sessions = CO.trading_days(DAY - timedelta(days=days * 2), DAY)[-days:]
    out = {}
    for i in range(n_symbols):
        p, rows = 10 + i, []
        for d in sessions:
            p *= 1 + rnd.gauss(0, 0.03)
            rows.append({"t": CO.ny_midnight(d), "o": p, "h": p * 1.01, "l": p * 0.99, "c": p,
                         "v": float(rnd.randint(1000, 100000))})
        out[f"S{i:03d}"] = rows
    return out


def test_univers_actions_300_valeurs_sans_filtre_de_taille():
    daily = stock_daily()
    rows = U.stock_universe(daily, DAY)
    assert len(rows) == 300 and len({r["symbol"] for r in rows}) == 300
    assert [r["rank"] for r in rows] == list(range(1, 301))
    reasons = [r["reason"] for r in rows]
    assert reasons[:200] == ["volume_dollars"] * 200 and {"variation", "rvol"} == set(reasons[200:])
    dv = [r["metrics"]["dollar_volume"] for r in rows[:200]]
    assert dv == sorted(dv, reverse=True)
    biggest_move = max((s for s in daily if s not in {r["symbol"] for r in rows[:200]}),
                       key=lambda s: abs(daily[s][-2]["c"] / daily[s][-3]["c"] - 1))
    assert biggest_move in {r["symbol"] for r in rows}


def test_univers_actions_ignore_le_jour_meme():
    daily = stock_daily()
    before = U.stock_universe(daily, DAY)
    for rows in daily.values():  # bougie du jour D (et du lendemain) : volume énorme sur une valeur obscure
        rows.append({"t": CO.ny_midnight(DAY), "o": 1, "h": 1, "l": 1, "c": 1, "v": 1e12})
        rows.append({"t": CO.ny_midnight(DAY + timedelta(days=1)), "o": 1, "h": 1, "l": 1, "c": 1, "v": 1e12})
    assert U.stock_universe(daily, DAY) == before


def test_valeur_sans_echange_la_veille_ecartee():
    daily = stock_daily(n_symbols=5)
    daily["S000"] = daily["S000"][:-2]  # ni le jour même ni la veille
    rows = U.stock_universe(daily, DAY)
    assert "S000" not in {r["symbol"] for r in rows} and len(rows) == 4


def test_univers_crypto_top_50_veille_utc():
    rnd = random.Random(5)
    cutoff = CO.utc_midnight(DAY)
    daily = {f"C{i:02d}-USDT": [{"t": cutoff - k * 86400, "o": 1, "h": 1, "l": 1, "c": 1.0 + i,
                                 "v": float(rnd.randint(1, 10**6))} for k in range(25, 0, -1)] for i in range(80)}
    rows = U.crypto_universe(daily, DAY)
    assert len(rows) == 50 and rows[0]["rank"] == 1
    vols = [r["metrics"]["dollar_volume"] for r in rows]
    assert vols == sorted(vols, reverse=True)
    for i, rows_ in enumerate(daily.values()):
        rows_.append({"t": cutoff, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1e15 * (i % 3)})
    assert U.crypto_universe(daily, DAY) == rows


def test_univers_enregistre_depuis_la_base(tmp_path):
    db = LabDB(str(tmp_path / "lab.db"))
    for s, rows in stock_daily(n_symbols=10).items():
        db.put_bars("stock", s, 1440, rows)
    rows = U.build(db, "stock", DAY)
    assert len(rows) == 10 and db.universe("stock", DAY.isoformat())[0]["symbol"] == rows[0]["symbol"]


def test_regime_de_marche():
    rows = [{"t": i * 86400, "c": 100 + i} for i in range(60)]
    r = U.regime(rows, 60 * 86400, 252)
    assert r["above_ma50"] and r["dist_ma50_pct"] > 0 and r["vol20_pct"] > 0
    assert U.regime(rows[:30], 60 * 86400, 252) is None
    assert U.regime(rows, 10 * 86400, 252) is None  # rien après la décision
