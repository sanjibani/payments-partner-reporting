"""chart_generator node: deterministic PNG charts from a PartnerSummary.

Input:  one PartnerSummary (and optionally an AnalysisOutput for chart titles)
Output: list[ChartFile]

No LLM. matplotlib only. Output lands in
out/<run_id>/partners/<partner_id>/charts/<kind>.png and the path is
embedded in state so the email_agent can reference it.
"""

from __future__ import annotations

import logging
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless
import matplotlib.pyplot as plt

from ..state import AnalysisOutput, ChartFile, GraphState, PartnerSummary

log = logging.getLogger(__name__)


def _success_rate_bar(summary: PartnerSummary, out_path: Path) -> ChartFile:
    gateways = [g.gateway for g in summary.by_gateway]
    rates = [g.success_rate * 100 for g in summary.by_gateway]

    fig, ax = plt.subplots(figsize=(6, 3.5))
    bars = ax.bar(gateways, rates, color=["#4caf50" if r >= 90 else "#ff9800" if r >= 70 else "#f44336" for r in rates])
    ax.set_ylabel("Success rate (%)")
    ax.set_ylim(0, 100)
    ax.set_title(f"Success rate by gateway  --  {summary.partner_name}")
    for bar, rate in zip(bars, rates):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + 1,
            f"{rate:.1f}%",
            ha="center",
            fontsize=9,
        )
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return ChartFile(
        path=str(out_path),
        title="Success rate by gateway",
        kind="success_rate_bar",
    )


def _failure_buckets(summary: PartnerSummary, out_path: Path) -> ChartFile:
    buckets = summary.top_failures
    if not buckets:
        fig, ax = plt.subplots(figsize=(6, 3))
        ax.text(0.5, 0.5, "No failures this week", ha="center", va="center")
        ax.set_axis_off()
    else:
        labels = [f"{b.gateway}\n{b.result_code}" for b in buckets]
        counts = [b.count for b in buckets]
        fig, ax = plt.subplots(figsize=(6, 3.5))
        ax.barh(labels[::-1], counts[::-1], color="#ef5350")
        ax.set_xlabel("Failure count")
        ax.set_title(f"Top failure buckets  --  {summary.partner_name}")
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return ChartFile(
        path=str(out_path),
        title="Top failure buckets this week",
        kind="failure_buckets_stacked",
    )


def _wow_trend(summary: PartnerSummary, out_path: Path) -> ChartFile:
    rates = [t for t in summary.trends if t.metric.endswith("_success_rate")]
    if not rates:
        fig, ax = plt.subplots(figsize=(6, 3))
        ax.text(0.5, 0.5, "No trend data", ha="center", va="center")
        ax.set_axis_off()
    else:
        labels = [t.metric.replace("_success_rate", "") for t in rates]
        this_w = [t.this_week for t in rates]
        last_w = [t.last_week for t in rates]
        fig, ax = plt.subplots(figsize=(6, 3.5))
        x = range(len(labels))
        ax.bar([i - 0.2 for i in x], last_w, width=0.4, label="Last week", color="#90a4ae")
        ax.bar([i + 0.2 for i in x], this_w, width=0.4, label="This week", color="#42a5f5")
        ax.set_xticks(list(x))
        ax.set_xticklabels(labels)
        ax.set_ylabel("Success rate (%)")
        ax.set_title(f"Week-over-week  --  {summary.partner_name}")
        ax.legend()
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return ChartFile(
        path=str(out_path),
        title="Week-over-week success rate trend",
        kind="wow_trend",
    )


async def chart_generator(state: GraphState) -> dict:
    summaries: dict[str, PartnerSummary] = state.get("partner_summaries") or {}
    analyses: dict[str, AnalysisOutput] = state.get("analyses") or {}
    out_dir = Path(state.get("out_dir", f"out/{state['run_id']}"))  # type: ignore[arg-type]

    charts: dict[str, list[ChartFile]] = {}
    for pid, summary in summaries.items():
        partner_dir = out_dir / "partners" / pid / "charts"
        partner_dir.mkdir(parents=True, exist_ok=True)
        chart_list = [
            _success_rate_bar(summary, partner_dir / "success_rate.png"),
            _failure_buckets(summary, partner_dir / "failures.png"),
            _wow_trend(summary, partner_dir / "wow.png"),
        ]
        # Optionally enrich titles from the LLM analysis
        if pid in analyses:
            analysis = analyses[pid]
            if analysis.recommended_actions:
                chart_list[0].title = (
                    f"Success rate by gateway  --  recommended: "
                    f"{analysis.recommended_actions[0][:60]}"
                )
        charts[pid] = chart_list

    log.info("chart_generator.done partners=%d charts/partner=%d", len(charts), 3)
    return {"charts": charts, "out_dir": str(out_dir)}