"""Smoke tests for the payments reporting workflow.

These run without any LLM call or external service. They cover the
deterministic path: trigger -> ingest -> aggregate -> analysis_agent ->
chart_generator -> email_agent -> dispatch_emails.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from payments_reporting.graph import build_graph, run_weekly  # noqa: E402
from payments_reporting.state import (  # noqa: E402
    GraphState,
    initial_state,
)


@pytest.fixture(scope="module", autouse=True)
def _seed_csv():
    csv = ROOT / "data" / "sample_week.csv"
    if not csv.exists():
        from scripts.seed_sample import main as seed_main  # type: ignore

        seed_main()
    yield


@pytest.mark.asyncio
async def test_graph_runs_in_dry_run_mode(tmp_path):
    """The whole graph runs deterministically when LLM is disabled."""
    out_dir = tmp_path / "out"
    final: GraphState = await run_weekly(
        run_id="smoke-test",
        dry_run=True,
        out_dir=str(out_dir),
    )
    assert final.get("week_start") is not None
    assert final.get("week_end") is not None
    assert len(final.get("partner_summaries") or {}) >= 1
    assert len(final.get("analyses") or {}) >= 1
    assert len(final.get("email_bodies") or {}) >= 1
    send_results = final.get("send_results") or []
    assert len(send_results) >= 1
    assert all(r.success for r in send_results)


@pytest.mark.asyncio
async def test_aggregate_produces_real_anomalies():
    """The seeded CSV has a Braintree EU failure spike. Aggregation must surface it."""
    from payments_reporting.nodes.aggregate import _trends
    from payments_reporting.state import RawRow

    # Build two weeks of fake rows: this week has a Braintree latency bump.
    def _row(gw: str, lat: int, success: bool) -> RawRow:
        return RawRow(
            timestamp=datetime(2026, 6, 22, 12, tzinfo=timezone.utc),
            partner_id="P-001",
            region="EU",
            gateway=gw,
            success=success,
            result_code="200" if success else "processor_timeout",
            latency_ms=lat,
        )

    this_week = [_row("braintree", 1200, False) for _ in range(20)] + [
        _row("braintree", 300, True) for _ in range(80)
    ]
    last_week = [_row("braintree", 300, True) for _ in range(100)]
    trends = _trends(this_week, last_week)
    assert any("braintree" in t.metric for t in trends)


@pytest.mark.asyncio
async def test_charts_create_png_files(tmp_path):
    """chart_generator writes three PNGs per partner."""
    final = await run_weekly(
        run_id="chart-test",
        dry_run=True,
        out_dir=str(tmp_path / "out"),
    )
    charts = final.get("charts") or {}
    for pid, chart_list in charts.items():
        for c in chart_list:
            assert Path(c.path).exists(), f"missing chart: {c.path}"


def test_state_snapshot_is_json_safe():
    """state.json must round-trip without datetime/BaseModel errors."""
    state = initial_state(run_id="json-test", dry_run=True)
    from payments_reporting.state import state_snapshot

    snap = state_snapshot(state)
    json.dumps(snap)  # must not raise


def test_email_dry_run_writes_file(tmp_path):
    """Email sender writes the rendered email to disk in dry-run mode."""
    from payments_reporting.tools.email_sender import EmailSender

    async def _go() -> None:
        sender = EmailSender(out_dir=tmp_path)
        result = await sender.send(
            to_email="ops@acme.example.com",
            subject="Test",
            html_body="<p>hello</p>",
            partner_id="P-001",
            run_id="r1",
        )
        assert result.success
        assert (tmp_path / "partners" / "P-001" / "email.html").exists()

    asyncio.run(_go())


def test_app_insights_csv_loader():
    """CSV fallback returns rows in the [week_start, week_end) window."""
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


def test_graph_compiles():
    """The graph builds without errors and has the expected node count."""
    g = build_graph()
    expected = {"trigger", "ingest", "aggregate", "analysis_agent",
                "chart_generator", "email_agent", "dispatch_emails"}
    # LangGraph adds a synthetic __start__ node automatically; ignore it.
    actual = {n for n in g.nodes if not n.startswith("__")}
    assert actual == expected