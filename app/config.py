"""Configuration lue dans les variables d'environnement (et le fichier .env en local)."""
import os

from dotenv import load_dotenv

load_dotenv()

USER_AGENT = os.getenv(
    "JARVIS_USER_AGENT",
    "JARVIS-personal/0.1 (+https://github.com/simaoqu06-rgb/jarvis)",
)
LAST_KNOWN_PATH = os.getenv("JARVIS_LAST_KNOWN_PATH", "data/last_known.json")
TIMEOUT_SECONDS = 8.0


def secret(name: str) -> str | None:
    """Renvoie une clé d'API, ou None si elle n'est pas définie."""
    value = os.getenv(name, "").strip()
    return value or None


def env_list(name: str, default: str = "") -> list[str]:
    """Liste séparée par des virgules, en majuscules (ex. JARVIS_WATCHLIST_STOCKS=AAPL,NVDA)."""
    return [v.strip().upper() for v in os.getenv(name, default).split(",") if v.strip()]


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


# Notifications Discord
ALERTS_STATE_PATH = os.getenv("JARVIS_ALERTS_STATE_PATH", "data/alerts_state.json")
OUTBOX_PATH = os.getenv("JARVIS_OUTBOX_PATH", "data/outbox.json")  # messages pas encore postés
WATCHLIST_CRYPTO = env_list("JARVIS_WATCHLIST_CRYPTO", "BTC,ETH")
WATCHLIST_STOCKS = env_list("JARVIS_WATCHLIST_STOCKS", "")
MOVE_ALERT_PCT = env_float("JARVIS_MOVE_ALERT_PCT", 5.0)
QUAKE_MIN_MAGNITUDE = env_float("JARVIS_QUAKE_MIN_MAGNITUDE", 6.0)
HOME_CITY = os.getenv("JARVIS_CITY", "").strip()
