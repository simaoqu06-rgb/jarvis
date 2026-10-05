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
