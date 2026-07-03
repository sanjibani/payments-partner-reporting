"""State DTOs, TypedDicts, and reducers for the weekly payments workflow.

This is the canonical state contract. Every node reads from and writes
to fields declared here. See plan.md section 4 for the design rationale.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, NotRequired, TypedDict

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Reducers
# ---------------------------------------------------------------------------


def merge_dicts(old: dict | None, new: dict | None) -> dict:
    """Shallow dict merge, new keys win.

    Used on the three partner-keyed fields (analyses, charts,
    email_bodies) so concurrent writes from Send fan-out branches
    accumulate instead of triggering INVALID_CONCURRENT_GRAPH_UPDATE.
    """
    out: dict = dict(old or {})
    out.update(new or {})
    return out


# ---------------------------------------------------------------------------
# Pydantic DTOs
# ---------------------------------------------------------------------------


class RawRow(BaseModel):
    """One payment request row from App Insights."""

    timestamp: datetime
    partner_id: str
    region: str
    gateway: str
    success: bool
    result_code: str
    latency_ms: int


class GatewayStat(BaseModel):
    """Per-gateway aggregate for one partner for one week."""

    gateway: str
    total: int
    successes: int
    failures: int
    success_rate: float
    avg_latency_ms: float


class FailureBucket(BaseModel):
    """Top failure (gateway, result_code) pair by count."""

    gateway: str
    result_code: str
    count: int


class TrendDelta(BaseModel):
    """Week-over-week delta for one metric."""

    metric: str
    this_week: float
    last_week: float
    delta_pct: float
    is_anomaly: bool


class PartnerMeta(BaseModel):
    """Static per-partner metadata. Loaded from config or DB."""

    partner_id: str
    partner_name: str
    contact_email: str
    regions: list[str] = Field(default_factory=list)
    tone: str = "neutral"  # "formal" | "neutral" | "friendly"


class PartnerSummary(BaseModel):
    """Deterministic per-partner aggregate. The DTO the LLM sees."""

    partner_id: str
    partner_name: str
    contact_email: str
    week_start: datetime
    week_end: datetime
    total_requests: int
    overall_success_rate: float
    by_gateway: list[GatewayStat]
    top_failures: list[FailureBucket]
    trends: list[TrendDelta]

    @property
    def headline(self) -> str:
        return (
            f"{self.partner_name}: {self.total_requests:,} requests, "
            f"{self.overall_success_rate * 100:.1f}% success this week"
        )


class AnalysisOutput(BaseModel):
    """Structured output from the analysis_agent (LLM #1)."""

    overview: str
    key_issues: list[str]
    likely_causes: list[str]
    recommended_actions: list[str]


class EmailOutput(BaseModel):
    """Structured output from the email_agent (LLM #2)."""

    subject: str
    html_body: str


class ChartFile(BaseModel):
    """A rendered chart on disk."""

    path: str
    title: str
    kind: str  # "success_rate_bar" | "failure_buckets_stacked" | "wow_trend"


class SendResult(BaseModel):
    """Result of one partner's email send."""

    partner_id: str
    success: bool
    message_id: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# TypedDict states
# ---------------------------------------------------------------------------


class GraphState(TypedDict):
    """Main graph state. See plan.md section 4 for the field contract.

    Partner-keyed fields use Annotated reducers so concurrent Send
    branches merge cleanly instead of INVALID_CONCURRENT_GRAPH_UPDATE.
    """

    # control
    run_id: str
    dry_run: bool
    out_dir: NotRequired[str]
    week_start: NotRequired[datetime]
    week_end: NotRequired[datetime]
    partners: NotRequired[list[PartnerMeta]]

    # data
    raw_metrics: NotRequired[dict[str, list[RawRow]]]
    last_week_metrics: NotRequired[dict[str, list[RawRow]]]
    partner_summaries: NotRequired[dict[str, PartnerSummary]]

    # LLM artefacts (concurrency-safe via merge_dicts reducer)
    analyses: NotRequired[Annotated[dict[str, AnalysisOutput], merge_dicts]]
    charts: NotRequired[Annotated[dict[str, list[ChartFile]], merge_dicts]]
    email_bodies: NotRequired[Annotated[dict[str, EmailOutput], merge_dicts]]

    # dispatch
    send_results: NotRequired[list[SendResult]]

    # observability
    errors: NotRequired[list[str]]
    node_durations_ms: NotRequired[dict[str, float]]


class PartnerPipelineState(TypedDict):
    """Per-partner subgraph input. Dispatched via Send.

    MUST contain only partner_id + summary. Any field that overlaps with
    GraphState keys (run_id, dry_run, out_dir, etc.) causes concurrent
    write conflicts on fan-back. Pass such metadata via the
    module-level _PIPELINE_CTX in partner_pipeline.py.
    """

    partner_id: str
    summary: PartnerSummary


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------


def initial_state(run_id: str, dry_run: bool = False) -> GraphState:
    """Empty GraphState seeded with control fields."""
    return {
        "run_id": run_id,
        "dry_run": dry_run,
    }


def state_snapshot(state: GraphState) -> dict[str, Any]:
    """Recursively convert GraphState to JSON-safe form for state.json.

    Handles BaseModel, dict, list, tuple, datetime. No `default=str`
    fallback -- we convert explicitly so the snapshot is deterministic.
    """
    return _dump(state)  # type: ignore[return-value]


def _dump(v: object) -> object:
    if isinstance(v, BaseModel):
        return v.model_dump(mode="json")
    if isinstance(v, dict):
        return {str(k): _dump(val) for k, val in v.items()}
    if isinstance(v, (list, tuple)):
        return [_dump(x) for x in v]
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


__all__ = [
    "AnalysisOutput",
    "ChartFile",
    "EmailOutput",
    "FailureBucket",
    "GatewayStat",
    "GraphState",
    "PartnerMeta",
    "PartnerPipelineState",
    "PartnerSummary",
    "RawRow",
    "SendResult",
    "TrendDelta",
    "initial_state",
    "merge_dicts",
    "state_snapshot",
]