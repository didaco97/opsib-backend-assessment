"""OPSiB pilot: retail data integration, exception detection, task reliability."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import logging

from fastapi import Depends, FastAPI, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai import provider as ai
from app.api.deps import get_session, settings
from app.api.serializers import (exception_dict, import_run_dict, task_dict)
from app.config import Settings
from app.db.base import create_all
from app.db.models import (Employee, ImportRun, OpsException, POLine,
                           ReceiptLine, SourceConflict, Task)
from app.detection.shortfall import run_detection
from app.ingestion.service import import_dataset, import_fixture_pack
from app.workflows import task_service
from app.workflows.task_service import WorkflowError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
# Structured, credential-free logs: IDs, counts and states only, no payloads or PII.

app = FastAPI(title="OPSiB Backend Assessment", version="1.0.0")


@app.on_event("startup")
def _startup() -> None:
    create_all()


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------- Ingestion ----------

@app.post("/imports/fixture-pack", status_code=201)
def import_fixtures(session: Session = Depends(get_session),
                    cfg: Settings = Depends(settings)):
    """Load all six CSVs from the configured fixtures directory, in dependency order."""
    results = import_fixture_pack(session, cfg.fixtures_dir, cfg.eval_time)
    return [r.as_dict() for r in results]


@app.post("/imports/{dataset}", status_code=201)
async def import_one(dataset: str, file: UploadFile,
                     session: Session = Depends(get_session),
                     cfg: Settings = Depends(settings)):
    text = (await file.read()).decode("utf-8")
    try:
        result = import_dataset(session, dataset, text, file.filename or dataset, cfg.eval_time)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return result.as_dict()


@app.get("/imports/{import_id}")
def get_import(import_id: str, session: Session = Depends(get_session)):
    run = session.get(ImportRun, import_id)
    if run is None:
        raise HTTPException(404, f"import {import_id} not found")
    return import_run_dict(run)


@app.get("/data-quality")
def data_quality(session: Session = Depends(get_session)):
    unmapped_po = session.scalars(select(POLine).where(
        (POLine.canonical_product_id.is_(None)) | (POLine.canonical_location_id.is_(None)))).all()
    unmapped_receipts = session.scalars(select(ReceiptLine).where(
        (ReceiptLine.canonical_product_id.is_(None)) | (ReceiptLine.canonical_location_id.is_(None)))).all()
    conflicts = session.scalars(select(SourceConflict).where(SourceConflict.status == "OPEN")).all()
    regional = session.scalars(select(Employee).where(Employee.canonical_location_id.is_(None))).all()
    return {
        "unmapped_po_lines": [p.source_record_id for p in unmapped_po],
        "unmapped_receipt_lines": [r.source_record_id for r in unmapped_receipts],
        "open_source_conflicts": [
            {"dataset": c.dataset, "source_record_id": c.source_record_id,
             "import_id": c.import_id, "created_at": c.created_at.isoformat()}
            for c in conflicts],
        "employees_without_store_scope": [e.source_employee_id for e in regional],
    }


# ---------- Detection ----------

@app.post("/detection/run")
def detect(session: Session = Depends(get_session), cfg: Settings = Depends(settings)):
    result = run_detection(session, cfg.eval_time, Decimal(str(cfg.shortfall_tolerance)))
    freshness = timedelta(minutes=cfg.availability_freshness_minutes)
    tasks = []
    for exc_id in result.created:
        exc = session.get(OpsException, exc_id)
        task = task_service.create_task_for_exception(
            session, exc, cfg.eval_time, freshness, cfg.task_due_hours)
        tasks.append({"exception_id": exc_id, "task_id": task.id,
                      "assignee_id": task.assignee_id, "status": task.status})
    out = result.as_dict()
    out["tasks_created"] = tasks
    return out


# ---------- Exceptions ----------

@app.get("/exceptions")
def list_exceptions(status: str | None = None, session: Session = Depends(get_session)):
    stmt = select(OpsException).order_by(OpsException.po_number)
    if status:
        stmt = stmt.where(OpsException.status == status)
    return [exception_dict(e) for e in session.scalars(stmt).all()]


@app.get("/exceptions/{exception_id}")
def get_exception(exception_id: str, session: Session = Depends(get_session)):
    exc = session.get(OpsException, exception_id)
    if exc is None:
        raise HTTPException(404, f"exception {exception_id} not found")
    return exception_dict(exc, with_events=True)


@app.post("/exceptions/{exception_id}/investigation-summary")
def investigation_summary(exception_id: str, session: Session = Depends(get_session)):
    exc = session.get(OpsException, exception_id)
    if exc is None:
        raise HTTPException(404, f"exception {exception_id} not found")
    provider = ai.get_provider()
    if provider is None:
        raise HTTPException(503, "no AI provider configured; deterministic finding is authoritative")
    context = ai.build_case_context(exc)
    allowed = set(exc.evidence_refs.get("receipt_source_record_ids", []))
    allowed.add(exc.evidence_refs.get("po_source_record_id"))
    try:
        summary = ai.validate_summary(provider.summarize(context), allowed)
    except ai.InvalidProviderOutput as exc2:
        raise HTTPException(502, f"AI provider output rejected: {exc2}")
    return summary.model_dump()


# ---------- Tasks ----------

@app.get("/tasks")
def list_tasks(assignee_id: str | None = None, location: str | None = None,
               status: str | None = None, session: Session = Depends(get_session)):
    stmt = select(Task).order_by(Task.created_at)
    if assignee_id:
        stmt = stmt.where(Task.assignee_id == assignee_id)
    if location:
        stmt = stmt.where(Task.canonical_location_id == location)
    if status:
        stmt = stmt.where(Task.status == status)
    return [task_dict(t) for t in session.scalars(stmt).all()]


@app.get("/tasks/{task_id}")
def get_task(task_id: str, session: Session = Depends(get_session)):
    task = session.get(Task, task_id)
    if task is None:
        raise HTTPException(404, f"task {task_id} not found")
    return task_dict(task, with_events=True)


def _do_transition(task_id, action, session, cfg, actor, resolution_code, evidence_ref,
                   reason=None):
    try:
        task, replay = task_service.transition_task(
            session, task_id, action, cfg.eval_time, actor=actor,
            resolution_code=resolution_code, resolution_evidence_ref=evidence_ref,
            reason=reason)
    except WorkflowError as exc:
        session.rollback()
        raise HTTPException(exc.status_code, exc.detail)
    out = task_dict(task, with_events=True)
    out["idempotent_replay"] = replay
    return out


@app.post("/tasks/{task_id}/acknowledge")
def acknowledge(task_id: str, actor: str = "system",
                session: Session = Depends(get_session), cfg: Settings = Depends(settings)):
    return _do_transition(task_id, "acknowledge", session, cfg, actor, None, None)


@app.post("/tasks/{task_id}/start")
def start(task_id: str, actor: str = "system",
          session: Session = Depends(get_session), cfg: Settings = Depends(settings)):
    return _do_transition(task_id, "start", session, cfg, actor, None, None)


@app.post("/tasks/{task_id}/complete")
def complete(task_id: str, body: dict, actor: str = "system",
             session: Session = Depends(get_session), cfg: Settings = Depends(settings)):
    return _do_transition(task_id, "complete", session, cfg, actor,
                          body.get("resolution_code"), body.get("resolution_evidence_ref"))


@app.post("/tasks/{task_id}/request-verification")
def request_verification(task_id: str, actor: str = "system",
                         session: Session = Depends(get_session), cfg: Settings = Depends(settings)):
    return _do_transition(task_id, "request-verification", session, cfg, actor, None, None)


@app.post("/tasks/{task_id}/verify")
def verify(task_id: str, actor: str = "system",
           session: Session = Depends(get_session), cfg: Settings = Depends(settings)):
    return _do_transition(task_id, "verify", session, cfg, actor, None, None)


@app.post("/tasks/{task_id}/reject")
def reject(task_id: str, body: dict, actor: str = "system",
           session: Session = Depends(get_session), cfg: Settings = Depends(settings)):
    return _do_transition(task_id, "reject", session, cfg, actor, None, None,
                          reason=body.get("reason"))


@app.post("/tasks/{task_id}/reopen")
def reopen(task_id: str, actor: str = "system",
           session: Session = Depends(get_session), cfg: Settings = Depends(settings)):
    return _do_transition(task_id, "reopen", session, cfg, actor, None, None)


@app.post("/tasks/{task_id}/reassign")
def reassign(task_id: str, body: dict,
             session: Session = Depends(get_session), cfg: Settings = Depends(settings)):
    try:
        task = task_service.reassign_task(
            session, task_id, body.get("new_assignee_id") or "",
            body.get("reason") or "", body.get("actor") or "system",
            cfg.eval_time, timedelta(minutes=cfg.availability_freshness_minutes))
    except WorkflowError as exc:
        session.rollback()
        raise HTTPException(exc.status_code, exc.detail)
    return task_dict(task, with_events=True)
