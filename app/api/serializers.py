from decimal import Decimal

from app.db.models import ImportRun, OpsException, Task


def _num(v) -> str:
    """Display a Numeric without padding zeros: 8.000000 -> '8'."""
    return format(Decimal(v).normalize(), "f")


def import_run_dict(run: ImportRun) -> dict:
    return {
        "import_id": run.id, "dataset": run.dataset,
        "source_filename": run.source_filename, "status": run.status,
        "accepted": run.accepted_count, "rejected": run.rejected_count,
        "duplicates": run.duplicate_count, "conflicts": run.conflict_count,
        "mapping_version": run.mapping_version, "created_at": run.created_at.isoformat(),
        "errors": [{"row": e.row_number, "source_record_id": e.source_record_id,
                    "reason": e.reason} for e in run.row_errors],
    }


def exception_dict(exc: OpsException, with_events: bool = False) -> dict:
    out = {
        "id": exc.id, "tenant": exc.tenant, "case_key": exc.case_key, "type": exc.type,
        "rule_version": exc.rule_version, "status": exc.status,
        "po_number": exc.po_number, "po_line_number": exc.po_line_number,
        "canonical_product_id": exc.canonical_product_id,
        "canonical_location_id": exc.canonical_location_id,
        "expected_qty": _num(exc.expected_qty), "received_qty": _num(exc.received_qty),
        "base_uom": exc.base_uom, "variance": _num(exc.variance),
        "explanation": exc.explanation, "missing_evidence": exc.missing_evidence,
        "evidence_refs": exc.evidence_refs, "detected_at": exc.detected_at.isoformat(),
    }
    if with_events:
        out["events"] = [{"event_type": e.event_type, "actor": e.actor, "detail": e.detail,
                          "at": e.at.isoformat()} for e in exc.events]
        out["tasks"] = [task_dict(t) for t in exc.tasks]
    return out


def task_dict(task: Task, with_events: bool = False) -> dict:
    out = {
        "id": task.id, "exception_id": task.exception_id, "title": task.title,
        "status": task.status, "assignee_id": task.assignee_id,
        "canonical_location_id": task.canonical_location_id,
        "routing_policy_version": task.routing_policy_version,
        "assignment_reason": task.assignment_reason,
        "resolution_code": task.resolution_code,
        "resolution_evidence_ref": task.resolution_evidence_ref,
        "due_at": task.due_at.isoformat(), "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }
    if with_events:
        out["history"] = [{"event_type": e.event_type, "actor": e.actor,
                           "from_status": e.from_status, "to_status": e.to_status,
                           "reason": e.reason, "at": e.at.isoformat()} for e in task.events]
    return out
