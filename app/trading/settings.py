"""Réglages du trading modifiables sans toucher au code : seuils de la note, du veto et du scanner.

Rien n'est gravé dans le marbre : les valeurs par défaut ci-dessous sont un point de départ,
le fichier data/trading_settings.json (JARVIS_SETTINGS_PATH) garde ce qui a changé. Les
changements viennent des propositions de JARVIS (app/trading/proposals.py), tirées du bilan
du forward-test, et ne s'appliquent qu'une fois approuvées par Simão.
"""
import json
import logging
import math
import os
import re
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


TRUE, FALSE = ("1", "true", "oui", "on", "vrai"), ("0", "false", "non", "off", "faux")
TICKER = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
POSITIVE_KEYS = {"rr_min"}  # doivent être strictement positifs (le R/R visé sert de diviseur)


def _number(key: str, value) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{key} : nombre attendu, pas {value!r}")
    if isinstance(value, str):
        try:
            value = float(value.strip().replace(",", "."))
        except ValueError:
            raise ValueError(f"{key} : nombre attendu, pas {value!r}") from None
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{key} : nombre attendu, pas {value!r}")
    return float(value)


def validate(key: str, value):
    """Valeur convertie au type du défaut ; ValueError (message en français) si elle ne convient pas."""
    default = DEFAULTS[key]
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str) and value.strip().lower() in TRUE + FALSE:
            return value.strip().lower() in TRUE
        raise ValueError(f"{key} : oui/non attendu, pas {value!r}")
    if isinstance(default, int):
        number = _number(key, value)
        if not number.is_integer():
            raise ValueError(f"{key} : nombre entier attendu, pas {value!r}")
        return int(number)
    if default is None or isinstance(default, float):
        number = _number(key, value)
        if key in POSITIVE_KEYS and number <= 0:
            raise ValueError(f"{key} doit être strictement positif")
        return number
    if isinstance(default, list):
        if isinstance(value, str):
            value = [x for x in re.split(r"[,\s;]+", value) if x]
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise ValueError(f"{key} : liste de tickers attendue (ex. « NVDA, AMD »), pas {value!r}")
        symbols = list(dict.fromkeys(x.strip().upper() for x in value if x.strip()))
        bad = [x for x in symbols if not TICKER.match(x)]
        if bad:
            raise ValueError(f"{key} : ticker invalide {', '.join(bad)}")
        return symbols
    if isinstance(default, dict):
        if not isinstance(value, dict):
            raise ValueError(f"{key} : dictionnaire attendu avec {', '.join(default)}")
        missing, extra = set(default) - set(value), set(value) - set(default)
        if missing or extra:
            raise ValueError(f"{key} : clés attendues {', '.join(default)}"
                             + (f" (manque {', '.join(sorted(missing))})" if missing else "")
                             + (f" (inconnues {', '.join(sorted(extra))})" if extra else ""))
        out = {k: _number(f"{key}.{k}", value[k]) for k in default}
        if any(v < 0 for v in out.values()) or not sum(out.values()) > 0:
            raise ValueError(f"{key} : poids positifs attendus, de somme non nulle")
        if key == "weights" and not out["technique"] + out["risk"] > 0:
            # crypto et actions sans catalyseur : seules technique et risque comptent (division par zéro)
            raise ValueError("weights : technique + risque doit être non nul")
        return out
    if isinstance(default, str):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} : texte attendu, pas {value!r}")
        value = value.strip()
        if key.endswith("_time_ny") and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError(f"{key} : heure HH:MM attendue, pas {value!r}")
        return value
    return value


class Settings:
    def __init__(self, path: str | None = SETTINGS_PATH):
        self.path = Path(path) if path else None
        self.overrides: dict = {}
        if self.path and self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                log.warning("Réglages du trading illisibles, valeurs par défaut")
                raw = {}
            # Fichier retouché à la main : une valeur invalide ferait planter chaque note ou chaque scan.
            for key, value in (raw.items() if isinstance(raw, dict) else ()):
                try:
                    if key in DEFAULTS and value is not None:
                        self.overrides[key] = validate(key, value)
                    else:
                        log.warning("Réglage du trading ignoré : %s", key)
                except (ValueError, TypeError) as exc:
                    log.warning("Réglage du trading ignoré (%s)", exc)

    def get(self, key: str):
        return self.overrides.get(key, DEFAULTS[key])

    def __getitem__(self, key: str):
        return self.get(key)

    def all(self) -> dict:
        return DEFAULTS | self.overrides

    def update(self, changes: dict) -> dict:
        """Applique des changements ; une valeur None remet le défaut. Refuse les clés inconnues et,
        sans rien appliquer, les valeurs du mauvais type (ValueError)."""
        unknown = set(changes) - set(DEFAULTS)
        if unknown:
            raise KeyError(", ".join(sorted(unknown)))
        checked = {key: None if value is None else validate(key, value) for key, value in changes.items()}
        for key, value in checked.items():
            if value is None:
                self.overrides.pop(key, None)
            else:
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
