# stock-forecaster

Evidence-first, uncertainty-aware research reports for a public company.

> This is informational research, not investment advice or a recommendation to buy, sell, or hold a security.

## What it does

`stock-forecaster analyze AAPL` collects data from permitted sources in parallel, normalizes every
observation into an **evidence ledger** (value, unit, as-of date, source, tier, retrieval status),
runs deterministic validation and analytics, and renders a cited Markdown report with
bull/base/bear scenarios, a sensitivity table, an external forecast comparison panel, limitations,
and a sources table. Every volatile number carries an `[ev-NNN]` citation.

| Layer | Owner |
|---|---|
| Prices, filings, ratios, technicals, DCF, scenarios | Deterministic Python (`adapters/`, `analytics/`, `validation.py`) |
| Fundamentals / valuation / technical / catalyst-risk / forecast-auditor / skeptic roles | LLM behind a provider-neutral `LLMProvider` (`llm/`), or deterministic templates when no provider is configured |
| Report prose | Citation-constrained; mechanically checked by `quality.py` |

Sources:

- **SEC EDGAR** (companyfacts XBRL API) — tier 1, reported annual financials incl. bank fields.
- **Yahoo Finance** via `yfinance` — tier 3 market data; analyst targets are labeled *consensus / method not disclosed*.
- **Fidelity / MSN Money** — link-only. No permitted structured feed exists, so they are recorded as
  `unavailable` with the public URL. Nothing is scraped, and no values are fabricated.

## Install

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"            # add ,openai or ,anthropic for LLM roles
```

## Run

```bash
export SEC_USER_AGENT="your-app your-email@example.com"   # required by SEC fair-access policy
stock-forecaster analyze AAPL --horizon 12m --out out/AAPL.md --json-out out/AAPL.json
stock-forecaster serve --port 8000                         # POST /v1/analyses, GET /v1/analyses/{id}[/evidence|/report]
```

Optional LLM roles (keys are read from the environment and never written to prompts, logs, or reports):

```bash
export LLM_PROVIDER=openai      # or anthropic; default none = deterministic templates
export LLM_MODEL=<model>
export OPENAI_API_KEY=...       # or ANTHROPIC_API_KEY
```

If model output fails schema validation it is retried once with a repair prompt, then discarded.
If LLM prose fails the quality gates, the report is regenerated deterministically.

## Develop

```bash
ruff check . && ruff format --check .
mypy stock_forecaster
pytest
```

Tests run entirely from fixtures in `tests/fixtures/` (sanitized AAPL Yahoo + SEC snapshots); nothing
in the suite touches the network.

## Roadmap

- Phase 2: more permitted forecast sources, duplicate-source detection, critic/auditor revision loop,
  evaluation set (unprofitable growth, bank, ADR, recent IPO, ambiguous ticker, source outage).
- Phase 3: web UI, run history, scenario sliders, watchlists (alerts only for data/thesis changes).
