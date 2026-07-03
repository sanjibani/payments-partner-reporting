"""Graph state and DTOs for the weekly payments reporting workflow.

The state is a TypedDict so LangGraph can pass it between nodes as a plain
dict. Each node reads specific keys, does work, and writes back new keys.
In an interview, walk an interviewer through this file and the table in
README.md.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, NotRequired, TypedDict

from pydantic import BaseModel, Field


def _merge_dicts(old: dict, new: dict) -> dict:
    """Reducer that merges two dicts (new keys win on conflict).

    Used for partner-keyed fields like analyses / charts / email_bodies
    so concurrent writes from Send fan-out branches accumulate into one
    dict instead of triggering INVALID_CONCURRENT_GRAPH_UPDATE.
    """
    merged = dict(old or {})
    merged.update(new or {})
    return merged


# ---------------------------------------------------------------------------
# Raw data: one row per payment request that hit our platform
# ---------------------------------------------------------------------------


class RawRow(BaseModel):
    """A single payment request record (one row from App Insights requests)."""

    timestamp: datetime
    partner_id: str
    region: str
    gateway: str  # "stripe" | "braintree" | "adyen" | ...
    success: bool
    result_code: str  # "200", "card_declined", "gateway_timeout", ...
    latency_ms: int


# ---------------------------------------------------------------------------
# Aggregated summary: one per partner per run
# ---------------------------------------------------------------------------


class GatewayStat(BaseModel):
    gateway: str
    total: int
    successes: int
    failures: int
    success_rate: float  # 0.0 to 1.0
    avg_latency_ms: float


class FailureBucket(BaseModel):
    gateway: str
    result_code: str
    count: int


class TrendDelta(BaseModel):
    """Week-over-week delta for a single metric."""

    metric: str  # e.g. "stripe_success_rate"
    this_week: float
    last_week: float
    delta_pct: float  # ((this_week - last_week) / last_week) * 100
    is_anomaly: bool  # True if |delta_pct| > 25


class PartnerSummary(BaseModel):
    """The deterministic per-partner aggregate that the LLM will interpret."""

    partner_id: str
    partner_name: str
    contact_email: str
    week_start: datetime
    week_end: datetime

    total_requests: int
    overall_success_rate: float

    by_gateway: list[GatewayStat]
    top_failures: list[FailureBucket]  # sorted desc, top 5
    trends: list[TrendDelta]

    @property
    def headline(self) -> str:
        return (
            f"{self.partner_name}: {self.total_requests:,} requests, "
            f"{self.overall_success_rate * 100:.1f}% success this week"
        )


# ---------------------------------------------------------------------------
# LLM-generated artefacts
# ---------------------------------------------------------------------------


class AnalysisOutput(BaseModel):
    """Structured output from the analysis_agent (LLM)."""

    overview: str
    key_issues: list[str]
    likely_causes: list[str]
    recommended_actions: list[str]


class ChartFile(BaseModel):
    """A rendered chart on disk, with metadata for the email body."""

    path: str  # absolute path to PNG
    title: str
    kind: str  # "success_rate_bar" | "failure_buckets_stacked" | "wow_trend"


class EmailOutput(BaseModel):
    """Structured output from the email_agent (LLM)."""

    subject: str
    html_body: str


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


class SendResult(BaseModel):
    partner_id: str
    success: bool
    message_id: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# LangGraph state  --  the dict that flows between nodes
# ---------------------------------------------------------------------------


class PartnerMeta(BaseModel):
    """Static per-partner metadata loaded from config."""

    partner_id: str
    partner_name: str
    contact_email: str
    regions: list[str] = Field(default_factory=list)
    tone: str = "neutral"  # "formal" | "neutral" | "friendly"


class GraphState(TypedDict):
    """The full state passed between LangGraph nodes.

    Each node reads what it needs and writes what it produces. In
    interviews, point at the per-node I/O table in README.md.

    Note: partner_summaries, analyses, charts, and email_bodies are all
    keyed by partner_id. That is what lets the per-partner parallel
    fan-out (via Send API) merge cleanly back into the main state --
    each branch writes its own key.
    """

    # control
    run_id: str
    dry_run: bool
    week_start: NotRequired[datetime]
    week_end: NotRequired[datetime]
    partners: NotRequired[list[PartnerMeta]]
    out_dir: NotRequired[str]

    # raw + aggregated
    raw_metrics: NotRequired[dict[str, list[RawRow]]]
    partner_summaries: NotRequired[dict[str, PartnerSummary]]
    last_week_metrics: NotRequired[dict[str, list[RawRow]]]  # for WoW

    # LLM artefacts  --  each entry keyed by partner_id.
    # Annotated reducers let concurrent Send fan-out branches write
    # different keys without INVALID_CONCURRENT_GRAPH_UPDATE.
    analyses: NotRequired[Annotated[dict[str, AnalysisOutput], _merge_dicts]]
    charts: NotRequired[Annotated[dict[str, list[ChartFile]], _merge_dicts]]
    email_bodies: NotRequired[Annotated[dict[str, EmailOutput], _merge_dicts]]

    # dispatch
    send_results: NotRequired[list[SendResult]]

    # observability
    errors: NotRequired[list[str]]
    node_durations_ms: NotRequired[dict[str, float]]


class PartnerPipelineState(TypedDict):
    """Per-partner subgraph state.

    Passed via the Send API after aggregate(). Each partner runs the
    analyze -> chart -> email pipeline in parallel. Nodes in this
    subgraph return partials that LangGraph merges back into the main
    GraphState keyed by partner_id.

    Note: this state intentionally does NOT include run_id, dry_run,
    or out_dir. Those live in a module-level context (see
    partner_pipeline.set_pipeline_ctx) so the subgraph final state has
    no overlapping keys with the main GraphState. Without that
    isolation, LangGraph's LastValue channel errors on concurrent
    writes when multiple Send branches fan back in.
    """

    partner_id: str
    summary: PartnerSummary


def initial_state(run_id: str, dry_run: bool = False) -> GraphState:
    return {
        "run_id": run_id,
        "dry_run": dry_run,
        "errors": [],
        "node_durations_ms": {},
    }


# Re-export for callers that want to type their JSON snapshots
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
]


def state_snapshot(state: GraphState) -> dict[str, Any]:
    """Convert state to a JSON-safe dict (for state.json dump)."""
    return _dump(state)  # type: ignore[return-value]


def _dump(v: object) -> object:
    if isinstance(v, BaseModel):
        return v.model_dump(mode="json")
    if isinstance(v, dict):
        return {str(kk): _dump(vv) for kk, vv in v.items()}
    if isinstance(v, list):
        return [_dump(x) for x in v]
    if isinstance(v, tuple):
        return [_dump(x) for x in v]
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v