"""Journal SQLite du labo : bougies, univers du jour, trades simulés, avancement et versions de méthode.

Fichier JARVIS_LAB_DB (data/lab.db par défaut), en mode WAL : le serveur peut lire l'état
(GET /api/lab/status) pendant que le worker écrit.
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
CREATE TABLE IF NOT EXISTS trades (
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
);
CREATE INDEX IF NOT EXISTS trades_day ON trades (market, day);
CREATE TABLE IF NOT EXISTS progress (
    job TEXT NOT NULL, key TEXT NOT NULL, value TEXT, PRIMARY KEY (job, key)
);
CREATE TABLE IF NOT EXISTS method_versions (
    version TEXT PRIMARY KEY, created_at TEXT NOT NULL, settings TEXT NOT NULL
);
"""


def db_path() -> str:
    return os.getenv("JARVIS_LAB_DB", DEFAULT_PATH)


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
        with self.transaction():
            for t in planned_at:
                self.conn.execute("DELETE FROM trades WHERE origin = ? AND method_version = ? AND market = ? "
                                  "AND planned_at = ?", (origin, version, market, t))
            for r in rows:
                cols = ["origin", "method_version", "market", "symbol", "side", "day", "planned_at",
                        "plan", "context", "result", *TRADE_FLAT]
                values = [origin, version, market, r["symbol"], r["side"], r["day"], r["planned_at"],
                          json.dumps(r["plan"], ensure_ascii=False), json.dumps(r["context"], ensure_ascii=False),
                          json.dumps(r["result"], ensure_ascii=False)] + [r["flat"].get(k) for k in TRADE_FLAT]
                self.conn.execute(
                    f"INSERT OR REPLACE INTO trades ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", values)

    def count_trades(self, **where) -> int:
        sql = "SELECT COUNT(*) FROM trades" + (" WHERE " + " AND ".join(f"{k} = ?" for k in where) if where else "")
        return self.conn.execute(sql, tuple(where.values())).fetchone()[0]

    # --- Versions de méthode ----------------------------------------------------------------
    def method_version(self, snapshot: dict) -> tuple[str, dict]:
        """Version en cours : la dernière enregistrée, sinon « v1 » = `snapshot` (réglages du moment)."""
        row = self.conn.execute("SELECT version, settings FROM method_versions ORDER BY created_at DESC, version DESC "
                                "LIMIT 1").fetchone()
        if row:
            return row[0], json.loads(row[1])
        self.conn.execute("INSERT INTO method_versions VALUES (?, ?, ?)",
                          ("v1", datetime.now(timezone.utc).isoformat(timespec="seconds"),
                           json.dumps(snapshot, ensure_ascii=False)))
        return "v1", snapshot


def open_readonly(path: str | None = None) -> sqlite3.Connection | None:
    """Connexion en lecture seule (route d'état) ; None si la base n'existe pas encore."""
    path = path or db_path()
    if not Path(path).exists():
        return None
    return sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True, check_same_thread=False)
