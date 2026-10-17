"""Employee eligibility and task assignment, policy routing/v1.

Eligibility (all must hold):
  1. employment_status == ACTIVE  (HRMS roster wins over the shift export)
  2. latest availability sample says ON_SHIFT
  3. the sample was observed within AVAILABILITY_FRESHNESS_MINUTES of the
     frozen evaluation time

Preference order:
  1. receiving_associate at the exception's canonical location
  2. fallback: store_manager at the same location
  3. otherwise the task is left unassigned and visible (NEEDS_MANUAL_ASSIGNMENT);
     nobody ineligible is ever assigned silently.

Every decision records why it was made.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Employee, WorkforceStatus


@dataclass
class AssignmentDecision:
    assignee_id: str | None
    reason: str


def _eligible(session: Session, employee: Employee, now: datetime,
              freshness: timedelta) -> tuple[bool, str]:
    if employee.employment_status != "ACTIVE":
        return False, f"{employee.source_employee_id} employment status {employee.employment_status}"
    sample = session.get(WorkforceStatus, employee.source_employee_id)
    if sample is None:
        return False, f"{employee.source_employee_id} has no availability sample"
    if sample.availability_status != "ON_SHIFT":
        return False, (f"{employee.source_employee_id} is {sample.availability_status} "
                       f"(observed {sample.observed_at.isoformat()})")
    if sample.observed_at < now - freshness:
        return False, (f"{employee.source_employee_id} availability sample is stale "
                       f"(observed {sample.observed_at.isoformat()})")
    return True, "eligible"


def decide_assignment(session: Session, canonical_location_id: str, now: datetime,
                      freshness: timedelta) -> AssignmentDecision:
    employees = session.scalars(select(Employee).order_by(Employee.source_employee_id)).all()

    for role, label in (("receiving_associate", "primary"), ("store_manager", "fallback")):
        eligible = []
        rejections = []
        for emp in employees:
            if emp.role != role or emp.canonical_location_id != canonical_location_id:
                continue
            ok, why = _eligible(session, emp, now, freshness)
            if ok:
                eligible.append(emp)
            else:
                rejections.append(why)
        if eligible:
            chosen = eligible[0]  # deterministic: lowest employee id
            reason = (f"{label} route: {chosen.source_employee_id} is an ACTIVE, on-shift {role} "
                      f"at {canonical_location_id} with a fresh availability sample")
            if rejections:
                reason += "; excluded: " + "; ".join(rejections)
            return AssignmentDecision(chosen.source_employee_id, reason)

    return AssignmentDecision(
        None,
        f"no ACTIVE on-shift receiving_associate or store_manager at {canonical_location_id} "
        "with a fresh availability sample; task needs manual assignment")


def check_explicit_assignee(session: Session, employee_id: str, now: datetime,
                            freshness: timedelta) -> tuple[bool, str]:
    emp = session.get(Employee, employee_id)
    if emp is None:
        return False, f"unknown employee {employee_id}"
    return _eligible(session, emp, now, freshness)
