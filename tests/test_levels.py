"""Tests hors ligne de la carte des niveaux (app/trading/levels.py) : zones citées, ventes, stop suiveur, entrées
et renforts en prix."""
from datetime import datetime, timezone

from app.trading import levels as L

NOW = datetime(2026, 10, 7, 19, 45, tzinfo=timezone.utc)  # mercredi 15 h 45 à New York
T0 = int(datetime(2026, 10, 1, 13, 30, tzinfo=timezone.utc).timestamp())


def pt(price, source="sommet 15 min", rank=L.RANK_SWING, at=None):
    at = T0 if at is None else at
    return L.Point(price, source, rank, (at, at + 900))


def zone(low, high=None, strong=True, contacts=2, sources=("sommet 15 min du 1er oct.",)):
    return L.Zone(low, low if high is None else high, list(sources), contacts, strong)


def card(zones, atr=1.0, daily_atr=4.0, bars=None, market="stock"):
    return {"atr": atr, "daily_atr": daily_atr, "zones": sorted(zones, key=lambda z: z.low), "bars": bars or [],
            "market": market}


def test_zone_solide_avec_deux_contacts_ou_un_niveau_journalier():
    zones = L.merge([pt(110.0), pt(110.3, at=T0 + 86400), pt(105.0, at=T0 + 3 * 86400),
                     L.Point(120.0, "sommet journalier du 5 juin", L.RANK_DAILY, (T0, T0 + 86400)),
                     L.Point(100.0, "VWAP du jour", L.RANK_DAY)], width=0.5)
    by_low = {z.low: z for z in zones}
    assert [z.low for z in zones] == [100.0, 105.0, 110.0, 120.0]
    assert by_low[110.0].strong and by_low[110.0].contacts == 2 and by_low[110.0].high == 110.3
    assert not by_low[105.0].strong       # un sommet isolé de 15 min ne décide d'aucun prix
    assert by_low[120.0].strong           # niveau journalier
    assert not by_low[100.0].strong       # le VWAP est affiché, pas utilisé (council du 2026-10-08)
    assert by_low[110.0].text() == "110–110.3 (sommet 15 min, 2 contacts)"


def test_deux_contacts_a_moins_d_une_heure_n_en_font_qu_un():
    z = L.merge([pt(110.0), pt(110.1, at=T0 + 1800)], width=0.5)[0]
    assert z.contacts == 1 and not z.strong


def test_ventes_juste_avant_les_resistances_solides_puis_paliers():
    m = card([zone(100.2), zone(103.0), zone(103.5), zone(104.5, strong=False), zone(108.0, 108.4), zone(95.0)])
    sales = L.sales(m, "long", 100.0, 3)
    # 100,2 est collé au prix (moins de 0,5 ATR), 103,5 trop près de la vente à 102,9 (moins d'un quart d'ATR
    # journalier), 104,5 n'est pas solide.
    assert [s["price"] for s in sales] == [102.9, 107.9, 108.9]
    assert sales[2]["zone"] is None and "aucune résistance" in L.sale_source(sales[2], "long")
    assert L.sale_source(sales[0], "long").startswith("sous 103 (")
    short = L.sales(m, "short", 100.0, 2)
    assert [s["price"] for s in short] == [95.1, 94.1]


def test_stop_suiveur_sous_le_dernier_creux_et_jamais_redescendu():
    closes = [100, 101, 102, 103, 102, 101, 100.5, 101.5, 103, 104, 105, 106, 107, 108, 109, 110]
    bars = [{"t": T0 + i * 900, "o": c, "h": c + 0.3, "l": c - 0.3, "c": c, "v": 1.0} for i, c in enumerate(closes)]
    bars.append({"t": T0 + len(closes) * 900, "o": 110, "h": 110, "l": 110, "c": 110, "v": 0, "pending": True})
    m = card([zone(100.0, 100.4, sources=("creux 15 min du 1er oct.",))], bars=bars)
    stop, why = L.trailing_stop(m, "long", 110.0, None)
    # Dernier creux 100,2 (bougie à 100,5) dans la zone 100–100,4 : stop sous toute la zone, marge 0,25 ATR.
    assert stop == 99.75 and "dernier creux 15 min, 100.2" in why and "sous sa zone 100–100.4" in why
    assert L.trailing_stop(m, "long", 110.0, 105.0) == (105.0, None)   # jamais redescendu
    assert L.trailing_stop(m, "long", 110.0, 98.0)[0] == 99.75          # remonté


def test_entrees_sur_repli_et_sur_cassure_avec_leur_rr():
    m = card([zone(98.0, 98.5), zone(101.0), zone(104.0), zone(107.0), zone(110.0)])
    got = {e["kind"]: e for e in L.entries(m, "long", 100.0, 98.0, 1.8)}
    assert got["maintenant"]["entry"] == 100.0 and got["maintenant"]["stop"] == 98.0
    assert got["maintenant"]["rr"] == [0.5, 2.0, 3.5] and got["maintenant"]["ok"]  # R/R mesuré sur la 2e vente
    # Repli : limite juste au-dessus du support, stop sous la zone ramené à 1 ATR.
    assert got["repli"]["entry"] == 98.6 and got["repli"]["stop"] == 97.6 and got["repli"]["zone"].low == 98.0
    # Cassure : stop d'achat juste au-dessus de la résistance, stop sous la zone cassée (1 ATR).
    assert got["cassure"]["entry"] == 101.1 and got["cassure"]["stop"] == 100.1
    assert got["cassure"]["targets"][0]["price"] == 103.9 and got["cassure"]["ok"]


def test_renfort_jamais_sous_le_prix_d_entree():
    m = card([zone(98.0, 98.5), zone(101.0), zone(104.0), zone(107.0), zone(110.0)])
    kinds = [c["kind"] for c in L.adds(m, "long", 100.0, 97.0, 1.8, opened=95.0)]
    assert kinds == ["repli", "cassure"]
    assert [c["kind"] for c in L.adds(m, "long", 100.0, 97.0, 1.8, opened=99.0)] == ["cassure"]
    breakout = L.adds(m, "long", 100.0, 97.0, 1.8)[-1]
    assert breakout["new_stop"] == 100.1  # le stop de toute la position passe sous la zone cassée
    # Un support sous le stop ne sert pas de renfort : un repli jusque-là sortirait la position.
    assert all(c["kind"] != "repli" for c in L.adds(m, "long", 100.0, 98.2, 1.8))


def test_carte_sur_des_bougies():
    # Oscillation entre 100 et 106 en bougies de 15 min : sommets et creux répétés = zones solides.
    step = 900
    start = int(NOW.timestamp()) - 400 * step
    bars = []
    for i in range(400):
        c = 100 + 6 * abs((i % 24) - 12) / 12
        bars.append({"t": start + i * step, "o": c, "h": c + 0.2, "l": c - 0.2, "c": c, "v": 1.0})
    m = L.build(bars, None, "stock", NOW)
    assert m["atr"] > 0 and m["daily_atr"] is None
    strong = [z for z in m["zones"] if z.strong]
    assert any(z.low <= 106.2 <= z.high + 0.01 for z in strong) and any(z.low <= 99.8 <= z.high + 0.01 for z in strong)
    assert all(a.low <= b.low for a, b in zip(m["zones"], m["zones"][1:]))
    assert any("sommet 15 min du" in s for z in strong for s in z.sources)


def test_jour_en_francais_et_prix_d_ordre():
    t = int(datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc).timestamp())
    assert L.day_fr(t, "stock") == "1er oct." and L.day_fr(t + 6 * 86400, "crypto") == "7 oct."
    assert L.tick(123.456, "stock") == 123.46 and L.tick(0.123456, "stock") == 0.1235
    assert L.tick(0.0000123456, "crypto") == 0.0000123456
