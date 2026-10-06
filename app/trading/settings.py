"""Réglages du trading modifiables sans toucher au code : seuils de la note, du veto et du scanner.

Rien n'est gravé dans le marbre : les valeurs par défaut ci-dessous sont un point de départ,
le fichier data/trading_settings.json (JARVIS_SETTINGS_PATH) garde ce qui a changé. Les
changements viennent des propositions de JARVIS (app/trading/proposals.py), tirées du bilan
du forward-test, et ne s'appliquent qu'une fois approuvées par Simão.
"""
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)
SETTINGS_PATH = os.getenv("JARVIS_SETTINGS_PATH", "data/trading_settings.json")

DEFAULTS: dict = {
    # --- Note composite et veto ---
    "rr_min": 2.0,              # R/R visé, mesuré sur le TP2
    "rr_tolerance": 0.2,        # R/R toléré en dessous du minimum si le reste suit (note ≥ go_min + 0,5)
    "go_min": 7.0,              # note composite minimale pour un GO
    "earnings_veto_hours": 48,  # résultats trimestriels dans moins de X heures = NO GO (0 = jamais)
    "weights": {"technique": 0.4, "catalyst": 0.3, "risk": 0.3},
    "rsi_min": 45.0,            # RSI 14 jours « sain » pour un long (inversé pour un short)
    "rsi_max": 75.0,
    "max_stop_atr": 2.5,        # stop plus loin que ça (en ATR de la bougie du plan) : note de risque en baisse
    "min_tp1_r": 0.8,           # TP1 plus près que ça (en R) : un mur juste devant l'entrée
    # --- Scanner avant l'ouverture US (longs) ---
    "scan_enabled": True,
    "scan_time_ny": "09:10",    # 15 h 10 à Paris la plupart de l'année, 20 min avant l'ouverture
    "scan_price_min": 5.0,
    "scan_price_max": None,     # pas de plafond : les fractions d'action rendent le prix indifférent
    "scan_rvol_min": 1.5,       # volume de la veille / moyenne 20 jours
    "scan_gap_min_pct": 2.0,    # ou un gap d'au moins 2 % en pré-ouverture : la valeur est « en jeu »
    "scan_atr_pct_min": 2.5,    # amplitude journalière moyenne : assez de mouvement pour des TP à +5-10 %
    "scan_above_ma20": True,
    "scan_above_ma50": True,
    "scan_exclude_dilution": True,
    "scan_max_results": 5,
    "scan_universe": [],        # tickers toujours examinés en plus des plus actifs et des plus fortes hausses
    # --- Forward-test sur le compte Alpaca fictif ---
    "paper_enabled": True,
    "paper_risk_pct": 1.0,      # % du capital fictif risqué par plan
    "paper_horizon_sessions": 2,  # au-delà, la position fictive est soldée (« expiré »)
}


class Settings:
    def __init__(self, path: str | None = SETTINGS_PATH):
        self.path = Path(path) if path else None
        self.overrides: dict = {}
        if self.path and self.path.exists():
            try:
                self.overrides = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                log.warning("Réglages du trading illisibles, valeurs par défaut")

    def get(self, key: str):
        return self.overrides.get(key, DEFAULTS[key])

    def __getitem__(self, key: str):
        return self.get(key)

    def all(self) -> dict:
        return DEFAULTS | self.overrides

    def update(self, changes: dict) -> dict:
        """Applique des changements ; une valeur None remet le défaut. Refuse les clés inconnues."""
        unknown = set(changes) - set(DEFAULTS)
        if unknown:
            raise KeyError(", ".join(sorted(unknown)))
        for key, value in changes.items():
            if value is None:
                self.overrides.pop(key, None)
            else:
                default = DEFAULTS[key]
                if isinstance(default, bool):
                    value = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "oui", "on")
                elif (default is None or isinstance(default, (int, float))) and isinstance(value, str):
                    value = float(value.replace(",", "."))
                self.overrides[key] = value
        self.save()
        return self.all()

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.overrides, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)


settings = Settings()
