"""Phase 2: user-supplied forecasts, duplicate/stale/outlier flags, critic/auditor loop, UI/API."""

from datetime import date, timedelta

from fastapi.testclient import TestClient

from stock_forecaster.adapters import SecEdgarAdapter, YahooAdapter, fidelity_adapter, msn_adapter
from stock_forecaster.adapters.user_supplied import UserForecastAdapter
from stock_forecaster.llm import NullProvider
from stock_forecaster.models import (
    DISCLAIMER,
    AnalysisRequest,
    RetrievalStatus,
    ReviewSeverity,
    UserForecast,
)
from stock_forecaster.pipeline import run_analysis


def _by_source(res):
    return {f.source.split(" (")[0]: f for f in res.forecasts}


def _run(aapl_yahoo, aapl_sec, forecasts):
    adapters = [
        YahooAdapter(info=aapl_yahoo["info"], history=aapl_yahoo["history"]),
        SecEdgarAdapter(facts=aapl_sec, cik=aapl_sec["cik"]),
        fidelity_adapter(),
        msn_adapter(),
        UserForecastAdapter(forecasts),
    ]
    req = AnalysisRequest(ticker="AAPL", user_forecasts=forecasts)
    return run_analysis(req, adapters=adapters, provider=NullProvider())


def test_user_forecast_is_snippet_only_low_tier():
    uf = UserForecast(source="Fidelity", value_mean=310.0, as_of=date.today(), analyst_count=30)
    recs = UserForecastAdapter([uf]).collect("AAPL")
    assert recs and all(r.retrieval_status == RetrievalStatus.SNIPPET_ONLY for r in recs)
    assert all(r.tier == 5 and r.confidence <= 0.4 for r in recs)
    assert all("user" in (r.notes or "").lower() for r in recs)


def test_duplicate_forecast_detected_not_averaged(aapl_yahoo, aapl_sec):
    today = date.today()
    fid = UserForecast(
        source="Fidelity", value_low=250, value_mean=310, value_high=350, as_of=today
    )
    msn = UserForecast(
        source="MSN Money", value_low=250, value_mean=310, value_high=350, as_of=today
    )
    res = _run(aapl_yahoo, aapl_sec, [fid, msn])
    by_src = _by_source(res)
    assert by_src["Fidelity"].value_mean == 310 and by_src["MSN Money"].value_mean == 310
    assert by_src["MSN Money"].duplicate_of == by_src["Fidelity"].source
    assert by_src["Fidelity"].duplicate_of is None
    assert "310" in res.report_markdown and "duplicate" in res.report_markdown.lower()
    assert any(r.check == "duplicate_forecast" for r in res.reviews)
    assert res.revision_count == 1
    assert res.quality_gate_failures == []


def test_stale_and_outlier_forecasts_flagged(aapl_yahoo, aapl_sec):
    old = date.today() - timedelta(days=120)
    stale = UserForecast(source="Fidelity", value_mean=300, as_of=old)
    wild = UserForecast(source="Other", value_mean=900, as_of=date.today())
    res = _run(aapl_yahoo, aapl_sec, [stale, wild])
    by_src = _by_source(res)
    assert by_src["Fidelity"].stale is True
    assert by_src["Other"].outlier is True
    assert by_src["Other"].method_disclosed is False
    checks = {r.check for r in res.reviews}
    assert {"stale_forecast", "outlier_forecast"} <= checks
    assert all(r.severity in ReviewSeverity for r in res.reviews)


def test_user_forecast_never_verified(aapl_yahoo, aapl_sec):
    res = _run(
        aapl_yahoo, aapl_sec, [UserForecast(source="MSN Money", value_mean=280, as_of=date.today())]
    )
    user_recs = [e for e in res.evidence if e.source_name.startswith("MSN") and e.value is not None]
    assert user_recs and all(e.retrieval_status == RetrievalStatus.SNIPPET_ONLY for e in user_recs)
    assert "user-supplied" in res.report_markdown.lower()


def test_review_runs_on_plain_result(aapl_result):
    assert aapl_result.revision_count in (0, 1)
    assert not any(
        r.severity == ReviewSeverity.BLOCKING and r.resolution is None for r in aapl_result.reviews
    )
    assert "## Review notes" in aapl_result.report_markdown


def test_ui_and_history_endpoints(aapl_result, monkeypatch):
    import stock_forecaster.api as api

    monkeypatch.setattr(api, "run_analysis", lambda request, on_status=None: aapl_result)
    client = TestClient(api.app)
    home = client.get("/")
    assert home.status_code == 200 and DISCLAIMER in home.text and "/static/app.js" in home.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200

    run_id = client.post("/v1/analyses", json={"ticker": "AAPL"}).json()["run_id"]
    hist = client.get("/v1/analyses").json()
    assert hist and hist[0]["run_id"] == run_id and hist[0]["ticker"] == "AAPL"
    full = client.get(f"/v1/analyses/{run_id}/full").json()
    assert {
        "scenarios",
        "forecasts",
        "evidence",
        "reviews",
        "calculations",
        "report_markdown",
    } <= full.keys()
    assert any(c["name"] == "scenarios" and "mode" in c["inputs"] for c in full["calculations"])
