from datetime import date

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from stock_forecaster.models import AnalysisResult, RunStatus
from stock_forecaster.store import RunStore
from stock_forecaster.watch import check_item, diff, snapshot


def _variant(base: AnalysisResult, run_id: str, **fields) -> AnalysisResult:
    """Copy of `base` with evidence values replaced: field=value or field=(value, as_of)."""
    r = base.model_copy(deep=True)
    r.run_id = run_id
    for e in r.evidence:
        if e.field in fields:
            v = fields[e.field]
            e.value, e.as_of = v if isinstance(v, tuple) else (v, e.as_of)
    return r


def _price(r: AnalysisResult) -> float:
    return next(e.value for e in r.evidence if e.field == "price_intraday")


def test_routine_rerun_with_price_drift_is_silent(aapl_result):
    later = _variant(aapl_result, "run-2", price_intraday=_price(aapl_result) * 1.01)
    assert diff(snapshot(aapl_result), snapshot(later)) == []


def test_small_consensus_wiggle_is_silent_large_move_alerts(aapl_result):
    mean = next(f.value_mean for f in aapl_result.forecasts if f.value_mean)
    wiggle = aapl_result.model_copy(deep=True)
    wiggle.forecasts[0].value_mean = mean * 1.005
    assert diff(snapshot(aapl_result), snapshot(wiggle)) == []
    moved = aapl_result.model_copy(deep=True)
    moved.forecasts[0].value_mean = mean * 1.06
    [c] = diff(snapshot(aapl_result), snapshot(moved))
    assert c.kind == "data" and c.key.startswith("forecast:") and "mean" in c.message


def test_new_filing_period_and_restatement_are_data_changes(aapl_result):
    rev = next(
        e for e in aapl_result.evidence if e.field == "revenue_fy" and e.source_type == "filing"
    )
    new_period = _variant(aapl_result, "run-2", revenue_fy=(rev.value * 1.1, date(2099, 9, 30)))
    changes = diff(snapshot(aapl_result), snapshot(new_period))
    assert any(c.key == "filing:revenue_fy" and "new period" in c.message for c in changes)
    restated = _variant(aapl_result, "run-3", revenue_fy=rev.value - 1e6)
    changes = diff(snapshot(aapl_result), snapshot(restated))
    assert any(c.key == "filing:revenue_fy" and "restated" in c.message for c in changes)


def test_thesis_changes(aapl_result):
    prev = snapshot(aapl_result)
    lo = prev.thesis["scenario_range"][0]
    crashed = _variant(aapl_result, "run-2", price_intraday=lo * 0.9)
    assert any(
        c.key == "scenario_range" and "below" in c.message for c in diff(prev, snapshot(crashed))
    )

    flagged = aapl_result.model_copy(deep=True)
    fund = next(c for c in flagged.calculations if c.name == "fundamentals")
    fund.outputs["red_flags"] = [*fund.outputs["red_flags"], "negative free cash flow"]
    sc = next(c for c in flagged.calculations if c.name == "scenarios")
    sc.outputs["mode"] = "sales"
    keys = {c.key for c in diff(prev, snapshot(flagged)) if c.kind == "thesis"}
    assert keys == {"red_flags", "scenario_mode"}


def test_red_flag_with_moving_number_is_not_new():
    from stock_forecaster.watch import _flag_shape

    assert _flag_shape("diluted share count up 17.1% YoY") == _flag_shape(
        "diluted share count up 4.0% YoY"
    )


def test_check_item_baseline_unchanged_alert_and_failure(aapl_result, isolated_db):
    store = RunStore(isolated_db)
    wl = store.create_watchlist("core", "12m", ["AAPL"])
    runs = iter(
        [
            aapl_result,
            _variant(aapl_result, "run-b"),
            AnalysisResult(
                run_id="run-c",
                request=aapl_result.request,
                status=RunStatus.FAILED,
                started_at=aapl_result.started_at,
                error="yahoo down",
            ),
            _variant(aapl_result, "run-d", eps_forward=999.0),
        ]
    )
    runner = lambda req: next(runs)  # noqa: E731
    assert check_item(store, wl, "AAPL", "12m", runner).status == "baseline"
    assert check_item(store, wl, "AAPL", "12m", runner).status == "unchanged"
    failed = check_item(store, wl, "AAPL", "12m", runner)
    assert failed.status == "failed" and store.list_alerts() == []
    alert = check_item(store, wl, "AAPL", "12m", runner)
    assert alert.status == "alert"
    # compared against the last *successful* run, not the failed one
    [a] = store.list_alerts()
    assert a["prev_run_id"] == "run-b" and a["run_id"] == "run-d"
    assert [c["key"] for c in a["changes"]] == ["consensus:eps_forward"]
    assert store.get("run-c").status == RunStatus.FAILED  # failed runs still land in history


def test_watchlist_api_flow(aapl_result, monkeypatch):
    import stock_forecaster.api as api

    results = iter([aapl_result, _variant(aapl_result, "run-2", eps_forward=999.0)])
    monkeypatch.setattr(api, "run_analysis", lambda request, on_status=None: next(results))
    client = TestClient(api.app)
    wl = client.post("/v1/watchlists", json={"name": "core", "tickers": ["aapl", "AAPL"]}).json()
    assert [i["ticker"] for i in wl["items"]] == ["AAPL"]
    assert client.post("/v1/watchlists", json={"name": "core"}).status_code == 409

    assert client.post(f"/v1/watchlists/{wl['id']}/check").status_code == 202
    got = client.get(f"/v1/watchlists/{wl['id']}").json()
    assert got["items"][0]["has_baseline"] and not got["checking"]
    assert client.get("/v1/alerts").json() == []

    client.post(f"/v1/watchlists/{wl['id']}/check")
    [alert] = client.get("/v1/alerts").json()
    assert alert["ticker"] == "AAPL" and alert["changes"][0]["kind"] == "data"
    assert client.get("/v1/watchlists").json()[0]["open_alerts"] == 1
    assert client.post(f"/v1/alerts/{alert['id']}/ack").status_code == 200
    assert client.get("/v1/alerts").json() == []
    assert len(client.get("/v1/alerts?include_acknowledged=true").json()) == 1

    client.post(f"/v1/watchlists/{wl['id']}/tickers", json={"ticker": "brk.b"})
    assert [i["ticker"] for i in client.get(f"/v1/watchlists/{wl['id']}").json()["items"]] == [
        "AAPL",
        "BRK-B",
    ]
    client.delete(f"/v1/watchlists/{wl['id']}/tickers/BRK.B")
    assert client.delete(f"/v1/watchlists/{wl['id']}").status_code == 204
    assert client.get(f"/v1/watchlists/{wl['id']}").status_code == 404


def test_cli_watch_check_exit_codes(aapl_result, isolated_db, monkeypatch):
    import stock_forecaster.cli as cli

    RunStore(isolated_db).create_watchlist("core", "12m", ["AAPL"])
    results = iter([aapl_result, _variant(aapl_result, "run-2", eps_forward=999.0)])
    monkeypatch.setattr(cli, "run_analysis", lambda request, on_status=None: next(results))
    runner = CliRunner()
    first = runner.invoke(cli.app, ["watch-check", "--db", str(isolated_db)])
    assert first.exit_code == 0 and "baseline" in first.output
    second = runner.invoke(cli.app, ["watch-check", "--db", str(isolated_db)])
    assert second.exit_code == 3 and "[data] eps_forward" in second.output


def test_exact_threshold_moves_alert(aapl_result):
    from stock_forecaster.watch import Snapshot

    def snap(reco, eps):
        return Snapshot(
            run_id="r",
            data={
                "consensus:recommendation_mean": {"value": reco},
                "consensus:eps_forward": {"value": eps},
            },
        )

    keys = {c.key for c in diff(snap(2.3, 7.0), snap(2.1, 7.14))}
    assert keys == {"consensus:recommendation_mean", "consensus:eps_forward"}
