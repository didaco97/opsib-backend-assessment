"""Idempotency: repeat imports and repeated detection change nothing."""
from sqlalchemy import func, select

from app.db.models import OpsException, POLine, ReceiptLine, Task
from app.detection.shortfall import run_detection
from app.ingestion.service import import_fixture_pack


def test_double_import_no_double_count(loaded, session, settings):
    res1 = run_detection(session, settings.eval_time)
    assert len(res1.created) == 3

    # Import the exact same files again.
    results = import_fixture_pack(session, settings.fixtures_dir, settings.eval_time)
    by = {r.dataset: r for r in results}
    # All 7 physical rows (6 unique + 1 in-file duplicate) are already known.
    assert by["purchase_order_lines"].accepted == 0
    assert by["purchase_order_lines"].duplicates == 7
    assert by["goods_receipt_lines"].accepted == 0
    assert by["goods_receipt_lines"].duplicates == 7

    # Business facts unchanged.
    assert session.scalar(select(func.count()).select_from(POLine)) == 6
    assert session.scalar(select(func.count()).select_from(ReceiptLine)) == 6

    # Detection re-run creates nothing new.
    res2 = run_detection(session, settings.eval_time)
    assert res2.created == []
    assert len(res2.existing) == 3
    assert session.scalar(select(func.count()).select_from(OpsException)) == 3
