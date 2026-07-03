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
all the parallel-fan-out behaviour we want.

Why a module-level context for run_id / dry_run / out_dir?

When a Send branch finishes, LangGraph merges the subgraph's final
state back into the parent graph's state. If PartnerPipelineState
shared any key with GraphState (e.g. run_id, dry_run, out_dir),
every parallel branch would attempt to write that key back, and the
LastValue channel would raise `INVALID_CONCURRENT_GRAPH_UPDATE`.
The fix is to keep those fields OUT of PartnerPipelineState and pass
them via a module-level dict that the subgraph nodes read at
invocation time. The parent graph sets the context inside
`fan_out_partners` immediately before returning the list of Sends.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from .llm import LLMClient
from .nodes.analysis import analyze_one
from .nodes.charts import render_partner_charts
from .nodes.email import compose_one
from .prompts import EMAIL_SYSTEM, email_user_prompt
from .state import (
    EmailOutput,
    PartnerMeta,
    PartnerPipelineState,
    PartnerSummary,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level context. Read-only metadata for the subgraph that must NOT
# overlap with GraphState keys.
# ---------------------------------------------------------------------------


_PIPELINE_CTX: dict[str, Any] = {
    "run_id": "",
    "dry_run": False,
    "out_dir": "out",
}


def set_pipeline_ctx(
    *, run_id: str, dry_run: bool, out_dir: str
) -> None:
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
# The per-partner pipeline node
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
        from .nodes.email import fallback_email

        email = fallback_email(summary, analysis)
    log.debug(
        "partner_pipeline.email partner=%s elapsed_ms=%.2f",
        pid,
        (time.perf_counter() - t0) * 1000,
    )

    # Return partials keyed by partner_id so the merge_dicts reducer
    # accumulates them correctly across concurrent fan-out branches.
    return {
        "analyses": {pid: analysis},
        "charts": {pid: chart_list},
        "email_bodies": {pid: email},
    }


__all__ = [
    "get_pipeline_ctx",
    "partner_pipeline_node",
    "set_pipeline_ctx",
]