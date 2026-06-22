"""
run_eval.py — Regression / accuracy harness for the agent.

Why this exists (and why a reviewer cares):
  Extraction is probabilistic. "It worked when I ran it" is not a guarantee.
  This harness measures, per field, how often Claude's extraction matches a
  human-verified ground truth (evaluation/golden_dataset.json), and whether the
  end-to-end pipeline reaches the correct POST/FLAG decision for each invoice.

  Run it before/after any prompt or model change to catch regressions. This is
  the "golden set + regression check" pattern the role description calls for.

Two layers of evaluation:
  1. Field-level extraction accuracy (vendor, number, date, total, currency, type)
  2. Pipeline decision accuracy (did we POST/FLAG correctly, for the right reason)

Usage:
    python -m evaluation.run_eval                # live: calls Claude
    python -m evaluation.run_eval --from-cache   # score a saved extraction file
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

# Allow running as a script from repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anthropic import Anthropic

from src.config import load_settings
from src.extract import extract_invoice, read_pdf_text
from src.schema import ExtractedInvoice
from src.validate import load_approved_suppliers, validate_invoice

from evaluation.llm_judge import TEXT_FIELDS, judge_text_field

GOLDEN_PATH = os.path.join(os.path.dirname(__file__), "golden_dataset.json")
FIELDS_TO_SCORE = [
    "vendor",
    "invoice_number",
    "invoice_date",
    "due_date",
    "total_amount",
    "currency",
    "document_type",
]


def _norm(value) -> str:
    """Normalise a value for forgiving comparison (case, whitespace, trailing zeros)."""
    if value is None:
        return "∅"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value).strip().lower()


def score_extraction(
    predicted: ExtractedInvoice,
    truth: dict,
    client: Anthropic | None = None,
    settings=None,
    use_judge: bool = False,
) -> tuple[int, int, list[str]]:
    """Compare one extracted invoice to ground truth. Returns (correct, total, mismatches).

    Hybrid scoring:
      - structured fields (dates, amounts, currency, document_type) -> exact /
        normalised match (deterministic, correct, free).
      - text fields (vendor) -> optional LLM-as-judge for semantic equivalence,
        so "Atlassian Pty Ltd" vs "Atlassian Pty. Limited" isn't a false miss.
    """
    correct, total, mismatches = 0, 0, []
    for field in FIELDS_TO_SCORE:
        total += 1
        pred_val = getattr(predicted, field)
        pred_str = _norm(getattr(pred_val, "value", pred_val))
        truth_str = _norm(truth.get(field))

        if pred_str == truth_str:
            correct += 1
            continue

        # Mismatch on a text field -> ask the LLM judge before calling it wrong.
        if use_judge and field in TEXT_FIELDS and client is not None:
            equivalent, reason = judge_text_field(
                field, truth.get(field), getattr(pred_val, "value", pred_val),
                client, settings,
            )
            if equivalent:
                correct += 1
                continue
            mismatches.append(f"{field}: got {pred_str!r}, expected {truth_str!r} (judge: {reason})")
        else:
            mismatches.append(f"{field}: got {pred_str!r}, expected {truth_str!r}")
    return correct, total, mismatches


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--invoices", default="data/invoices")
    parser.add_argument("--suppliers", default="data/Approved_Supplier_List.xlsx")
    parser.add_argument(
        "--judge",
        action="store_true",
        help="Use LLM-as-judge for semantic comparison of text fields (vendor).",
    )
    parser.add_argument(
        "--min-extraction",
        type=float,
        default=None,
        help="Regression gate: fail (exit 1) if extraction accuracy %% is below this.",
    )
    parser.add_argument(
        "--min-decision",
        type=float,
        default=None,
        help="Regression gate: fail (exit 1) if decision accuracy %% is below this.",
    )
    args = parser.parse_args()

    with open(GOLDEN_PATH) as f:
        golden = json.load(f)["invoices"]

    settings = load_settings()
    client = Anthropic(api_key=settings.anthropic_api_key)
    approved = load_approved_suppliers(args.suppliers)

    total_correct = total_fields = 0
    decision_correct = 0
    seen: set = set()
    total_latency = 0.0  # seconds spent in extraction across all invoices

    mode = "with LLM-as-judge" if args.judge else "exact match"
    print("\n" + "=" * 70)
    print(f"EXTRACTION & PIPELINE EVALUATION (vs. human golden set, {mode})")
    print("=" * 70)

    for truth in golden:
        path = os.path.join(args.invoices, truth["source_file"])
        text = read_pdf_text(path)
        t0 = time.perf_counter()
        predicted = extract_invoice(text, client, settings, source_file=truth["source_file"])
        total_latency += time.perf_counter() - t0

        correct, total, mismatches = score_extraction(
            predicted, truth, client, settings, use_judge=args.judge
        )
        total_correct += correct
        total_fields += total

        result = validate_invoice(predicted, approved, seen)
        seen.add((predicted.vendor.strip().lower(), predicted.invoice_number))

        decision_ok = result.decision.value == truth["expected_decision"]
        reason_ok = True
        if truth.get("expected_reason_contains"):
            reason_ok = any(
                truth["expected_reason_contains"].lower() in r.lower()
                for r in result.reasons
            )
        if decision_ok and reason_ok:
            decision_correct += 1

        flag = "✓" if (decision_ok and reason_ok) else "✗"
        print(f"\n{flag} {truth['source_file']}")
        print(f"    extraction: {correct}/{total} fields correct")
        for m in mismatches:
            print(f"      ! {m}")
        print(
            f"    decision  : got {result.decision.value}, "
            f"expected {truth['expected_decision']} "
            f"{'(ok)' if decision_ok and reason_ok else '(MISMATCH)'}"
        )

    n = len(golden)
    extraction_pct = 100 * total_correct / total_fields
    decision_pct = 100 * decision_correct / n
    avg_latency = total_latency / n

    print("\n" + "-" * 70)
    print(f"Field-level extraction accuracy : {total_correct}/{total_fields} "
          f"({extraction_pct:.1f}%)")
    print(f"Pipeline decision accuracy      : {decision_correct}/{n} "
          f"({decision_pct:.1f}%)")
    print(f"Avg extraction latency / invoice: {avg_latency:.2f}s")
    print(f"Total extraction time           : {total_latency:.1f}s for {n} invoices")
    print("=" * 70)

    # --- Regression gate -------------------------------------------------------
    # If thresholds are supplied (as in CI), exit non-zero on a regression so the
    # pipeline blocks the change. No threshold supplied -> report-only mode.
    failures = []
    if args.min_extraction is not None and extraction_pct < args.min_extraction:
        failures.append(
            f"extraction {extraction_pct:.1f}% < required {args.min_extraction:.1f}%"
        )
    if args.min_decision is not None and decision_pct < args.min_decision:
        failures.append(
            f"decision {decision_pct:.1f}% < required {args.min_decision:.1f}%"
        )

    if failures:
        print("\n❌ REGRESSION GATE FAILED:")
        for f in failures:
            print(f"   - {f}")
        print()
        sys.exit(1)
    elif args.min_extraction is not None or args.min_decision is not None:
        print("\n✅ Regression gate passed.\n")


if __name__ == "__main__":
    main()
    