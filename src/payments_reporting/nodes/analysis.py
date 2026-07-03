"""analysis_agent node: LLM turns each PartnerSummary into a structured AnalysisOutput.

Input:  one PartnerSummary (per partner)
Output: AnalysisOutput (overview, key_issues, likely_causes, recommended_actions)

Falls back to a deterministic template if the LLM is unavailable so the
weekly batch never blocks on a model outage.
"""

from __future__ import annotations

import json
import logging

from ..llm import LLMClient
from ..prompts import ANALYSIS_SYSTEM, analysis_user_prompt
from ..state import AnalysisOutput, GraphState, PartnerSummary

log = logging.getLogger(__name__)


def _fallback_analysis(summary: PartnerSummary) -> AnalysisOutput:
    """Deterministic output when LLM is disabled or fails.

    Honest, short, grounded only in numbers the code computed. Same
    structure as the LLM output so downstream nodes do not branch.
    """
    top = summary.top_failures[:2]
    issues = [
        f"{b.gateway} returned {b.result_code} {b.count} times this week"
        for b in top
    ] or ["No notable failure spike this week"]
    anomalies = [t.metric for t in summary.trends if t.is_anomaly]
    actions = []
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
        likely_causes=[
            "Pattern matches gateway timeout / decline codes",
        ] if top else ["No anomalies detected"],
        recommended_actions=actions,
    )


async def _analyse_one(
    llm: LLMClient, summary: PartnerSummary
) -> AnalysisOutput:
    summary_json = summary.model_dump_json(indent=2)
    user = analysis_user_prompt(summary_json, summary.partner_name)
    try:
        data = llm.complete_json(ANALYSIS_SYSTEM, user)
        return AnalysisOutput.model_validate(data)
    except Exception as e:  # noqa: BLE001  --  graceful degradation
        log.warning(
            "analysis_agent.fallback partner=%s reason=%s",
            summary.partner_id,
            e,
        )
        return _fallback_analysis(summary)


async def analysis_agent(state: GraphState) -> dict:
    summaries: dict[str, PartnerSummary] = state.get("partner_summaries") or {}
    llm = LLMClient(disabled=bool(state.get("dry_run")))

    out: dict[str, AnalysisOutput] = {}
    for pid, summary in summaries.items():
        out[pid] = await _analyse_one(llm, summary)

    log.info("analysis_agent.done partners=%d", len(out))
    return {"analyses": out}


__all__ = ["analysis_agent"]