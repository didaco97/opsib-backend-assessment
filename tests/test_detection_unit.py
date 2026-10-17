"""Unit tests for the shortfall rule: partial receipts, deadline boundary,
cancelled lines, tolerance, dedupe."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.db.models import ImportRun, POLine, ReceiptLine
from app.detection.shortfall import run_detection


def _ensure_import(session):
    if session.get(ImportRun, "t") is None:
        session.add(ImportRun(id="t", dataset="purchase_order_lines",
                              source_filename="unit.csv", status="COMPLETED",
                              mapping_version="mapping/v1", created_at=NOW))
        session.commit()

NOW = datetime(2026, 10, 16, 15, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
PAST = NOW - timedelta(hours=3)
FUTURE = NOW + timedelta(hours=3)


def add_po(session, po="PO-X", line=1, qty=100, status="OPEN", deadline=PAST):
    _ensure_import(session)
    p = POLine(source_record_id=f"SRC-{po}-{line}", po_number=po, line_number=line,
               supplier_code="SUP", source_location_code="MUM-014",
               canonical_location_id="STORE-014", source_item_code="ERP-SKU-001",
               canonical_product_id="SKU-001", ordered_qty=Decimal(qty), ordered_uom="EA",
               ordered_qty_base=Decimal(qty), base_uom="EA", status=status,
               expected_receipt_by=deadline, import_id="t", content_hash=f"h-{po}-{line}")
    session.add(p)
    session.commit()
    return p


def add_receipt(session, src, po="PO-X", line=1, qty=10, posting="POSTED"):
    r = ReceiptLine(source_record_id=src, receipt_number="GRN-X", receipt_line=1,
                    po_number=po, po_line_number=line, source_location_code="WH-MH-014",
                    canonical_location_id="STORE-014", product_ref="WMS-BRC-001",
                    canonical_product_id="SKU-001", accepted_qty=Decimal(qty), uom="EA",
                    accepted_qty_base=Decimal(qty), base_uom="EA", posting_status=posting,
                    received_at=PAST, import_id="t", content_hash=f"h-{src}")
    session.add(r)
    session.commit()


def variances(session):
    res = run_detection(session, NOW)
    return res


def test_partial_receipts_sum_and_variance(session):
    add_po(session, qty=100)
    add_receipt(session, "R1", qty=60)
    add_receipt(session, "R2", qty=32)
    res = variances(session)
    assert len(res.created) == 1
    from app.db.models import OpsException
    exc = session.get(OpsException, res.created[0])
    assert Decimal(exc.variance) == Decimal("8")
    assert "does not establish" in exc.explanation  # neutral on cause


def test_deadline_exactly_now_is_actionable(session):
    add_po(session, qty=50, deadline=NOW)
    add_receipt(session, "R1", qty=20)
    res = variances(session)
    assert len(res.created) == 1


def test_future_deadline_not_actionable(session):
    add_po(session, qty=50, deadline=FUTURE)
    res = variances(session)
    assert res.created == []
    assert res.skipped[0]["reason"].startswith("expected-receipt deadline")


def test_cancelled_po_never_raises(session):
    add_po(session, qty=20, status="CANCELLED")
    res = variances(session)
    assert res.created == []
    assert "CANCELLED" in res.skipped[0]["reason"]


def test_full_receipt_no_exception(session):
    add_po(session, qty=40)
    add_receipt(session, "R1", qty=40)
    assert variances(session).created == []


def test_unposted_receipt_not_counted(session):
    add_po(session, qty=30)
    add_receipt(session, "R1", qty=30, posting="DRAFT")
    res = variances(session)
    assert len(res.created) == 1  # nothing POSTED -> full variance


def test_unmapped_po_is_data_quality_not_exception(session):
    p = add_po(session, qty=10)
    p.canonical_product_id = None
    session.commit()
    res = variances(session)
    assert res.created == []
    assert "data-quality" in res.skipped[0]["reason"]


def test_configurable_tolerance(session):
    add_po(session, qty=100)
    add_receipt(session, "R1", qty=99)
    assert run_detection(session, NOW, tolerance=Decimal("2")).created == []
    drop = run_detection(session, NOW, tolerance=Decimal("0"))
    assert len(drop.created) == 1


def test_exception_neutral_on_supplier_fault(session):
    add_po(session, qty=100)
    add_receipt(session, "R1", qty=90)
    res = variances(session)
    from app.db.models import OpsException
    exc = session.get(OpsException, res.created[0])
    assert "supplier dispatch / ASN record" in exc.missing_evidence
    assert "not proven supplier fault" in exc.explanation
