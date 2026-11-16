from fastapi.testclient import TestClient

from stock_forecaster.models import AnalysisRequest, AnalysisResult, RunStatus, utc_now
from stock_forecaster.store import RunStore


def test_round_trip_survives_reopen(aapl_result, isolated_db):
    store = RunStore(isolated_db)
    store.save(aapl_result)
    store.close()

    reopened = RunStore(isolated_db)
    back = reopened.get(aapl_result.run_id)
    assert back is not None
    assert back.model_dump(mode="json") == aapl_result.model_dump(mode="json")
    [row] = reopened.list_runs()
    assert row["ticker"] == "AAPL" and row["status"] == "complete"
    assert reopened.list_runs(ticker="MSFT") == []


def test_interrupted_runs_are_failed_on_restart(isolated_db):
    store = RunStore(isolated_db)
    r = AnalysisResult(
        run_id="run-x",
        request=AnalysisRequest(ticker="AAPL"),
        status=RunStatus.QUEUED,
        started_at=utc_now(),
    )
    store.save(r)
    store.set_status("run-x", RunStatus.MODELING)
    assert store.get("run-x").status == RunStatus.MODELING
    assert store.mark_interrupted() == 1
    after = store.get("run-x")
    assert after.status == RunStatus.FAILED and "interrupted" in after.error
    assert store.mark_interrupted() == 0


def test_api_history_persists_across_restart(aapl_result, isolated_db, monkeypatch):
    import stock_forecaster.api as api

    monkeypatch.setattr(api, "run_analysis", lambda request, on_status=None: aapl_result)
    client = TestClient(api.app)
    run_id = client.post("/v1/analyses", json={"ticker": "aapl"}).json()["run_id"]
    assert client.get(f"/v1/analyses/{run_id}").json()["status"] == "complete"

    api.configure_store(str(isolated_db))  # simulates a process restart on the same file
    hist = client.get("/v1/analyses?ticker=AAPL").json()
    assert [h["run_id"] for h in hist] == [run_id]
    report = client.get(f"/v1/analyses/{run_id}/report")
    assert report.status_code == 200 and report.text == aapl_result.report_markdown

    assert client.delete(f"/v1/analyses/{run_id}").status_code == 204
    assert client.get(f"/v1/analyses/{run_id}").status_code == 404
    assert client.delete(f"/v1/analyses/{run_id}").status_code == 404


def test_pipeline_crash_is_recorded_not_lost(monkeypatch):
    import stock_forecaster.api as api

    def boom(request, on_status=None):
        raise RuntimeError("adapter exploded")

    monkeypatch.setattr(api, "run_analysis", boom)
    client = TestClient(api.app)
    run_id = client.post("/v1/analyses", json={"ticker": "AAPL"}).json()["run_id"]
    r = client.get(f"/v1/analyses/{run_id}").json()
    assert r["status"] == "failed" and "adapter exploded" in r["error"]


def test_active_run_cannot_be_deleted(aapl_result):
    import stock_forecaster.api as api

    store = api.get_store()
    queued = aapl_result.model_copy(update={"run_id": "run-q", "status": RunStatus.COLLECTING})
    store.save(queued)
    client = TestClient(api.app)
    assert client.delete("/v1/analyses/run-q").status_code == 409
    assert store.get("run-q") is not None
