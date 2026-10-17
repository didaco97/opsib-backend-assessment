"""Deterministic receipt-shortfall detection.

Rule receipt-shortfall/v1:
  For an eligible (non-cancelled) PO line whose expected-receipt deadline has
  passed at the frozen evaluation time, compare the ordered quantity in base
  UOM against the sum of POSTED receipt quantities in base UOM (each source
  receipt record counted once). Positive variance above tolerance raises one
  exception per PO line; re-runs are idempotent via the unique case_key.

Lines that cannot be evaluated (unmapped product/location/quantity) are
data-quality issues, not business exceptions, and are reported as skipped.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import MAPPING_VERSION, RULE_VERSION, TENANT_ID
from app.db.models import ExceptionEvent, OpsException, POLine, ReceiptLine

NEUTRAL_EXPLANATION = (
    "Receipts posted against this PO line total {received} {uom} of {expected} {uom} ordered, "
    "leaving a variance of {variance} {uom} after the expected-receipt deadline passed. "
    "The fixture pack contains no supplier dispatch/ASN data, so this finding does not establish "
    "whether the supplier dispatched fewer units, a carrier lost stock, or the shortage happened "
    "in receiving. It is a receipt shortfall against the PO, not proven supplier fault."
)
log = logging.getLogger("opsib.detection")

MISSING_EVIDENCE = ["supplier dispatch / ASN record", "carrier proof of delivery",
                    "receiving dock count audit"]


def _fmt(qty) -> str:
    return format(Decimal(qty).normalize(), "f")


@dataclass
class DetectionRunResult:
    created: list[str] = field(default_factory=list)    # exception ids
    existing: list[str] = field(default_factory=list)   # idempotent re-run hits
    skipped: list[dict] = field(default_factory=list)   # {po_line, reason}

    def as_dict(self):
        return {"created_exception_ids": self.created,
                "existing_exception_ids": self.existing,
                "skipped": self.skipped}


def case_key(tenant: str, rule_version: str, po_number: str, line_number: int) -> str:
    return hashlib.sha256(f"{tenant}|{rule_version}|{po_number}|{line_number}".encode()).hexdigest()


def run_detection(session: Session, now: datetime, tolerance: Decimal = Decimal("0")) -> DetectionRunResult:
    result = DetectionRunResult()
    po_lines = session.scalars(select(POLine).order_by(POLine.po_number, POLine.line_number)).all()
    for po in po_lines:
        label = f"{po.po_number}/{po.line_number}"
        if po.status != "OPEN":
            result.skipped.append({"po_line": label, "reason": f"PO status {po.status}: not eligible"})
            continue
        if po.canonical_product_id is None or po.canonical_location_id is None or po.ordered_qty_base is None:
            result.skipped.append({"po_line": label,
                                   "reason": "unresolved product/location mapping: data-quality issue, not an exception"})
            continue
        if po.expected_receipt_by > now:
            result.skipped.append({"po_line": label,
                                   "reason": "expected-receipt deadline has not passed at the frozen evaluation time"})
            continue

        receipts = session.scalars(
            select(ReceiptLine)
            .where(ReceiptLine.po_number == po.po_number,
                   ReceiptLine.po_line_number == po.line_number,
                   ReceiptLine.posting_status == "POSTED")
            .order_by(ReceiptLine.source_record_id)).all()
        # source_record_id is the primary key, so each source record is counted once.
        usable = [r for r in receipts if r.accepted_qty_base is not None]
        accepted = sum((Decimal(r.accepted_qty_base) for r in usable), Decimal("0"))
        expected = Decimal(po.ordered_qty_base)
        variance = expected - accepted
        if variance <= tolerance:
            continue

        key = case_key(TENANT_ID, RULE_VERSION, po.po_number, po.line_number)
        existing = session.scalar(select(OpsException).where(OpsException.case_key == key))
        if existing:
            result.existing.append(existing.id)
            continue
        exc = OpsException(
            tenant=TENANT_ID, case_key=key, type="receipt_shortfall", rule_version=RULE_VERSION,
            status="OPEN", po_number=po.po_number, po_line_number=po.line_number,
            canonical_product_id=po.canonical_product_id,
            canonical_location_id=po.canonical_location_id,
            expected_qty=expected, received_qty=accepted, base_uom=po.base_uom or "EA",
            variance=variance,
            explanation=NEUTRAL_EXPLANATION.format(expected=_fmt(expected), received=_fmt(accepted),
                                                   variance=_fmt(variance), uom=po.base_uom or "EA"),
            missing_evidence=MISSING_EVIDENCE,
            evidence_refs={
                "po_source_record_id": po.source_record_id,
                "receipt_source_record_ids": [r.source_record_id for r in usable],
                "import_id": po.import_id,
                "rule_version": RULE_VERSION,
                "mapping_version": MAPPING_VERSION,
            },
            detected_at=now)
        session.add(exc)
        session.flush()
        session.add(ExceptionEvent(exception_id=exc.id, event_type="DETECTED",
                                   detail={"variance": str(variance)}, at=now))
        result.created.append(exc.id)
    session.commit()
    log.info("detection_run created=%d existing=%d skipped=%d",
             len(result.created), len(result.existing), len(result.skipped))
    return result
