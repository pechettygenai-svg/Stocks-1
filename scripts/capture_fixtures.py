"""Capture trimmed live Yahoo + SEC payloads as offline test fixtures.

Usage: SEC_USER_AGENT="name email" python scripts/capture_fixtures.py RIVN JPM TSM BRK-B
Only the keys/tags the adapters read are kept, so fixtures stay small.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from stock_forecaster.adapters.base import AdapterError
from stock_forecaster.adapters.sec import CONCEPTS, IFRS_CONCEPTS, SecEdgarAdapter
from stock_forecaster.adapters.yahoo import _INFO_FIELDS, _TEXT_FIELDS, YahooAdapter

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
EXTRA_INFO = {"regularMarketTime", "financialCurrency", "quoteType", "firstTradeDateMilliseconds"}
ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "40-F"}


def trim_sec(facts: dict[str, Any]) -> dict[str, Any]:
    wanted = {
        c for m in (CONCEPTS, IFRS_CONCEPTS) for concepts, _, _ in m.values() for c in concepts
    }
    out: dict[str, Any] = {}
    for taxonomy, tags in facts.get("facts", {}).items():
        if taxonomy == "dei":
            continue
        kept = {}
        for tag, node in tags.items():
            if tag not in wanted:
                continue
            units = {
                u: [p for p in pts if p.get("form") in ANNUAL_FORMS and p.get("fp") == "FY"][-12:]
                for u, pts in node.get("units", {}).items()
            }
            kept[tag] = {"label": node.get("label"), "units": units}
        out[taxonomy] = kept
    return {"cik": facts.get("cik"), "entityName": facts.get("entityName"), "facts": out}


def capture(ticker: str) -> None:
    slug = ticker.lower().replace("-", "_").replace(".", "_")
    y = YahooAdapter()
    info, history = y._fetch(ticker)
    keep = set(_INFO_FIELDS) | set(_TEXT_FIELDS) | EXTRA_INFO
    info = {k: v for k, v in info.items() if k in keep}
    (OUT / f"{slug}_yahoo.json").write_text(
        json.dumps({"info": info, "history": history}, indent=1)
    )
    sec = SecEdgarAdapter()
    try:
        cik = sec.resolve_cik(ticker)
        facts = trim_sec(sec.fetch_facts(cik))
        facts["ticker_map"] = {
            str(i): r for i, r in enumerate(sec._load_ticker_map()) if int(r["cik_str"]) == cik
        }
    except AdapterError as exc:
        facts = {"cik": None, "error": str(exc), "facts": {}}
    (OUT / f"{slug}_sec_facts.json").write_text(json.dumps(facts, indent=1))
    print(ticker, "->", slug, "sessions:", len(history["close"]), "cik:", facts.get("cik"))


if __name__ == "__main__":
    for t in sys.argv[1:]:
        capture(t)
