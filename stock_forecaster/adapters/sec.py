"""SEC EDGAR adapter using the public XBRL companyfacts API (tier 1 filings).

Fair-access rules require a descriptive User-Agent; set SEC_USER_AGENT.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any

import httpx

from ..models import EvidenceRecord, SourceType, normalize_ticker
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


# Foreign private issuers (20-F/40-F) tag in ifrs-full. Share counts are omitted:
# ordinary shares do not map 1:1 to the US-listed ADRs.
IFRS_CONCEPTS: dict[str, tuple[list[str], str, str]] = {
    "revenue_fy": (["Revenue"], "duration", "Total revenue, fiscal year"),
    "operating_income_fy": (
        ["ProfitLossFromOperatingActivities"],
        "duration",
        "Operating income, fiscal year",
    ),
    "net_income_fy": (
        ["ProfitLossAttributableToOwnersOfParent", "ProfitLoss"],
        "duration",
        "Net income, fiscal year",
    ),
    "operating_cash_flow_fy": (
        ["CashFlowsFromUsedInOperatingActivities"],
        "duration",
        "Cash from operations, fiscal year",
    ),
    "capex_fy": (
        ["PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"],
        "duration",
        "Capital expenditures, FY",
    ),
    "cash": (["CashAndCashEquivalents"], "instant", "Cash and equivalents, FY end"),
    "total_equity": (
        ["EquityAttributableToOwnersOfParent", "Equity"],
        "instant",
        "Shareholders' equity, FY end",
    ),
    "total_assets": (["Assets"], "instant", "Total assets, FY end"),
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
        ticker_map: dict[str, Any] | None = None,
    ):
        self._facts = facts
        self._cik = cik
        self._timeout = timeout
        self._ticker_map = ticker_map
        self.share_classes: list[str] = []

    def _load_ticker_map(self) -> list[dict[str, Any]]:
        if self._ticker_map is None:
            try:
                resp = httpx.get(
                    TICKERS_URL, headers={"User-Agent": _user_agent()}, timeout=self._timeout
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:  # pragma: no cover - network path
                raise AdapterError(f"ticker map fetch failed: {exc}") from exc
            self._ticker_map = dict(resp.json())
        return list(self._ticker_map.values())

    def resolve_cik(self, ticker: str) -> int:
        if self._cik is not None and self._ticker_map is None:
            return self._cik
        rows = self._load_ticker_map()
        want = normalize_ticker(ticker)
        cik = self._cik
        if cik is None:
            cik = next(
                (int(r["cik_str"]) for r in rows if normalize_ticker(str(r["ticker"])) == want),
                None,
            )
        if cik is None:
            raise AdapterError(
                f"{ticker} not found in SEC ticker map (foreign listing or non-filer?)"
            )
        # One registrant, several listed common classes (BRK-A/BRK-B, GOOG/GOOGL).
        # Preferreds (JPM-PC) and unrelated issuer notes (AMJB) are not share classes.
        self.share_classes = sorted(
            t
            for t in {normalize_ticker(str(r["ticker"])) for r in rows if int(r["cik_str"]) == cik}
            if "-P" not in t and t[:3] == want[:3]
        )
        return cik

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

        taxonomy, concept_map = "us-gaap", CONCEPTS
        gaap = facts.get("facts", {}).get("us-gaap", {})
        if not _annual_series(gaap, CONCEPTS["revenue_fy"][0], "duration") and facts.get(
            "facts", {}
        ).get("ifrs-full"):
            taxonomy, concept_map = "ifrs-full", IFRS_CONCEPTS
            gaap = facts["facts"]["ifrs-full"]
        url = FILING_INDEX_URL.format(cik=cik)
        records: list[EvidenceRecord] = []
        if len(self.share_classes) > 1:
            records.append(
                EvidenceRecord(
                    claim=f"SEC CIK {cik} lists several share classes: "
                    + ", ".join(self.share_classes),
                    field="sec_share_classes",
                    value=", ".join(self.share_classes),
                    source_name="SEC EDGAR (company_tickers.json)",
                    source_url=TICKERS_URL,
                    source_type=SourceType.FILING,
                    confidence=0.95,
                    notes=f"requested {normalize_ticker(ticker)}; filing per-share figures "
                    "may be stated for a different class",
                )
            )
        latest_end = _latest_period_end(gaap, concept_map)
        for field, (concepts, kind, definition) in concept_map.items():
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
                        f"{field}: latest {taxonomy} tag ({series[0]['concept']}) ends "
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
                        currency=pt["unit"] if _is_currency(pt["unit"]) else None,
                        as_of=pt["end"],
                        published_at=pt["filed"],
                        period=f"FY{pt['fy']}",
                        definition=f"{taxonomy}:{pt['concept']}",
                        source_name=f"SEC EDGAR {pt['form']} (XBRL companyfacts)",
                        source_url=url,
                        source_type=SourceType.FILING,
                        confidence=0.95,
                        notes=f"accession {pt['accn']}",
                    )
                )
        if not any(
            r.source_type == SourceType.FILING
            and r.value is not None
            and r.field != "sec_share_classes"
            for r in records
        ):
            records.append(
                unavailable("SEC EDGAR", "filing", url, "no us-gaap/ifrs-full annual facts")
            )
        return records


def _is_currency(unit: str) -> bool:
    return len(unit) == 3 and unit.isalpha() and unit.isupper()


def _latest_period_end(
    gaap: dict[str, Any], concept_map: dict[str, tuple[list[str], str, str]] = CONCEPTS
) -> date | None:
    """Most recent annual period end across the core income-statement concepts."""
    ends = [
        pt["end"]
        for concepts, kind, _ in (concept_map["revenue_fy"], concept_map["net_income_fy"])
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
        concept_best: list[dict[str, Any]] = []
        best_n = 0
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
            # Same concept in two currencies (20-F convenience translation): the
            # reporting currency is the unit with the longer history.
            if series and (
                not concept_best
                or (series[0]["end"], len(points)) > (concept_best[0]["end"], best_n)
            ):
                concept_best, best_n = series, len(points)
        if concept_best and (not best or concept_best[0]["end"] > best[0]["end"]):
            best = concept_best
    return best
