"""
notifier.py — Deliver the run report to Finance by email (SMTP).

After a run, the agent produces a CSV summarising every invoice and its outcome
(posted / draft / rejected, with reasons). This module emails that CSV as an
attachment so the report lands in Finance's inbox automatically, rather than
sitting on disk waiting to be opened.

Uses Gmail SMTP with the same app password already in .env (sending over SMTP is
separate from the IMAP reading path). Recipients are configurable.

This is the "delivery" half of the human-in-the-loop surface: the agent decides
and records; this makes sure a human actually receives what needs review.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

logger = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465  # SSL


def send_report(
    sender: str,
    app_password: str,
    recipients: list[str],
    report_path: str,
    summary: str = "",
) -> bool:
    """Email the CSV report as an attachment. Returns True on success.

    Fails soft: a delivery failure is logged and returns False, but never crashes
    the agent — producing the report matters more than emailing it.
    """
    if not os.path.exists(report_path):
        logger.warning("Report not found at %s — nothing to email.", report_path)
        return False

    msg = EmailMessage()
    msg["Subject"] = "Invoicing Agent — run report"
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(
        "Attached is the latest invoicing-agent run report.\n\n"
        f"{summary}\n\n"
        "Rows marked DRAFT or REVIEW need a human to confirm in Odoo; "
        "rows marked REJECT were intentionally not posted.\n\n"
        "— Automated message from the Invoicing Agent"
    )

    with open(report_path, "rb") as f:
        data = f.read()
    msg.add_attachment(
        data,
        maintype="text",
        subtype="csv",
        filename=os.path.basename(report_path),
    )

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
            server.login(sender, app_password)
            server.send_message(msg)
        logger.info("Report emailed to %s", ", ".join(recipients))
        return True
    except Exception as exc:
        logger.error("Failed to email report: %s", exc)
        return False
