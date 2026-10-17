"""Optional AI investigation-summary boundary.

The deterministic detector owns quantities and status. An AI provider may only
explain evidence, name missing context and suggest investigation steps:
- input is a typed evidence package (case facts + evidence record IDs only)
- output is schema-validated, and every cited record ID must exist in the
  supplied evidence package
- the service keeps working with no provider configured (503 on the endpoint)
  and rejects invalid provider output without touching business state
"""
from __future__ import annotations

import json
import os
from typing import Protocol

from pydantic import BaseModel, ValidationError


class InvestigationSummary(BaseModel):
    summary: str
    missing_evidence: list[str]
    suggested_steps: list[str]
    cited_record_ids: list[str]


class SummaryProvider(Protocol):
    def summarize(self, case_context: dict) -> dict: ...


class ProviderUnavailable(Exception):
    pass


class InvalidProviderOutput(Exception):
    pass


def get_provider() -> SummaryProvider | None:
    """No live key is needed for this exercise. If OPSIB_ANTHROPIC_KEY is set,
    a real Claude call could be wired here behind the same protocol."""
    if os.environ.get("OPSIB_ANTHROPIC_KEY"):
        raise NotImplementedError("wire the Anthropic client here behind SummaryProvider")
    return None


def build_case_context(exc) -> dict:
    return {
        "exception_id": exc.id,
        "type": exc.type,
        "po_number": exc.po_number,
        "po_line_number": exc.po_line_number,
        "canonical_product_id": exc.canonical_product_id,
        "canonical_location_id": exc.canonical_location_id,
        "expected_qty": str(exc.expected_qty),
        "received_qty": str(exc.received_qty),
        "variance": str(exc.variance),
        "base_uom": exc.base_uom,
        "explanation": exc.explanation,
        "evidence_refs": exc.evidence_refs,
    }


def validate_summary(raw: dict, allowed_record_ids: set[str]) -> InvestigationSummary:
    try:
        summary = InvestigationSummary(**raw)
    except ValidationError as exc:
        raise InvalidProviderOutput(f"provider output failed schema validation: {exc}") from exc
    bad = [rid for rid in summary.cited_record_ids if rid not in allowed_record_ids]
    if bad:
        raise InvalidProviderOutput(f"provider cited record IDs not in evidence: {bad}")
    return summary
