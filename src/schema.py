"""
schema.py — Structured-output guardrail for invoice extraction.

The LLM returns free-form text. Before we trust any of it, we force it through
these Pydantic models. If Claude returns malformed JSON, a wrong type, or a
missing required field, validation raises here — loudly and early — instead of
silently corrupting a bill in Odoo.

This is the boundary between "probabilistic LLM output" and "deterministic
business logic". Everything downstream can assume the data is well-formed.
"""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class DocumentType(str, Enum):
    """An invoice is payable; a receipt is already paid (informational only)."""

    INVOICE = "invoice"
    RECEIPT = "receipt"


class LineItem(BaseModel):
    """A single billed line. Quantity/unit_price are optional because some
    invoices only show a lump-sum amount with no breakdown."""

    description: str
    quantity: Optional[float] = None
    unit_price: Optional[float] = None


class ExtractedInvoice(BaseModel):
    """
    The canonical shape of an extracted invoice.

    Note what is and isn't Optional:
      - invoice_number and due_date are Optional because real invoices sometimes
        omit them (GitHub sample has no number; AWS has no due date). We do NOT
        want extraction to crash on those — we want them to surface as nulls so
        the *validation* layer can make the human-in-the-loop decision.
      - vendor, invoice_date, total_amount, currency are required: if Claude
        cannot find these, that is an extraction failure worth raising on.
    """

    vendor: str = Field(..., min_length=1)
    invoice_number: Optional[str] = None
    invoice_date: date
    due_date: Optional[date] = None
    total_amount: float = Field(..., ge=0)
    currency: str = Field(..., min_length=3, max_length=3)
    payment_terms: Optional[str] = None
    line_items: list[LineItem] = Field(default_factory=list)
    document_type: DocumentType = DocumentType.INVOICE

    # Carried through the pipeline for traceability; not extracted by the LLM.
    source_file: Optional[str] = None

    @field_validator("currency")
    @classmethod
    def normalise_currency(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("vendor")
    @classmethod
    def strip_vendor(cls, v: str) -> str:
        return v.strip()
