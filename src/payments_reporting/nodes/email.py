"""email_agent node: LLM composes a partner-specific HTML email.

Input:  PartnerSummary + AnalysisOutput + chart metadata + PartnerMeta.tone
Output: EmailOutput (subject, html_body)

Falls back to a deterministic template if the LLM is unavailable.
"""

from __future__ import annotations

import logging

from ..llm import LLMClient
from ..prompts import EMAIL_SYSTEM, email_user_prompt
from ..state import (
    AnalysisOutput,
    ChartFile,
    EmailOutput,
    GraphState,
    PartnerMeta,
    PartnerSummary,
)

log = logging.getLogger(__name__)


def _fallback_email(
    summary: PartnerSummary, analysis: AnalysisOutput
) -> EmailOutput:
    """Deterministic HTML email when the LLM is disabled or fails."""
    issues_html = "".join(f"<li>{x}</li>" for x in analysis.key_issues)
    actions_html = "".join(f"<li>{x}</li>" for x in analysis.recommended_actions)
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


async def _compose_one(
    llm: LLMClient,
    summary: PartnerSummary,
    analysis: AnalysisOutput,
    charts: list[ChartFile],
    meta: PartnerMeta,
) -> EmailOutput:
    analysis_json = analysis.model_dump_json(indent=2)
    chart_titles = [c.title for c in charts]
    user = email_user_prompt(meta.partner_name, meta.tone, analysis_json, chart_titles)
    try:
        data = llm.complete_json(EMAIL_SYSTEM, user)
        return EmailOutput.model_validate(data)
    except Exception as e:  # noqa: BLE001
        log.warning(
            "email_agent.fallback partner=%s reason=%s",
            meta.partner_id,
            e,
        )
        return _fallback_email(summary, analysis)


async def email_agent(state: GraphState) -> dict:
    summaries: dict[str, PartnerSummary] = state.get("partner_summaries") or {}
    analyses: dict[str, AnalysisOutput] = state.get("analyses") or {}
    charts: dict[str, list[ChartFile]] = state.get("charts") or {}
    metas = {p.partner_id: p for p in (state.get("partners") or [])}
    llm = LLMClient(disabled=bool(state.get("dry_run")))

    out: dict[str, EmailOutput] = {}
    for pid, summary in summaries.items():
        out[pid] = await _compose_one(
            llm,
            summary,
            analyses.get(pid) or AnalysisOutput(overview=summary.headline, key_issues=[], likely_causes=[], recommended_actions=[]),
            charts.get(pid, []),
            metas[pid],
        )

    log.info("email_agent.done partners=%d", len(out))
    return {"email_bodies": out}


__all__ = ["email_agent"]