"""Journal SQLite du labo : bougies, univers du jour, trades simulés, avancement et versions de méthode.

Fichier JARVIS_LAB_DB (data/lab.db par défaut), en mode WAL : le serveur peut lire l'état
(GET /api/lab/status) pendant que le worker écrit.

Labo 2 : régimes de marché (regimes), mouvements du jour de l'univers (day_moves), fiches
catalyseurs (catalysts), résumés du soir (summaries), règles testées (rules), seuils du validateur
(lab_config) et statistiques précalculées (stats_cache). Seul le worker écrit ; le serveur web
n'ouvre la base qu'en lecture seule (les règles soumises passent par une file de fichiers).

Labo 3 : versions de méthode « filtres » (v2 = v1 + règles approuvées) dans method_versions
(colonnes base, rules, role…), décision de Simão sur chaque règle validée (rules.decision) et
événements à annoncer sur Discord (lab_events). Les trades restent journalisés sous la version de
génération (base IS NULL, « v1 ») : champion et challenger en sont des sous-ensembles filtrés.
"""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PATH = "data/lab.db"
TRADE_FLAT = ("triggered", "filled_at", "exit_reason", "r_net", "r_gross", "tp1_hit", "tp2_hit", "tp3_hit",
              "minutes_to_tp1", "minutes_to_tp2", "minutes_to_tp3", "mfe_r", "mae_r", "day_range_pct",
              "decision", "composite")

TRADES_DDL = """CREATE TABLE IF NOT EXISTS {name} (
    id INTEGER PRIMARY KEY,
    origin TEXT NOT NULL CHECK (origin IN ('replay', 'live')),
    method_version TEXT NOT NULL, market TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
    day TEXT NOT NULL, planned_at INTEGER NOT NULL,
    plan TEXT, context TEXT, result TEXT,
    triggered INTEGER, filled_at INTEGER, exit_reason TEXT, r_net REAL, r_gross REAL,
    tp1_hit INTEGER, tp2_hit INTEGER, tp3_hit INTEGER,
    minutes_to_tp1 REAL, minutes_to_tp2 REAL, minutes_to_tp3 REAL,
    mfe_r REAL, mae_r REAL, day_range_pct REAL, decision TEXT, composite REAL,
    UNIQUE (origin, method_version, market, symbol, planned_at)
);"""
# Labo v2 (strategies.py) : une ligne par stratégie, actif et jour (v1 : par créneau), toutes les sorties
# et tous les tests de robustesse dans la même ligne. Colonnes dans STRAT_COLUMNS.
STRAT_COLUMNS = ("origin", "market", "symbol", "day", "strategy", "slot", "signal_at", "entry", "stop", "risk_pct",
                 "triggered", "filled_at", "entry_mode", "minutes_to_fill", "exit_reason", "r_net", "r_gross",
                 "tp1_hit", "mfe_r", "mae_r", "r_sans_be", "r_2r", "r_suiveur", "r_glisse_x2", "r_glisse_x3",
                 "r_retard", "r_manque", "temoin_r", "temoin_triggered", "gap_open_pct", "rank", "r_emperor",
                 "r_emperor_alerte", "r_emperor_confirme")
STRAT_DDL = """CREATE TABLE IF NOT EXISTS {name} (
    id INTEGER PRIMARY KEY,
    origin TEXT NOT NULL CHECK (origin IN ('replay', 'live')),
    market TEXT NOT NULL, symbol TEXT NOT NULL, day TEXT NOT NULL, strategy TEXT NOT NULL,
    slot INTEGER NOT NULL, signal_at INTEGER, entry REAL, stop REAL, risk_pct REAL,
    triggered INTEGER, filled_at INTEGER, entry_mode TEXT, minutes_to_fill REAL, exit_reason TEXT,
    r_net REAL, r_gross REAL, tp1_hit INTEGER, mfe_r REAL, mae_r REAL,
    r_sans_be REAL, r_2r REAL, r_suiveur REAL, r_glisse_x2 REAL, r_glisse_x3 REAL, r_retard REAL, r_manque REAL,
    temoin_r REAL, temoin_triggered INTEGER, gap_open_pct REAL, rank INTEGER, r_emperor REAL,
    r_emperor_alerte REAL, r_emperor_confirme REAL,
    UNIQUE (origin, market, symbol, day, strategy, slot)
);
CREATE INDEX IF NOT EXISTS {name}_day ON {name} (market, day);"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
    market TEXT NOT NULL, symbol TEXT NOT NULL, tf INTEGER NOT NULL, ts INTEGER NOT NULL,
    o REAL, h REAL, l REAL, c REAL, v REAL,
    PRIMARY KEY (market, symbol, tf, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS bars_daily ON bars (market, tf, ts) WHERE tf = 1440;
CREATE TABLE IF NOT EXISTS universe (
    market TEXT NOT NULL, day TEXT NOT NULL, symbol TEXT NOT NULL, rank INTEGER, reason TEXT, metrics TEXT,
    PRIMARY KEY (market, day, symbol)
);
{trades}
CREATE INDEX IF NOT EXISTS trades_day ON trades (market, day);
CREATE TABLE IF NOT EXISTS progress (
    job TEXT NOT NULL, key TEXT NOT NULL, value TEXT, PRIMARY KEY (job, key)
);
CREATE TABLE IF NOT EXISTS method_versions (
    version TEXT PRIMARY KEY, created_at TEXT NOT NULL, settings TEXT NOT NULL
);
-- Labo 2 : mesures et validation.
CREATE INDEX IF NOT EXISTS trades_time ON trades (planned_at);
CREATE INDEX IF NOT EXISTS trades_symbol ON trades (market, symbol, day);
CREATE TABLE IF NOT EXISTS regimes (
    market TEXT NOT NULL, day TEXT NOT NULL, trend TEXT, vol_tercile INTEGER, label TEXT NOT NULL, metrics TEXT,
    PRIMARY KEY (market, day)
);
CREATE TABLE IF NOT EXISTS day_moves (
    market TEXT NOT NULL, day TEXT NOT NULL, symbol TEXT NOT NULL,
    open REAL, prev_close REAL, high REAL, low REAL, close REAL,
    up_pct REAL, down_pct REAL, gap_pct REAL, t_high INTEGER, t_low INTEGER, rvol30 REAL,
    PRIMARY KEY (market, day, symbol)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS day_moves_up ON day_moves (up_pct);
CREATE TABLE IF NOT EXISTS catalysts (
    id INTEGER PRIMARY KEY, market TEXT NOT NULL, day TEXT NOT NULL, symbol TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('mouvement', 'contre_exemple')), direction TEXT,
    amplitude_pct REAL, gap_pct REAL, start_ts INTEGER, extreme_ts INTEGER,
    signals TEXT, news TEXT, news_status TEXT NOT NULL, note TEXT,
    catalyst_type TEXT, classified_at TEXT, created_at TEXT NOT NULL,
    UNIQUE (market, day, symbol)
);
CREATE INDEX IF NOT EXISTS catalysts_pending ON catalysts (news_status, day);
CREATE TABLE IF NOT EXISTS summaries (
    day TEXT PRIMARY KEY, created_at TEXT NOT NULL, data TEXT NOT NULL, text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY, hash TEXT NOT NULL UNIQUE, name TEXT, rule TEXT NOT NULL,
    status TEXT NOT NULL, reason TEXT, metrics TEXT, trial INTEGER,
    submitted_at TEXT NOT NULL, tested_at TEXT
);
CREATE TABLE IF NOT EXISTS lab_config (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stats_cache (
    key TEXT PRIMARY KEY, created_at TEXT NOT NULL, trades INTEGER, data TEXT NOT NULL
);
-- Labo 3 : événements à annoncer (méthode en place, challenger retiré), lus par le serveur web.
CREATE TABLE IF NOT EXISTS lab_events (
    id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, ts INTEGER NOT NULL, kind TEXT NOT NULL,
    version TEXT, text TEXT NOT NULL, data TEXT
);
-- Carnet des leçons (lessons.py) : observations chiffrées de la routine du soir, re-mesurées chaque nuit.
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY, hash TEXT NOT NULL UNIQUE, text TEXT NOT NULL, cond TEXT NOT NULL, markets TEXT,
    sens TEXT NOT NULL, pourquoi TEXT, status TEXT NOT NULL, metrics TEXT, soiree TEXT,
    created_at TEXT NOT NULL, measured_at TEXT, retired_at TEXT, retired_reason TEXT
);
-- Grade de JARVIS (grade.py) : historique des changements, par marché.
CREATE TABLE IF NOT EXISTS grades (
    id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, market TEXT NOT NULL, version TEXT,
    rank INTEGER, measured INTEGER, source TEXT NOT NULL, reason TEXT, metrics TEXT, soiree TEXT
);
-- Bloc scellé (seal.py) : mêmes tables, jamais lues par les statistiques, la routine ni le grade.
{trades_sealed}
{strat}
{strat_sealed}
""".format(trades=TRADES_DDL.format(name="trades"), trades_sealed=TRADES_DDL.format(name="trades_sealed"),
           strat=STRAT_DDL.format(name="strat_trades"), strat_sealed=STRAT_DDL.format(name="strat_trades_sealed"))
# Colonnes ajoutées après coup (ALTER TABLE à l'ouverture par le worker ; idempotent).
MIGRATIONS = {
    "rules": (("pourquoi", "TEXT"), ("council", "TEXT"), ("soiree", "TEXT"), ("decision", "TEXT"),
              ("decided_at", "TEXT")),
    "lessons": (("base_trade_id", "INTEGER"),),
    "method_versions": (("base", "TEXT"), ("rules", "TEXT"), ("role", "TEXT"), ("started_ts", "INTEGER"),
                        ("decided_at", "TEXT"), ("note", "TEXT")),
    # Labo v2, déclarations n° 2 et 3 : sorties EmperorBTC.
    "strat_trades": (("r_emperor", "REAL"), ("r_emperor_alerte", "REAL"), ("r_emperor_confirme", "REAL")),
    "strat_trades_sealed": (("r_emperor", "REAL"), ("r_emperor_alerte", "REAL"), ("r_emperor_confirme", "REAL")),
}
GENERATION_SQL = "SELECT version FROM method_versions WHERE base IS NULL ORDER BY created_at DESC, version DESC LIMIT 1"
LEGACY_VERSION_SQL = "SELECT version FROM method_versions ORDER BY created_at DESC, version DESC LIMIT 1"


def migrate(conn: sqlite3.Connection) -> None:
    for table, columns in MIGRATIONS.items():
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def generation_version(conn: sqlite3.Connection) -> str | None:
    """Version sous laquelle les trades sont journalisés (la dernière sans base) ; marche aussi sur une
    base d'avant le Labo 3 (lecture seule, colonne base absente)."""
    try:
        row = conn.execute(GENERATION_SQL).fetchone()
    except sqlite3.OperationalError:
        row = conn.execute(LEGACY_VERSION_SQL).fetchone()
    return row[0] if row else None


def db_path() -> str:
    return os.getenv("JARVIS_LAB_DB", DEFAULT_PATH)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _bar(row) -> dict:
    return {"t": row[0], "o": row[1], "h": row[2], "l": row[3], "c": row[4], "v": row[5]}


class LabDB:
    """Accès en écriture (worker, tests). Une seule connexion, transactions explicites."""

    def __init__(self, path: str | None = None):
        self.path = path or db_path()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        migrate(self.conn)
        self._depth = 0

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self):
        """Tout ou rien (imbrication tolérée : seule la plus externe valide)."""
        if self._depth:
            self._depth += 1
            try:
                yield self
            finally:
                self._depth -= 1
            return
        self.conn.execute("BEGIN")
        self._depth = 1
        try:
            yield self
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")
        finally:
            self._depth = 0

    # --- Bougies ---------------------------------------------------------------------------
    def put_bars(self, market: str, symbol: str, tf: int, rows: list[dict]) -> int:
        with self.transaction():
            self.conn.executemany(
                "INSERT OR REPLACE INTO bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(market, symbol, tf, int(r["t"]), r["o"], r["h"], r["l"], r["c"], r.get("v") or 0.0) for r in rows])
        return len(rows)

    def get_bars(self, market: str, symbol: str, tf: int, start: int, end: int) -> list[dict]:
        """Bougies de début dans [start, end[, de la plus ancienne à la plus récente."""
        cur = self.conn.execute(
            "SELECT ts, o, h, l, c, v FROM bars WHERE market = ? AND symbol = ? AND tf = ? AND ts >= ? AND ts < ? "
            "ORDER BY ts", (market, symbol, tf, start, end))
        return [_bar(r) for r in cur]

    def delete_symbol(self, market: str, symbol: str) -> None:
        """Oublie toutes les bougies d'un symbole et leur couverture (avant un rechargement complet)."""
        with self.transaction():
            self.conn.execute("DELETE FROM bars WHERE market = ? AND symbol = ?", (market, symbol))
            self.conn.execute("DELETE FROM progress WHERE job IN (?, ?) AND key = ?",
                              (f"covD:{market}", f"cov5:{market}", symbol))

    def daily_cross_section(self, market: str, start: int, end: int) -> dict[str, list[dict]]:
        """Bougies journalières de tous les symboles entre deux dates (pour l'univers du jour)."""
        out: dict[str, list[dict]] = {}
        cur = self.conn.execute(
            "SELECT symbol, ts, o, h, l, c, v FROM bars WHERE market = ? AND tf = 1440 AND ts >= ? AND ts < ? "
            "ORDER BY symbol, ts", (market, start, end))
        for row in cur:
            out.setdefault(row[0], []).append(_bar(row[1:]))
        return out

    # --- Avancement -------------------------------------------------------------------------
    def get(self, job: str, key: str, default=None):
        row = self.conn.execute("SELECT value FROM progress WHERE job = ? AND key = ?", (job, key)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, job: str, key: str, value) -> None:
        self.conn.execute("INSERT OR REPLACE INTO progress VALUES (?, ?, ?)", (job, key, json.dumps(value)))

    def keys(self, job: str) -> dict:
        return {k: json.loads(v) for k, v in self.conn.execute("SELECT key, value FROM progress WHERE job = ?", (job,))}

    # --- Univers ----------------------------------------------------------------------------
    def replace_universe(self, market: str, day: str, rows: list[dict]) -> None:
        with self.transaction():
            self.conn.execute("DELETE FROM universe WHERE market = ? AND day = ?", (market, day))
            self.conn.executemany(
                "INSERT INTO universe VALUES (?, ?, ?, ?, ?, ?)",
                [(market, day, r["symbol"], r["rank"], r["reason"], json.dumps(r.get("metrics") or {}))
                 for r in rows])

    def universe(self, market: str, day: str) -> list[dict]:
        cur = self.conn.execute("SELECT symbol, rank, reason, metrics FROM universe WHERE market = ? AND day = ? "
                                "ORDER BY rank", (market, day))
        return [{"symbol": s, "rank": r, "reason": why, "metrics": json.loads(m or "{}")} for s, r, why, m in cur]

    # --- Trades -----------------------------------------------------------------------------
    def replace_trades(self, origin: str, version: str, market: str, planned_at: list[int], rows: list[dict]) -> None:
        """Remplace les trades de ces créneaux (rejouer un jour ne crée jamais de doublon)."""
        from app.lab import seal  # import tardif : seal n'importe rien, mais garde db sans dépendance

        with self.transaction():
            for t in planned_at:
                for table in ("trades", "trades_sealed"):
                    self.conn.execute(f"DELETE FROM {table} WHERE origin = ? AND method_version = ? AND market = ? "
                                      "AND planned_at = ?", (origin, version, market, t))
            for r in rows:
                table = "trades_sealed" if seal.sealed(origin, r["day"]) else "trades"
                cols = ["origin", "method_version", "market", "symbol", "side", "day", "planned_at",
                        "plan", "context", "result", *TRADE_FLAT]
                values = [origin, version, market, r["symbol"], r["side"], r["day"], r["planned_at"],
                          json.dumps(r["plan"], ensure_ascii=False), json.dumps(r["context"], ensure_ascii=False),
                          json.dumps(r["result"], ensure_ascii=False)] + [r["flat"].get(k) for k in TRADE_FLAT]
                self.conn.execute(
                    f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", values)

    def replace_strat(self, origin: str, market: str, day: str, rows: list[dict], symbols: list[str] | None = None) -> None:
        """Remplace les lignes des stratégies du jour (toutes, ou celles de `symbols`) : rejouer un jour
        ne crée jamais de doublon. Les jours du bloc scellé vont dans strat_trades_sealed."""
        from app.lab import seal

        table = "strat_trades_sealed" if seal.sealed(origin, day) else "strat_trades"
        with self.transaction():
            for t in ("strat_trades", "strat_trades_sealed"):
                if symbols is None:
                    self.conn.execute(f"DELETE FROM {t} WHERE origin = ? AND market = ? AND day = ?", (origin, market, day))
                else:
                    self.conn.executemany(f"DELETE FROM {t} WHERE origin = ? AND market = ? AND day = ? AND symbol = ?",
                                          [(origin, market, day, s) for s in symbols])
            self.conn.executemany(
                f"INSERT OR REPLACE INTO {table} ({', '.join(STRAT_COLUMNS)}) VALUES ({', '.join('?' * len(STRAT_COLUMNS))})",
                [[origin, market, r["symbol"], day, *[r.get(k) for k in STRAT_COLUMNS[4:]]] for r in rows])

    def count_trades(self, **where) -> int:
        sql = "SELECT COUNT(*) FROM trades" + (" WHERE " + " AND ".join(f"{k} = ?" for k in where) if where else "")
        return self.conn.execute(sql, tuple(where.values())).fetchone()[0]

    # --- Versions de méthode ----------------------------------------------------------------
    def method_version(self, snapshot: dict) -> tuple[str, dict]:
        """Version de génération en cours (celle des trades journalisés) : la dernière sans base, sinon
        « v1 » = `snapshot` (réglages du moment). Les versions « filtres » (Labo 3) n'en sont pas."""
        row = self.conn.execute("SELECT version, settings FROM method_versions WHERE base IS NULL "
                                "ORDER BY created_at DESC, version DESC LIMIT 1").fetchone()
        if row:
            return row[0], json.loads(row[1])
        self.conn.execute("INSERT INTO method_versions (version, created_at, settings) VALUES (?, ?, ?)",
                          ("v1", datetime.now(timezone.utc).isoformat(timespec="seconds"),
                           json.dumps(snapshot, ensure_ascii=False)))
        return "v1", snapshot


def open_readonly(path: str | None = None) -> sqlite3.Connection | None:
    """Connexion en lecture seule (route d'état) ; None si la base n'existe pas encore."""
    path = path or db_path()
    if not Path(path).exists():
        return None
    return sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True, check_same_thread=False)
