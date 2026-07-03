"""Helpers for the email LLM call.

The graph node (`partner_email`) lives in `partner_pipeline.py`. This
module exposes the deterministic fallback and the per-partner LLM
helper that the subgraph node uses.
"""

from __future__ import annotations

import logging

from ..llm import LLMClient
from ..prompts import EMAIL_SYSTEM, email_user_prompt
from ..state import (
    AnalysisOutput,
    ChartFile,
    EmailOutput,
    PartnerMeta,
    PartnerSummary,
)

log = logging.getLogger(__name__)


def fallback_email(
    summary: PartnerSummary, analysis: AnalysisOutput
) -> EmailOutput:
    """Deterministic HTML email when the LLM is disabled or fails."""
    issues_html = "".join(f"<li>{x}</li>" for x in analysis.key_issues)
    actions_html = "".join(
        f"<li>{x}</li>" for x in analysis.recommended_actions
    )
    html = f"""
    <p>Hi {summary.partner_name} team,</p>
    <p>{analysis.overview}</p>
    <h3>What we saw this week</h3>
    <ul>{issues_html}</ul>
    <h3>What we recommend</h3>
    <ul>{actions_html}</ul>
    <p>Reach the payments team at payments-platform@example.com.</p>
    <p style="color:#888;font-size:11px">
      Auto-generated weekly report. Numbers from Application Insights.
    </p>
    """
    return EmailOutput(
        subject=f"Weekly payments summary  --  {summary.partner_name}",
        html_body=html.strip(),
    )


async def compose_one(
    llm: LLMClient,
    summary: PartnerSummary,
    analysis: AnalysisOutput,
    charts: list[ChartFile],
    meta: PartnerMeta,
) -> EmailOutput:
    """Run one partner's email composition. Used by the subgraph node."""
    analysis_json = analysis.model_dump_json(indent=2)
    chart_titles = [c.title for c in charts]
    user = email_user_prompt(
        meta.partner_name, meta.tone, analysis_json, chart_titles
    )
    try:
        data = llm.complete_json(EMAIL_SYSTEM, user)
        return EmailOutput.model_validate(data)
    except Exception as e:  # noqa: BLE001
        log.warning(
            "compose_one.fallback partner=%s reason=%s",
            meta.partner_id,
            e,
        )
        return fallback_email(summary, analysis)


__all__ = ["compose_one", "fallback_email"]