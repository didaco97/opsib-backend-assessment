"""Task creation and lifecycle operations.

Concurrency/timeout safety:
- Task creation is protected by a per-exception check inside one transaction;
  the exceptions.case_key unique constraint plus this check mean two workers
  cannot create two investigation tasks for the same business issue.
- State changes run inside a transaction with a row lock (SELECT ... FOR
  UPDATE where the dialect supports it). If the API commits a completion but
  the client times out and retries, the retry sees the task already in
  COMPLETED-or-later and gets back the same state with idempotent_replay=true
  instead of an error or a duplicate audit event.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import ROUTING_POLICY_VERSION
from app.db.models import (ExceptionEvent, OpsException, Task, TaskEvent)
from app.routing.assignment import (check_explicit_assignee,
                                    decide_assignment)
from app.workflows.state_machine import ACTION_TARGETS, TERMINAL_OR_BEYOND, can_transition

log = logging.getLogger("opsib.workflows")


class WorkflowError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def create_task_for_exception(session: Session, exc: OpsException, now: datetime,
                              freshness: timedelta, due_hours: int) -> Task:
    existing = session.scalar(select(Task).where(Task.exception_id == exc.id))
    if existing:
        return existing
    decision = decide_assignment(session, exc.canonical_location_id, now, freshness)
    status = "ASSIGNED" if decision.assignee_id else "CREATED"
    task = Task(
        exception_id=exc.id,
        title=f"Investigate receipt shortfall {exc.po_number} line {exc.po_line_number} "
              f"({format(Decimal(str(exc.variance)).normalize(), 'f')} {exc.base_uom})",
        status=status,
        assignee_id=decision.assignee_id,
        canonical_location_id=exc.canonical_location_id,
        routing_policy_version=ROUTING_POLICY_VERSION,
        assignment_reason=decision.reason,
        due_at=now + timedelta(hours=due_hours),
        created_at=now, updated_at=now)
    session.add(task)
    session.flush()
    session.add(TaskEvent(task_id=task.id, event_type="CREATED", to_status=status, at=now))
    if decision.assignee_id:
        session.add(TaskEvent(task_id=task.id, event_type="ASSIGNED", from_status="CREATED",
                              to_status="ASSIGNED", reason=decision.reason, at=now))
    session.commit()
    return task


def _lock_task(session: Session, task_id: str) -> Task:
    stmt = select(Task).where(Task.id == task_id)
    if session.bind.dialect.name != "sqlite":
        stmt = stmt.with_for_update()
    task = session.scalar(stmt)
    if task is None:
        raise WorkflowError(404, f"task {task_id} not found")
    return task


def transition_task(session: Session, task_id: str, action: str, now: datetime,
                    actor: str = "system", resolution_code: str | None = None,
                    resolution_evidence_ref: str | None = None,
                    reason: str | None = None) -> tuple[Task, bool]:
    """Returns (task, idempotent_replay)."""
    if action not in ACTION_TARGETS:
        raise WorkflowError(400, f"unknown action {action!r}")
    target = ACTION_TARGETS[action]
    task = _lock_task(session, task_id)

    if task.status == target or (target == "COMPLETED" and task.status in TERMINAL_OR_BEYOND):
        # Safe answer to a retried request after a client timeout.
        session.commit()
        return task, True

    if not can_transition(task.status, target):
        raise WorkflowError(
            409, f"invalid transition {task.status} -> {target}; "
                 f"allowed: {sorted(can_transition_targets(task.status))}")

    if action == "complete":
        if not resolution_code or not resolution_evidence_ref:
            raise WorkflowError(422, "complete requires resolution_code and resolution_evidence_ref")
        task.resolution_code = resolution_code
        task.resolution_evidence_ref = resolution_evidence_ref

    if action == "reject" and not reason:
        raise WorkflowError(422, "reject requires a reason")

    if action == "verify":
        if not task.resolution_code or not task.resolution_evidence_ref:
            raise WorkflowError(422, "verification requires a resolution code and evidence reference")
        exc = session.get(OpsException, task.exception_id)
        exc.status = "RESOLVED"
        session.add(ExceptionEvent(exception_id=exc.id, event_type="RESOLVED", actor=actor,
                                   detail={"task_id": task.id,
                                           "resolution_code": task.resolution_code}, at=now))

    from_status = task.status
    task.status = target
    task.updated_at = now
    session.add(TaskEvent(task_id=task.id, event_type=target, actor=actor,
                          from_status=from_status, to_status=target, reason=reason, at=now))
    session.commit()
    log.info("task_transition task_id=%s action=%s from=%s to=%s actor=%s",
             task.id, action, from_status, target, actor)
    return task, False


def can_transition_targets(status: str) -> set[str]:
    from app.workflows.state_machine import TRANSITIONS
    return TRANSITIONS.get(status, set())


def reassign_task(session: Session, task_id: str, new_assignee_id: str, reason: str,
                  actor: str, now: datetime, freshness: timedelta) -> Task:
    if not reason:
        raise WorkflowError(422, "reassignment requires a reason")
    task = _lock_task(session, task_id)
    if task.status in TERMINAL_OR_BEYOND:
        raise WorkflowError(409, f"cannot reassign a task in status {task.status}")
    ok, why = check_explicit_assignee(session, new_assignee_id, now, freshness)
    if not ok:
        raise WorkflowError(422, f"assignee not eligible: {why}")
    previous = task.assignee_id
    task.assignee_id = new_assignee_id
    task.updated_at = now
    session.add(TaskEvent(task_id=task.id, event_type="REASSIGNED", actor=actor,
                          from_status=task.status, to_status=task.status,
                          reason=f"{previous or 'unassigned'} -> {new_assignee_id}: {reason}",
                          at=now))
    session.commit()
    log.info("task_reassigned task_id=%s from_assignee=%s to=%s actor=%s",
             task.id, previous, new_assignee_id, actor)
    return task
