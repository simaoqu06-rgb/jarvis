"""Santé des sources vue par ce processus : dernière lecture réussie, dernière erreur, latence (en mémoire).

Noté par les appels HTTP communs (core/http.py) et par Alpaca (sources/alpaca.py), par hôte, sans jamais garder
l'URL ni ses paramètres (des clés d'API y passent) : seulement le nom de la source et le texte court de l'erreur.
Le worker du labo (conteneur jarvis-lab) a ses propres appels, invisibles ici.

Rubrique « À faire » (29/10/2026) : chaque source garde aussi le premier échec depuis le dernier succès
(`premier_echec`), le nombre d'échecs d'affilée (`echecs_suite`), l'heure de la dernière tentative et le code HTTP de
la dernière erreur (`code`), pour qu'une source qui n'est plus appelée (Twelve Data hors séance) ne passe jamais
pour une source en panne.
"""
import re
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
CODE_RE = re.compile(r"^HTTP (\d{3})\b")
# Un message d'erreur ne garde jamais une clé : `apikey=…`, `token=…`, `Bearer …` masqués
SCRUB = re.compile(r"(?i)(api[_-]?key|apikey|token|secret|password|key)=([^&\s]+)")
BEARER = re.compile(r"(?i)\bbearer\s+\S+")
_state: dict[str, dict] = {}


def name_of(url: str) -> str:
    host = urlsplit(url).netloc.lower().split(":")[0]
    return NAMES.get(host, host or "inconnue")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def scrub(text: str) -> str:
    return BEARER.sub("Bearer ***", SCRUB.sub(r"\1=***", text))


def _latency(started: float | None) -> int | None:
    return None if started is None else round((clock.monotonic() - started) * 1000)


def ok(url: str, started: float | None) -> None:
    s = _state.setdefault(name_of(url), {"ok": 0, "erreurs": 0})
    now = _now()
    s.update(dernier_succes=now, derniere_tentative=now, latence_ms=_latency(started), ok=s["ok"] + 1,
             dernier_resultat="ok", echecs_suite=0, premier_echec=None)


def fail(url: str, error: Exception, started: float | None) -> None:
    s = _state.setdefault(name_of(url), {"ok": 0, "erreurs": 0})
    now = _now()
    text = scrub(str(error))[:160]
    m = CODE_RE.match(text)
    s.update(derniere_erreur=now, derniere_tentative=now, erreur=text, latence_ms=_latency(started),
             erreurs=s["erreurs"] + 1, dernier_resultat="erreur", echecs_suite=s.get("echecs_suite", 0) + 1,
             premier_echec=s.get("premier_echec") or now, code=int(m.group(1)) if m else None)


def snapshot() -> dict[str, dict]:
    """{source: {dernier_succes, derniere_erreur, erreur, code, latence_ms, ok, erreurs, echecs_suite, premier_echec,
    derniere_tentative, statut}} ; statut « ok » quand la dernière tentative a réussi, « erreur » sinon."""
    return {name: dict(s) | {"statut": "erreur" if s.get("dernier_resultat") == "erreur" else "ok"}
            for name, s in sorted(_state.items())}
