"""FastAPI service for the weekly payments reporting workflow.

Endpoints:
- POST /run-weekly        --  kicks off the full LangGraph batch
- GET  /healthz           --  liveness probe
- GET  /runs/{run_id}     --  JSON snapshot of the last run's final state

In Azure Container Apps this is the HTTP target the Logic App / cron
trigger hits every Monday 06:00 UTC.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from payments_reporting.graph import dump_state_json, run_weekly

log = logging.getLogger(__name__)

app = FastAPI(
    title="Payments Partner Reporting",
    version="0.1.0",
    description=(
        "Weekly LangGraph workflow that ships partner-specific payments "
        "analytics via email. Triggered by POST /run-weekly."
    ),
)


class RunWeeklyRequest(BaseModel):
    dry_run: bool = Field(
        default=False,
        description="If true, skip LLM calls and SMTP send. Charts and email HTML still produced.",
    )
    out_dir: str | None = Field(
        default=None,
        description="Override the output directory. Defaults to out/<run_id>/.",
    )


class RunWeeklyResponse(BaseModel):
    run_id: str
    started_at: datetime
    finished_at: datetime
    partners: int
    emails_sent: int
    errors: list[str]
    state_json: str
    out_dir: str


_RECENT: dict[str, RunWeeklyResponse] = {}


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/run-weekly", response_model=RunWeeklyResponse)
async def run_weekly_endpoint(req: RunWeeklyRequest) -> RunWeeklyResponse:
    run_id = uuid.uuid4().hex[:12]
    started = datetime.now(timezone.utc)
    out_dir = req.out_dir or f"out/{run_id}"
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    log.info("run-weekly.start run_id=%s dry_run=%s", run_id, req.dry_run)

    try:
        final = await run_weekly(
            run_id=run_id, dry_run=req.dry_run, out_dir=out_dir
        )
    except Exception as e:  # noqa: BLE001  --  top-level safety net
        log.exception("run-weekly.crash run_id=%s", run_id)
        raise HTTPException(status_code=500, detail=str(e)) from e

    finished = datetime.now(timezone.utc)

    send_results = final.get("send_results") or []
    emails_sent = sum(1 for r in send_results if r.success)

    response = RunWeeklyResponse(
        run_id=run_id,
        started_at=started,
        finished_at=finished,
        partners=len(final.get("partner_summaries") or {}),
        emails_sent=emails_sent,
        errors=list(final.get("errors") or []),
        state_json=dump_state_json(final),
        out_dir=out_dir,
    )
    _RECENT[run_id] = response

    # Persist state.json so a subsequent GET can read it without holding
    # the whole response in memory forever.
    Path(out_dir, "state.json").write_text(response.state_json, encoding="utf-8")

    log.info(
        "run-weekly.done run_id=%s partners=%d emails_sent=%d",
        run_id,
        response.partners,
        emails_sent,
    )
    return response


@app.get("/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    """Return the JSON snapshot of a previous run from disk."""
    state_path = Path(f"out/{run_id}/state.json")
    if not state_path.exists():
        cached = _RECENT.get(run_id)
        if cached is None:
            raise HTTPException(status_code=404, detail=f"run {run_id} not found")
        return cached.model_dump(mode="json")
    import json

    return json.loads(state_path.read_text(encoding="utf-8"))