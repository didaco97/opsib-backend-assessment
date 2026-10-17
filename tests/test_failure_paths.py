"""Failure paths: unknown mappings, conflicting duplicate IDs, no eligible
assignee, invalid imports."""
from sqlalchemy import select

from app.db.models import POLine, SourceConflict
from app.detection.shortfall import run_detection
from app.ingestion.service import import_dataset
from app.workflows.task_service import create_task_for_exception
from datetime import timedelta

from app.db.models import OpsException


def test_unknown_product_mapping_is_data_quality_not_exception(loaded, session, settings):
    csv_text = (
        "source_record_id,po_number,line_number,supplier_code,location_code,item_code,"
        "ordered_qty,ordered_uom,status,expected_receipt_by\n"
        "ERP-PO900-L1,PO-900,1,SUP-99,MUM-014,ERP-UNKNOWN-1,10,EA,OPEN,2026-10-15T12:00:00+05:30\n"
    )
    res = import_dataset(session, "purchase_order_lines", csv_text, "extra.csv", settings.eval_time)
    assert res.accepted == 1  # stored, but unresolved
    det = run_detection(session, settings.eval_time)
    po900 = [s for s in det.skipped if s["po_line"] == "PO-900/1"]
    assert po900 and "data-quality" in po900[0]["reason"]
    assert all(session.get(OpsException, i).po_number != "PO-900" for i in det.created)


def test_conflicting_duplicate_id_flagged_not_overwritten(loaded, session, settings):
    original = session.get(POLine, "ERP-PO501-L1")
    csv_text = (
        "source_record_id,po_number,line_number,supplier_code,location_code,item_code,"
        "ordered_qty,ordered_uom,status,expected_receipt_by\n"
        "ERP-PO501-L1,PO-501,1,SUP-07,MUM-014,ERP-SKU-001,999,EA,OPEN,2026-10-15T12:00:00+05:30\n"
    )
    res = import_dataset(session, "purchase_order_lines", csv_text, "changed.csv", settings.eval_time)
    assert res.conflicts == 1 and res.accepted == 0
    conflict = session.scalars(select(SourceConflict)).all()[0]
    assert conflict.source_record_id == "ERP-PO501-L1" and conflict.status == "OPEN"
    session.refresh(original)
    assert int(original.ordered_qty) == 100  # untouched


def test_invalid_rows_visible_with_reasons(session, settings):
    csv_text = (
        "source_record_id,po_number,line_number,supplier_code,location_code,item_code,"
        "ordered_qty,ordered_uom,status,expected_receipt_by\n"
        "BAD-1,PO-B1,1,SUP-1,MUM-014,ERP-SKU-001,notanumber,EA,OPEN,2026-10-15T12:00:00+05:30\n"
        "BAD-2,PO-B2,1,SUP-1,MUM-014,ERP-SKU-001,10,EA,OPEN,2026-10-15 12:00\n"
    )
    res = import_dataset(session, "purchase_order_lines", csv_text, "bad.csv", settings.eval_time)
    assert res.accepted == 0 and res.rejected == 2
    assert all(e["reason"] for e in res.errors)
    assert res.status == "COMPLETED_WITH_ERRORS"


def test_no_eligible_assignee_task_stays_visible(session, settings, engine):
    """Only an off-shift associate exists -> task must not be assigned."""
    employees = (
        "source_employee_id,display_name,employment_status,role,location_code,"
        "manager_employee_id,region_id\n"
        "HR-900,Test Person,ACTIVE,receiving_associate,ST-014,,REGION-MH\n"
    )
    workforce = (
        "source_employee_id,availability_status,shift_start,shift_end,observed_at\n"
        "HR-900,OFF_SHIFT,2026-10-16T08:00:00+05:30,2026-10-16T16:00:00+05:30,"
        "2026-10-16T14:50:00+05:30\n"
    )
    import_dataset(session, "location_aliases",
                   "source_system,external_location_id,canonical_location_id,region_id\n"
                   "HRMS,ST-014,STORE-014,REGION-MH\n", "la.csv", settings.eval_time)
    import_dataset(session, "employees", employees, "e.csv", settings.eval_time)
    import_dataset(session, "workforce_status", workforce, "w.csv", settings.eval_time)

    from app.detection.shortfall import case_key
    exc = OpsException(
        tenant="t", case_key=case_key("t", "v", "PO-Z", 1), type="receipt_shortfall",
        rule_version="v", status="OPEN", po_number="PO-Z", po_line_number=1,
        canonical_product_id="SKU-001", canonical_location_id="STORE-014",
        expected_qty=10, received_qty=5, base_uom="EA", variance=5,
        explanation="x", missing_evidence=[], evidence_refs={},
        detected_at=settings.eval_time)
    session.add(exc)
    session.commit()
    task = create_task_for_exception(session, exc, settings.eval_time,
                                     timedelta(minutes=60), 24)
    assert task.assignee_id is None
    assert task.status == "CREATED"
    assert "manual assignment" in task.assignment_reason
