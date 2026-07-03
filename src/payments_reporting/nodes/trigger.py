"""trigger_node: resolve the week window and partner list.

Reads nothing. Writes week_start, week_end, partners.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from ..state import GraphState, PartnerMeta

log = logging.getLogger(__name__)

DEFAULT_PARTNERS: list[PartnerMeta] = [
    PartnerMeta(
        partner_id="P-001",
        partner_name="Acme Insurance EU",
        contact_email="ops@acme-eu.example.com",
        regions=["EU"],
        tone="formal",
    ),
    PartnerMeta(
        partner_id="P-002",
        partner_name="Bharat Pay Asia",
        contact_email="payments@bharatpay.example.com",
        regions=["APAC"],
        tone="neutral",
    ),
    PartnerMeta(
        partner_id="P-003",
        partner_name="Latam Pay Partners",
        contact_email="team@latampay.example.com",
        regions=["LATAM"],
        tone="friendly",
    ),
]


def _resolve_week(
    week_start: datetime | None,
) -> tuple[datetime, datetime]:
    """Return (last_full_week_start, last_full_week_end) in UTC.

    Last full Monday-anchored 7-day window ending at the most recent
    Sunday 23:59:59 UTC. Configurable via WEEK_START_ISO env var.
    """
    if week_start is None:
        env = os.getenv("WEEK_START_ISO")
        if env:
            week_start = datetime.fromisoformat(env)
    if week_start is None:
        now = datetime.now(timezone.utc)
        days_since_monday = now.weekday()
        last_monday = (
            now - timedelta(days=days_since_monday + 7)
        ).replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = last_monday
    if week_start.tzinfo is None:
        week_start = week_start.replace(tzinfo=timezone.utc)
    week_end = week_start + timedelta(days=7)
    return week_start, week_end


def _resolve_partners(
    override: list[PartnerMeta] | None,
) -> list[PartnerMeta]:
    if override:
        return override
    env = os.getenv("PARTNERS_OVERRIDE")
    if env:
        ids = [s.strip() for s in env.split(",") if s.strip()]
        by_id = {p.partner_id: p for p in DEFAULT_PARTNERS}
        return [by_id[i] for i in ids if i in by_id] or list(DEFAULT_PARTNERS)
    return list(DEFAULT_PARTNERS)


async def trigger(state: GraphState) -> dict:
    log.info(
        "trigger.start run_id=%s dry_run=%s",
        state["run_id"],
        state.get("dry_run"),
    )
    week_start, week_end = _resolve_week(None)
    partners = _resolve_partners(None)
    log.info(
        "trigger.done week_start=%s week_end=%s partners=%d",
        week_start.isoformat(),
        week_end.isoformat(),
        len(partners),
    )
    return {
        "week_start": week_start,
        "week_end": week_end,
        "partners": partners,
    }


__all__ = ["DEFAULT_PARTNERS", "trigger"]