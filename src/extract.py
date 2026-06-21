"""
extract.py — Stage 3 of the pipeline: intelligent parsing.

PDF text  ->  Claude (LLM)  ->  raw JSON  ->  Pydantic guardrail  ->  ExtractedInvoice

Two ideas worth calling out in the interview:

1. Why an LLM and not regex/templates?
   The sample pack alone has 8 different layouts, 3 currencies, 4 date formats,
   and an invoice with no number. Per-vendor templates are brittle and don't
   scale to "any vendor". An LLM generalises across layouts; we then constrain
   its output with a schema so we get the flexibility without the unreliability.

2. Why the retry loop?
   LLMs occasionally emit malformed JSON or a wrong type. We treat the schema
   as a contract: if parsing fails, we feed the error back to the model and ask
   it to fix its own output. This is the guardrail that makes a probabilistic
   component safe to put in front of an accounting system.
"""

from __future__ import annotations

import json
import logging

import pdfplumber
from anthropic import Anthropic
from pydantic import ValidationError

from .config import Settings
from .schema import ExtractedInvoice

logger = logging.getLogger(__name__)

_EXTRACTION_PROMPT = """You are extracting structured data from a vendor invoice for an accounting system.

Read the invoice text and return ONLY a JSON object (no prose, no markdown fences) with exactly these fields:

- vendor: the company issuing the invoice (the seller, not "Huma" the buyer)
- invoice_number: the invoice or receipt number. Use null if none is printed.
- invoice_date: issue date in YYYY-MM-DD
- due_date: payment due date in YYYY-MM-DD, or null if not present
- total_amount: the final total due, as a number with no currency symbol or thousands separators
- currency: the 3-letter ISO code (USD, GBP, EUR), inferred from the symbol if needed
- payment_terms: e.g. "Net 30", "Card on file", "Paid". null if not present.
- line_items: list of objects {{"description": str, "quantity": number, "unit_price": number}}
- document_type: "invoice" if payable, "receipt" if it states payment was already taken

Be faithful to the document. Do not invent a value to fill a field — use null where the brief allows it.

Invoice text:
---
{pdf_text}
---

Return ONLY the JSON object."""


def read_pdf_text(filepath: str) -> str:
    """Extract raw text from a (text-based) PDF.

    Production note: these samples are digitally-generated PDFs with a real text
    layer. A scanned/photographed invoice would need an OCR pass first
    (e.g. Textract / Tesseract) — flagged as a known limitation, not handled here.
    """
    with pdfplumber.open(filepath) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def _strip_fences(raw: str) -> str:
    return raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()


def extract_invoice(
    pdf_text: str,
    client: Anthropic,
    settings: Settings,
    source_file: str | None = None,
    max_attempts: int = 2,
) -> ExtractedInvoice:
    """Call Claude and coerce the result into a validated ExtractedInvoice.

    Retries once on malformed output, feeding the validation error back to the
    model so it can self-correct.
    """
    messages = [{"role": "user", "content": _EXTRACTION_PROMPT.format(pdf_text=pdf_text)}]

    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        response = client.messages.create(
            model=settings.extraction_model,
            max_tokens=1500,
            messages=messages,
        )
        raw = _strip_fences(response.content[0].text)

        try:
            data = json.loads(raw)
            invoice = ExtractedInvoice(**data, source_file=source_file)
            logger.info("Extracted %s on attempt %d", source_file, attempt)
            return invoice
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = exc
            logger.warning("Attempt %d failed for %s: %s", attempt, source_file, exc)
            # Feed the error back and ask for a correction.
            messages.append({"role": "assistant", "content": raw})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"That output failed validation with error:\n{exc}\n\n"
                        f"Return a corrected JSON object only."
                    ),
                }
            )

    raise ValueError(
        f"Extraction failed for {source_file} after {max_attempts} attempts: {last_error}"
    )
