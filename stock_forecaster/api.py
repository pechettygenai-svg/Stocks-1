"""HTTP API. Runs are executed in a background thread; status is polled."""

from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .models import (
    DISCLAIMER,
    AnalysisRequest,
    AnalysisResult,
    RunStatus,
    normalize_ticker,
    utc_now,
)
from .pipeline import run_analysis
from .store import RunStore

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
def create_analysis(request: AnalysisRequest, background: BackgroundTasks) -> dict[str, Any]:
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    placeholder = AnalysisResult(
        run_id=run_id, request=request, status=RunStatus.QUEUED, started_at=utc_now()
    )
    get_store().save(placeholder)
    background.add_task(_execute, run_id, request)
    return {"run_id": run_id, "status": RunStatus.QUEUED.value, "disclaimer": DISCLAIMER}


def _get(run_id: str) -> AnalysisResult:
    r = get_store().get(run_id)
    if r is None:
        raise HTTPException(404, "run not found")
    return r


@app.get("/v1/analyses")
def list_analyses(ticker: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """Run history, persisted in SQLite (newest first)."""
    return get_store().list(normalize_ticker(ticker) if ticker else None, min(max(limit, 1), 1000))


@app.delete("/v1/analyses/{run_id}", status_code=204)
def delete_analysis(run_id: str) -> None:
    r = _get(run_id)
    if r.status not in (RunStatus.COMPLETE, RunStatus.FAILED):
        raise HTTPException(409, f"run is {r.status.value}; delete it after it finishes")
    get_store().delete(run_id)


@app.get("/v1/analyses/{run_id}")
def get_analysis(run_id: str) -> dict[str, Any]:
    r = _get(run_id)
    return r.model_dump(mode="json", exclude={"evidence", "report_markdown"})


@app.get("/v1/analyses/{run_id}/full")
def get_full(run_id: str) -> dict[str, Any]:
    """Everything the UI needs in one call: result, evidence and report."""
    return _get(run_id).model_dump(mode="json")


@app.get("/v1/analyses/{run_id}/evidence")
def get_evidence(run_id: str) -> list[dict[str, Any]]:
    return [e.model_dump(mode="json") for e in _get(run_id).evidence]


@app.get("/v1/analyses/{run_id}/report", response_class=PlainTextResponse)
def get_report(run_id: str) -> str:
    r = _get(run_id)
    if r.status != RunStatus.COMPLETE:
        raise HTTPException(409, f"run status is {r.status.value}")
    return r.report_markdown or ""


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(_STATIC / "index.html")


app.mount("/static", StaticFiles(directory=_STATIC), name="static")
