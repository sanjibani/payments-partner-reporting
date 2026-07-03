"""Email sender with dry-run by default.

Real SMTP mode is only used when SMTP_HOST is set AND EMAIL_DRY_RUN != "1".
In dry-run mode, the sender writes the rendered email to a file under
out_dir/partners/<pid>/email.html and returns success=True with a
dry-run message_id. This lets the entire graph run end-to-end without
any external service.
"""

from __future__ import annotations

import asyncio
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from ..state import EmailOutput, SendResult

log = logging.getLogger(__name__)


class EmailSender:
    def __init__(
        self,
        *,
        dry_run: bool | None = None,
        smtp_host: str | None = None,
        smtp_port: int | None = None,
        smtp_user: str | None = None,
        smtp_password: str | None = None,
        email_from: str | None = None,
        out_dir: Path | None = None,
    ) -> None:
        self.smtp_host = smtp_host or os.getenv("SMTP_HOST", "")
        self.smtp_port = int(smtp_port or os.getenv("SMTP_PORT", "587"))
        self.smtp_user = smtp_user or os.getenv("SMTP_USER", "")
        self.smtp_password = smtp_password or os.getenv("SMTP_PASSWORD", "")
        self.email_from = email_from or os.getenv(
            "EMAIL_FROM", "payments-reports@example.com"
        )
        env_dry = os.getenv("EMAIL_DRY_RUN", "1") == "1"
        self.dry_run = dry_run if dry_run is not None else env_dry
        self.out_dir = out_dir

    def is_configured(self) -> bool:
        """True when SMTP is set AND dry_run is not forced."""
        return bool(self.smtp_host) and not self.dry_run

    async def send(
        self,
        to_email: str,
        subject: str,
        html_body: str,
        partner_id: str,
        run_id: str,
    ) -> SendResult:
        if self.is_configured():
            return await self._send_smtp(to_email, subject, html_body)
        return await self._send_dry_run(
            to_email, subject, html_body, partner_id, run_id
        )

    # ---- Real SMTP mode ----

    async def _send_smtp(
        self, to_email: str, subject: str, html_body: str
    ) -> SendResult:
        msg = EmailMessage()
        msg["From"] = self.email_from
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.set_content("This email requires an HTML-capable client.")
        msg.add_alternative(html_body, subtype="html")

        def _do_send() -> str:
            context = ssl.create_default_context()
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as s:
                s.starttls(context=context)
                if self.smtp_user:
                    s.login(self.smtp_user, self.smtp_password)
                s.send_message(msg)
            return msg["Message-ID"] or "no-message-id"

        try:
            loop = asyncio.get_event_loop()
            message_id = await loop.run_in_executor(None, _do_send)
            return SendResult(
                partner_id=to_email, success=True, message_id=message_id
            )
        except (smtplib.SMTPException, OSError) as e:
            log.exception("smtp.send failed to=%s", to_email)
            return SendResult(
                partner_id=to_email, success=False, error=str(e)
            )

    # ---- Dry-run mode ----

    async def _send_dry_run(
        self,
        to_email: str,
        subject: str,
        html_body: str,
        partner_id: str,
        run_id: str,
    ) -> SendResult:
        if self.out_dir:
            out_path = (
                self.out_dir / "partners" / partner_id / "email.html"
            )
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(
                _wrap_dry_run_html(subject, html_body, to_email),
                encoding="utf-8",
            )
            log.info(
                "email.dry_run to=%s subject=%s path=%s",
                to_email,
                subject,
                out_path,
            )
        return SendResult(
            partner_id=partner_id,
            success=True,
            message_id=f"dry-run:{run_id}:{partner_id}",
        )


def _wrap_dry_run_html(subject: str, body: str, to_email: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{subject}</title></head><body>"
        "<p style='color:#888;font-size:11px'>"
        f"DRY-RUN DELIVERY (to={to_email})</p>"
        f"<hr>{body}</body></html>"
    )


__all__ = ["EmailSender"]