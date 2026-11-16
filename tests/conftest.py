import json
from pathlib import Path

import pytest

from stock_forecaster.adapters import SecEdgarAdapter, YahooAdapter, fidelity_adapter, msn_adapter
from stock_forecaster.llm import NullProvider
from stock_forecaster.models import AnalysisRequest
from stock_forecaster.pipeline import run_analysis

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(scope="session")
def aapl_yahoo() -> dict:
    return load("aapl_yahoo.json")


@pytest.fixture(scope="session")
def aapl_sec() -> dict:
    return load("aapl_sec_facts.json")


@pytest.fixture(scope="session")
def aapl_result(aapl_yahoo, aapl_sec):
    adapters = [
        YahooAdapter(info=aapl_yahoo["info"], history=aapl_yahoo["history"]),
        SecEdgarAdapter(facts=aapl_sec, cik=aapl_sec["cik"]),
        fidelity_adapter(),
        msn_adapter(),
    ]
    return run_analysis(AnalysisRequest(ticker="AAPL"), adapters=adapters, provider=NullProvider())


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Every test gets its own SQLite file; never the user's ~/.stock_forecaster."""
    import stock_forecaster.api as api

    path = tmp_path / "runs.db"
    monkeypatch.setenv("STOCK_FORECASTER_DB", str(path))
    for var in ("FINNHUB_API_KEY", "FMP_API_KEY", "ALPHAVANTAGE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(api, "_store", None)
    yield path
    if api._store is not None:
        api._store.close()
        api._store = None
