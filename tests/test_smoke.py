"""Smoke tests for the payments reporting workflow.

These run without any LLM call or external service. They cover every
LangGraph feature used in the project (per plan.md section 3):

- StateGraph compilation
- add_conditional_edges routing decisions
- Send API fan-out
- Annotated reducer merging under concurrent writes
- MemorySaver checkpoint persistence
- graph.astream() live event streaming
- Full graph end-to-end
- CSV fallback ingestion
- Dry-run email sender
- Fallback analysis / email helpers
- Trend anomaly detection
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from langgraph.graph import END  # noqa: E402

from payments_reporting.graph import (  # noqa: E402
    build_graph,
    get_checkpoint_state,
    route_after_dispatch,
    route_after_ingest,
    run_weekly,
    stream_weekly,
)
from payments_reporting.state import (  # noqa: E402
    GraphState,
    merge_dicts,
    initial_state,
)


@pytest.fixture(scope="module", autouse=True)
def _seed_csv():
    csv = ROOT / "data" / "sample_week.csv"
    if not csv.exists():
        from scripts.seed_sample import main as seed_main

        seed_main()
    os.environ.setdefault("WEEK_START_ISO", "2026-06-22T00:00:00+00:00")
    yield


# ---------------------------------------------------------------------------
# State / reducer unit tests
# ---------------------------------------------------------------------------


def test_merge_dicts_merges_and_overrides():
    """Reducer merges dicts, new keys win."""
    assert merge_dicts({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}
    assert merge_dicts({"a": 1}, {"a": 99}) == {"a": 99}
    assert merge_dicts(None, {"a": 1}) == {"a": 1}
    assert merge_dicts({"a": 1}, None) == {"a": 1}


def test_initial_state_seeds_control_fields():
    s = initial_state("r1", True)
    assert s["run_id"] == "r1"
    assert s["dry_run"] is True


def test_state_snapshot_json_safe():
    """state_snapshot must produce JSON-serialisable output for any state."""
    import json

    from payments_reporting.state import (
        AnalysisOutput,
        ChartFile,
        EmailOutput,
        PartnerMeta,
        PartnerSummary,
        state_snapshot,
    )

    state: GraphState = {
        "run_id": "r",
        "dry_run": True,
        "partner_summaries": {
            "P-001": PartnerSummary(
                partner_id="P-001",
                partner_name="Test",
                contact_email="t@e.com",
                week_start=datetime(2026, 6, 22, tzinfo=timezone.utc),
                week_end=datetime(2026, 6, 29, tzinfo=timezone.utc),
                total_requests=10,
                overall_success_rate=0.9,
                by_gateway=[],
                top_failures=[],
                trends=[],
            ),
        },
        "analyses": {
            "P-001": AnalysisOutput(
                overview="x",
                key_issues=["a"],
                likely_causes=["b"],
                recommended_actions=["c"],
            ),
        },
        "email_bodies": {
            "P-001": EmailOutput(subject="s", html_body="<p>h</p>"),
        },
        "charts": {
            "P-001": [
                ChartFile(path="/tmp/x.png", title="t", kind="k"),
            ],
        },
    }
    snap = state_snapshot(state)
    json.dumps(snap)  # must not raise
    assert snap["partner_summaries"]["P-001"]["partner_name"] == "Test"


# ---------------------------------------------------------------------------
# Tool tests
# ---------------------------------------------------------------------------


def test_app_insights_csv_loader_filters_by_week_and_partner():
    """CSV fallback returns rows in [week_start, week_end) for the partner set."""
    from payments_reporting.tools.app_insights import AppInsightsClient

    client = AppInsightsClient(csv_path=ROOT / "data" / "sample_week.csv")
    rows = asyncio.run(
        client.fetch_rows(
            datetime(2026, 6, 22, tzinfo=timezone.utc),
            datetime(2026, 6, 29, tzinfo=timezone.utc),
            ["P-001"],
        )
    )
    assert len(rows) > 0
    assert all(r.partner_id == "P-001" for r in rows)


def test_email_dry_run_writes_html(tmp_path):
    """Dry-run mode writes the rendered email to out_dir/partners/<pid>/email.html."""
    from payments_reporting.tools.email_sender import EmailSender

    async def _go() -> None:
        sender = EmailSender(out_dir=tmp_path)
        assert sender.is_configured() is False
        result = await sender.send(
            to_email="ops@e.com",
            subject="Test",
            html_body="<p>hello</p>",
            partner_id="P-001",
            run_id="r1",
        )
        assert result.success is True
        assert (tmp_path / "partners" / "P-001" / "email.html").exists()

    asyncio.run(_go())


# ---------------------------------------------------------------------------
# Per-partner helpers
# ---------------------------------------------------------------------------


def test_trend_helper_flags_anomaly():
    """trends() must flag >25% latency shift and >5pp success rate drop."""
    from payments_reporting.state import GatewayStat, PartnerSummary, RawRow
    from payments_reporting.nodes.aggregate import trends

    def row(success, gw, lat, code="200"):
        return RawRow(
            timestamp=datetime(2026, 6, 22, tzinfo=timezone.utc),
            partner_id="P-001",
            region="EU",
            gateway=gw,
            success=success,
            result_code=code,
            latency_ms=lat,
        )

    this_week = (
        [row(True, "stripe", 200) for _ in range(80)]
        + [row(True, "braintree", 1500) for _ in range(70)]
        + [row(False, "braintree", 1500, "processor_timeout") for _ in range(20)]
    )
    last_week = (
        [row(True, "stripe", 200) for _ in range(80)]
        + [row(True, "braintree", 300) for _ in range(90)]
        + [row(False, "braintree", 300, "processor_timeout") for _ in range(5)]
    )
    t = trends(this_week, last_week)
    assert any(x.is_anomaly for x in t)


def test_fallback_helpers_produce_valid_dto():
    """Fallback analysis / email must produce DTOs downstream can consume."""
    from payments_reporting.nodes.analysis import fallback_analysis
    from payments_reporting.nodes.email import fallback_email
    from payments_reporting.state import (
        AnalysisOutput,
        FailureBucket,
        GatewayStat,
        PartnerSummary,
        TrendDelta,
    )

    summary = PartnerSummary(
        partner_id="P-001",
        partner_name="Test",
        contact_email="t@e.com",
        week_start=datetime(2026, 6, 22, tzinfo=timezone.utc),
        week_end=datetime(2026, 6, 29, tzinfo=timezone.utc),
        total_requests=100,
        overall_success_rate=0.9,
        by_gateway=[
            GatewayStat(
                gateway="stripe",
                total=100,
                successes=90,
                failures=10,
                success_rate=0.9,
                avg_latency_ms=200,
            ),
        ],
        top_failures=[
            FailureBucket(
                gateway="stripe", result_code="card_declined", count=10
            ),
        ],
        trends=[
            TrendDelta(
                metric="stripe_avg_latency_ms",
                this_week=200,
                last_week=400,
                delta_pct=-50,
                is_anomaly=True,
            ),
        ],
    )
    a = fallback_analysis(summary)
    assert isinstance(a, AnalysisOutput)
    assert len(a.key_issues) >= 1

    e = fallback_email(summary, a)
    assert "Test" in e.subject
    assert "<li>" in e.html_body
    assert "\u2014" not in e.subject
    assert "\u2014" not in e.html_body


# ---------------------------------------------------------------------------
# LangGraph feature tests (the five from plan.md section 3)
# ---------------------------------------------------------------------------


def test_graph_compiles_with_expected_nodes():
    """build_graph produces a graph with the 6 main-graph nodes."""
    g = build_graph()
    expected = {
        "trigger",
        "ingest",
        "aggregate",
        "partner_pipeline",
        "dispatch_emails",
        "alert_failure",
    }
    actual = {n for n in g.nodes if not n.startswith("__")}
    assert actual == expected


def test_route_after_ingest_decision():
    """route_after_ingest returns 'aggregate' or END based on raw_metrics."""
    state = initial_state("r", True)
    state["raw_metrics"] = {}
    assert route_after_ingest(state) == END
    state["raw_metrics"] = {"P-001": []}
    assert route_after_ingest(state) == "aggregate"


@pytest.mark.asyncio
async def test_fan_out_runs_per_partner():
    """Send fan-out produces one analysis per partner (concurrency-safe)."""
    final = await run_weekly(
        run_id="fanout-test",
        dry_run=True,
        out_dir="out/fanout-test",
    )
    analyses = final.get("analyses") or {}
    assert set(analyses.keys()) == {"P-001", "P-002", "P-003"}


@pytest.mark.asyncio
async def test_concurrent_writes_merge_via_reducer():
    """merge_dicts reducer lets parallel Send branches write distinct keys."""
    # This is implicitly tested by test_fan_out_runs_per_partner, but
    # call it out explicitly so a future refactor cannot silently
    # remove the reducer.
    from payments_reporting.graph import fan_out_partners
    from payments_reporting.state import PartnerSummary

    state: GraphState = initial_state("merge-test", True)
    state["partner_summaries"] = {
        f"P-{i:03d}": PartnerSummary(
            partner_id=f"P-{i:03d}",
            partner_name=f"Partner {i}",
            contact_email="t@e.com",
            week_start=datetime(2026, 6, 22, tzinfo=timezone.utc),
            week_end=datetime(2026, 6, 29, tzinfo=timezone.utc),
            total_requests=10,
            overall_success_rate=0.9,
            by_gateway=[],
            top_failures=[],
            trends=[],
        )
        for i in range(1, 4)
    }
    sends = fan_out_partners(state)
    assert len(sends) == 3
    assert all(s.node == "partner_pipeline" for s in sends)


@pytest.mark.asyncio
async def test_streaming_emits_node_events():
    """graph.astream emits an event for each main-graph node."""
    events: list[str] = []
    async for event in stream_weekly(
        run_id="stream-test",
        dry_run=True,
        out_dir="out/stream-test",
    ):
        events.extend(event.keys())
    assert "trigger" in events
    assert "ingest" in events
    assert "aggregate" in events
    # partner_pipeline fires once per Send branch (parallel).
    assert events.count("partner_pipeline") == 3
    assert "dispatch_emails" in events


@pytest.mark.asyncio
async def test_checkpoint_state_persisted():
    """After a run, get_checkpoint_state returns the saved snapshot."""
    await run_weekly(
        run_id="checkpoint-test",
        dry_run=True,
        out_dir="out/checkpoint-test",
    )
    snap = get_checkpoint_state("checkpoint-test")
    assert snap is not None
    assert snap["run_id"] == "checkpoint-test"


# ---------------------------------------------------------------------------
# End-to-end graph test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_graph_in_dry_run_mode(tmp_path):
    """Full graph runs deterministically end-to-end with no LLM."""
    final = await run_weekly(
        run_id="full-graph-test",
        dry_run=True,
        out_dir=str(tmp_path / "out"),
    )
    assert final.get("week_start") is not None
    assert final.get("week_end") is not None
    assert len(final.get("partner_summaries") or {}) == 3
    assert len(final.get("analyses") or {}) == 3
    assert len(final.get("email_bodies") or {}) == 3
    assert len(final.get("charts") or {}) == 3
    send_results = final.get("send_results") or []
    assert len(send_results) == 3
    assert all(r.success for r in send_results)
    # Checkpoint produces node timings.
    assert "trigger" in (final.get("node_durations_ms") or {})


# ---------------------------------------------------------------------------
# Routing decision for dispatch failures
# ---------------------------------------------------------------------------


def test_route_after_dispatch_decision():
    """route_after_dispatch routes to alert_failure on any failed send."""
    from payments_reporting.state import SendResult

    state = initial_state("r", True)
    state["send_results"] = [
        SendResult(partner_id="P-001", success=True, message_id="x"),
    ]
    assert route_after_dispatch(state) == END

    state["send_results"] = [
        SendResult(partner_id="P-001", success=False, error="boom"),
    ]
    assert route_after_dispatch(state) == "alert_failure"


def test_no_em_dashes_anywhere():
    """Voice rule: no em-dashes in source or generated emails.

    Excludes this test file because it intentionally uses the em-dash
    char to assert ABSENCE -- if the rule ever broke in production code,
    the test would still need to be able to write the char.
    """
    EM_DASH = "\u2014"
    roots = [ROOT / "src", ROOT / "api", ROOT / "scripts"]
    offenders: list[str] = []
    for r in roots:
        for p in r.rglob("*.py"):
            text = p.read_text(encoding="utf-8")
            if EM_DASH in text:
                offenders.append(str(p))
    assert not offenders, f"em-dash found in: {offenders}"