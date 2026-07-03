"""ingest_node: fetch raw metrics for the week.

Input:  week_start, week_end, partners
Output: raw_metrics: dict[partner_id, list[RawRow]]
        last_week_metrics: dict[partner_id, list[RawRow]]  (for WoW trends)

In production this hits App Insights KQL via tools.app_insights.
Locally it reads a CSV so the whole pipeline is runnable without Azure.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

from ..state import GraphState, RawRow
from ..tools.app_insights import AppInsightsClient

log = logging.getLogger(__name__)


def _group_by_partner(rows: list[RawRow]) -> dict[str, list[RawRow]]:
    grouped: dict[str, list[RawRow]] = defaultdict(list)
    for r in rows:
        grouped[r.partner_id].append(r)
    return dict(grouped)


async def ingest(state: GraphState) -> dict:
    week_start = state["week_start"]
    week_end = state["week_end"]
    partners = state["partners"]
    csv_path_env = state.get("dry_run") and "data/sample_week.csv" or None
    csv_path = Path(csv_path_env) if csv_path_env else None

    client = AppInsightsClient(csv_path=csv_path)
    try:
        rows = await client.fetch_rows(
            week_start, week_end, [p.partner_id for p in partners]
        )
        last_week_rows = await client.fetch_rows(
            week_start - timedelta(days=7),
            week_end - timedelta(days=7),
            [p.partner_id for p in partners],
        )
    finally:
        await client.aclose()

    raw = _group_by_partner(rows)
    last_week = _group_by_partner(last_week_rows)

    log.info(
        "ingest.done rows=%d partners=%d",
        len(rows),
        len(raw),
    )
    return {"raw_metrics": raw, "last_week_metrics": last_week}