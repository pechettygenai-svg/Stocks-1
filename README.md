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
stock-forecaster serve --port 8000                         # web UI at http://localhost:8000, Swagger at /docs
```

API: `POST /v1/analyses`, `GET /v1/analyses[?ticker=]` (history), `GET /v1/analyses/{id}[/evidence|/report|/full]`,
`DELETE /v1/analyses/{id}`. Run history is stored in SQLite at `$STOCK_FORECASTER_DB` (default
`~/.stock_forecaster/runs.db`, or `serve --db PATH`) and survives restarts; runs that were mid-pipeline
when the server stopped are marked failed (`interrupted`) on the next start.

### External forecast sources

Besides Yahoo's consensus, three keyed feeds are queried through their official APIs when you
set your own key (keys are read from the environment and never stored in evidence or reports):

| Source key | Env var | What is used | Free-tier notes |
|---|---|---|---|
| `finnhub` | `FINNHUB_API_KEY` | recommendation trends; price target | price target needs a paid plan (reported as partial) |
| `fmp` | `FMP_API_KEY` | `price-target-consensus`, `grades-consensus` | no as-of date; retrieval date is used and noted |
| `alphavantage` | `ALPHAVANTAGE_API_KEY` | `OVERVIEW` target price and rating counts | 25 calls/day; throttle messages shown as unavailable |

Without a key the source shows as "not configured" in the comparison panel. Each provider gets a
price-target row and an analyst-rating-mix row (counts plus a mean on the 1=strong-buy..5=strong-sell
scale). Rows are never averaged; a mean-only target that matches another source's mean is flagged
as a duplicate (Alpha Vantage typically republishes the same consensus). Fidelity and MSN remain
link-only.

### Watchlists

```bash
curl -X POST localhost:8000/v1/watchlists -H 'content-type: application/json' \
     -d '{"name": "core", "horizon": "12m", "tickers": ["AAPL", "JPM", "BRK.B"]}'
curl -X POST localhost:8000/v1/watchlists/1/check     # or the "Check now" button in the UI
curl localhost:8000/v1/alerts                         # POST /v1/alerts/{id}/ack to dismiss
stock-forecaster watch-check [--name core]            # for cron; exit code 3 when an alert fired
```

A check re-runs the normal pipeline for every ticker and compares it with that ticker's last
successful run. The first check only records a baseline. After that an alert is raised only for:

- **data changes**: a new or restated SEC filing value, a consensus field moving >= 2%
  (recommendation mean >= 0.2), an external forecast row changing, a source becoming (un)available;
- **thesis changes**: scenario mode (e.g. earnings -> sales), price leaving the previous run's
  Bear-low..Bull-high range, a new or cleared red flag, quality-gate failures, blocking review findings.

Price drift inside the previously modeled range produces no alert. A failed check is recorded in
history but neither raises an alert nor replaces the baseline. Endpoints: `GET|POST /v1/watchlists`,
`GET|DELETE /v1/watchlists/{id}`, `POST /v1/watchlists/{id}/tickers`,
`DELETE /v1/watchlists/{id}/tickers/{ticker}`, `POST /v1/watchlists/{id}/check`, `GET /v1/alerts`.

The web UI (served from `stock_forecaster/web/`, no build step) has a ticker/horizon form with live
stage status, the cited report, and tabs for scenarios (with local sliders that re-run the same formula),
the external forecast comparison panel, fundamentals/technicals, valuation, the evidence ledger, and the
critic/auditor review. Fidelity/MSN values are never fetched; you may transcribe a target you read there
and it is stored as a tier-5 `snippet_only` user-supplied opinion, flagged for duplicates/staleness/outliers.

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

Tests run entirely from fixtures in `tests/fixtures/`; nothing in the suite touches the network.
Besides AAPL, `tests/test_edge_cases.py` replays trimmed snapshots of tricky listings:

| Fixture | Case | What it checks |
|---|---|---|
| RIVN | unprofitable growth | sales-mode scenarios, DCF skipped on negative FCF, red flags |
| JPM | bank | NII / provision / loans-to-deposits ratios, discontinued SEC tags dropped |
| TSM | ADR, IFRS filer | `ifrs-full` facts read in TWD; no TWD statement x USD quote math |
| ACCV | recent IPO | 29 sessions of history, no annual filings yet, assumed growth flagged |
| BRK.B | ambiguous ticker | `BRK.B` -> `BRK-B`, multi-class warning, class-equivalent share count |

Refresh them with `SEC_USER_AGENT="name email" python scripts/capture_fixtures.py RIVN JPM TSM BRK-B ACCV`.

## Roadmap

- Done: single-ticker core, forecast comparison with duplicate/stale/outlier flags, critic/auditor
  revision loop, web UI with run history and scenario sliders, fixture evaluation set (unprofitable
  growth, bank, ADR, recent IPO, ambiguous ticker), SQLite run history, watchlists with
  data/thesis-change alerts, keyed forecast feeds (Finnhub, Financial Modeling Prep, Alpha Vantage).
