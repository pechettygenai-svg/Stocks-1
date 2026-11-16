"""Tricky listings, replayed offline from trimmed live payloads (scripts/capture_fixtures.py)."""

import pytest
from conftest import load

from stock_forecaster.adapters import SecEdgarAdapter, YahooAdapter, fidelity_adapter, msn_adapter
from stock_forecaster.llm import NullProvider
from stock_forecaster.models import AnalysisRequest, AnalysisResult, RunStatus, normalize_ticker
from stock_forecaster.pipeline import run_analysis

CASES = {"rivn": "RIVN", "jpm": "JPM", "tsm": "TSM", "brk_b": "BRK.B", "accv": "ACCV"}


def _run(slug: str) -> AnalysisResult:
    y, s = load(f"{slug}_yahoo.json"), load(f"{slug}_sec_facts.json")
    adapters = [
        YahooAdapter(info=y["info"], history=y["history"]),
        SecEdgarAdapter(facts=s, ticker_map=s["ticker_map"]),
        fidelity_adapter(),
        msn_adapter(),
    ]
    return run_analysis(
        AnalysisRequest(ticker=CASES[slug]), adapters=adapters, provider=NullProvider()
    )


@pytest.fixture(scope="module")
def results() -> dict[str, AnalysisResult]:
    return {slug: _run(slug) for slug in CASES}


def _calc(r: AnalysisResult, name: str):
    return next(c for c in r.calculations if c.name == name)


@pytest.mark.parametrize("slug", list(CASES))
def test_every_case_completes_with_gates_and_scenarios(results, slug):
    r = results[slug]
    assert r.status == RunStatus.COMPLETE, r.error
    assert r.quality_gate_failures == []
    assert [s.name for s in r.scenarios] == ["Bear", "Base", "Bull"]
    for s in r.scenarios:
        assert 0 < s.implied_price_low < s.implied_price_mid < s.implied_price_high


def test_unprofitable_growth_uses_sales_mode_and_skips_dcf(results):
    r = results["rivn"]
    assert _calc(r, "scenarios").outputs["mode"] == "sales"
    assert any("not profitable" in w for w in _calc(r, "scenarios").warnings)
    assert any("negative/zero free cash flow" in w for w in _calc(r, "valuation").warnings)
    assert "negative net income in latest fiscal year" in r.validation_warnings
    assert any("free cash flow" in f.lower() for f in _calc(r, "fundamentals").outputs["red_flags"])


def test_bank_gets_bank_ratios_and_drops_discontinued_tags(results):
    r = results["jpm"]
    out = _calc(r, "fundamentals").outputs
    for k in ("nii_to_revenue", "provision_to_loans", "loans_to_deposits", "equity_to_assets"):
        assert k in out
    assert out["nii_to_revenue"] is not None and 0 < out["nii_to_revenue"] < 1
    assert out["equity_to_assets"] is not None and 0 < out["equity_to_assets"] < 0.2
    stale = [w for w in r.validation_warnings if "older than latest filing period" in w]
    assert any("long_term_debt" in w for w in stale)
    # Preferreds and issuer notes under the same CIK are not ambiguous common classes.
    assert not any("ambiguous listing" in w for w in r.validation_warnings)
    assert _calc(r, "scenarios").outputs["mode"] == "earnings"


def test_non_bank_has_no_bank_missing_input_noise(results):
    warnings = _calc(results["rivn"], "fundamentals").warnings
    assert not any("deposits" in w or "net_interest_income" in w for w in warnings)


def test_adr_reads_ifrs_and_never_mixes_currencies(results):
    r = results["tsm"]
    rev = [e for e in r.evidence if e.field == "revenue_fy" and e.source_name.startswith("SEC")]
    assert rev and rev[0].currency == "TWD" and rev[0].definition == "ifrs-full:Revenue"
    fund = _calc(r, "fundamentals")
    assert fund.outputs["fcf_yield"] is None
    assert fund.outputs["market_cap_to_revenue_fy"] is None
    assert any("statements in TWD but quote in USD" in w for w in fund.warnings)
    assert any("statements in TWD, quote in USD" in w for w in _calc(r, "valuation").warnings)
    assert "implied_fcf_growth_at_market_price" not in _calc(r, "valuation").outputs
    assert any("not used for per-share scenarios" in w for w in _calc(r, "scenarios").warnings)
    yahoo_rev = next(e for e in r.evidence if e.field == "revenue_ttm")
    assert yahoo_rev.currency == "TWD"


def test_recent_ipo_degrades_gracefully(results):
    r = results["accv"]
    assert any("only 29 sessions" in w for w in _calc(r, "technicals").warnings)
    assert any("no us-gaap/ifrs-full annual facts" in w for w in r.validation_warnings)
    sc = _calc(r, "scenarios")
    assert sc.outputs["mode"] == "sales"
    assert any("assumed" in w for w in sc.warnings)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("brk.b", "BRK-B"),
        ("BRK/B", "BRK-B"),
        (" brk b ", "BRK-B"),
        ("aapl", "AAPL"),
        ("vod.l", "VOD.L"),
        ("shop.to", "SHOP.TO"),
        ("7203.T", "7203.T"),
        ("bbd.b.to", "BBD-B.TO"),
    ],
)
def test_normalize_ticker(raw, expected):
    assert normalize_ticker(raw) == expected


def test_share_class_ticker_resolves_and_flags_ambiguity(results):
    s = load("brk_b_sec_facts.json")
    sec = SecEdgarAdapter(facts=s, ticker_map=s["ticker_map"])
    assert sec.resolve_cik("BRK.B") == sec.resolve_cik("brk-b") == 1067983
    assert sec.share_classes == ["BRK-A", "BRK-B"]
    r = results["brk_b"]
    assert r.request.ticker == "BRK-B"
    assert any("ambiguous listing" in w and "BRK-A" in w for w in r.validation_warnings)
    assert any("class-equivalent shares" in w for w in _calc(r, "scenarios").warnings)


def test_unknown_ticker_is_unavailable_not_invented():
    s = load("brk_b_sec_facts.json")
    recs = SecEdgarAdapter(facts=s, ticker_map=s["ticker_map"]).collect("ZZZZ")
    assert len(recs) == 1 and recs[0].value is None


def test_multi_class_issuer_without_annual_facts_is_unavailable():
    s = load("brk_b_sec_facts.json")
    empty = {"cik": s["cik"], "facts": {}, "ticker_map": s["ticker_map"]}
    recs = SecEdgarAdapter(facts=empty, ticker_map=s["ticker_map"]).collect("BRK-B")
    assert any(r.field == "sec_share_classes" for r in recs)
    assert any(
        r.value is None and "no us-gaap/ifrs-full annual facts" in (r.notes or r.claim)
        for r in recs
    )
