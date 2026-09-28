"""HTTP API. Runs are executed in a background thread; status is polled."""

from __future__ import annotations

import threading
import uuid
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import PlainTextResponse

from .models import DISCLAIMER, AnalysisRequest, AnalysisResult, RunStatus, utc_now
from .pipeline import run_analysis

app = FastAPI(
    title="Stock Forecasting Agent",
    description=DISCLAIMER,
    version="0.1.0",
)

_runs: dict[str, AnalysisResult] = {}
_lock = threading.Lock()


def _execute(run_id: str, request: AnalysisRequest) -> None:
    def on_status(s: RunStatus) -> None:
        with _lock:
            if run_id in _runs:
                _runs[run_id].status = s

    result = run_analysis(request, on_status=on_status)
    result.run_id = run_id
    with _lock:
        _runs[run_id] = result


@app.post("/v1/analyses", status_code=202)
def create_analysis(request: AnalysisRequest, background: BackgroundTasks) -> dict[str, Any]:
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    placeholder = AnalysisResult(
        run_id=run_id, request=request, status=RunStatus.QUEUED, started_at=utc_now()
    )
    with _lock:
        _runs[run_id] = placeholder
    background.add_task(_execute, run_id, request)
    return {"run_id": run_id, "status": RunStatus.QUEUED.value, "disclaimer": DISCLAIMER}


def _get(run_id: str) -> AnalysisResult:
    with _lock:
        r = _runs.get(run_id)
    if r is None:
        raise HTTPException(404, "run not found")
    return r


@app.get("/v1/analyses/{run_id}")
def get_analysis(run_id: str) -> dict[str, Any]:
    r = _get(run_id)
    return r.model_dump(mode="json", exclude={"evidence", "report_markdown"})


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
