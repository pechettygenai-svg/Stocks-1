import re

from fastapi.testclient import TestClient

from stock_forecaster.adapters import SecEdgarAdapter, YahooAdapter
from stock_forecaster.adapters.base import AdapterError
from stock_forecaster.ledger import EvidenceLedger
from stock_forecaster.llm import NullProvider
from stock_forecaster.models import (
    DISCLAIMER,
    AnalysisRequest,
    EvidenceRecord,
    RetrievalStatus,
    RunStatus,
    SourceType,
)
from stock_forecaster.pipeline import run_analysis
from stock_forecaster.quality import run_quality_gates


def test_run_completes_and_passes_quality_gates(aapl_result):
    assert aapl_result.status == RunStatus.COMPLETE, aapl_result.error
    assert aapl_result.quality_gate_failures == []
    md = aapl_result.report_markdown
    assert md.startswith("# Apple Inc. (AAPL)")
    assert DISCLAIMER in md.splitlines()[1]
    for section in (
        "## Executive view",
        "## Scenario range",
        "## External forecast comparison",
        "## Limitations",
        "## Sources",
    ):
        assert section in md


def test_sec_facts_prefer_recent_concept_and_period(aapl_result):
    ev = {e.field: e for e in aapl_result.evidence}
    rev = ev["revenue_fy"]
    # Apple reports revenue under RevenueFromContract...; the stale `Revenues`
    # concept (last used FY2018) must not win.
    assert rev.definition.endswith("RevenueFromContractWithCustomerExcludingAssessedTax")
    assert rev.period == "FY2025" and ev["revenue_fy_prior"].period == "FY2024"
    assert rev.value > 4e11
    assert rev.source_type == SourceType.FILING and rev.tier == 1


def test_every_citation_in_report_exists(aapl_result):
    ids = {e.id for e in aapl_result.evidence}
    cited = set(re.findall(r"\[(ev-\d+)\]", aapl_result.report_markdown))
    assert cited and cited <= ids


def test_unavailable_sources_are_listed_not_fabricated(aapl_result):
    panel = {f.source: f for f in aapl_result.forecasts}
    for src in ("Fidelity", "MSN Money"):
        assert panel[src].status == RetrievalStatus.UNAVAILABLE
        assert panel[src].value_mean is None
    assert panel["Yahoo Finance (analyst consensus)"].method_disclosed is False
    assert "unavailable: Fidelity, MSN Money" in aapl_result.report_markdown


def test_market_data_conflict_is_surfaced(aapl_result):
    from stock_forecaster.validation import validate

    fields = [e.field for e in aapl_result.evidence]
    assert "revenue_fy" in fields and "revenue_ttm" in fields  # both retained
    ledger = EvidenceLedger()
    ledger.extend([e.model_copy() for e in aapl_result.evidence])
    ledger.add(
        EvidenceRecord(
            claim="rev",
            field="revenue_fy",
            value=1.0,
            source_name="SEC",
            source_type=SourceType.FILING,
            tier=0,
        )
    )
    assert any(w.startswith("revenue: filing FY") for w in validate(ledger))


def test_adapter_outage_yields_unavailable_record(aapl_yahoo):
    class Broken(SecEdgarAdapter):
        def resolve_cik(self, ticker):
            raise AdapterError("rate limited")

    res = run_analysis(
        AnalysisRequest(ticker="AAPL", include_sources=["yahoo", "sec"]),
        adapters=[YahooAdapter(info=aapl_yahoo["info"], history=aapl_yahoo["history"]), Broken()],
        provider=NullProvider(),
    )
    assert res.status == RunStatus.COMPLETE
    unavailable = [e for e in res.evidence if e.retrieval_status == RetrievalStatus.UNAVAILABLE]
    assert any("rate limited" in (e.notes or "") for e in unavailable)
    assert any("source unavailable: SEC EDGAR" in w for w in res.validation_warnings)
    assert res.quality_gate_failures == []


def test_unknown_ticker_marks_yahoo_unavailable():
    class Dead(YahooAdapter):
        def _fetch(self, ticker):
            raise AdapterError("no quote returned")

    res = run_analysis(AnalysisRequest(ticker="ZZZZ"), adapters=[Dead()], provider=NullProvider())
    assert res.status == RunStatus.COMPLETE
    assert res.scenarios == []
    assert "bull/base/bear scenarios missing" in res.quality_gate_failures


def test_quality_gate_rejects_certainty_language(aapl_result):
    ledger = EvidenceLedger()
    ledger.extend([e.model_copy() for e in aapl_result.evidence])
    bad = aapl_result.model_copy(deep=True)
    bad.report_markdown = bad.report_markdown.replace(
        "## Executive view",
        "## Executive view\nThe stock will reach 500 and is a strong buy. [ev-999]",
    )
    failures = run_quality_gates(bad, ledger)
    assert any("will reach" in f for f in failures)
    assert any("strong buy" in f for f in failures)
    assert any("ev-999" in f for f in failures)


def test_ledger_resolution_order_prefers_filings():
    ledger = EvidenceLedger()
    ledger.add(
        EvidenceRecord(
            claim="rev", field="x", value=10.0, source_name="Y", source_type=SourceType.MARKET_DATA
        )
    )
    ledger.add(
        EvidenceRecord(
            claim="rev", field="x", value=12.0, source_name="SEC", source_type=SourceType.FILING
        )
    )
    assert ledger.value("x") == 12.0
    assert ledger.conflicts("x")


def test_api_roundtrip(aapl_result, monkeypatch):
    import stock_forecaster.api as api

    monkeypatch.setattr(api, "run_analysis", lambda request, on_status=None: aapl_result)
    client = TestClient(api.app)
    r = client.post("/v1/analyses", json={"ticker": "AAPL", "horizon": "12m"})
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    assert client.get(f"/v1/analyses/{run_id}").json()["status"] == "complete"
    assert len(client.get(f"/v1/analyses/{run_id}/evidence").json()) > 20
    assert DISCLAIMER in client.get(f"/v1/analyses/{run_id}/report").text
    assert client.get("/v1/analyses/nope").status_code == 404
