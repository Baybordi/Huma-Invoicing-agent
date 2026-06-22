"""
odoo_client.py — Stage 4b: Odoo integration (the system of record).

All XML-RPC interaction with Odoo is isolated here so the rest of the codebase
never speaks the ERP's protocol. The orchestrator calls high-level methods
(post_bill, create_draft_for_review); it does not know about account.move,
partner_ids, or the (0, 0, {...}) command tuples.

Three outcomes map to three behaviours:
  - POST   -> create the bill AND confirm it (action_post) -> status "Posted"
  - REVIEW -> create the bill as a DRAFT only, with the reasons in chatter,
              so a human reviews and confirms it in Odoo
  - REJECT -> handled by the orchestrator: nothing is created in Odoo at all
"""

from __future__ import annotations

import logging
import xmlrpc.client
from dataclasses import dataclass

from .config import Settings
from .schema import ExtractedInvoice

logger = logging.getLogger(__name__)


@dataclass
class PostResult:
    bill_id: int | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.bill_id is not None


class OdooClient:
    def __init__(self, settings: Settings):
        self._db = settings.odoo_db
        self._api_key = settings.odoo_api_key
        common = xmlrpc.client.ServerProxy(f"{settings.odoo_url}/xmlrpc/2/common")
        self._uid = common.authenticate(
            settings.odoo_db, settings.odoo_username, settings.odoo_api_key, {}
        )
        if not self._uid:
            raise RuntimeError("Odoo authentication failed — check credentials in .env")
        self._models = xmlrpc.client.ServerProxy(f"{settings.odoo_url}/xmlrpc/2/object")
        logger.info("Connected to Odoo as uid=%s", self._uid)

    def _execute(self, model: str, method: str, *args, **kwargs):
        return self._models.execute_kw(
            self._db, self._uid, self._api_key, model, method, list(args), kwargs
        )

    def find_vendor_id(self, vendor_name: str) -> int | None:
        """Resolve a vendor name to a res.partner id, or None if not present.

        We search only contacts flagged as suppliers (supplier_rank > 0) to avoid
        accidentally matching a customer with a similar name.
        """
        matches = self._execute(
            "res.partner",
            "search",
            [["name", "ilike", vendor_name], ["supplier_rank", ">", 0]],
        )
        return matches[0] if matches else None

    def bill_exists(self, vendor_id: int, invoice_number: str) -> bool:
        """ERP-level duplicate guard: has this ref already been posted for this
        vendor? Protects against re-runs, not just within-run duplicates."""
        existing = self._execute(
            "account.move",
            "search",
            [
                ["move_type", "=", "in_invoice"],
                ["partner_id", "=", vendor_id],
                ["ref", "=", invoice_number],
            ],
        )
        return bool(existing)

    def _build_line_ids(self, invoice: ExtractedInvoice):
        return [
            (
                0,
                0,
                {
                    "name": item.description,
                    "quantity": item.quantity or 1,
                    "price_unit": item.unit_price or 0.0,
                },
            )
            for item in invoice.line_items
        ]

    def _create_bill(self, vendor_id: int, invoice: ExtractedInvoice) -> int:
        """Create the account.move (vendor bill). It starts life as a draft;
        callers decide whether to confirm it."""
        return self._execute(
            "account.move",
            "create",
            {
                "move_type": "in_invoice",
                "partner_id": vendor_id,
                "invoice_date": invoice.invoice_date.isoformat()
                if invoice.invoice_date
                else False,
                "ref": invoice.invoice_number or False,
                "invoice_line_ids": self._build_line_ids(invoice),
            },
        )

    def _audit_note(self, bill_id: int, body: str) -> None:
        """Audit-log control: leave a transparent trail in the record's chatter.

        This Odoo/XML-RPC path escapes HTML, so we send plain text with real line
        breaks (\\n). Odoo's chatter converts newlines to line breaks on display,
        so the note reads cleanly without any visible markup.
        """
        self._execute(
            "account.move",
            "message_post",
            [bill_id],
            body=body,
            message_type="comment",
            subtype_xmlid="mail.mt_note",
        )

    def post_bill(self, invoice: ExtractedInvoice) -> PostResult:
        """POST outcome: create the bill AND confirm it (action_post), so it
        lands in Odoo as a fully posted, payable bill — not a draft."""
        vendor_id = self.find_vendor_id(invoice.vendor)
        if vendor_id is None:
            return PostResult(
                None,
                f"Vendor '{invoice.vendor}' has no supplier record in Odoo — "
                f"create it during SE onboarding before posting.",
            )

        if invoice.invoice_number and self.bill_exists(vendor_id, invoice.invoice_number):
            return PostResult(
                None,
                f"A bill with ref '{invoice.invoice_number}' already exists for "
                f"this vendor in Odoo — skipped to avoid double entry.",
            )

        bill_id = self._create_bill(vendor_id, invoice)

        # Confirm the bill: draft -> posted.
        self._execute("account.move", "action_post", [bill_id])

        self._audit_note(
            bill_id,
            f"Posted automatically by the Invoicing Agent from "
            f"{invoice.source_file or 'an emailed PDF'}. All controls passed.",
        )

        logger.info("Posted (confirmed) bill %s for %s", bill_id, invoice.vendor)
        return PostResult(bill_id)

    def create_draft_for_review(
        self, invoice: ExtractedInvoice, reasons: list[str]
    ) -> PostResult:
        """REVIEW outcome: the vendor is known and approved, but something needs
        a human (e.g. a missing field). Create the bill as a DRAFT (do not
        confirm) and write the reasons into its chatter, so a reviewer opens it
        in Vendors > Bills, sees exactly why it was held, and confirms or edits —
        without re-keying anything."""
        vendor_id = self.find_vendor_id(invoice.vendor)
        if vendor_id is None:
            return PostResult(
                None,
                f"Vendor '{invoice.vendor}' not in Odoo — kept in flag report only.",
            )

        if invoice.invoice_number and self.bill_exists(vendor_id, invoice.invoice_number):
            return PostResult(
                None,
                f"A bill with ref '{invoice.invoice_number}' already exists — "
                f"not creating a duplicate draft.",
            )

        draft_id = self._create_bill(vendor_id, invoice)  # left as draft on purpose

        reason_lines = "\n".join(f"  - {r}" for r in reasons)
        self._audit_note(
            draft_id,
            f"Held for human review by the Invoicing Agent.\n"
            f"Source: {invoice.source_file or 'emailed PDF'}\n"
            f"Reason(s):\n{reason_lines}\n"
            f"Left in draft — please review before confirming.",
        )

        logger.info("Created review draft %s for %s", draft_id, invoice.vendor)
        return PostResult(draft_id)
    
    