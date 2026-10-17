"""Persistence model.

Principles:
- Raw source facts keep their full source identity (system, record ID, import run).
- Canonical fields (canonical_product_id, canonical_location_id, *_qty_base) are
  derived at import time through versioned mapping rules; NULL canonical fields
  mean "unresolved mapping" and are surfaced via /data-quality.
- Uniqueness constraints protect business invariants; services also check
  before insert so identical repeats are reported, not errors.
"""
from __future__ import annotations

import hashlib
import json
import uuid

from sqlalchemy import (JSON, ForeignKey, Numeric, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.db.types import AwareDateTime


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def content_hash(fields: dict) -> str:
    return hashlib.sha256(json.dumps(fields, sort_keys=True, default=str).encode()).hexdigest()


class Base(DeclarativeBase):
    pass


class ImportRun(Base):
    __tablename__ = "import_runs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("imp"))
    dataset: Mapped[str] = mapped_column(String(40))
    source_filename: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(30))  # COMPLETED | COMPLETED_WITH_ERRORS
    accepted_count: Mapped[int] = mapped_column(default=0)
    rejected_count: Mapped[int] = mapped_column(default=0)
    duplicate_count: Mapped[int] = mapped_column(default=0)
    conflict_count: Mapped[int] = mapped_column(default=0)
    mapping_version: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[object] = mapped_column(AwareDateTime)

    row_errors: Mapped[list["ImportRowError"]] = relationship(back_populates="import_run",
                                                              cascade="all, delete-orphan")


class ImportRowError(Base):
    __tablename__ = "import_row_errors"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    import_id: Mapped[str] = mapped_column(ForeignKey("import_runs.id"))
    row_number: Mapped[int] = mapped_column()
    source_record_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    reason: Mapped[str] = mapped_column(Text)

    import_run: Mapped[ImportRun] = relationship(back_populates="row_errors")


class SourceConflict(Base):
    """Same source ID arriving with different business content.

    The existing row is never silently overwritten; a conflict is recorded
    for review instead.
    """
    __tablename__ = "source_conflicts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    dataset: Mapped[str] = mapped_column(String(40))
    source_record_id: Mapped[str] = mapped_column(String(80))
    existing_hash: Mapped[str] = mapped_column(String(64))
    incoming_hash: Mapped[str] = mapped_column(String(64))
    incoming_payload: Mapped[dict] = mapped_column(JSON)
    import_id: Mapped[str] = mapped_column(ForeignKey("import_runs.id"))
    status: Mapped[str] = mapped_column(String(20), default="OPEN")  # OPEN | RESOLVED
    created_at: Mapped[object] = mapped_column(AwareDateTime)


class ProductAlias(Base):
    __tablename__ = "product_aliases"

    source_system: Mapped[str] = mapped_column(String(20), primary_key=True)
    external_item_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    canonical_product_id: Mapped[str] = mapped_column(String(80))
    source_uom: Mapped[str] = mapped_column(String(10))
    base_uom: Mapped[str] = mapped_column(String(10))
    to_base_factor: Mapped[object] = mapped_column(Numeric(18, 6))
    import_id: Mapped[str] = mapped_column(ForeignKey("import_runs.id"))


class LocationAlias(Base):
    __tablename__ = "location_aliases"

    source_system: Mapped[str] = mapped_column(String(20), primary_key=True)
    external_location_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    canonical_location_id: Mapped[str] = mapped_column(String(80))
    region_id: Mapped[str] = mapped_column(String(80))
    import_id: Mapped[str] = mapped_column(ForeignKey("import_runs.id"))


class POLine(Base):
    __tablename__ = "po_lines"
    __table_args__ = (UniqueConstraint("po_number", "line_number", name="uq_po_line"),)

    source_record_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    po_number: Mapped[str] = mapped_column(String(40))
    line_number: Mapped[int] = mapped_column()
    supplier_code: Mapped[str] = mapped_column(String(40))
    source_location_code: Mapped[str] = mapped_column(String(40))
    canonical_location_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    source_item_code: Mapped[str] = mapped_column(String(80))
    canonical_product_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    ordered_qty: Mapped[object] = mapped_column(Numeric(18, 6))
    ordered_uom: Mapped[str] = mapped_column(String(10))
    ordered_qty_base: Mapped[object | None] = mapped_column(Numeric(18, 6), nullable=True)
    base_uom: Mapped[str | None] = mapped_column(String(10), nullable=True)
    status: Mapped[str] = mapped_column(String(20))  # OPEN | CANCELLED
    expected_receipt_by: Mapped[object] = mapped_column(AwareDateTime)
    import_id: Mapped[str] = mapped_column(ForeignKey("import_runs.id"))
    content_hash: Mapped[str] = mapped_column(String(64))


class ReceiptLine(Base):
    __tablename__ = "receipt_lines"

    source_record_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    receipt_number: Mapped[str] = mapped_column(String(40))
    receipt_line: Mapped[int] = mapped_column()
    po_number: Mapped[str] = mapped_column(String(40))
    po_line_number: Mapped[int] = mapped_column()
    source_location_code: Mapped[str] = mapped_column(String(40))
    canonical_location_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    product_ref: Mapped[str] = mapped_column(String(80))
    canonical_product_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    accepted_qty: Mapped[object] = mapped_column(Numeric(18, 6))
    uom: Mapped[str] = mapped_column(String(10))
    accepted_qty_base: Mapped[object | None] = mapped_column(Numeric(18, 6), nullable=True)
    base_uom: Mapped[str | None] = mapped_column(String(10), nullable=True)
    posting_status: Mapped[str] = mapped_column(String(20))  # POSTED | ...
    received_at: Mapped[object] = mapped_column(AwareDateTime)
    import_id: Mapped[str] = mapped_column(ForeignKey("import_runs.id"))
    content_hash: Mapped[str] = mapped_column(String(64))


class Employee(Base):
    __tablename__ = "employees"

    source_employee_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(120))
    employment_status: Mapped[str] = mapped_column(String(20))  # ACTIVE | INACTIVE
    role: Mapped[str] = mapped_column(String(40))
    source_location_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    canonical_location_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    manager_employee_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    region_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    import_id: Mapped[str] = mapped_column(ForeignKey("import_runs.id"))
    content_hash: Mapped[str] = mapped_column(String(64))


class WorkforceStatus(Base):
    """Latest availability sample per employee."""
    __tablename__ = "workforce_status"

    source_employee_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    availability_status: Mapped[str] = mapped_column(String(20))  # ON_SHIFT | OFF_SHIFT
    shift_start: Mapped[object] = mapped_column(AwareDateTime)
    shift_end: Mapped[object] = mapped_column(AwareDateTime)
    observed_at: Mapped[object] = mapped_column(AwareDateTime)
    import_id: Mapped[str] = mapped_column(ForeignKey("import_runs.id"))
    content_hash: Mapped[str] = mapped_column(String(64))


class OpsException(Base):
    """A business case (kept separate from the work tasks it spawns)."""
    __tablename__ = "exceptions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("exc"))
    tenant: Mapped[str] = mapped_column(String(40))
    case_key: Mapped[str] = mapped_column(String(64), unique=True)  # idempotent business key
    type: Mapped[str] = mapped_column(String(40))                   # receipt_shortfall
    rule_version: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(20), default="OPEN")  # OPEN | RESOLVED
    po_number: Mapped[str] = mapped_column(String(40))
    po_line_number: Mapped[int] = mapped_column()
    canonical_product_id: Mapped[str] = mapped_column(String(80))
    canonical_location_id: Mapped[str] = mapped_column(String(80))
    expected_qty: Mapped[object] = mapped_column(Numeric(18, 6))
    received_qty: Mapped[object] = mapped_column(Numeric(18, 6))
    base_uom: Mapped[str] = mapped_column(String(10))
    variance: Mapped[object] = mapped_column(Numeric(18, 6))
    explanation: Mapped[str] = mapped_column(Text)
    missing_evidence: Mapped[dict] = mapped_column(JSON)
    evidence_refs: Mapped[dict] = mapped_column(JSON)  # source record IDs used
    detected_at: Mapped[object] = mapped_column(AwareDateTime)

    tasks: Mapped[list["Task"]] = relationship(back_populates="exception")
    events: Mapped[list["ExceptionEvent"]] = relationship(back_populates="exception",
                                                          cascade="all, delete-orphan")


class ExceptionEvent(Base):
    __tablename__ = "exception_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    exception_id: Mapped[str] = mapped_column(ForeignKey("exceptions.id"))
    event_type: Mapped[str] = mapped_column(String(40))
    actor: Mapped[str] = mapped_column(String(80), default="system")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    at: Mapped[object] = mapped_column(AwareDateTime)

    exception: Mapped[OpsException] = relationship(back_populates="events")


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("task"))
    exception_id: Mapped[str] = mapped_column(ForeignKey("exceptions.id"))
    title: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30))  # see workflows/state_machine.py
    assignee_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    canonical_location_id: Mapped[str] = mapped_column(String(80))
    routing_policy_version: Mapped[str] = mapped_column(String(40))
    assignment_reason: Mapped[str] = mapped_column(Text)
    resolution_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    resolution_evidence_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    due_at: Mapped[object] = mapped_column(AwareDateTime)
    created_at: Mapped[object] = mapped_column(AwareDateTime)
    updated_at: Mapped[object] = mapped_column(AwareDateTime)

    exception: Mapped[OpsException] = relationship(back_populates="tasks")
    events: Mapped[list["TaskEvent"]] = relationship(back_populates="task",
                                                     cascade="all, delete-orphan")


class TaskEvent(Base):
    __tablename__ = "task_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    event_type: Mapped[str] = mapped_column(String(40))
    actor: Mapped[str] = mapped_column(String(80), default="system")
    from_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    at: Mapped[object] = mapped_column(AwareDateTime)

    task: Mapped[Task] = relationship(back_populates="events")
