"""
email_poller.py — Stages 1 & 2: email monitoring + PDF identification.

This replaces the mocked "folder as inbox" with a real one. It connects to a
Gmail inbox over IMAP, finds messages that carry application/pdf attachments,
saves those PDFs to a local folder, and returns their paths — which is exactly
the shape the rest of the pipeline already consumes.

The seam this plugs into: agent.py previously called iter_invoice_files() over a
folder. Now it can call fetch_pdf_attachments() instead, and nothing downstream
(extract -> validate -> Odoo) changes. That isolation is the whole point.

Notes:
  - Auth uses a Gmail App Password (not the account password), read from .env.
  - We search UNSEEN messages so re-runs don't reprocess the same mail; the
    duplicate-detection layer is still the real safety net against double-posting.
  - This is read-only against the inbox apart from the messages being marked
    read by the fetch; we never delete or send anything.
"""

from __future__ import annotations

import email
import imaplib
import logging
import os
from email.header import decode_header

logger = logging.getLogger(__name__)

IMAP_HOST = "imap.gmail.com"


def _decode(value: str | None) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    out = ""
    for text, enc in parts:
        if isinstance(text, bytes):
            out += text.decode(enc or "utf-8", errors="replace")
        else:
            out += text
    return out


def fetch_pdf_attachments(
    address: str,
    app_password: str,
    save_dir: str,
    mark_seen: bool = True,
    only_unseen: bool = True,
) -> list[str]:
    """Connect to Gmail, pull PDF attachments from matching messages, save them.

    Returns the list of saved PDF file paths (the agent then processes these).
    """
    os.makedirs(save_dir, exist_ok=True)
    saved: list[str] = []

    imap = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        imap.login(address, app_password)
        imap.select("INBOX")

        criterion = "(UNSEEN)" if only_unseen else "(ALL)"
        status, data = imap.search(None, criterion)
        if status != "OK":
            logger.warning("IMAP search failed: %s", status)
            return saved

        msg_ids = data[0].split()
        logger.info("Found %d message(s) to scan", len(msg_ids))

        for msg_id in msg_ids:
            # Peek so we don't implicitly change the seen flag before we decide.
            fetch_cmd = "(RFC822)" if mark_seen else "(BODY.PEEK[])"
            status, msg_data = imap.fetch(msg_id, fetch_cmd)
            if status != "OK":
                continue

            message = email.message_from_bytes(msg_data[0][1])
            subject = _decode(message.get("Subject"))

            had_pdf = False
            for part in message.walk():
                content_type = part.get_content_type()
                filename = _decode(part.get_filename())
                is_pdf = content_type == "application/pdf" or (
                    filename and filename.lower().endswith(".pdf")
                )
                if not is_pdf:
                    continue

                had_pdf = True
                payload = part.get_payload(decode=True)
                if not payload:
                    continue

                safe_name = filename or f"attachment_{msg_id.decode()}.pdf"
                out_path = os.path.join(save_dir, safe_name)
                with open(out_path, "wb") as f:
                    f.write(payload)
                saved.append(out_path)
                logger.info("Saved %s (from subject: %r)", out_path, subject)

            if not had_pdf:
                logger.info("Skipped message with no PDF (subject: %r)", subject)

        return saved
    finally:
        try:
            imap.logout()
        except Exception:
            pass


if __name__ == "__main__":
    # Standalone smoke test: pull PDFs from the inbox into data/invoices.
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from src.config import load_settings

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s | %(message)s")
    settings = load_settings()
    if not settings.gmail_address or not settings.gmail_app_password:
        raise SystemExit("Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env first.")

    files = fetch_pdf_attachments(
        settings.gmail_address,
        settings.gmail_app_password,
        save_dir="data/invoices",
    )
    print(f"\nDownloaded {len(files)} PDF attachment(s):")
    for f in files:
        print("  -", f)
