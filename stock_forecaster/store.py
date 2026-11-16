"""SQLite persistence for run history (survives server restarts).

One row per run: summary columns for the history list, plus the full
`AnalysisResult` JSON so a stored run renders exactly as it did when it finished.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .models import AnalysisResult, RunStatus, utc_now

DB_ENV = "STOCK_FORECASTER_DB"
DEFAULT_DB = Path.home() / ".stock_forecaster" / "runs.db"
TERMINAL = {RunStatus.COMPLETE.value, RunStatus.FAILED.value}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    ticker        TEXT NOT NULL,
    horizon       TEXT NOT NULL,
    status        TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    company_name  TEXT,
    gate_failures INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    result_json   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_started ON runs (started_at DESC);
CREATE INDEX IF NOT EXISTS runs_ticker ON runs (ticker, started_at DESC);
"""


def default_db_path() -> str:
    return os.environ.get(DB_ENV) or str(DEFAULT_DB)


class RunStore:
    def __init__(self, path: str | Path | None = None):
        self.path = str(path or default_db_path())
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock, self._db:
            if self.path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)

    def save(self, r: AnalysisResult) -> None:
        row = (
            r.run_id,
            r.request.ticker,
            r.request.horizon.value,
            r.status.value,
            r.started_at.isoformat(),
            r.finished_at.isoformat() if r.finished_at else None,
            r.company_name,
            len(r.quality_gate_failures),
            r.error,
            r.model_dump_json(),
        )
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)", row)

    def set_status(self, run_id: str, status: RunStatus) -> None:
        # Only the summary column; the JSON is rewritten when the run finishes.
        with self._lock, self._db:
            self._db.execute("UPDATE runs SET status = ? WHERE run_id = ?", (status.value, run_id))

    def get(self, run_id: str) -> AnalysisResult | None:
        with self._lock:
            row = self._db.execute(
                "SELECT status, result_json FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        r = AnalysisResult.model_validate_json(row["result_json"])
        r.status = RunStatus(row["status"])
        return r

    def list(self, ticker: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        sql = "SELECT * FROM runs"
        args: list[Any] = []
        if ticker:
            sql += " WHERE ticker = ?"
            args.append(ticker)
        sql += " ORDER BY started_at DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [
            {
                "run_id": r["run_id"],
                "ticker": r["ticker"],
                "company_name": r["company_name"],
                "horizon": r["horizon"],
                "status": r["status"],
                "started_at": r["started_at"],
                "finished_at": r["finished_at"],
                "quality_gate_failures": r["gate_failures"],
                "error": r["error"],
            }
            for r in rows
        ]

    def delete(self, run_id: str) -> bool:
        with self._lock, self._db:
            cur = self._db.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
        return cur.rowcount > 0

    def mark_interrupted(self) -> int:
        """Runs left mid-pipeline by a previous process can never finish; fail them."""
        with self._lock:
            rows = self._db.execute(
                "SELECT run_id FROM runs WHERE status NOT IN (?, ?)", tuple(TERMINAL)
            ).fetchall()
        for row in rows:
            r = self.get(row["run_id"])
            if r is None:  # pragma: no cover - deleted concurrently
                continue
            r.status = RunStatus.FAILED
            r.error = "interrupted: server restarted before the run finished"
            r.finished_at = utc_now()
            self.save(r)
        return len(rows)

    def close(self) -> None:
        with self._lock:
            self._db.close()
