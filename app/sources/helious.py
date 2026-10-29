"""Helious : calendrier économique (seule source de JARVIS pour l'agenda macro).

Quota lu sur GET /v1/access le 29/10/2026 : 1 crédit par appel de données, remis à zéro le 1er du mois.
- sans clé : 30 appels par minute et 50 crédits par mois **partagés par l'adresse réseau** du serveur ;
- clé gratuite (HELIOUS_API_KEY, compte sur helious.io) : 60 appels par minute et 100 crédits par mois à JARVIS seul,
  envoyée en `Authorization: Bearer hel_…`.
Les actus de marché ne passent plus par Helious : sans abonnement, son flux a 60 min de retard, et ses 4 appels par
heure vidaient le quota du mois en une demi-journée (refus HTTP 429 vus le 29/10/2026). needs.economic_calendar relit
le calendrier toutes les 8 h avec la clé, une fois par jour sans. Après un refus (429), plus aucun appel pendant
REFUSED_PAUSE_S : le cockpit ne réessaie pas à chaque rafraîchissement. Mention « Helious » + lien demandée.
"""
import time

from app import config
from app.core.http import SourceError, get_json

BASE = "https://api.helious.io/v1"
KEY_NAME = "HELIOUS_API_KEY"
REFUSED_PAUSE_S = 1800
UNEXPECTED = "réponse inattendue de Helious (pas de data.events)"
# Toute la semaine en un crédit (vérifié le 29/10/2026 : 154 événements sur 9 jours, sans la coupe à 100 par défaut)
CALENDAR_PARAMS = {"days_back": 1, "days_ahead": 8, "limit": 300}
_paused_until = 0.0  # horloge monotone : fin de la pause après un refus


def _headers() -> dict | None:
    key = config.secret(KEY_NAME)
    return {"Authorization": f"Bearer {key}"} if key else None


def _events_missing(payload) -> str | None:
    """Réponse 200 sans calendrier : notée comme un échec dans la santé des sources (r_calendar la voit)."""
    data = payload.get("data") if isinstance(payload, dict) else None
    return None if isinstance(data, dict) and isinstance(data.get("events"), list) else UNEXPECTED


async def _get(path: str, params: dict | None = None) -> dict:
    global _paused_until
    left = _paused_until - time.monotonic()
    if left > 0:  # pas d'appel : la santé des sources garde le dernier refus
        raise SourceError(f"HTTP 429 (refus de Helious, prochain essai dans {max(1, round(left / 60))} min)")
    try:
        return await get_json(f"{BASE}/{path}", params=params, headers=_headers(), refusal=_events_missing)
    except SourceError as exc:
        # refus (gratuit chez Helious, inutile d'insister) ou réponse 200 illisible (un crédit facturé à chaque essai)
        if str(exc) in ("HTTP 429", UNEXPECTED, "réponse qui n'est pas du JSON"):
            _paused_until = time.monotonic() + REFUSED_PAUSE_S
        raise


async def economic_calendar() -> list[dict]:
    payload = await _get("calendar", CALENDAR_PARAMS)
    return [
        {
            "datetime_utc": e.get("datetime_utc"),
            "title": e.get("title"),
            "country": e.get("country"),
            "impact": e.get("impact"),
            "forecast": e.get("forecast"),
            "previous": e.get("previous"),
            "actual": e.get("actual"),
            "status": e.get("status"),
        }
        for e in payload["data"]["events"]
    ]
