"""Santé des sources vue par ce processus : dernière lecture réussie, dernière erreur, latence (en mémoire).

Noté par les appels HTTP communs (core/http.py) et par Alpaca (sources/alpaca.py), par hôte, sans jamais garder
l'URL ni ses paramètres (des clés d'API y passent) : seulement le nom de la source et le texte court de l'erreur.
Le worker du labo (conteneur jarvis-lab) a ses propres appels, invisibles ici.
"""
import time as clock
from datetime import datetime, timezone
from urllib.parse import urlsplit

NAMES = {
    "api.twelvedata.com": "Twelve Data", "api.kraken.com": "Kraken", "finnhub.io": "Finnhub",
    "data.alpaca.markets": "Alpaca (données)", "paper-api.alpaca.markets": "Alpaca (compte fictif)",
    "api.frankfurter.app": "Frankfurter (BCE)", "api.frankfurter.dev": "Frankfurter (BCE)",
    "api.coinlore.net": "CoinLore", "api.coinpaprika.com": "CoinPaprika", "date.nager.at": "Nager.Date",
    "www.sec.gov": "SEC EDGAR", "efts.sec.gov": "SEC EDGAR", "data.sec.gov": "SEC EDGAR",
    "gamma-api.polymarket.com": "Polymarket", "clob.polymarket.com": "Polymarket",
    "earthquake.usgs.gov": "USGS (séismes)", "api.open-meteo.com": "Open-Meteo",
}
_state: dict[str, dict] = {}


def name_of(url: str) -> str:
    host = urlsplit(url).netloc.lower().split(":")[0]
    return NAMES.get(host, host or "inconnue")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ok(url: str, started: float) -> None:
    s = _state.setdefault(name_of(url), {"ok": 0, "erreurs": 0})
    s.update(dernier_succes=_now(), latence_ms=round((clock.monotonic() - started) * 1000), ok=s["ok"] + 1)


def fail(url: str, error: Exception, started: float) -> None:
    s = _state.setdefault(name_of(url), {"ok": 0, "erreurs": 0})
    s.update(derniere_erreur=_now(), erreur=str(error)[:160], latence_ms=round((clock.monotonic() - started) * 1000),
             erreurs=s["erreurs"] + 1)


def snapshot() -> dict[str, dict]:
    """{source: {dernier_succes, derniere_erreur, erreur, latence_ms, ok, erreurs, statut}} ; statut « ok » quand la
    dernière lecture a réussi, « erreur » quand la dernière tentative a échoué."""
    out = {}
    for name, s in sorted(_state.items()):
        bad = s.get("derniere_erreur") and (not s.get("dernier_succes") or s["derniere_erreur"] > s["dernier_succes"])
        out[name] = dict(s) | {"statut": "erreur" if bad else "ok"}
    return out
