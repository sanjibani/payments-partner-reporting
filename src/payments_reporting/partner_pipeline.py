"""Per-partner pipeline node: analyze -> chart -> email.

This is a single async function that runs the full per-partner LLM
pipeline. It is invoked once per partner via the Send API from the
main graph's `fan_out_partners` routing function. LangGraph runs all
branches in parallel; each branch's return value is a partial that
gets merged into the main GraphState keyed by partner_id.

Why a single function and not a compiled subgraph?

When you embed a compiled LangGraph subgraph as a node and dispatch
it via Send, the Send dict sometimes does not propagate cleanly into
the subgraph's initial state (we hit a KeyError on `partner_id` in
testing). A single node function avoids that machinery while keeping
all the parallel-fan-out behavior we want.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from .llm import LLMClient
from .nodes.analysis import analyze_one, fallback_analysis
from .nodes.charts import render_partner_charts
from .nodes.email import compose_one, fallback_email
from .prompts import EMAIL_SYSTEM, email_user_prompt
from .state import (
    AnalysisOutput,
    ChartFile,
    EmailOutput,
    PartnerMeta,
    PartnerPipelineState,
    PartnerSummary,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level context. Read-only metadata for the subgraph that must NOT
# overlap with GraphState keys (otherwise the Send fan-back causes
# LangGraph's INVALID_CONCURRENT_GRAPH_UPDATE).
# ---------------------------------------------------------------------------


_PIPELINE_CTX: dict[str, Any] = {
    "run_id": "",
    "dry_run": False,
    "out_dir": "out",
}


def set_pipeline_ctx(*, run_id: str, dry_run: bool, out_dir: str) -> None:
    """Called by fan_out_partners immediately before dispatching Sends."""
    global _PIPELINE_CTX
    _PIPELINE_CTX = {
        "run_id": run_id,
        "dry_run": dry_run,
        "out_dir": out_dir,
    }


def get_pipeline_ctx() -> dict[str, Any]:
    return _PIPELINE_CTX


# ---------------------------------------------------------------------------
# The actual per-partner pipeline node
# ---------------------------------------------------------------------------


async def partner_pipeline_node(
    state: PartnerPipelineState,
) -> dict[str, Any]:
    """Run analyze -> chart -> email for one partner.

    Input:  PartnerPipelineState (partner_id + summary, dispatched via Send)
    Output: partial GraphState with three partner_id-keyed entries:
            analyses, charts, email_bodies
    """
    pid = state["partner_id"]
    summary: PartnerSummary = state["summary"]
    ctx = get_pipeline_ctx()

    llm = LLMClient(disabled=bool(ctx.get("dry_run")))

    # 1. analyze (LLM #1)
    t0 = time.perf_counter()
    analysis = await analyze_one(llm, summary)
    log.debug(
        "partner_pipeline.analyze partner=%s elapsed_ms=%.2f",
        pid,
        (time.perf_counter() - t0) * 1000,
    )

    # 2. chart (deterministic matplotlib)
    t0 = time.perf_counter()
    out_dir = Path(ctx["out_dir"]) / "partners" / pid
    chart_list = render_partner_charts(summary, out_dir)
    if analysis.recommended_actions:
        chart_list[0].title = (
            f"Success rate by gateway -- recommended: "
            f"{analysis.recommended_actions[0][:60]}"
        )
    log.debug(
        "partner_pipeline.chart partner=%s elapsed_ms=%.2f",
        pid,
        (time.perf_counter() - t0) * 1000,
    )

    # 3. email (LLM #2)
    t0 = time.perf_counter()
    meta = PartnerMeta(
        partner_id=summary.partner_id,
        partner_name=summary.partner_name,
        contact_email=summary.contact_email,
    )
    try:
        from .prompts import EMAIL_SYSTEM, email_user_prompt

        data = llm.complete_json(
            EMAIL_SYSTEM,
            email_user_prompt(
                meta.partner_name,
                meta.tone,
                analysis.model_dump_json(indent=2),
                [c.title for c in chart_list],
            ),
        )
        email = EmailOutput.model_validate(data)
    except Exception as e:  # noqa: BLE001
        log.warning("partner_pipeline.email.fallback partner=%s %s", pid, e)
        email = fallback_email(summary, analysis)
    log.debug(
        "partner_pipeline.email partner=%s elapsed_ms=%.2f",
        pid,
        (time.perf_counter() - t0) * 1000,
    )

    # Return partials keyed by partner_id so LangGraph merges them
    # correctly when fan-in happens.
    return {
        "analyses": {pid: analysis},
        "charts": {pid: chart_list},
        "email_bodies": {pid: email},
    }


# ---------------------------------------------------------------------------
# Convenience exports for callers that want to introspect or test
# individual sub-steps without going through the full pipeline.
# ---------------------------------------------------------------------------


async def partner_analyze(state: PartnerPipelineState) -> dict[str, Any]:
    pid = state["partner_id"]
    summary = state["summary"]
    ctx = get_pipeline_ctx()
    llm = LLMClient(disabled=bool(ctx.get("dry_run")))
    analysis = await analyze_one(llm, summary)
    return {"analyses": {pid: analysis}}


async def partner_chart(state: PartnerPipelineState) -> dict[str, Any]:
    from pathlib import Path

    pid = state["partner_id"]
    summary = state["summary"]
    ctx = get_pipeline_ctx()
    out_dir = Path(ctx["out_dir"]) / "partners" / pid
    chart_list = render_partner_charts(summary, out_dir)
    analyses = state.get("analyses") or {}
    analysis = analyses.get(pid) if isinstance(analyses, dict) else None
    if analysis and getattr(analysis, "recommended_actions", None):
        chart_list[0].title = (
            f"Success rate by gateway -- recommended: "
            f"{analysis.recommended_actions[0][:60]}"
        )
    return {"charts": {pid: chart_list}}


async def partner_email(state: PartnerPipelineState) -> dict[str, Any]:
    pid = state["partner_id"]
    summary = state["summary"]
    analyses = state.get("analyses") or {}
    charts = state.get("charts") or {}
    analysis = analyses.get(pid) if isinstance(analyses, dict) else None
    chart_list = charts.get(pid) if isinstance(charts, dict) else None

    if analysis is None:
        analysis = fallback_analysis(summary)
    if chart_list is None:
        chart_list = []

    meta = PartnerMeta(
        partner_id=summary.partner_id,
        partner_name=summary.partner_name,
        contact_email=summary.contact_email,
    )
    ctx = get_pipeline_ctx()
    llm = LLMClient(disabled=bool(ctx.get("dry_run")))
    try:
        data = llm.complete_json(
            EMAIL_SYSTEM,
            email_user_prompt(
                meta.partner_name,
                meta.tone,
                analysis.model_dump_json(indent=2),
                [c.title for c in chart_list],
            ),
        )
        email = EmailOutput.model_validate(data)
    except Exception as e:  # noqa: BLE001
        log.warning("partner_email.fallback partner=%s reason=%s", pid, e)
        email = fallback_email(summary, analysis)
    return {"email_bodies": {pid: email}}


__all__ = [
    "get_partner_ctx",
    "partner_analyze",
    "partner_chart",
    "partner_email",
    "partner_pipeline_node",
    "set_pipeline_ctx",
]


def get_partner_ctx() -> dict[str, Any]:
    return _PIPELINE_CTX