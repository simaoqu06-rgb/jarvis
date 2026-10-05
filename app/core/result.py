"""Format de réponse identique pour toutes les données, quelle que soit la source."""
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Result:
    need: str                      # la donnée demandée, ex. "crypto_prices"
    data: Any                      # le contenu normalisé (même forme pour toutes les sources)
    source: str | None             # la source qui a répondu
    attribution: str | None        # mention à afficher quand la licence l'exige
    updated_at: str | None         # heure (UTC) de la dernière mise à jour réussie
    stale: bool = False            # True : dernière valeur connue, sources en panne
    message: str | None = None     # explication claire en cas de repli
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "Result":
        return cls(**raw)
