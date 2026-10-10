"""Note composite d'un plan (sur 10) et verdict GO / NO GO.

Trois sous-notes, chacune avec ses raisons en une ligne :
- technique (40 %) : checklist EmperorBTC (tendance, EMA 200 4 h, Stoch RSI, VWAP, pivot, volume),
  RSI 14 jours dans une zone saine, R/R du plan (mesuré sur le TP2), bonus divergence et bougie ;
- catalyseur (30 %, actions seulement) : actus réelles et leur tonalité, résultats trimestriels
  récents ou imminents, augmentation de capital ;
- risque (30 %) : distance du stop en ATR, mur proche (TP1 trop près), espérance rejouée sur
  l'historique.
Sans catalyseur (crypto, et le labo qui note ses actions avec cat=None), la note se calcule sur les deux
autres. Action dont Finnhub n'a pas répondu (hors scanner : !plan, !avis, API) : catalyseur neutre 5,0 avec
son poids (« catalyseur non vérifié »), pour qu'une panne ne monte pas la note.

Veto : résultats trimestriels imminents ou date des résultats non vérifiée (calendrier Finnhub muet), R/R
trop bas. Le R/R n'est pas suivi à la lettre : un R/R jusqu'à `rr_tolerance` sous le minimum passe si la note
dépasse go_min + 0,5.

Le GO n'est pas une prédiction : le labo ne trouve pas la note prédictive (pente nulle ou négative en R par
point, stats.calibration). C'est une règle de sélection figée, la population que mesure la déclaration n° 7
(confirmation des GO) ; la changer changerait ce qui est mesuré. Les leçons confirmées du labo s'affichent à
côté (« note avec leçons ») sans toucher au verdict, à la note ni au tri.
"""
import time

from app.trading import indicators as I

CORE = ("trend_daily", "ema200_4h", "stoch_rsi", "vwap", "pivot", "volume")
BONUS = ("divergence", "candle")


def _clamp(x: float) -> float:
    return round(max(0.0, min(10.0, x)), 1)


def plan_rr(plan: dict) -> float:
    """R/R du plan : celui du TP2 (le TP1 sert à sécuriser, le TP3 est un bonus)."""
    targets = plan["targets"]
    return targets[min(1, len(targets) - 1)]["r_multiple"]


def technique(plan: dict, daily: list[dict] | None, s) -> tuple[float, list[str]]:
    items = {i["key"]: i for i in plan["checklist"]["items"]}
    known = [items[k] for k in CORE if items[k]["ok"] is not None]
    ok = sum(1 for i in known if i["ok"])
    reasons = [f"checklist {ok}/{len(known)}" + (f" (manque : {', '.join(i['label'].lower() for i in known if not i['ok'])})"
                                                 if ok < len(known) else "")]
    points, slots = float(ok), len(known)
    if daily and len(daily) > 15:
        rsi = I.rsi_series([c["c"] for c in daily])[-1]
        lo, hi = (s["rsi_min"], s["rsi_max"]) if plan["side"] == "long" else (100 - s["rsi_max"], 100 - s["rsi_min"])
        good = rsi is not None and lo <= rsi <= hi
        points += 1 if good else 0
        slots += 1
        reasons.append(f"RSI 14 j {rsi:.0f} {'dans' if good else 'hors de'} la zone {lo:.0f}-{hi:.0f}")
    rr = plan_rr(plan)
    points += min(rr / s["rr_min"], 1.0) if s["rr_min"] > 0 else 1.0  # rr_min ≤ 0 : aucun R/R exigé
    slots += 1
    reasons.append(f"R/R {rr:g} pour {s['rr_min']:g} visé")
    bonus = [items[k]["label"].replace(" (bonus)", "").lower() for k in BONUS if items[k]["ok"]]
    points += 0.5 * len(bonus)
    if bonus:
        reasons.append("bonus : " + ", ".join(bonus))
    return _clamp(10 * points / slots), reasons


def catalyst(cat: dict | None, side: str, s) -> tuple[float | None, list[str]]:
    if not cat or not cat.get("available"):
        return None, ["non évalué" + (f" ({'; '.join(cat['errors'])})" if cat and cat.get("errors") else "")]
    sign = 1 if side == "long" else -1
    score, reasons = 5.0, []
    if cat["fresh_news"]:
        score += 1
        reasons.append("actus fraîches (< 48 h)")
    pos, neg = cat["positive"], cat["negative"]
    if pos or neg:
        score += sign * (min(pos, 3) - 1.5 * min(neg, 3))
        reasons.append(f"titres : {pos} positif{'s' if pos > 1 else ''}, {neg} négatif{'s' if neg > 1 else ''}")
    elif not cat["news"]:
        reasons.append("aucune actu sur 7 jours")
    last = cat.get("last_earnings")
    if last and last["days_ago"] <= 5 and last.get("eps_surprise_pct") is not None:
        surprise = last["eps_surprise_pct"]
        if abs(surprise) >= 5:
            score += sign * (1.5 if surprise > 0 else -1.5)
        reasons.append(f"résultats il y a {last['days_ago']} j, BPA {surprise:+.0f} % vs attendu")
    if cat["dilution"]:
        score -= 3 * sign
        reasons.append("augmentation de capital annoncée")
    if not cat.get("earnings_known", True):  # calendrier en échec : veto résultats impossible à vérifier
        score = min(score, 5.0)
        reasons.append("date des résultats non vérifiée (Finnhub sans réponse)")
    hours = cat.get("hours_to_earnings")
    if hours is not None and s["earnings_veto_hours"] and hours <= s["earnings_veto_hours"]:
        score = min(score, 2.0)
        reasons.append(f"résultats dans {hours:.0f} h")
    elif hours is not None:
        reasons.append(f"prochains résultats dans {hours / 24:.0f} j")
    return _clamp(score), reasons


def risk(plan: dict, daily: list[dict] | None, s) -> tuple[float, list[str]]:
    score, reasons = 10.0, []
    # ATR nul ou absent (ex. arrondi à 6 décimales d'une crypto à 0,00001) : distance en ATR non mesurable.
    stop_atr = abs(plan["entry"] - plan["stop"]) / plan["atr"] if plan.get("atr") else None
    if stop_atr is None:
        reasons.append("stop en ATR non mesurable (ATR indisponible)")
    elif stop_atr > s["max_stop_atr"]:
        score -= 2
        reasons.append(f"stop large ({stop_atr:.1f} ATR)")
    else:
        reasons.append(f"stop à {stop_atr:.1f} ATR")
    tp1 = plan["targets"][0]
    if tp1["r_multiple"] < s["min_tp1_r"]:
        score -= 2
        reasons.append(f"mur proche : TP1 à {tp1['r_multiple']:g} R")
    if tp1["expectancy_r"] < 0:
        score -= 2
        reasons.append(f"espérance TP1 rejouée {tp1['expectancy_r']:+.2f} R")
    elif tp1["hit_rate"] < 0.3:
        score -= 1
        reasons.append(f"TP1 touché avant le stop dans {tp1['hit_rate']:.0%} des cas passés")
    else:
        reasons.append(f"TP1 avant le stop dans {tp1['hit_rate']:.0%} des cas passés")
    if plan["stop_first_rate"] > 0.6:
        score -= 1
        reasons.append(f"stop touché en premier dans {plan['stop_first_rate']:.0%} des cas")
    return _clamp(score), reasons


def evaluate(plan: dict, daily: list[dict] | None, cat: dict | None, s, lessons: dict | None = None) -> dict:
    """Note composite, sous-notes et verdict GO / NO GO avec ses raisons. `lessons` : ajustement des
    leçons confirmées du labo (app/lab/lessons.adjustment), en direct seulement : affiché à part
    (`composite_lecons`), sans effet sur la note, les vetos ni le verdict."""
    tech, tech_r = technique(plan, daily, s)
    cata, cata_r = catalyst(cat, plan["side"], s) if plan["market"] == "stock" else (None, ["non évalué pour la crypto"])
    if plan["market"] == "stock" and cat is not None and cata is None:  # Finnhub muet : neutre, pas de renormalisation
        cata, cata_r = 5.0, ["catalyseur non vérifié" + (f" ({'; '.join(cat['errors'])})" if cat.get("errors") else "")]
    rsk, rsk_r = risk(plan, daily, s)
    w = s["weights"]
    parts = [(tech, w["technique"]), (rsk, w["risk"])] + ([(cata, w["catalyst"])] if cata is not None else [])
    composite = round(sum(v * k for v, k in parts) / sum(k for _, k in parts), 1)
    adjusted = _clamp(composite + lessons["ajustement"]) if lessons else None
    vetoes, notes = [], []
    rr = plan_rr(plan)
    if rr < s["rr_min"]:
        if rr >= s["rr_min"] - s["rr_tolerance"] and composite >= s["go_min"] + 0.5:
            notes.append(f"R/R {rr:g} toléré sous {s['rr_min']:g} : le reste suit")
        else:
            vetoes.append(f"R/R {rr:g} sous le minimum de {s['rr_min']:g}")
    hours = (cat or {}).get("hours_to_earnings")
    if plan["market"] == "stock" and hours is not None and s["earnings_veto_hours"] and hours <= s["earnings_veto_hours"]:
        vetoes.append(f"résultats trimestriels dans {hours:.0f} h")
    # cat=None (le labo : generator, live) : rien à vérifier ; un catalyseur lu sans calendrier, si.
    if plan["market"] == "stock" and cat is not None and s["earnings_veto_hours"] and not cat.get("earnings_known", True):
        vetoes.append("date des résultats non vérifiée (Finnhub sans réponse)")
    if composite < s["go_min"]:
        vetoes.append(f"note {composite:g} sous {s['go_min']:g}")
    out = {
        "composite": composite, "go": not vetoes, "decision": "GO" if not vetoes else "NO GO",
        "reasons": vetoes or notes, "go_min": s["go_min"],
        "parts": {
            "technique": {"score": tech, "weight": w["technique"], "reasons": tech_r},
            "catalyst": {"score": cata, "weight": w["catalyst"] if cata is not None else 0, "reasons": cata_r},
            "risk": {"score": rsk, "weight": w["risk"], "reasons": rsk_r},
        },
    }
    if lessons:
        out["lecons"] = lessons
        out["composite_lecons"] = adjusted
    return out


# --- Note non prédictive : la pente mesurée au labo, à côté de chaque GO affiché -------------------

SLOPE_TTL = 600.0
_slopes: dict = {"key": None, "at": 0.0, "data": {}}


def lab_slopes() -> dict:
    """Pente de la note en R par point, par marché, lue dans le cache des statistiques du labo
    (stats.calibration, écrit par le worker) ; relue au plus toutes les SLOPE_TTL s. {} sans labo."""
    from app.lab import db as lab_db  # import tardif : le labo importe ce module

    key, now = lab_db.db_path(), time.monotonic()
    if _slopes["key"] == key and now - _slopes["at"] < SLOPE_TTL:
        return _slopes["data"]
    data = {}
    try:
        conn = lab_db.open_readonly(key)
        if conn is not None:
            try:
                row = conn.execute(
                    "SELECT json_extract(data, '$.marches.stock.calibration_note.pente_r_par_point'), "
                    "json_extract(data, '$.marches.crypto.calibration_note.pente_r_par_point') "
                    "FROM stats_cache WHERE key = 'courant'").fetchone()
            finally:
                conn.close()
            if row:
                data = {m: v for m, v in zip(("stock", "crypto"), row) if v is not None}
    except Exception:  # base ancienne ou illisible : rien à dire
        data = {}
    _slopes.update(key=key, at=now, data=data)
    return data


def slope_note(market: str) -> str | None:
    """« note non prédictive au labo (pente −0,020 R/pt, actions) » quand la pente mesurée est ≤ 0 ;
    None si elle est positive ou pas encore mesurée."""
    slope = lab_slopes().get(market)
    if slope is None or slope > 0:
        return None
    value = f"{slope:+.3f}".replace(".", ",").replace("-", "−")
    return f"note non prédictive au labo (pente {value} R/pt, {'actions' if market == 'stock' else 'crypto'})"
