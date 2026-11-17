"""HTTP API. Runs are executed in a background thread; status is polled."""

from __future__ import annotations

import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pydantic import Field as PField

from .models import (
    DISCLAIMER,
    AnalysisRequest,
    AnalysisResult,
    Horizon,
    RunStatus,
    normalize_ticker,
    utc_now,
)
from .pipeline import run_analysis
from .store import RunStore
from .watch import check_watchlist

app = FastAPI(
    title="Stock Forecasting Agent",
    description=DISCLAIMER,
    version="0.1.0",
)

_STATIC = Path(__file__).parent / "web"
_store: RunStore | None = None
_store_lock = threading.Lock()


def get_store() -> RunStore:
    """Opened lazily so `STOCK_FORECASTER_DB` (or `configure_store`) applies."""
    global _store
    with _store_lock:
        if _store is None:
            _store = RunStore()
            _store.mark_interrupted()
        return _store


def configure_store(path: str | None = None) -> RunStore:
    global _store
    with _store_lock:
        if _store is not None:
            _store.close()
        _store = RunStore(path)
        _store.mark_interrupted()
        return _store


def _owner(x_sf_user: Annotated[str, Header()] = "") -> str:
    """Whose runs and watchlists to use. A front end that has its own logins (the
    PHP site's sf_proxy.php) sets X-SF-User; without it everything is shared under
    the empty owner. The service binds to localhost, so only that front end can set it."""
    return x_sf_user.strip()[:64]


Owner = Annotated[str, Depends(_owner)]


def _execute(run_id: str, request: AnalysisRequest) -> None:
    store = get_store()

    def on_status(s: RunStatus) -> None:
        store.set_status(run_id, s)

    try:
        result = run_analysis(request, on_status=on_status)
    except Exception as exc:  # run_analysis records its own failures; this is a backstop
        result = AnalysisResult(
            run_id=run_id,
            request=request,
            status=RunStatus.FAILED,
            started_at=utc_now(),
            finished_at=utc_now(),
            error=f"{type(exc).__name__}: {exc}",
        )
    result.run_id = run_id
    store.save(result)


@app.post("/v1/analyses", status_code=202)
def create_analysis(
    request: AnalysisRequest, background: BackgroundTasks, owner: Owner
) -> dict[str, Any]:
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    placeholder = AnalysisResult(
        run_id=run_id, request=request, status=RunStatus.QUEUED, started_at=utc_now()
    )
    get_store().save(placeholder, owner=owner)
    background.add_task(_execute, run_id, request)
    return {"run_id": run_id, "status": RunStatus.QUEUED.value, "disclaimer": DISCLAIMER}


def _get(run_id: str, owner: str) -> AnalysisResult:
    r = get_store().get(run_id, owner)
    if r is None:
        raise HTTPException(404, "run not found")
    return r


@app.get("/v1/analyses")
def list_analyses(
    owner: Owner, ticker: str | None = None, limit: int = 200
) -> list[dict[str, Any]]:
    """Run history, persisted in SQLite (newest first)."""
    return get_store().list_runs(
        normalize_ticker(ticker) if ticker else None, min(max(limit, 1), 1000), owner
    )


@app.delete("/v1/analyses/{run_id}", status_code=204)
def delete_analysis(run_id: str, owner: Owner) -> None:
    r = _get(run_id, owner)
    if r.status not in (RunStatus.COMPLETE, RunStatus.FAILED):
        raise HTTPException(409, f"run is {r.status.value}; delete it after it finishes")
    get_store().delete(run_id)


@app.get("/v1/analyses/{run_id}")
def get_analysis(run_id: str, owner: Owner) -> dict[str, Any]:
    r = _get(run_id, owner)
    return r.model_dump(mode="json", exclude={"evidence", "report_markdown"})


@app.get("/v1/analyses/{run_id}/full")
def get_full(run_id: str, owner: Owner) -> dict[str, Any]:
    """Everything the UI needs in one call: result, evidence and report."""
    return _get(run_id, owner).model_dump(mode="json")


@app.get("/v1/analyses/{run_id}/evidence")
def get_evidence(run_id: str, owner: Owner) -> list[dict[str, Any]]:
    return [e.model_dump(mode="json") for e in _get(run_id, owner).evidence]


@app.get("/v1/analyses/{run_id}/report", response_class=PlainTextResponse)
def get_report(run_id: str, owner: Owner) -> str:
    r = _get(run_id, owner)
    if r.status != RunStatus.COMPLETE:
        raise HTTPException(409, f"run status is {r.status.value}")
    return r.report_markdown or ""


# ---------- watchlists ----------
class WatchlistCreate(BaseModel):
    name: str = PField(min_length=1, max_length=80)
    horizon: Horizon = Horizon.M12
    tickers: list[str] = PField(default_factory=list)


class TickerIn(BaseModel):
    ticker: str


_checking: set[int] = set()
_checking_lock = threading.Lock()


def _watchlist(wl_id: int, owner: str) -> dict[str, Any]:
    wl = get_store().get_watchlist(wl_id, owner)
    if wl is None:
        raise HTTPException(404, "watchlist not found")
    with _checking_lock:
        wl["checking"] = wl_id in _checking
    return wl


def _tickers(raw: list[str]) -> list[str]:
    out = []
    for t in raw:
        n = normalize_ticker(t)
        if n and n not in out:
            out.append(n)
    return out


def _run_check(wl_id: int) -> None:
    try:
        check_watchlist(get_store(), wl_id, lambda req: run_analysis(req))
    finally:
        with _checking_lock:
            _checking.discard(wl_id)


@app.post("/v1/watchlists", status_code=201)
def create_watchlist(body: WatchlistCreate, owner: Owner) -> dict[str, Any]:
    try:
        wl_id = get_store().create_watchlist(
            body.name.strip(), body.horizon.value, _tickers(body.tickers), owner
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, f"watchlist {body.name!r} already exists") from exc
    return _watchlist(wl_id, owner)


@app.get("/v1/watchlists")
def list_watchlists(owner: Owner) -> list[dict[str, Any]]:
    return get_store().list_watchlists(owner)


@app.get("/v1/watchlists/{wl_id}")
def get_watchlist(wl_id: int, owner: Owner) -> dict[str, Any]:
    return _watchlist(wl_id, owner)


@app.delete("/v1/watchlists/{wl_id}", status_code=204)
def delete_watchlist(wl_id: int, owner: Owner) -> None:
    if not get_store().delete_watchlist(wl_id, owner):
        raise HTTPException(404, "watchlist not found")


@app.post("/v1/watchlists/{wl_id}/tickers")
def add_ticker(wl_id: int, body: TickerIn, owner: Owner) -> dict[str, Any]:
    _watchlist(wl_id, owner)
    for t in _tickers([body.ticker]):
        get_store().add_item(wl_id, t)
    return _watchlist(wl_id, owner)


@app.delete("/v1/watchlists/{wl_id}/tickers/{ticker}")
def remove_ticker(wl_id: int, ticker: str, owner: Owner) -> dict[str, Any]:
    _watchlist(wl_id, owner)
    if not get_store().remove_item(wl_id, normalize_ticker(ticker)):
        raise HTTPException(404, "ticker not on watchlist")
    return _watchlist(wl_id, owner)


@app.post("/v1/watchlists/{wl_id}/check", status_code=202)
def check(wl_id: int, background: BackgroundTasks, owner: Owner) -> dict[str, Any]:
    """Re-run every ticker. The first check sets a baseline; later checks raise an
    alert only for data or thesis changes (see `stock_forecaster.watch`)."""
    _watchlist(wl_id, owner)
    with _checking_lock:
        if wl_id in _checking:
            raise HTTPException(409, "check already running")
        _checking.add(wl_id)
    background.add_task(_run_check, wl_id)
    return {"watchlist_id": wl_id, "checking": True}


@app.get("/v1/alerts")
def list_alerts(
    owner: Owner, watchlist_id: int | None = None, include_acknowledged: bool = False
) -> list[dict[str, Any]]:
    return get_store().list_alerts(watchlist_id, include_acknowledged, owner=owner)


@app.post("/v1/alerts/{alert_id}/ack")
def ack_alert(alert_id: int, owner: Owner) -> dict[str, Any]:
    if not get_store().ack_alert(alert_id, owner):
        raise HTTPException(404, "alert not found")
    return {"id": alert_id, "acknowledged": True}


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(_STATIC / "index.html")


app.mount("/static", StaticFiles(directory=_STATIC), name="static")
