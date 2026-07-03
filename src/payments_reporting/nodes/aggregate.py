"""aggregate_node: build per-partner PartnerSummary from raw metrics.

Input:  raw_metrics, last_week_metrics, partners, week_start, week_end
Output: partner_summaries: dict[partner_id, PartnerSummary]

Pure deterministic code. The LLM never sees raw rows, only these
structured DTOs  --  that's why the cost is bounded and the output is
grounded.
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

ANOMALY_THRESHOLD_PCT = 25.0


def _by_partner(rows: list[RawRow]) -> dict[str, list[RawRow]]:
    out: dict[str, list[RawRow]] = {}
    for r in rows:
        out.setdefault(r.partner_id, []).append(r)
    return out


def _gateway_stats(rows: list[RawRow]) -> list[GatewayStat]:
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


def _top_failures(rows: list[RawRow], n: int = 5) -> list[FailureBucket]:
    counter: Counter[tuple[str, str]] = Counter()
    for r in rows:
        if not r.success:
            counter[(r.gateway, r.result_code)] += 1
    return [
        FailureBucket(gateway=gw, result_code=code, count=c)
        for (gw, code), c in counter.most_common(n)
    ]


def _trends(
    this_rows: list[RawRow], last_rows: list[RawRow]
) -> list[TrendDelta]:
    """Compare this-week vs last-week on a few headline metrics."""
    last_by_gw = _gateway_stats(last_rows)

    def _metric(gw_rows: list[RawRow]) -> tuple[float, float]:
        total = len(gw_rows)
        succ = sum(1 for r in gw_rows if r.success)
        rate = succ / total if total else 0.0
        avg_lat = mean(r.latency_ms for r in gw_rows) if gw_rows else 0.0
        return rate, avg_lat

    out: list[TrendDelta] = []
    for gw_stat in _gateway_stats(this_rows):
        gw_rows = [r for r in this_rows if r.gateway == gw_stat.gateway]
        rate_now, lat_now = _metric(gw_rows)
        match = next((g for g in last_by_gw if g.gateway == gw_stat.gateway), None)
        if match is None:
            continue
        _, lat_last = _metric([r for r in last_rows if r.gateway == gw_stat.gateway])
        delta_pct = (
            ((lat_now - lat_last) / lat_last * 100.0) if lat_last else 0.0
        )
        out.append(
            TrendDelta(
                metric=f"{gw_stat.gateway}_avg_latency_ms",
                this_week=round(lat_now, 1),
                last_week=round(lat_last, 1),
                delta_pct=round(delta_pct, 1),
                is_anomaly=abs(delta_pct) > ANOMALY_THRESHOLD_PCT,
            )
        )
        out.append(
            TrendDelta(
                metric=f"{gw_stat.gateway}_success_rate",
                this_week=round(rate_now * 100, 2),
                last_week=round(match.success_rate * 100, 2),
                delta_pct=round(
                    (rate_now - match.success_rate) * 100, 1
                ),
                is_anomaly=abs(rate_now - match.success_rate) > 0.05,
            )
        )
    return out


def _build_summary(
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
        by_gateway=_gateway_stats(rows),
        top_failures=_top_failures(rows),
        trends=_trends(rows, last_rows),
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
        summaries[pid] = _build_summary(
            meta, rows, last_rows, week_start, week_end
        )

    log.info("aggregate.done summaries=%d", len(summaries))
    return {"partner_summaries": summaries}