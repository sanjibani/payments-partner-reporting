"""Helpers for the analysis LLM call.

The graph node (`partner_analyze`) lives in `partner_pipeline.py`. This
module exposes the deterministic fallback and the per-partner LLM
helper that the subgraph node uses.
"""

from __future__ import annotations

import logging

from ..llm import LLMClient
from ..prompts import ANALYSIS_SYSTEM, analysis_user_prompt
from ..state import AnalysisOutput, PartnerSummary

log = logging.getLogger(__name__)


def fallback_analysis(summary: PartnerSummary) -> AnalysisOutput:
    """Deterministic output when LLM is disabled or fails.

    Honest, short, grounded only in numbers the code computed. Same
    structure as the LLM output so downstream nodes do not branch.
    """
    top = summary.top_failures[:2]
    issues = [
        f"{b.gateway} returned {b.result_code} {b.count} times this week"
        for b in top
    ] or ["No notable failure spike this week"]
    actions: list[str] = []
    for t in summary.trends:
        if t.is_anomaly:
            actions.append(
                f"Investigate {t.metric}: changed "
                f"{t.delta_pct:+.1f}% vs last week"
            )
    if not actions:
        actions = ["No action required; metrics stable"]
    return AnalysisOutput(
        overview=summary.headline,
        key_issues=issues,
        likely_causes=(
            ["Pattern matches gateway timeout / decline codes"]
            if top
            else ["No anomalies detected"]
        ),
        recommended_actions=actions,
    )


async def analyze_one(
    llm: LLMClient, summary: PartnerSummary
) -> AnalysisOutput:
    """Run one partner's analysis. Used by the subgraph node."""
    summary_json = summary.model_dump_json(indent=2)
    user = analysis_user_prompt(summary_json, summary.partner_name)
    try:
        data = llm.complete_json(ANALYSIS_SYSTEM, user)
        return AnalysisOutput.model_validate(data)
    except Exception as e:  # noqa: BLE001
        log.warning(
            "analyze_one.fallback partner=%s reason=%s",
            summary.partner_id,
            e,
        )
        return fallback_analysis(summary)


__all__ = ["analyze_one", "fallback_analysis"]