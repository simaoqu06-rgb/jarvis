"""Checklist d'entrée et signaux de sortie anticipée, d'après la formation EmperorBTC.

Entrée (« Trident de trading ») : la tendance de fond dans le sens du trade (MM 50 et 200 jours,
EMA 200 en 4 h), puis le déclencheur de day trading : Stoch RSI qui croise, VWAP cassé, pivot tenu
ou cassé, avec du volume. Bonus : divergence RSI et bougie de retournement.

Sortie : « un renversement n'est pas un signal de trade inverse, c'est un signal de prise de
bénéfices » : divergence RSI contraire, anomalie corps/volume (Weis), mouvement sans volume,
bougie de retournement contre le trade.
"""
from datetime import datetime

from app.trading import indicators as I


def _item(key: str, label: str, ok: bool | None, detail: str) -> dict:
    return {"key": key, "label": label, "ok": ok, "detail": detail}


def checklist(bars: list[dict], daily: list[dict] | None, h4: list[dict] | None, market: str, side: str,
              atr_value: float, now: datetime | None = None) -> dict:
    """`now` : heure du calcul (maintenant par défaut) ; avant l'ouverture US, la séance du jour
    n'a pas commencé : pas de VWAP du jour, pivots tirés de la dernière séance complète."""
    long = side == "long"
    sign = 1 if long else -1
    last = bars[-1]
    price = last["c"]
    items = []

    # 1. Tendance de fond : « tradez dans le sens du marché », « ne détenez rien sous la MM 200 jours ».
    if daily and len(daily) >= 50:
        closes = [c["c"] for c in daily]
        m50, m200 = I.sma(closes, 50), I.sma(closes, 200)
        ok = (price - m50) * sign > 0 and (m200 is None or (price - m200) * sign > 0)
        detail = f"prix {'au-dessus' if price > m50 else 'en dessous'} de la MM 50 jours ({m50:.4g})"
        if m200:
            detail += f" et {'au-dessus' if price > m200 else 'en dessous'} de la MM 200 jours ({m200:.4g})"
        items.append(_item("trend_daily", "Tendance journalière", ok, detail))
    else:
        items.append(_item("trend_daily", "Tendance journalière", None, "historique journalier indisponible"))

    if h4 and len(h4) >= 200:
        e200 = I.ema_series([c["c"] for c in h4], 200)[-1]
        ok = (price - e200) * sign > 0
        items.append(_item("ema200_4h", "EMA 200 en 4 h", ok,
                           f"prix {'au-dessus' if price > e200 else 'en dessous'} de l'EMA 200 4 h ({e200:.4g})"))
    else:
        items.append(_item("ema200_4h", "EMA 200 en 4 h", None, "historique 4 h indisponible"))

    # 2. Déclencheur de day trading : Stoch RSI + VWAP + pivot (les trois, dans n'importe quel ordre).
    closes = [c["c"] for c in bars]
    k, d = I.stoch_rsi(closes)
    crossed = any((k[-i] - d[-i]) * sign > 0 and (k[-i - 1] - d[-i - 1]) * sign <= 0 for i in range(1, 4))
    items.append(_item("stoch_rsi", "Stoch RSI", crossed and (k[-1] - d[-1]) * sign > 0,
                       f"K {k[-1]:.0f} / D {d[-1]:.0f}, croisement {'haussier' if long else 'baissier'} "
                       f"{'récent' if crossed else 'absent sur les 3 dernières bougies'}"))

    vw = I.vwap(bars, market, now)
    if vw:
        items.append(_item("vwap", "VWAP de la séance", (price - vw) * sign > 0,
                           f"prix {'au-dessus' if price > vw else 'en dessous'} du VWAP ({vw:.4g})"))
    else:
        items.append(_item("vwap", "VWAP de la séance", None,
                           "séance du jour pas encore ouverte" if I.current_session(bars, market, now)
                           != I.session_key(bars[-1]["t"], market) else "pas de volume sur la séance"))

    prev = I.previous_session(bars, market, now)
    if prev:
        levels = I.pivots(prev)
        held = broke = None
        for name, lvl in levels.items():
            recent = bars[-3:]
            touched = any(abs((c["l"] if long else c["h"]) - lvl) <= 0.3 * atr_value for c in recent)
            if touched and (price - lvl) * sign > 0:
                held = name
            if (bars[-4]["c"] - lvl) * sign <= 0 < (price - lvl) * sign:
                broke = name
        ok = bool(held or broke)
        detail = (f"{'support' if long else 'résistance'} {held} tenu" if held else
                  f"{'résistance' if long else 'support'} {broke} cassé" if broke else
                  "ni pivot tenu ni pivot cassé sur les 3 dernières bougies")
        items.append(_item("pivot", "Points pivots", ok, detail))
    else:
        items.append(_item("pivot", "Points pivots", None, "séance précédente indisponible"))

    # 3. Volume : la hausse (baisse) doit être accompagnée d'un volume supérieur à la moyenne 14.
    rv = I.relative_volume(bars)
    moving = (price - bars[-2]["c"]) * sign > 0
    items.append(_item("volume", "Volume", bool(rv and rv >= 1 and moving),
                       f"dernière bougie à {rv:.1f}× le volume moyen 14" if rv else "volume indisponible"))

    # Bonus : divergence et bougies de retournement / absorption.
    div = I.rsi_divergence(bars)
    want = "haussière" if long else "baissière"
    items.append(_item("divergence", "Divergence RSI (bonus)", True if div == want else None,
                       f"divergence {div} récente" if div else "aucune divergence récente"))
    pattern = None
    for prev_c, c in zip(bars[-4:-1], bars[-3:]):
        eng = I.engulfing(prev_c, c)
        if eng == ("haussier" if long else "baissier"):
            pattern = f"engulfing {eng}"
        elif long and I.is_hammer(c):
            pattern = "marteau"
        elif not long and I.is_shooting_star(c):
            pattern = "étoile filante"
    items.append(_item("candle", "Bougie de retournement (bonus)", True if pattern else None,
                       f"{pattern} sur les 3 dernières bougies" if pattern else "aucune"))

    core = [i for i in items if i["key"] in ("trend_daily", "ema200_4h", "stoch_rsi", "vwap", "pivot", "volume")]
    score = sum(1 for i in core if i["ok"])
    by_key = {i["key"]: i for i in items}
    trigger = all(by_key[k]["ok"] for k in ("stoch_rsi", "vwap", "pivot"))
    if by_key["trend_daily"]["ok"] is False:
        verdict = "Contre la tendance de fond"
    elif trigger:
        verdict = "Feu vert : déclencheur complet"
    else:
        verdict = "Dans la tendance, déclencheur incomplet : attendre"
    return {"items": items, "score": score, "max_score": len(core), "trigger": trigger, "verdict": verdict}


def exit_signals(bars: list[dict], side: str) -> list[tuple[str, str]]:
    """Signes de retournement sur les dernières bougies closes : (type, explication)."""
    long = side == "long"
    sign = 1 if long else -1
    out = []
    div = I.rsi_divergence(bars)
    if div == ("baissière" if long else "haussière"):
        out.append(("divergence", f"divergence RSI {div} : l'élan faiblit"))
    if (I.weis_anomaly(bars) if long else I.weis_anomaly_short(bars)):
        out.append(("weis", "anomalie corps/volume : gros volume, petit corps, longue mèche contre toi"))
    recent = bars[-3:]
    vols = [c["v"] for c in bars]
    avg = sum(vols[-17:-3]) / 14 if len(vols) >= 17 else None
    if avg and all((c["c"] - c["o"]) * sign > 0 and c["v"] < avg for c in recent):
        out.append(("low_volume", "le prix avance sans volume (3 bougies sous la moyenne) : participation faible"))
    last, prev = bars[-1], bars[-2]
    eng = I.engulfing(prev, last)
    rv = I.relative_volume(bars) or 0
    if eng == ("baissier" if long else "haussier") and rv >= 1:
        out.append(("reversal_candle", f"engulfing {eng} avec du volume"))
    elif long and I.is_shooting_star(last) and rv >= 1:
        out.append(("reversal_candle", "étoile filante avec du volume"))
    elif not long and I.is_hammer(last) and rv >= 1:
        out.append(("reversal_candle", "marteau avec du volume"))
    return out
