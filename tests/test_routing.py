"""Routing: eligibility, exclusions, fallback, manual-assignment path."""
from datetime import timedelta

from app.db.models import OpsException, Task
from app.detection.shortfall import run_detection
from app.routing.assignment import check_explicit_assignee, decide_assignment
from app.workflows.task_service import create_task_for_exception
from sqlalchemy import select


def _freshness(settings):
    return timedelta(minutes=settings.availability_freshness_minutes)


def test_store014_picks_hr102(loaded, session, settings):
    d = decide_assignment(session, "STORE-014", settings.eval_time, _freshness(settings))
    assert d.assignee_id == "HR-102"
    # off-shift and inactive exclusions are recorded in the reason
    assert "HR-101" in d.reason and "OFF_SHIFT" in d.reason
    assert "HR-103" in d.reason and "INACTIVE" in d.reason


def test_store015_falls_back_to_store_manager(loaded, session, settings):
    d = decide_assignment(session, "STORE-015", settings.eval_time, _freshness(settings))
    assert d.assignee_id == "HR-401"
    assert "fallback" in d.reason


def test_no_eligible_anywhere_means_manual(loaded, session, settings):
    d = decide_assignment(session, "STORE-999", settings.eval_time, _freshness(settings))
    assert d.assignee_id is None
    assert "manual assignment" in d.reason


def test_explicit_assignee_validation(loaded, session, settings):
    fresh = _freshness(settings)
    assert check_explicit_assignee(session, "HR-101", settings.eval_time, fresh)[0] is False
    assert check_explicit_assignee(session, "HR-103", settings.eval_time, fresh)[0] is False
    assert check_explicit_assignee(session, "HR-102", settings.eval_time, fresh)[0] is True
    assert check_explicit_assignee(session, "NOBODY", settings.eval_time, fresh)[0] is False


def test_end_to_end_task_assignment(loaded, session, settings):
    res = run_detection(session, settings.eval_time)
    tasks = []
    for exc_id in res.created:
        exc = session.get(OpsException, exc_id)
        tasks.append(create_task_for_exception(
            session, exc, settings.eval_time, _freshness(settings), settings.task_due_hours))
    by_po = {t.exception.po_number: t for t in tasks}
    assert by_po["PO-501"].assignee_id == "HR-102"
    assert by_po["PO-506"].assignee_id == "HR-102"
    assert by_po["PO-505"].assignee_id == "HR-401"   # fallback store manager
    for t in tasks:
        assert t.status == "ASSIGNED"
        # idempotent: creating again returns the same task
        again = create_task_for_exception(session, t.exception, settings.eval_time,
                                          _freshness(settings), settings.task_due_hours)
        assert again.id == t.id
    assert len(session.scalars(select(Task)).all()) == 3
