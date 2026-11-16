"""SQLite persistence for run history (survives server restarts).

One row per run: summary columns for the history list, plus the full
`AnalysisResult` JSON so a stored run renders exactly as it did when it finished.
"""

from __future__ import annotations

import json
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
CREATE TABLE IF NOT EXISTS watchlists (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    horizon    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watchlist_items (
    watchlist_id    INTEGER NOT NULL REFERENCES watchlists(id) ON DELETE CASCADE,
    ticker          TEXT NOT NULL,
    added_at        TEXT NOT NULL,
    last_run_id     TEXT,
    last_checked_at TEXT,
    last_error      TEXT,
    baseline_json   TEXT,
    PRIMARY KEY (watchlist_id, ticker)
);
CREATE TABLE IF NOT EXISTS alerts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    watchlist_id INTEGER NOT NULL REFERENCES watchlists(id) ON DELETE CASCADE,
    ticker       TEXT NOT NULL,
    run_id       TEXT NOT NULL,
    prev_run_id  TEXT,
    created_at   TEXT NOT NULL,
    changes_json TEXT NOT NULL,
    acknowledged INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS alerts_wl ON alerts (watchlist_id, created_at DESC);
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
        self._db.execute("PRAGMA foreign_keys = ON")
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

    def list_runs(self, ticker: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
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

    # ---------- watchlists ----------
    def create_watchlist(self, name: str, horizon: str, tickers: list[str]) -> int:
        now = utc_now().isoformat()
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO watchlists (name, horizon, created_at) VALUES (?,?,?)",
                (name, horizon, now),
            )
            wl_id = int(cur.lastrowid or 0)
            self._db.executemany(
                "INSERT OR IGNORE INTO watchlist_items (watchlist_id, ticker, added_at) "
                "VALUES (?,?,?)",
                [(wl_id, t, now) for t in tickers],
            )
        return wl_id

    def list_watchlists(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT w.*, "
                "(SELECT COUNT(*) FROM watchlist_items i WHERE i.watchlist_id = w.id) AS n_items, "
                "(SELECT COUNT(*) FROM alerts a WHERE a.watchlist_id = w.id "
                " AND a.acknowledged = 0) AS open_alerts "
                "FROM watchlists w ORDER BY w.name"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_watchlist(self, wl_id: int) -> dict[str, Any] | None:
        with self._lock:
            wl = self._db.execute("SELECT * FROM watchlists WHERE id = ?", (wl_id,)).fetchone()
            if wl is None:
                return None
            items = self._db.execute(
                "SELECT ticker, added_at, last_run_id, last_checked_at, last_error, "
                "baseline_json IS NOT NULL AS has_baseline FROM watchlist_items "
                "WHERE watchlist_id = ? ORDER BY ticker",
                (wl_id,),
            ).fetchall()
        out = dict(wl)
        out["items"] = [dict(i, has_baseline=bool(i["has_baseline"])) for i in items]
        return out

    def delete_watchlist(self, wl_id: int) -> bool:
        with self._lock, self._db:
            cur = self._db.execute("DELETE FROM watchlists WHERE id = ?", (wl_id,))
        return cur.rowcount > 0

    def add_item(self, wl_id: int, ticker: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO watchlist_items (watchlist_id, ticker, added_at) "
                "VALUES (?,?,?)",
                (wl_id, ticker, utc_now().isoformat()),
            )

    def remove_item(self, wl_id: int, ticker: str) -> bool:
        with self._lock, self._db:
            cur = self._db.execute(
                "DELETE FROM watchlist_items WHERE watchlist_id = ? AND ticker = ?",
                (wl_id, ticker),
            )
        return cur.rowcount > 0

    def item_baseline(self, wl_id: int, ticker: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT baseline_json FROM watchlist_items WHERE watchlist_id = ? AND ticker = ?",
                (wl_id, ticker),
            ).fetchone()
        return row["baseline_json"] if row else None

    def update_item(
        self,
        wl_id: int,
        ticker: str,
        *,
        run_id: str | None = None,
        baseline: str | None = None,
        error: str | None = None,
    ) -> None:
        """Record a check. `baseline` is only replaced when given, so a failed run
        keeps the previous baseline."""
        sets = ["last_checked_at = ?", "last_error = ?"]
        args: list[Any] = [utc_now().isoformat(), error]
        if run_id is not None:
            sets.append("last_run_id = ?")
            args.append(run_id)
        if baseline is not None:
            sets.append("baseline_json = ?")
            args.append(baseline)
        args += [wl_id, ticker]
        with self._lock, self._db:
            self._db.execute(
                f"UPDATE watchlist_items SET {', '.join(sets)} "
                "WHERE watchlist_id = ? AND ticker = ?",
                args,
            )

    def add_alert(
        self,
        wl_id: int,
        ticker: str,
        *,
        run_id: str,
        prev_run_id: str | None,
        changes: list[dict[str, Any]],
    ) -> int:
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO alerts (watchlist_id, ticker, run_id, prev_run_id, created_at, "
                "changes_json) VALUES (?,?,?,?,?,?)",
                (wl_id, ticker, run_id, prev_run_id, utc_now().isoformat(), json.dumps(changes)),
            )
        return int(cur.lastrowid or 0)

    def list_alerts(
        self, wl_id: int | None = None, include_acknowledged: bool = False, limit: int = 200
    ) -> list[dict[str, Any]]:
        sql = "SELECT a.*, w.name AS watchlist FROM alerts a JOIN watchlists w ON w.id = a.watchlist_id"
        where, args = [], []
        if wl_id is not None:
            where.append("a.watchlist_id = ?")
            args.append(wl_id)
        if not include_acknowledged:
            where.append("a.acknowledged = 0")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY a.id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["changes"] = json.loads(d.pop("changes_json"))
            d["acknowledged"] = bool(d["acknowledged"])
            out.append(d)
        return out

    def ack_alert(self, alert_id: int) -> bool:
        with self._lock, self._db:
            cur = self._db.execute("UPDATE alerts SET acknowledged = 1 WHERE id = ?", (alert_id,))
        return cur.rowcount > 0

    def close(self) -> None:
        with self._lock:
            self._db.close()
