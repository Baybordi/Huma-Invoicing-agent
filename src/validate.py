"""
validate.py — Stage 4a: business-rule validation (the compliance gate).

This is deterministic, auditable, non-AI logic. It takes a well-formed
ExtractedInvoice and decides POST vs FLAG against the four controls in the brief:

    1. Completeness   — every mandatory field present
    2. Approved supplier — vendor is on the Approved Supplier List with Status=Approved
    3. Duplicate      — (vendor, invoice_number) not seen before
    4. Document type  — a receipt is already paid, so it is never auto-posted

Design choices to defend:
  - Separation: extraction (probabilistic) and validation (deterministic) are
    different layers. A reviewer can read this file and verify the controls
    without understanding the LLM at all.
  - The result is a structured object (decision + list of reasons), not a print.
    The orchestrator decides what to *do* with a FLAG; this layer only judges.
  - Supplier matching is conservative: exact match, then a guarded fuzzy match.
    We never *expand* approval — an unknown vendor must fail closed (FLAG).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import openpyxl

from .schema import DocumentType, ExtractedInvoice

# The brief's mandatory fields. invoice_number and due_date are included because
# the brief lists them as required data points; their absence is exactly the kind
# of thing a human should review, so we treat missing values as a completeness fail.
REQUIRED_FIELDS = [
    "vendor",
    "invoice_number",
    "invoice_date",
    "total_amount",
    "currency",
    "payment_terms",
    "due_date",
    "line_items",
]


class Decision(str, Enum):
    POST = "POST"        # clean + approved -> create AND confirm the bill
    REVIEW = "REVIEW"    # real vendor, but needs a human -> create a DRAFT bill
    REJECT = "REJECT"    # receipt / duplicate / unknown vendor -> do NOT create


@dataclass
class ValidationResult:
    decision: Decision = Decision.POST
    reasons: list[str] = field(default_factory=list)
    supplier_currency: str | None = None  # expected currency from the approved list

    def review(self, reason: str) -> None:
        """A human should look, but it's a legitimate bill from a known vendor."""
        # Never downgrade a REJECT to REVIEW.
        if self.decision != Decision.REJECT:
            self.decision = Decision.REVIEW
        self.reasons.append(reason)

    def reject(self, reason: str) -> None:
        """Not a payable bill at all (receipt / duplicate / unknown vendor)."""
        self.decision = Decision.REJECT
        self.reasons.append(reason)


@dataclass(frozen=True)
class ApprovedSupplier:
    name: str
    status: str
    currency: str
    payment_terms: str


def load_approved_suppliers(filepath: str) -> dict[str, ApprovedSupplier]:
    """Read the Approved Supplier List into a name-keyed lookup.

    We key on a normalised (lower, stripped) legal name and only include rows
    whose Status is 'Approved'. A vendor present but, say, 'Suspended' must NOT
    be treated as approved.
    """
    wb = openpyxl.load_workbook(filepath, data_only=True)
    ws = wb["Approved Suppliers"]

    suppliers: dict[str, ApprovedSupplier] = {}
    for row in ws.iter_rows(min_row=5, values_only=True):  # data starts at row 5
        supplier_id, name = row[0], row[1]
        # Only real data rows have a SUP-#### id. This also skips the trailing
        # free-text "Note:" row at the bottom of the sheet, which would otherwise
        # be mis-read as a supplier.
        if not isinstance(supplier_id, str) or not supplier_id.startswith("SUP-"):
            continue
        if not name:
            continue
        status = (row[4] or "").strip()
        suppliers[name.strip().lower()] = ApprovedSupplier(
            name=name.strip(),
            status=status,
            currency=(row[6] or "").strip(),
            payment_terms=(row[5] or "").strip(),
        )
    return suppliers


def _match_supplier(
    vendor: str, approved: dict[str, ApprovedSupplier]
) -> ApprovedSupplier | None:
    """Exact match first, then a guarded containment match for naming variants
    (e.g. 'Functional Software, Inc. (Sentry)' vs 'Functional Software, Inc.').

    Guard: we require the shorter name to be a reasonably long token so we don't
    match on noise. This errs toward FLAG, which is the safe direction.
    """
    key = vendor.strip().lower()
    if key in approved:
        return approved[key]

    for name, info in approved.items():
        shorter, longer = sorted([name, key], key=len)
        if len(shorter) >= 6 and shorter in longer:
            return info
    return None


def check_completeness(invoice: ExtractedInvoice) -> list[str]:
    """Return the list of missing mandatory fields (empty == complete)."""
    missing = []
    for f in REQUIRED_FIELDS:
        value = getattr(invoice, f, None)
        if value is None or (isinstance(value, list) and len(value) == 0):
            missing.append(f)
    return missing


def validate_invoice(
    invoice: ExtractedInvoice,
    approved: dict[str, ApprovedSupplier],
    seen: set[tuple[str, str | None]],
) -> ValidationResult:
    result = ValidationResult()

    # 1. Completeness — a real bill from a known vendor that's just missing a
    #    field is REVIEW (create a draft, let a human fill the gap).
    missing = check_completeness(invoice)
    if missing:
        result.review(f"Missing mandatory field(s): {', '.join(missing)}")

    # 2. Approved supplier — an unknown vendor is REJECT: we don't create a bill,
    #    we route to Supplier Evaluation. A wrong-status vendor is also REJECT.
    supplier = _match_supplier(invoice.vendor, approved)
    if supplier is None:
        result.reject(
            f"Vendor '{invoice.vendor}' is not on the Approved Supplier List — "
            f"route to Supplier Evaluation (SE) review."
        )
    elif supplier.status.lower() != "approved":
        result.reject(
            f"Vendor '{invoice.vendor}' is on the list but status is "
            f"'{supplier.status}', not Approved."
        )
    else:
        result.supplier_currency = supplier.currency
        # Currency mismatch is suspicious but the vendor is real -> REVIEW.
        if supplier.currency and invoice.currency != supplier.currency:
            result.review(
                f"Currency mismatch: invoice is {invoice.currency} but supplier "
                f"record expects {supplier.currency}."
            )

    # 3. Document type — a receipt is already paid: REJECT (never a payable bill).
    if invoice.document_type == DocumentType.RECEIPT:
        result.reject(
            "Document is a receipt (already paid) — informational only, no bill to post."
        )

    # 4. Duplicate — REJECT: the bill already exists, don't create another.
    key = (invoice.vendor.strip().lower(), invoice.invoice_number)
    if invoice.invoice_number is not None and key in seen:
        result.reject(
            f"Duplicate: invoice {invoice.invoice_number} from {invoice.vendor} "
            f"has already been processed this run."
        )

    return result

