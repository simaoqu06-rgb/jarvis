"""Le pire et le meilleur de tous les ordres (journal de Killmode, quatrième avis de GPT) contre un oracle écrit
sans le code du solde : chaque chemin de prix compatible avec les bougies d'1 min, pas à pas d'un centime.

L'oracle suit les hypothèses d'exécution du journal : l'ordre stop d'achat se remplit à l'entrée quand le chemin
l'atteint, à l'ouverture quand une minute ouvre au-dessus (minutes avant `until`) ; un objectif part à son prix
quand le chemin le dépasse de k pas (k = 1, 2 pour `r_2t`), au prix d'entrée s'il est déjà dépassé quand l'achat
se remplit à l'ouverture, à son prix quand une minute suivante ouvre au-delà ; le stop passe à l'entrée au TP1 ;
une sortie au stop se fait à son prix, ou à l'ouverture d'une minute qui ouvre dessous ; le reste part à la
dernière clôture. Dans une minute : toute marche de ±1 c de l'ouverture à la clôture qui reste dans [plus bas,
plus haut] et touche les deux (aucun prix sauté). Le solde (`settle`) doit rendre, à la taille de Simão (brackets
inégaux, 1 à 40 actions), le minimum (`r_20e`, `r_2t`) et le maximum (`r_fav`) des issues de l'oracle.
"""
import random

import pytest

from app.trading import killmode as K
from tests.test_lab_strategies import DAY, OPEN

T0 = OPEN + 31 * 60


def oracle(bars, entry, stop, tps, until, k=1):
    """Issues possibles (prix et nature de la sortie de chaque tiers) ; None si l'achat peut ne pas partir. Prix en
    centimes."""
    states = {("flat",)}

    def level(hit):
        return stop if hit == 0 else entry

    def targets(fill, hit, ex, p, price_of):
        while hit < 3 and p >= tps[hit] + k:
            ex, hit = ex + ((price_of(hit), "tp"),), hit + 1
        return ("closed", ex) if hit == 3 else ("open", fill, hit, ex)

    def step(st, p, active, posted_beyond):
        if st[0] == "closed":
            return st
        if st[0] == "flat":
            return targets(entry, 0, (), p, lambda h: tps[h]) if active and p >= entry else st
        _, fill, hit, ex = st
        if p <= level(hit):
            return "closed", ex + ((level(hit), "stop"),) * (3 - hit)
        return targets(fill, hit, ex, p, lambda h: fill if posted_beyond and tps[h] <= fill else tps[h])

    for t, o, h, lo, c in bars:
        active, nxt = t < until, set()
        for st in states:
            if st[0] == "closed":
                nxt.add(st)
                continue
            if st[0] == "flat":
                s0 = targets(o, 0, (), o, lambda _: o) if active and o >= entry else st
            else:
                _, fill, hit, ex = st
                if o <= level(hit):
                    nxt.add(("closed", ex + ((o, "stop"),) * (3 - hit)))
                    continue
                s0 = targets(fill, hit, ex, o, lambda j: tps[j])
            beyond = st[0] == "flat" and s0[0] != "flat"  # achat rempli à cette ouverture
            todo = [(o, o == h, o == lo, s0)]
            seen = set(todo)
            while todo:
                p, vh, vl, s = todo.pop()
                if vh and vl and (p == c or s[0] == "closed"):
                    nxt.add(s)
                    if s[0] == "closed":
                        continue
                for q in (p - 1, p + 1):
                    if lo <= q <= h:
                        node = (q, vh or q == h, vl or q == lo, step(s, q, active, beyond))
                        if node not in seen:
                            seen.add(node)
                            todo.append(node)
        states = nxt
    out = set()
    for st in states:
        if st[0] == "flat":
            return None
        out.add(st[1] if st[0] == "closed" else st[3] + ((bars[-1][4], "close"),) * (3 - st[2]))
    return out


def fill_of(bars, entry, until):
    for t, o, h, _, _ in bars:
        if t >= until:
            return None
        if o >= entry:
            return o
        if h >= entry:
            return entry
    return None


def sequence(rng):
    """Une séance tirée au hasard autour des niveaux qui comptent (entrée, stop, objectifs, à un pas près)."""
    entry = rng.randint(300, 9000)
    risk = rng.choice([rng.randint(3, 10), rng.randint(5, 40)])
    stop, tps = entry - risk, [entry + risk, entry + 2 * risk, entry + 3 * risk]
    up = [entry - 1, entry, entry + 1] + [x + d for x in tps for d in (-1, 0, 1, 2, 3)]
    dn = [stop - 1, stop, stop + 1, entry - 1, entry, entry + 1, tps[0] - 1, tps[0], tps[0] + 1, tps[1]]
    n = rng.randint(2, 7)
    p = entry - rng.choice([0, 1, rng.randint(0, risk), rng.randint(0, 2 * risk)])
    if rng.random() < 0.15:
        p = rng.choice([entry, entry + 1, tps[0] + 1, tps[0] + 2, tps[1] + 1, tps[0] + rng.randint(0, 2 * risk)])
    bars = []
    for i in range(n):
        o = p
        if i:
            u = rng.random()
            o = p if u < 0.5 else p + rng.choice([-1, 1]) if u < 0.65 else p + rng.randint(-2 * risk, 2 * risk)
            if rng.random() < 0.2:
                o = rng.choice(dn + up + [stop - rng.randint(1, risk), entry - rng.randint(1, 2 * risk)])
        c = o + rng.randint(-3 * risk // 2, 3 * risk // 2)
        if rng.random() < 0.25:
            c = rng.choice(dn + up)
        hi, lo = max(o, c), min(o, c)
        h = hi + rng.choice([0, 0, rng.randint(0, risk), rng.randint(0, 3 * risk // 2)])
        low = lo - rng.choice([0, 0, rng.randint(0, risk), rng.randint(0, 3 * risk // 2)])
        if rng.random() < 0.5:
            h = rng.choice([x for x in up if x >= hi] or [h])
        if rng.random() < 0.5:
            low = rng.choice([x for x in dn if x <= lo] or [low])
        low = max(low, 100)
        o, c = max(o, low), max(c, low)
        bars.append((T0 + 60 * i, o, max(h, o, c), low, c))
        p = c
    until = T0 + 60 * rng.choice([n + 1, n + 1, rng.randint(1, n)])
    return entry, stop, tps, bars, until


def cents(x):
    return round(x / 100, 2)


def at_size(call, rate, fill, outcome):
    exits = [{"prix": cents(p), "part": 1 / 3, "raison": {"tp": "tp1", "stop": "stop", "close": "cloture"}[kind]}
             for p, kind in outcome]
    return K._at_size(call, {"declenche": True, "eurusd": rate, "plan_ibkr": "fixe", "remplissage": cents(fill),
                             "remplissage_mesure": False, "sorties": exits})


@pytest.mark.parametrize("seed", range(4))
def test_pire_et_meilleur_de_tous_les_ordres_contre_l_oracle(seed):
    rng = random.Random(seed)
    checked = 0
    for _ in range(250):
        entry, stop, tps, bars, until = sequence(rng)
        q = rng.choice([1, 2, 4, 5, 11, 40])
        risk = cents(entry) - cents(stop)
        rate = (q + 0.5) * risk / K.RISK_EUR  # q actions à 20 € de risque
        assert K.shares(risk, rate) == q
        call = {"symbol": "ZZZ", "day": DAY.isoformat(), "at": T0 - 60, "price": cents(entry), "entry": cents(entry),
                "stop": cents(stop), "targets": [cents(t) for t in tps], "until": until,
                "close_ts": T0 + 60 * len(bars), "prev_close": None, "adv20": None}
        res = K.settle(call, [{"t": t, "o": cents(o), "h": cents(h), "l": cents(lo), "c": cents(c), "v": 100.0}
                              for t, o, h, lo, c in bars], eurusd=rate, plan_ibkr="fixe")
        row = call | {"groupe": "kill", "resultat": res | {"eurusd": rate, "plan_ibkr": "fixe"}}
        fill = fill_of(bars, entry, until)
        for key, k in (("r_20e", 1), ("r_2t", 2)):
            outcomes = oracle(bars, entry, stop, tps, until, k)
            if outcomes is None:
                assert not res["declenche"]
                continue
            vals = [at_size(call, rate, fill, v) for v in outcomes]
            assert K.value(row, key) == pytest.approx(min(vals), abs=2e-4), (key, q, entry, stop, bars, until)
            if key == "r_20e":
                assert K.value(row, "r_fav") == pytest.approx(max(vals), abs=2e-4), (q, entry, stop, bars, until)
                checked += 1
    assert checked > 200


def test_contre_exemple_a_de_gpt():
    """GPT, contre-exemple A : le pire local (objectif puis retour à l'entrée dans la minute) n'est pas le pire
    global. Entrée 100, stop 99, objectifs 101/102/103 ; minute 100,50 / 101,20 / 99,80 / 100,40, puis ouverture
    à 98,50. Garder la position (plus bas d'abord, puis TP1) mène au gap : −0,67 R brut, pas +0,33."""
    bars = [(T0, 9990, 10000, 9980, 10000), (T0 + 60, 10050, 10120, 9980, 10040), (T0 + 120, 9850, 9850, 9850, 9850)]
    call = {"symbol": "ZZZ", "day": DAY.isoformat(), "at": T0 - 60, "price": 99.9, "entry": 100.0, "stop": 99.0,
            "targets": [101.0, 102.0, 103.0], "until": T0 + 180, "close_ts": T0 + 180, "prev_close": None,
            "adv20": None}
    res = K.settle(call, [{"t": t, "o": cents(o), "h": cents(h), "l": cents(lo), "c": cents(c), "v": 100.0}
                          for t, o, h, lo, c in bars])
    gross = [round(sum(e["part"] * (e["prix"] - res["remplissage"]) for e in res[k]), 4)
             for k in ("sorties", "sorties_fav")]
    assert gross == [-0.6667, 0.3333]
    outcomes = oracle(bars, 10000, 9900, [10100, 10200, 10300], T0 + 180)
    assert sorted(round(sum(p for p, _ in v) / 3 - 10000, 2) for v in outcomes) == [-66.67, 33.33]  # centimes
