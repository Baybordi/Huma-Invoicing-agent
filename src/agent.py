"""
agent.py — The orchestrator. This is the agent's control loop.

It wires the four stages into one pipeline and is the single entry point you run
for the demo:

    ingest PDFs  ->  extract (LLM + guardrail)  ->  validate (business rules)
                 ->  route on a THREE-tier decision:

        POST   : clean + approved            -> create AND confirm the bill in Odoo
        REVIEW : approved vendor, needs a fix -> create a DRAFT bill in Odoo for a human
        REJECT : receipt / duplicate / unknown vendor -> do NOT touch Odoo; report only

This mirrors how a real accounts-payable team works: most invoices post straight
through, a few are held as drafts for a human to confirm, and some should never
become a bill at all.

Design notes for the interview:
  - "Fail closed": anything uncertain is held for a human, never silently posted.
  - Odoo connection is lazy: extract/validate run without Odoo (use --dry-run).
  - The inbox is mocked as a folder of PDFs (the brief allows this). Only
    `iter_invoice_files` would change to plug in a real IMAP/Graph poller.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
from dataclasses import asdict, dataclass

from anthropic import Anthropic

from .config import load_settings
from .extract import extract_invoice, read_pdf_text
from .odoo_client import OdooClient
from .validate import (
    Decision,
    ValidationResult,
    load_approved_suppliers,
    validate_invoice,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s"
)
logger = logging.getLogger("agent")


@dataclass
class InvoiceOutcome:
    source_file: str
    vendor: str | None = None
    invoice_number: str | None = None
    invoice_date: str | None = None
    due_date: str | None = None
    total_amount: float | None = None
    currency: str | None = None
    payment_terms: str | None = None
    document_type: str | None = None
    line_item_count: int | None = None
    decision: str | None = None
    reasons: list[str] | None = None
    odoo_id: int | None = None        # posted bill id OR review-draft id
    odoo_status: str | None = None    # "posted", "draft", or "not created"
    error: str | None = None


def iter_invoice_files(folder: str) -> list[str]:
    """The mocked 'inbox'. A real implementation would poll IMAP/Graph and filter
    for application/pdf attachments — same return shape, swappable in isolation."""
    return sorted(
        os.path.join(folder, f)
        for f in os.listdir(folder)
        if f.lower().endswith(".pdf")
    )


def run(
    invoices_dir: str,
    supplier_list_path: str,
    dry_run: bool = False,
) -> list[InvoiceOutcome]:
    settings = load_settings()
    client = Anthropic(api_key=settings.anthropic_api_key)
    approved = load_approved_suppliers(supplier_list_path)
    logger.info("Loaded %d approved suppliers", len(approved))

    odoo: OdooClient | None = None  # connect lazily, only when we touch Odoo

    seen: set[tuple[str, str | None]] = set()
    outcomes: list[InvoiceOutcome] = []

    for path in iter_invoice_files(invoices_dir):
        filename = os.path.basename(path)
        outcome = InvoiceOutcome(source_file=filename)
        logger.info("Processing %s", filename)

        # --- Stage 3: extract -------------------------------------------------
        try:
            text = read_pdf_text(path)
            invoice = extract_invoice(text, client, settings, source_file=filename)
        except Exception as exc:  # extraction failed even after retry
            outcome.decision = Decision.REVIEW.value
            outcome.reasons = [f"Extraction failed: {exc}"]
            outcome.odoo_status = "not created"
            outcomes.append(outcome)
            logger.error("Extraction failed for %s: %s", filename, exc)
            continue

        outcome.vendor = invoice.vendor
        outcome.invoice_number = invoice.invoice_number
        outcome.invoice_date = invoice.invoice_date.isoformat() if invoice.invoice_date else None
        outcome.due_date = invoice.due_date.isoformat() if invoice.due_date else None
        outcome.total_amount = invoice.total_amount
        outcome.currency = invoice.currency
        outcome.payment_terms = invoice.payment_terms
        outcome.document_type = invoice.document_type.value
        outcome.line_item_count = len(invoice.line_items)

        # --- Stage 4a: validate ----------------------------------------------
        result: ValidationResult = validate_invoice(invoice, approved, seen)
        outcome.decision = result.decision.value
        outcome.reasons = result.reasons
        seen.add((invoice.vendor.strip().lower(), invoice.invoice_number))

        # --- Stage 4b: route on the three-tier decision -----------------------
        if dry_run:
            outcome.odoo_status = "dry-run (not sent)"
            outcomes.append(outcome)
            continue

        if result.decision == Decision.POST:
            if odoo is None:
                odoo = OdooClient(settings)
            post = odoo.post_bill(invoice)
            if post.ok:
                outcome.odoo_id = post.bill_id
                outcome.odoo_status = "posted"
            else:
                # Odoo refused (e.g. already exists) — downgrade to REVIEW report.
                outcome.decision = Decision.REVIEW.value
                outcome.reasons = (outcome.reasons or []) + [post.error]
                outcome.odoo_status = "not created"

        elif result.decision == Decision.REVIEW:
            if odoo is None:
                odoo = OdooClient(settings)
            review = odoo.create_draft_for_review(invoice, outcome.reasons or [])
            if review.ok:
                outcome.odoo_id = review.bill_id
                outcome.odoo_status = "draft"
            else:
                outcome.reasons = (outcome.reasons or []) + [review.error]
                outcome.odoo_status = "not created"

        else:  # REJECT — never create anything in Odoo
            outcome.odoo_status = "not created"

        outcomes.append(outcome)

    _print_summary(outcomes, dry_run)
    return outcomes


def _print_summary(outcomes: list[InvoiceOutcome], dry_run: bool) -> None:
    posted = [o for o in outcomes if o.odoo_status == "posted"]
    drafts = [o for o in outcomes if o.odoo_status == "draft"]
    rejected = [o for o in outcomes if o.decision == Decision.REJECT.value]

    print("\n" + "=" * 72)
    print("INVOICING AGENT — RUN SUMMARY")
    print("=" * 72)
    for o in outcomes:
        if o.odoo_status == "posted":
            status = f"POSTED   (Odoo bill #{o.odoo_id}, confirmed)"
        elif o.odoo_status == "draft":
            status = f"DRAFT    (Odoo #{o.odoo_id}, awaiting human review)"
        elif dry_run:
            status = f"{o.decision} (dry-run, not sent)"
        else:
            status = f"{o.decision}   (not created in Odoo)"
        print(f"\n• {o.source_file}")
        print(f"    vendor : {o.vendor}")
        print(f"    number : {o.invoice_number}")
        print(f"    result : {status}")
        for reason in o.reasons or []:
            print(f"      - {reason}")

    print("\n" + "-" * 72)
    if dry_run:
        print(f"Total: {len(outcomes)}  |  (dry-run: nothing sent to Odoo)")
    else:
        print(
            f"Total: {len(outcomes)}  |  Posted: {len(posted)}  |  "
            f"Drafts for review: {len(drafts)}  |  Rejected: {len(rejected)}"
        )
    print("=" * 72 + "\n")


def write_csv_report(outcomes: list[InvoiceOutcome], path: str) -> None:
    """Write a finance-friendly CSV: one row per invoice, all extracted fields
    plus the decision, the Odoo status, and the reason. Opens straight in Excel."""
    columns = [
        "source_file",
        "vendor",
        "invoice_number",
        "invoice_date",
        "due_date",
        "total_amount",
        "currency",
        "payment_terms",
        "document_type",
        "line_item_count",
        "decision",
        "odoo_status",
        "odoo_id",
        "reasons",
    ]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        for o in outcomes:
            writer.writerow(
                [
                    o.source_file,
                    o.vendor or "",
                    o.invoice_number or "",
                    o.invoice_date or "",
                    o.due_date or "",
                    o.total_amount if o.total_amount is not None else "",
                    o.currency or "",
                    o.payment_terms or "",
                    o.document_type or "",
                    o.line_item_count if o.line_item_count is not None else "",
                    o.decision or "",
                    o.odoo_status or "",
                    o.odoo_id if o.odoo_id is not None else "",
                    " | ".join(o.reasons or []),
                ]
            )
    logger.info("Wrote CSV report to %s", path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Huma automated invoicing agent")
    parser.add_argument("--invoices", default="data/invoices")
    parser.add_argument("--suppliers", default="data/Approved_Supplier_List.xlsx")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run extract + validate only; do not connect to or post to Odoo.",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Optional path to write the run outcomes as JSON.",
    )
    parser.add_argument(
        "--report",
        default="run_report.csv",
        help="Path to write the human-readable CSV report (default: run_report.csv).",
    )
    args = parser.parse_args()

    outcomes = run(args.invoices, args.suppliers, dry_run=args.dry_run)

    if args.out:
        with open(args.out, "w") as f:
            json.dump([asdict(o) for o in outcomes], f, indent=2)
        logger.info("Wrote outcomes to %s", args.out)

    if args.report:
        write_csv_report(outcomes, args.report)


if __name__ == "__main__":
    main()
    
    