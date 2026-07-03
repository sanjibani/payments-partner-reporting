"""dispatch_emails node: send per-partner HTML emails.

Input:  email_bodies, partner_summaries, run_id, out_dir
Output: send_results: list[SendResult]
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..state import EmailOutput, GraphState, SendResult
from ..tools.email_sender import EmailSender

log = logging.getLogger(__name__)


async def dispatch_emails(state: GraphState) -> dict:
    bodies: dict[str, EmailOutput] = state.get("email_bodies") or {}
    summaries = state.get("partner_summaries") or {}
    run_id = state["run_id"]
    out_dir = Path(state.get("out_dir") or f"out/{run_id}")

    sender = EmailSender(out_dir=out_dir)
    results: list[SendResult] = []
    for pid, body in bodies.items():
        summary = summaries.get(pid)
        if summary is None:
            continue
        result: SendResult = await sender.send(
            to_email=summary.contact_email,
            subject=body.subject,
            html_body=body.html_body,
            partner_id=pid,
            run_id=run_id,
        )
        results.append(result)

    failures = [r for r in results if not r.success]
    log.info(
        "dispatch.done sent=%d failed=%d", len(results), len(failures)
    )
    return {"send_results": results}


__all__ = ["dispatch_emails"]