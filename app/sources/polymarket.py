"""Polymarket : cotes des marchés « décision de la Fed », sans clé, en lecture seule.

Demande de Simão (25 octobre 2026, fil « Évaluer l'IA Argona sur Polymarket ») : tester les cotes Polymarket comme
donnée pour les plans d'actions, pas pour parier (Polymarket est bloqué en France). Une cote est un prix daté :
rejouable sans regard vers l'avenir, heure par heure.

Sources (API publiques, sans compte) :
- https://gamma-api.polymarket.com/events : événements et leurs marchés. Un événement « Fed decision in <mois> »
  regroupe des marchés oui/non, un par issue (« No change », « 25 bps decrease », « 50+ bps decrease »,
  « 25+ bps increase »…) ; `clobTokenIds` (liste JSON en texte) donne le jeton du « Yes » en premier.
- https://clob.polymarket.com/prices-history : historique du prix d'un jeton, {"history": [{"t": epoch, "p": prix}]}.
  Pour un marché fermé, `interval=max` peut revenir vide : on demande des tranches startTs/endTs (CHUNK_S) au pas
  `fidelity` en minutes.
"""
import json
import re

from app.core.http import SourceError, get_json

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
TAGS = ("fed-rates", "fed")
CHUNK_S = 7 * 86400
FIDELITY_MIN = 60
MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
          "november", "december")
TITLE_RE = re.compile(r"\bfed(?:eral reserve)?\b.*\bdecision\b.*\bin\s+(" + "|".join(MONTHS) + r")\b(?:\s+(\d{4}))?",
                      re.IGNORECASE)
BPS_RE = re.compile(r"(\d+)\s*\+?\s*(?:bps|bp|basis points?)\s*(decrease|cut|increase|hike|raise)", re.IGNORECASE)
NO_CHANGE_RE = re.compile(r"\bno change\b|\bpause\b|\bhold\b|\bunchanged\b", re.IGNORECASE)


async def events(params: dict) -> list[dict]:
    data = await get_json(f"{GAMMA}/events", params=params)
    if not isinstance(data, list):
        raise SourceError("liste d'événements attendue")
    return data


async def history(token: str, start_ts: int, end_ts: int) -> list[tuple[int, float]]:
    data = await get_json(f"{CLOB}/prices-history", params={"market": token, "startTs": start_ts, "endTs": end_ts,
                                                             "fidelity": FIDELITY_MIN})
    return parse_history(data)


def parse_history(data) -> list[tuple[int, float]]:
    """{"history": [{"t": 1700000000, "p": 0.42}, …]} → [(t, p)] triés, prix dans [0, 1] seulement."""
    if not isinstance(data, dict) or not isinstance(data.get("history"), list):
        raise SourceError("historique de prix illisible")
    out = []
    for row in data["history"]:
        try:
            t, p = int(row["t"]), float(row["p"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0.0 <= p <= 1.0:
            out.append((t, p))
    return sorted(set(out))


def meeting_month(title: str | None) -> tuple[int, int | None] | None:
    """« Fed decision in December? » → (12, None) ; « Fed decision in March 2025? » → (3, 2025)."""
    m = TITLE_RE.search(title or "")
    if not m:
        return None
    return MONTHS.index(m.group(1).lower()) + 1, int(m.group(2)) if m.group(2) else None


def bps(label: str | None) -> int | None:
    """Issue d'un marché en points de base signés : « 25 bps decrease » → −25, « No change » → 0, « 25+ bps
    increase » → +25 (un « + » compte pour sa borne basse), None si illisible."""
    text = label or ""
    m = BPS_RE.search(text)
    if m:
        size = int(m.group(1))
        return -size if m.group(2).lower() in ("decrease", "cut") else size
    return 0 if NO_CHANGE_RE.search(text) else None


def yes_token(market: dict) -> str | None:
    """Jeton du « Yes » : premier de clobTokenIds (texte JSON ou liste), à condition que l'issue soit « Yes »."""
    tokens, outcomes = market.get("clobTokenIds"), market.get("outcomes")
    try:
        tokens = json.loads(tokens) if isinstance(tokens, str) else tokens
        outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
    except ValueError:
        return None
    if not tokens or not isinstance(tokens, list):
        return None
    if outcomes and str(outcomes[0]).strip().lower() != "yes":
        return None
    return str(tokens[0])


def outcomes(event: dict) -> list[dict]:
    """Marchés d'un événement « décision de la Fed » : {token, bps, label, question}. Issues illisibles écartées."""
    out = []
    for market in event.get("markets") or []:
        label = market.get("groupItemTitle") or market.get("question")
        value = bps(label)
        if value is None:
            value = bps(market.get("question"))
        token = yes_token(market)
        if value is None or token is None:
            continue
        out.append({"token": token, "bps": value, "label": str(label)[:80],
                    "question": str(market.get("question") or "")[:200]})
    return out
