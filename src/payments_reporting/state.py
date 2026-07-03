"""Graph state and DTOs for the weekly payments reporting workflow.

The state is a TypedDict so LangGraph can pass it between nodes as a plain
dict. Each node reads specific keys, does work, and writes back new keys.
In an interview, walk an interviewer through this file and the table in
README.md.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, NotRequired, TypedDict

from pydantic import BaseModel, Field


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
    """

    # control
    run_id: str
    dry_run: bool
    week_start: NotRequired[datetime]
    week_end: NotRequired[datetime]
    partners: NotRequired[list[PartnerMeta]]

    # raw + aggregated
    raw_metrics: NotRequired[dict[str, list[RawRow]]]
    partner_summaries: NotRequired[dict[str, PartnerSummary]]
    last_week_metrics: NotRequired[dict[str, list[RawRow]]]  # for WoW

    # LLM artefacts
    analyses: NotRequired[dict[str, AnalysisOutput]]
    charts: NotRequired[dict[str, list[ChartFile]]]
    email_bodies: NotRequired[dict[str, EmailOutput]]

    # dispatch
    send_results: NotRequired[list[SendResult]]

    # observability
    errors: NotRequired[list[str]]
    node_durations_ms: NotRequired[dict[str, float]]


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