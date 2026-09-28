"""SEC EDGAR adapter using the public XBRL companyfacts API (tier 1 filings).

Fair-access rules require a descriptive User-Agent; set SEC_USER_AGENT.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any

import httpx

from ..models import EvidenceRecord, SourceType
from .base import AdapterError, unavailable

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
FILING_INDEX_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=10-K"

# canonical field -> candidate us-gaap concepts (first match wins), kind
# kind: "duration" (income/cash-flow) or "instant" (balance sheet)
CONCEPTS: dict[str, tuple[list[str], str, str]] = {
    "revenue_fy": (
        [
            "Revenues",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "SalesRevenueNet",
            "InterestAndDividendIncomeOperating",
        ],
        "duration",
        "Total revenue, fiscal year",
    ),
    "operating_income_fy": (["OperatingIncomeLoss"], "duration", "Operating income, fiscal year"),
    "net_income_fy": (["NetIncomeLoss", "ProfitLoss"], "duration", "Net income, fiscal year"),
    "eps_diluted_fy": (
        ["EarningsPerShareDiluted", "EarningsPerShareBasic"],
        "duration",
        "Diluted EPS, fiscal year",
    ),
    "operating_cash_flow_fy": (
        ["NetCashProvidedByUsedInOperatingActivities"],
        "duration",
        "Cash from operations, fiscal year",
    ),
    "capex_fy": (
        ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
        "duration",
        "Capital expenditures, FY",
    ),
    "diluted_shares_fy": (
        ["WeightedAverageNumberOfDilutedSharesOutstanding"],
        "duration",
        "Weighted avg diluted shares, FY",
    ),
    "cash": (
        [
            "CashAndCashEquivalentsAtCarryingValue",
            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        ],
        "instant",
        "Cash and equivalents, FY end",
    ),
    "long_term_debt": (
        ["LongTermDebt", "LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations"],
        "instant",
        "Long-term debt, FY end",
    ),
    "total_equity": (
        [
            "StockholdersEquity",
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        ],
        "instant",
        "Shareholders' equity, FY end",
    ),
    "total_assets": (["Assets"], "instant", "Total assets, FY end"),
    # Bank-specific
    "net_interest_income_fy": (["InterestIncomeExpenseNet"], "duration", "Net interest income, FY"),
    "provision_credit_losses_fy": (
        ["ProvisionForLoanLeaseAndOtherLosses", "ProvisionForLoanAndLeaseLosses"],
        "duration",
        "Provision for credit losses, FY",
    ),
    "loans_net": (["LoansAndLeasesReceivableNetReportedAmount"], "instant", "Net loans, FY end"),
    "deposits": (["Deposits"], "instant", "Total deposits, FY end"),
}


def _user_agent() -> str:
    return os.environ.get("SEC_USER_AGENT", "stock-forecaster research bot admin@example.com")


class SecEdgarAdapter:
    name = "sec"

    def __init__(
        self,
        facts: dict[str, Any] | None = None,
        cik: int | None = None,
        timeout: float = 20.0,
    ):
        self._facts = facts
        self._cik = cik
        self._timeout = timeout

    def resolve_cik(self, ticker: str) -> int:
        if self._cik is not None:
            return self._cik
        try:
            resp = httpx.get(
                TICKERS_URL, headers={"User-Agent": _user_agent()}, timeout=self._timeout
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:  # pragma: no cover - network path
            raise AdapterError(f"ticker map fetch failed: {exc}") from exc
        for row in resp.json().values():
            if str(row.get("ticker", "")).upper() == ticker.upper().replace("-", ""):
                return int(row["cik_str"])
        raise AdapterError(f"{ticker} not found in SEC ticker map (foreign listing or non-filer?)")

    def fetch_facts(self, cik: int) -> dict[str, Any]:
        if self._facts is not None:
            return self._facts
        try:
            resp = httpx.get(
                FACTS_URL.format(cik=cik),
                headers={"User-Agent": _user_agent()},
                timeout=self._timeout,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:  # pragma: no cover - network path
            raise AdapterError(f"companyfacts fetch failed: {exc}") from exc
        return dict(resp.json())

    def collect(self, ticker: str) -> list[EvidenceRecord]:
        try:
            cik = self.resolve_cik(ticker)
            facts = self.fetch_facts(cik)
        except AdapterError as exc:
            return [unavailable("SEC EDGAR", "filing", TICKERS_URL, str(exc))]

        gaap = facts.get("facts", {}).get("us-gaap", {})
        url = FILING_INDEX_URL.format(cik=cik)
        records: list[EvidenceRecord] = []
        latest_end = _latest_period_end(gaap)
        for field, (concepts, kind, definition) in CONCEPTS.items():
            series = _annual_series(gaap, concepts, kind)
            if not series:
                continue
            # A concept the filer stopped tagging (e.g. bank debt/loan tags replaced
            # by newer ones) is dropped rather than reported as current.
            if latest_end and (latest_end - series[0]["end"]).days > 400:
                records.append(
                    unavailable(
                        "SEC EDGAR",
                        "filing",
                        url,
                        f"{field}: latest us-gaap tag ({series[0]['concept']}) ends "
                        f"{series[0]['end']}, older than latest filing period {latest_end}",
                        field=field,
                    )
                )
                continue
            # latest two fiscal years, for growth calculations
            for i, pt in enumerate(series[:2]):
                suffix = "" if i == 0 else "_prior"
                records.append(
                    EvidenceRecord(
                        claim=f"{definition} {pt['fy']}: {pt['val']:,.0f}"
                        if abs(pt["val"]) >= 1000
                        else f"{definition} {pt['fy']}: {pt['val']:.2f}",
                        field=field + suffix,
                        value=pt["val"],
                        unit=pt["unit"],
                        currency=pt["unit"] if pt["unit"] in ("USD", "EUR", "GBP") else None,
                        as_of=pt["end"],
                        published_at=pt["filed"],
                        period=f"FY{pt['fy']}",
                        definition=f"us-gaap:{pt['concept']}",
                        source_name=f"SEC EDGAR {pt['form']} (XBRL companyfacts)",
                        source_url=url,
                        source_type=SourceType.FILING,
                        confidence=0.95,
                        notes=f"accession {pt['accn']}",
                    )
                )
        if not records:
            records.append(unavailable("SEC EDGAR", "filing", url, "no us-gaap annual facts"))
        return records


def _latest_period_end(gaap: dict[str, Any]) -> date | None:
    """Most recent annual period end across the core income-statement concepts."""
    ends = [
        pt["end"]
        for concepts, kind, _ in (CONCEPTS["revenue_fy"], CONCEPTS["net_income_fy"])
        for pt in _annual_series(gaap, concepts, kind)[:1]
    ]
    return max(ends) if ends else None


def _annual_series(gaap: dict[str, Any], concepts: list[str], kind: str) -> list[dict[str, Any]]:
    """Annual points for the first concept that has data, keyed by period-end
    date (not the filing's `fy`, which also tags comparative prior-year values).
    Among candidate concepts, prefer the one with the most recent period end."""
    best: list[dict[str, Any]] = []
    for concept in concepts:
        node = gaap.get(concept)
        if not node:
            continue
        for unit, points in node.get("units", {}).items():
            rows: dict[str, dict[str, Any]] = {}
            for p in points:
                if p.get("form") not in ("10-K", "20-F", "40-F", "10-K/A") or p.get("fp") != "FY":
                    continue
                start, end = p.get("start"), p.get("end")
                if not end:
                    continue
                if kind == "duration":
                    if not start:
                        continue
                    days = (date.fromisoformat(end) - date.fromisoformat(start)).days
                    if not 340 <= days <= 380:
                        continue
                existing = rows.get(end)
                # prefer the most recently filed value for a period (restatements)
                if existing is None or p["filed"] > existing["filed_str"]:
                    rows[end] = {
                        "fy": int(end[:4]),
                        "val": float(p["val"]),
                        "unit": unit,
                        "end": date.fromisoformat(end),
                        "filed": date.fromisoformat(p["filed"]),
                        "filed_str": p["filed"],
                        "form": p["form"],
                        "accn": p.get("accn"),
                        "concept": concept,
                    }
            series = [rows[k] for k in sorted(rows, reverse=True)]
            if series and (not best or series[0]["end"] > best[0]["end"]):
                best = series
    return best
