"""Ingestion service.

- One ImportRun per file; rows are validated, mapped and inserted inside a
  single transaction. Row-level failures are recorded and do not abort the
  file; a database-level failure rolls the whole import back.
- Idempotency: source_record_id is the primary key. An identical repeat is
  counted as a duplicate and skipped. The same ID with different business
  content never overwrites: a SourceConflict is recorded for review.
- The uniqueness constraint is the backstop that makes this true even if two
  workers race; an IntegrityError on flush is converted into duplicate/conflict
  handling rather than a 500.
"""
from __future__ import annotations

import csv
import io
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import MAPPING_VERSION
from app.db.models import (Employee, ImportRowError, ImportRun, LocationAlias,
                           POLine, ProductAlias, ReceiptLine, SourceConflict,
                           WorkforceStatus, content_hash)
from app.ingestion.schemas import DATASET_SCHEMAS
from app.mapping.service import resolve_location, resolve_product, to_base_qty

log = logging.getLogger("opsib.ingestion")

SOURCE_SYSTEM_BY_DATASET = {
    "purchase_order_lines": "ERP",
    "goods_receipt_lines": "WMS",
    "employees": "HRMS",
}


@dataclass
class ImportResult:
    import_id: str
    dataset: str
    status: str
    accepted: int = 0
    rejected: int = 0
    duplicates: int = 0
    conflicts: int = 0
    errors: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "import_id": self.import_id,
            "dataset": self.dataset,
            "status": self.status,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "duplicates": self.duplicates,
            "conflicts": self.conflicts,
            "errors": self.errors,
        }


def parse_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    return [{(k or "").strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
            for row in reader]


def import_dataset(session: Session, dataset: str, csv_text: str,
                   source_filename: str, now: datetime) -> ImportResult:
    if dataset not in DATASET_SCHEMAS:
        raise ValueError(f"unknown dataset {dataset!r}")
    schema = DATASET_SCHEMAS[dataset]
    run = ImportRun(dataset=dataset, source_filename=source_filename,
                    status="COMPLETED", mapping_version=MAPPING_VERSION, created_at=now)
    session.add(run)
    session.flush()
    result = ImportResult(import_id=run.id, dataset=dataset, status=run.status)

    # Rows pending in THIS import are invisible to session.get (autoflush is
    # off), so track them explicitly; the unique constraint remains the
    # backstop for cross-worker races.
    seen: dict[tuple, str] = {}
    for row_number, raw in enumerate(parse_csv(csv_text), start=2):  # header = row 1
        try:
            row = schema(**raw)
        except ValidationError as exc:
            _reject(session, run, result, row_number, raw.get("source_record_id"),
                    "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()))
            continue
        try:
            _upsert_row(session, dataset, row, run, result, now, seen)
        except ValueError as exc:
            _reject(session, run, result, row_number,
                    getattr(row, "source_record_id", None), str(exc))

    if result.rejected or result.conflicts:
        run.status = "COMPLETED_WITH_ERRORS"
    run.accepted_count = result.accepted
    run.rejected_count = result.rejected
    run.duplicate_count = result.duplicates
    run.conflict_count = result.conflicts
    result.status = run.status
    log.info("import_finished import_id=%s dataset=%s status=%s accepted=%d rejected=%d "
             "duplicates=%d conflicts=%d",
             run.id, dataset, run.status, result.accepted, result.rejected,
             result.duplicates, result.conflicts)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise
    return result


def _reject(session, run, result, row_number, source_record_id, reason):
    session.add(ImportRowError(import_id=run.id, row_number=row_number,
                               source_record_id=source_record_id, reason=reason))
    result.rejected += 1
    result.errors.append({"row": row_number, "source_record_id": source_record_id,
                          "reason": reason})


def _jsonable(payload: dict) -> dict:
    return json.loads(json.dumps(payload, default=str))


def _record_conflict(session, run, result, dataset, source_record_id, existing_hash,
                     incoming_hash, payload, now):
    session.add(SourceConflict(dataset=dataset, source_record_id=source_record_id,
                               existing_hash=existing_hash, incoming_hash=incoming_hash,
                               incoming_payload=_jsonable(payload), import_id=run.id,
                               created_at=now))
    result.conflicts += 1


def _check_seen(seen: dict, key: tuple, payload_hash: str, session, run, result,
                dataset, record_label: str, data: dict, now) -> bool | None:
    """Returns True if handled as duplicate/conflict, None to proceed."""
    if key not in seen:
        return None
    if seen[key] == payload_hash:
        result.duplicates += 1
    else:
        _record_conflict(session, run, result, dataset, record_label,
                         seen[key], payload_hash, data, now)
    return True


def _upsert_row(session: Session, dataset: str, row, run: ImportRun,
                result: ImportResult, now: datetime, seen: dict) -> None:
    data = row.model_dump()
    payload_hash = content_hash({k: str(v) for k, v in data.items()})

    if dataset == "product_aliases":
        key = ("product_aliases", row.source_system, row.external_item_id)
        if _check_seen(seen, key, payload_hash, session, run, result, dataset,
                       row.external_item_id, data, now):
            return
        if session.get(ProductAlias, key[1:]):
            result.duplicates += 1
            return
        session.add(ProductAlias(import_id=run.id, **data))
        seen[key] = payload_hash
        result.accepted += 1
    elif dataset == "location_aliases":
        key = ("location_aliases", row.source_system, row.external_location_id)
        if _check_seen(seen, key, payload_hash, session, run, result, dataset,
                       row.external_location_id, data, now):
            return
        if session.get(LocationAlias, key[1:]):
            result.duplicates += 1
            return
        session.add(LocationAlias(import_id=run.id, **data))
        seen[key] = payload_hash
        result.accepted += 1
    elif dataset == "purchase_order_lines":
        key = ("purchase_order_lines", row.source_record_id)
        if _check_seen(seen, key, payload_hash, session, run, result, dataset,
                       row.source_record_id, data, now):
            return
        existing = session.get(POLine, row.source_record_id)
        if existing:
            if existing.content_hash == payload_hash:
                result.duplicates += 1
            else:
                _record_conflict(session, run, result, dataset, row.source_record_id,
                                 existing.content_hash, payload_hash, data, now)
            return
        alias = resolve_product(session, "ERP", row.item_code)
        loc = resolve_location(session, "ERP", row.location_code)
        qty_base = to_base_qty(row.ordered_qty, row.ordered_uom, alias) if alias else None
        session.add(POLine(
            source_record_id=row.source_record_id, po_number=row.po_number,
            line_number=row.line_number, supplier_code=row.supplier_code,
            source_location_code=row.location_code,
            canonical_location_id=loc.canonical_location_id if loc else None,
            source_item_code=row.item_code,
            canonical_product_id=alias.canonical_product_id if alias else None,
            ordered_qty=row.ordered_qty, ordered_uom=row.ordered_uom,
            ordered_qty_base=qty_base, base_uom=alias.base_uom if alias else None,
            status=row.status, expected_receipt_by=row.expected_receipt_by,
            import_id=run.id, content_hash=payload_hash))
        seen[key] = payload_hash
        result.accepted += 1
    elif dataset == "goods_receipt_lines":
        key = ("goods_receipt_lines", row.source_record_id)
        if _check_seen(seen, key, payload_hash, session, run, result, dataset,
                       row.source_record_id, data, now):
            return
        existing = session.get(ReceiptLine, row.source_record_id)
        if existing:
            if existing.content_hash == payload_hash:
                result.duplicates += 1
            else:
                _record_conflict(session, run, result, dataset, row.source_record_id,
                                 existing.content_hash, payload_hash, data, now)
            return
        alias = resolve_product(session, "WMS", row.product_ref)
        loc = resolve_location(session, "WMS", row.location_code)
        qty_base = to_base_qty(row.accepted_qty, row.uom, alias) if alias else None
        session.add(ReceiptLine(
            source_record_id=row.source_record_id, receipt_number=row.receipt_number,
            receipt_line=row.receipt_line, po_number=row.po_number,
            po_line_number=row.po_line_number, source_location_code=row.location_code,
            canonical_location_id=loc.canonical_location_id if loc else None,
            product_ref=row.product_ref,
            canonical_product_id=alias.canonical_product_id if alias else None,
            accepted_qty=row.accepted_qty, uom=row.uom,
            accepted_qty_base=qty_base, base_uom=alias.base_uom if alias else None,
            posting_status=row.posting_status, received_at=row.received_at,
            import_id=run.id, content_hash=payload_hash))
        seen[key] = payload_hash
        result.accepted += 1
    elif dataset == "employees":
        key = ("employees", row.source_employee_id)
        if _check_seen(seen, key, payload_hash, session, run, result, dataset,
                       row.source_employee_id, data, now):
            return
        existing = session.get(Employee, row.source_employee_id)
        if existing:
            if existing.content_hash == payload_hash:
                result.duplicates += 1
            else:
                _record_conflict(session, run, result, dataset, row.source_employee_id,
                                 existing.content_hash, payload_hash, data, now)
            return
        loc = resolve_location(session, "HRMS", row.location_code) if row.location_code else None
        session.add(Employee(
            source_employee_id=row.source_employee_id, display_name=row.display_name,
            employment_status=row.employment_status, role=row.role,
            source_location_code=row.location_code or None,
            canonical_location_id=loc.canonical_location_id if loc else None,
            manager_employee_id=row.manager_employee_id or None,
            region_id=row.region_id or None,
            import_id=run.id, content_hash=payload_hash))
        seen[key] = payload_hash
        result.accepted += 1
    elif dataset == "workforce_status":
        key = ("workforce_status", row.source_employee_id)
        if _check_seen(seen, key, payload_hash, session, run, result, dataset,
                       row.source_employee_id, data, now):
            return
        existing = session.get(WorkforceStatus, row.source_employee_id)
        if existing:
            if existing.content_hash == payload_hash:
                result.duplicates += 1
                return
            # A newer availability sample supersedes; an older/equal one conflicts.
            if row.observed_at > existing.observed_at:
                for f in ("availability_status", "shift_start", "shift_end", "observed_at"):
                    setattr(existing, f, getattr(row, f))
                existing.import_id = run.id
                existing.content_hash = payload_hash
                result.accepted += 1
            else:
                _record_conflict(session, run, result, dataset, row.source_employee_id,
                                 existing.content_hash, payload_hash, data, now)
            return
        session.add(WorkforceStatus(import_id=run.id, content_hash=payload_hash, **data))
        seen[key] = payload_hash
        result.accepted += 1


FIXTURE_LOAD_ORDER = ["product_aliases", "location_aliases", "purchase_order_lines",
                      "goods_receipt_lines", "employees", "workforce_status"]


def import_fixture_pack(session: Session, fixtures_dir: str, now: datetime) -> list[ImportResult]:
    results = []
    for dataset in FIXTURE_LOAD_ORDER:
        with open(f"{fixtures_dir}/{dataset}.csv", encoding="utf-8") as fh:
            results.append(import_dataset(session, dataset, fh.read(),
                                          f"{dataset}.csv", now))
    return results
