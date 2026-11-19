"""SQLite persistence for run history (survives server restarts).

One row per run: summary columns for the history list, plus the full
`AnalysisResult` JSON so a stored run renders exactly as it did when it finished.

Runs and watchlists carry an `owner` (empty for the standalone app). Methods take
`owner=None` to mean "any owner" (CLI, internal use) or a string to scope to one user.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Callable
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
    result_json   TEXT NOT NULL,
    owner         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS runs_started ON runs (started_at DESC);
CREATE INDEX IF NOT EXISTS runs_ticker ON runs (ticker, started_at DESC);
CREATE INDEX IF NOT EXISTS runs_owner ON runs (owner, started_at DESC);
CREATE TABLE IF NOT EXISTS watchlists (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    horizon    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    owner      TEXT NOT NULL DEFAULT '',
    UNIQUE (owner, name)
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

_RUN_COLS = (
    "run_id, ticker, horizon, status, started_at, finished_at, company_name, "
    "gate_failures, error, result_json"
)


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}


def _migrate(db: sqlite3.Connection) -> None:
    """Add `owner` to databases created before per-user data. Existing rows stay
    with the empty owner (the standalone app). BEGIN IMMEDIATE serializes processes
    opening the same old file at once; the loser re-reads the upgraded schema."""
    # Only changeable outside a transaction. With foreign keys off and legacy_alter_table
    # on, the watchlists rebuild keeps child tables pointing at `watchlists`.
    db.execute("PRAGMA foreign_keys = OFF")
    db.execute("PRAGMA legacy_alter_table = ON")
    try:
        db.execute("BEGIN IMMEDIATE")
        try:
            cols = {t: _columns(db, t) for t in ("runs", "watchlists")}
            if cols["runs"] and "owner" not in cols["runs"]:
                db.execute("ALTER TABLE runs ADD COLUMN owner TEXT NOT NULL DEFAULT ''")
            if cols["watchlists"] and "owner" not in cols["watchlists"]:
                # Rebuild to swap UNIQUE(name) for UNIQUE(owner, name); ids are kept.
                db.execute("ALTER TABLE watchlists RENAME TO watchlists_old")
                db.execute(
                    "CREATE TABLE watchlists (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    " name TEXT NOT NULL, horizon TEXT NOT NULL, created_at TEXT NOT NULL,"
                    " owner TEXT NOT NULL DEFAULT '', UNIQUE (owner, name))"
                )
                db.execute(
                    "INSERT INTO watchlists (id, name, horizon, created_at)"
                    " SELECT id, name, horizon, created_at FROM watchlists_old"
                )
                db.execute("DROP TABLE watchlists_old")
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    finally:
        db.execute("PRAGMA legacy_alter_table = OFF")
        db.execute("PRAGMA foreign_keys = ON")


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
        with self._lock:
            if self.path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            _migrate(self._db)  # before the schema script, which indexes `owner`
            self._db.executescript(_SCHEMA)
            _migrate(self._db)
            self._db.commit()

    def save(self, r: AnalysisResult, owner: str = "") -> None:
        """Insert or update a run. `owner` is set on insert only; updates keep it."""
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
            owner,
        )
        updates = ", ".join(f"{c} = excluded.{c}" for c in _RUN_COLS.split(", ")[1:])
        with self._lock, self._db:
            self._db.execute(
                f"INSERT INTO runs ({_RUN_COLS}, owner) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                f"ON CONFLICT(run_id) DO UPDATE SET {updates}",
                row,
            )

    def set_status(self, run_id: str, status: RunStatus) -> None:
        # Only the summary column; the JSON is rewritten when the run finishes.
        with self._lock, self._db:
            self._db.execute("UPDATE runs SET status = ? WHERE run_id = ?", (status.value, run_id))

    def get(self, run_id: str, owner: str | None = None) -> AnalysisResult | None:
        with self._lock:
            row = self._db.execute(
                "SELECT status, result_json, owner FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None or (owner is not None and row["owner"] != owner):
            return None
        r = AnalysisResult.model_validate_json(row["result_json"])
        r.status = RunStatus(row["status"])
        return r

    def list_runs(
        self, ticker: str | None = None, limit: int = 200, owner: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM runs"
        where: list[str] = []
        args: list[Any] = []
        if ticker:
            where.append("ticker = ?")
            args.append(ticker)
        if owner is not None:
            where.append("owner = ?")
            args.append(owner)
        if where:
            sql += " WHERE " + " AND ".join(where)
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
    def create_watchlist(self, name: str, horizon: str, tickers: list[str], owner: str = "") -> int:
        now = utc_now().isoformat()
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO watchlists (name, horizon, created_at, owner) VALUES (?,?,?,?)",
                (name, horizon, now, owner),
            )
            wl_id = int(cur.lastrowid or 0)
            self._db.executemany(
                "INSERT OR IGNORE INTO watchlist_items (watchlist_id, ticker, added_at) "
                "VALUES (?,?,?)",
                [(wl_id, t, now) for t in tickers],
            )
        return wl_id

    def list_watchlists(self, owner: str | None = None) -> list[dict[str, Any]]:
        where, args = ("WHERE w.owner = ? ", (owner,)) if owner is not None else ("", ())
        with self._lock:
            rows = self._db.execute(
                "SELECT w.*, "
                "(SELECT COUNT(*) FROM watchlist_items i WHERE i.watchlist_id = w.id) AS n_items, "
                "(SELECT COUNT(*) FROM alerts a WHERE a.watchlist_id = w.id "
                " AND a.acknowledged = 0) AS open_alerts "
                f"FROM watchlists w {where}ORDER BY w.name",
                args,
            ).fetchall()
        return [dict(r) for r in rows]

    def get_watchlist(self, wl_id: int, owner: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            wl = self._db.execute("SELECT * FROM watchlists WHERE id = ?", (wl_id,)).fetchone()
            if wl is None or (owner is not None and wl["owner"] != owner):
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

    def delete_watchlist(self, wl_id: int, owner: str | None = None) -> bool:
        sql = "DELETE FROM watchlists WHERE id = ?"
        args: list[Any] = [wl_id]
        if owner is not None:
            sql += " AND owner = ?"
            args.append(owner)
        with self._lock, self._db:
            cur = self._db.execute(sql, args)
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

    def advance_baseline(
        self,
        wl_id: int,
        ticker: str,
        *,
        run_id: str,
        baseline: str,
        decide: Callable[[str], tuple[str | None, list[dict[str, Any]]]],
    ) -> tuple[bool, int | None, list[dict[str, Any]]]:
        """Swap in a new baseline and record any alert in one write transaction.
        `decide(prev_baseline)` returns (prev_run_id, changes). Returns
        (had_baseline, alert_id, changes). BEGIN IMMEDIATE serializes concurrent
        checks (other threads or processes) on the same DB, so two checks cannot
        both diff against the same old baseline, and a failed alert insert rolls
        the baseline back so the change is reported on the next check."""
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute(
                "SELECT baseline_json FROM watchlist_items WHERE watchlist_id = ? AND ticker = ?",
                (wl_id, ticker),
            ).fetchone()
            prev = row["baseline_json"] if row else None
            prev_run_id, changes = decide(prev) if prev is not None else (None, [])
            self._db.execute(
                "UPDATE watchlist_items SET last_checked_at = ?, last_error = NULL, "
                "last_run_id = ?, baseline_json = ? WHERE watchlist_id = ? AND ticker = ?",
                (utc_now().isoformat(), run_id, baseline, wl_id, ticker),
            )
            alert_id = None
            if changes:
                cur = self._db.execute(
                    "INSERT INTO alerts (watchlist_id, ticker, run_id, prev_run_id, created_at, "
                    "changes_json) VALUES (?,?,?,?,?,?)",
                    (
                        wl_id,
                        ticker,
                        run_id,
                        prev_run_id,
                        utc_now().isoformat(),
                        json.dumps(changes),
                    ),
                )
                alert_id = int(cur.lastrowid or 0)
        return prev is not None, alert_id, changes

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
        self,
        wl_id: int | None = None,
        include_acknowledged: bool = False,
        limit: int = 200,
        owner: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT a.*, w.name AS watchlist FROM alerts a JOIN watchlists w ON w.id = a.watchlist_id"
        where: list[str] = []
        args: list[Any] = []
        if owner is not None:
            where.append("w.owner = ?")
            args.append(owner)
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

    def ack_alert(self, alert_id: int, owner: str | None = None) -> bool:
        sql = "UPDATE alerts SET acknowledged = 1 WHERE id = ?"
        args: list[Any] = [alert_id]
        if owner is not None:
            sql += " AND watchlist_id IN (SELECT id FROM watchlists WHERE owner = ?)"
            args.append(owner)
        with self._lock, self._db:
            cur = self._db.execute(sql, args)
        return cur.rowcount > 0

    def close(self) -> None:
        with self._lock:
            self._db.close()
