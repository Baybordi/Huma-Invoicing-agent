"""
llm_judge.py — LLM-as-judge for semantic field comparison.

Exact string matching is too brittle for free-text fields: "Atlassian Pty Ltd"
vs "Atlassian Pty. Limited" are the same vendor to a human but differ byte-for-
byte. This module uses a second Claude call as an impartial judge to decide
whether an extracted value is *semantically equivalent* to the golden value.

Design (the senior move): a HYBRID strategy.
  - Structured fields (dates, amounts, currency, document_type) -> exact /
    normalised match. Deterministic, free, and correct — you do NOT want an LLM
    deciding whether 6875.00 == 6875.0; that's a job for code.
  - Free-text fields (vendor, line-item descriptions) -> LLM judge, because
    surface form varies but meaning is what matters.

The judge is constrained to return strict JSON (verdict + reason) so its output
is itself machine-checkable. We default to "not equal" if the judge misbehaves,
so the eval fails closed rather than flattering the model.
"""

from __future__ import annotations

import json
import logging

from anthropic import Anthropic

from src.config import Settings

logger = logging.getLogger(__name__)

# Fields where meaning matters more than surface form -> use the judge.
TEXT_FIELDS = {"vendor"}

_JUDGE_PROMPT = """You are an impartial evaluation judge for an invoice-extraction system.

Decide whether the MODEL value refers to the SAME real-world {field} as the GOLD value.
Treat these as equivalent: abbreviations (Ltd/Limited), punctuation, casing, legal-
suffix variation, and trading-name vs legal-name where clearly the same entity.
Treat as NOT equivalent: genuinely different entities or materially different values.

GOLD value:  {gold}
MODEL value: {model}

Return ONLY this JSON, no prose:
{{"equivalent": true or false, "reason": "<one short sentence>"}}"""


def judge_text_field(
    field: str,
    gold_value: str | None,
    model_value: str | None,
    client: Anthropic,
    settings: Settings,
) -> tuple[bool, str]:
    """Use Claude to judge semantic equivalence of one text field.

    Returns (equivalent, reason). Fails closed (False) on any judge error.
    """
    # Cheap deterministic shortcuts before spending a call.
    if gold_value is None and model_value is None:
        return True, "both empty"
    if gold_value is None or model_value is None:
        return False, "one value missing"
    if gold_value.strip().lower() == model_value.strip().lower():
        return True, "exact match"

    prompt = _JUDGE_PROMPT.format(
        field=field, gold=gold_value, model=model_value
    )
    try:
        response = client.messages.create(
            model=settings.extraction_model,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        verdict = json.loads(raw)
        return bool(verdict["equivalent"]), str(verdict.get("reason", ""))
    except Exception as exc:  # judge failed -> fail closed
        logger.warning("Judge error on %s: %s", field, exc)
        return False, f"judge error: {exc}"
