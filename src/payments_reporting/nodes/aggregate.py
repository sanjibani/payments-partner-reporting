"""aggregate_node: build per-partner PartnerSummary from raw metrics.

Pure deterministic Python. Reads raw_metrics, last_week_metrics,
partners. Writes partner_summaries.
"""

from __future__ import annotations

import logging
from collections import Counter
from statistics import mean

from ..state import (
    FailureBucket,
    GatewayStat,
    GraphState,
    PartnerMeta,
    PartnerSummary,
    RawRow,
    TrendDelta,
)

log = logging.getLogger(__name__)

ANOMALY_LATENCY_THRESHOLD_PCT = 25.0
ANOMALY_SUCCESS_RATE_THRESHOLD = 0.05


def gateway_stats(rows: list[RawRow]) -> list[GatewayStat]:
    """Per-gateway aggregate from a list of rows."""
    by_gw: dict[str, list[RawRow]] = {}
    for r in rows:
        by_gw.setdefault(r.gateway, []).append(r)
    out: list[GatewayStat] = []
    for gw, items in sorted(by_gw.items()):
        total = len(items)
        successes = sum(1 for r in items if r.success)
        failures = total - successes
        rate = successes / total if total else 0.0
        avg_lat = mean(r.latency_ms for r in items) if items else 0.0
        out.append(
            GatewayStat(
                gateway=gw,
                total=total,
                successes=successes,
                failures=failures,
                success_rate=rate,
                avg_latency_ms=avg_lat,
            )
        )
    return out


def top_failures(rows: list[RawRow], n: int = 5) -> list[FailureBucket]:
    counter: Counter[tuple[str, str]] = Counter()
    for r in rows:
        if not r.success:
            counter[(r.gateway, r.result_code)] += 1
    return [
        FailureBucket(gateway=gw, result_code=code, count=c)
        for (gw, code), c in counter.most_common(n)
    ]


def trends(
    this_rows: list[RawRow], last_rows: list[RawRow]
) -> list[TrendDelta]:
    """Compare this-week vs last-week on a few headline metrics."""
    last_by_gw = gateway_stats(last_rows)
    out: list[TrendDelta] = []
    for gw_stat in gateway_stats(this_rows):
        gw_rows = [r for r in this_rows if r.gateway == gw_stat.gateway]
        match = next(
            (g for g in last_by_gw if g.gateway == gw_stat.gateway), None
        )
        if match is None:
            continue
        this_lat = mean(r.latency_ms for r in gw_rows) if gw_rows else 0.0
        last_lat = match.avg_latency_ms
        lat_delta = (
            ((this_lat - last_lat) / last_lat * 100.0) if last_lat else 0.0
        )
        out.append(
            TrendDelta(
                metric=f"{gw_stat.gateway}_avg_latency_ms",
                this_week=round(this_lat, 1),
                last_week=round(last_lat, 1),
                delta_pct=round(lat_delta, 1),
                is_anomaly=abs(lat_delta) > ANOMALY_LATENCY_THRESHOLD_PCT,
            )
        )
        sr_delta = gw_stat.success_rate - match.success_rate
        out.append(
            TrendDelta(
                metric=f"{gw_stat.gateway}_success_rate",
                this_week=round(gw_stat.success_rate * 100, 2),
                last_week=round(match.success_rate * 100, 2),
                delta_pct=round(sr_delta * 100, 1),
                is_anomaly=abs(sr_delta) > ANOMALY_SUCCESS_RATE_THRESHOLD,
            )
        )
    return out


def build_summary(
    meta: PartnerMeta,
    rows: list[RawRow],
    last_rows: list[RawRow],
    week_start,
    week_end,
) -> PartnerSummary:
    total = len(rows)
    succ = sum(1 for r in rows if r.success)
    overall_rate = succ / total if total else 0.0
    return PartnerSummary(
        partner_id=meta.partner_id,
        partner_name=meta.partner_name,
        contact_email=meta.contact_email,
        week_start=week_start,
        week_end=week_end,
        total_requests=total,
        overall_success_rate=round(overall_rate, 4),
        by_gateway=gateway_stats(rows),
        top_failures=top_failures(rows),
        trends=trends(rows, last_rows),
    )


async def aggregate(state: GraphState) -> dict:
    raw = state.get("raw_metrics") or {}
    last = state.get("last_week_metrics") or {}
    by_meta = {p.partner_id: p for p in (state.get("partners") or [])}
    week_start = state["week_start"]
    week_end = state["week_end"]

    summaries: dict[str, PartnerSummary] = {}
    for pid, meta in by_meta.items():
        rows = raw.get(pid, [])
        last_rows = last.get(pid, [])
        summaries[pid] = build_summary(
            meta, rows, last_rows, week_start, week_end
        )
    log.info("aggregate.done summaries=%d", len(summaries))
    return {"partner_summaries": summaries}


__all__ = ["aggregate", "build_summary", "gateway_stats", "top_failures", "trends"]