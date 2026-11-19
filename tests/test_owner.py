"""Per-user data: runs, watchlists and alerts are scoped by the X-SF-User header."""

import sqlite3

from fastapi.testclient import TestClient

from stock_forecaster.store import RunStore
from stock_forecaster.watch import check_watchlist

U1 = {"X-SF-User": "u1"}
U2 = {"X-SF-User": "u2"}


def _client(monkeypatch, result):
    import stock_forecaster.api as api

    monkeypatch.setattr(api, "run_analysis", lambda request, on_status=None: result)
    return TestClient(api.app)


def test_runs_are_private_to_their_owner(aapl_result, isolated_db, monkeypatch):
    client = _client(monkeypatch, aapl_result)
    run_id = client.post("/v1/analyses", json={"ticker": "AAPL"}, headers=U1).json()["run_id"]

    assert [h["run_id"] for h in client.get("/v1/analyses", headers=U1).json()] == [run_id]
    assert client.get("/v1/analyses", headers=U2).json() == []
    assert client.get("/v1/analyses").json() == []  # no header = the shared, empty owner
    for path in ("", "/full", "/evidence", "/report"):
        assert client.get(f"/v1/analyses/{run_id}{path}", headers=U2).status_code == 404
    assert client.delete(f"/v1/analyses/{run_id}", headers=U2).status_code == 404
    assert client.get(f"/v1/analyses/{run_id}", headers=U1).json()["status"] == "complete"


def test_watchlists_and_alerts_are_private(aapl_result, isolated_db, monkeypatch):
    client = _client(monkeypatch, aapl_result)
    body = {"name": "core", "tickers": ["AAPL"]}
    wl = client.post("/v1/watchlists", json=body, headers=U1).json()
    # Same name is fine for another user, but not twice for one user.
    assert client.post("/v1/watchlists", json=body, headers=U2).status_code == 201
    assert client.post("/v1/watchlists", json=body, headers=U1).status_code == 409

    assert [w["id"] for w in client.get("/v1/watchlists", headers=U1).json()] == [wl["id"]]
    wid = wl["id"]
    assert client.get(f"/v1/watchlists/{wid}", headers=U2).status_code == 404
    assert client.post(f"/v1/watchlists/{wid}/check", headers=U2).status_code == 404
    add = client.post(f"/v1/watchlists/{wid}/tickers", json={"ticker": "MSFT"}, headers=U2)
    assert add.status_code == 404
    assert client.delete(f"/v1/watchlists/{wid}/tickers/AAPL", headers=U2).status_code == 404
    assert client.delete(f"/v1/watchlists/{wid}", headers=U2).status_code == 404

    import stock_forecaster.api as api

    store = api.get_store()
    alert_id = store.add_alert(wid, "AAPL", run_id="r1", prev_run_id=None, changes=[])
    assert [a["id"] for a in client.get("/v1/alerts", headers=U1).json()] == [alert_id]
    assert client.get("/v1/alerts", headers=U2).json() == []
    assert client.post(f"/v1/alerts/{alert_id}/ack", headers=U2).status_code == 404
    assert client.post(f"/v1/alerts/{alert_id}/ack", headers=U1).status_code == 200
    assert client.delete(f"/v1/watchlists/{wid}", headers=U1).status_code == 204


def test_watchlist_check_saves_runs_under_the_watchlist_owner(aapl_result, isolated_db):
    store = RunStore(isolated_db)
    wid = store.create_watchlist("core", "12m", ["AAPL"], owner="u7")
    check_watchlist(store, wid, lambda req: aapl_result)
    assert [r["run_id"] for r in store.list_runs(owner="u7")] == [aapl_result.run_id]
    assert store.list_runs(owner="") == []
    store.save(aapl_result)  # a later update keeps the original owner
    assert store.list_runs(owner="u7") != []


OLD_SCHEMA = """
CREATE TABLE runs (run_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, horizon TEXT NOT NULL,
    status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, company_name TEXT,
    gate_failures INTEGER NOT NULL DEFAULT 0, error TEXT, result_json TEXT NOT NULL);
CREATE TABLE watchlists (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
    horizon TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE watchlist_items (watchlist_id INTEGER NOT NULL REFERENCES watchlists(id)
    ON DELETE CASCADE, ticker TEXT NOT NULL, added_at TEXT NOT NULL, last_run_id TEXT,
    last_checked_at TEXT, last_error TEXT, baseline_json TEXT, PRIMARY KEY (watchlist_id, ticker));
CREATE TABLE alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, watchlist_id INTEGER NOT NULL
    REFERENCES watchlists(id) ON DELETE CASCADE, ticker TEXT NOT NULL, run_id TEXT NOT NULL,
    prev_run_id TEXT, created_at TEXT NOT NULL, changes_json TEXT NOT NULL,
    acknowledged INTEGER NOT NULL DEFAULT 0);
INSERT INTO watchlists VALUES (5, 'core', '12m', '2026-01-01');
INSERT INTO watchlist_items (watchlist_id, ticker, added_at) VALUES (5, 'AAPL', '2026-01-01');
INSERT INTO alerts (watchlist_id, ticker, run_id, created_at, changes_json)
    VALUES (5, 'AAPL', 'r1', '2026-01-01', '[]');
"""


def test_database_from_before_owners_is_migrated(aapl_result, isolated_db):
    db = sqlite3.connect(isolated_db)
    db.executescript(OLD_SCHEMA)
    db.execute(
        "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "run-old",
            "AAPL",
            "12m",
            "complete",
            "2026-01-01",
            None,
            None,
            0,
            None,
            aapl_result.model_dump_json(),
        ),
    )
    db.commit()
    db.close()

    store = RunStore(isolated_db)
    assert [r["run_id"] for r in store.list_runs(owner="")] == ["run-old"]
    wl = store.get_watchlist(5, owner="")
    assert wl is not None and [i["ticker"] for i in wl["items"]] == ["AAPL"]
    assert [a["watchlist_id"] for a in store.list_alerts(owner="")] == [5]
    store.create_watchlist("core", "12m", [], owner="u1")  # old UNIQUE(name) is gone
    assert store.delete_watchlist(5, owner="")
    assert store.list_alerts(owner="", include_acknowledged=True) == []  # cascade still works
    store.close()
    RunStore(isolated_db).close()  # reopening a migrated file is a no-op


def test_concurrent_openers_migrate_an_old_file_once(isolated_db):
    import threading

    db = sqlite3.connect(isolated_db)
    db.executescript(OLD_SCHEMA)
    db.close()
    barrier = threading.Barrier(4)
    errors: list[BaseException] = []

    def open_store() -> None:
        barrier.wait()
        try:
            RunStore(isolated_db).close()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=open_store) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    store = RunStore(isolated_db)
    assert store.get_watchlist(5, owner="") is not None
    store.close()


def test_cli_named_check_never_runs_another_users_list(aapl_result, isolated_db, monkeypatch):
    from typer.testing import CliRunner

    import stock_forecaster.cli as cli

    monkeypatch.setattr(cli, "run_analysis", lambda req: aapl_result)
    store = RunStore(isolated_db)
    store.create_watchlist("core", "12m", ["AAPL"], owner="u1")
    store.create_watchlist("core", "12m", ["AAPL"], owner="u2")
    runner = CliRunner()
    base = ["watch-check", "--db", str(isolated_db), "--name", "core"]
    ambiguous = runner.invoke(cli.app, base)
    assert ambiguous.exit_code == 1 and store.list_runs() == []
    assert runner.invoke(cli.app, [*base, "--owner", "u2"]).exit_code == 0
    assert store.list_runs(owner="u1") == [] and len(store.list_runs(owner="u2")) == 1
