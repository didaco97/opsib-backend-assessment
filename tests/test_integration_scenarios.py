"""Integration: load the real fixture pack, verify the six required outcomes."""
from decimal import Decimal

from sqlalchemy import select

from app.db.models import OpsException, Task
from app.detection.shortfall import run_detection


def test_fixture_import_counts(loaded):
    by_dataset = {r.dataset: r for r in loaded}
    # PO file: 7 rows, one identical duplicate -> 6 accepted, 1 duplicate
    po = by_dataset["purchase_order_lines"]
    assert (po.accepted, po.duplicates, po.rejected) == (6, 1, 0)
    # receipts: 7 rows, one identical duplicate -> 6 accepted, 1 duplicate
    rc = by_dataset["goods_receipt_lines"]
    assert (rc.accepted, rc.duplicates, rc.rejected) == (6, 1, 0)
    for ds in ("product_aliases", "location_aliases", "employees", "workforce_status"):
        assert by_dataset[ds].rejected == 0, ds


def test_required_scenario_outcomes(loaded, session, settings):
    res = run_detection(session, settings.eval_time)
    assert len(res.created) == 3
    excs = {e.po_number: e for e in session.scalars(select(OpsException)).all()}

    e501 = excs["PO-501"]   # 100 ordered, 60+32 posted -> 8 short
    assert Decimal(e501.variance) == Decimal("8") and e501.base_uom == "EA"
    e505 = excs["PO-505"]   # 10 BOX = 120 EA expected, 96 EA posted -> 24 short
    assert Decimal(e505.expected_qty) == Decimal("120")
    assert Decimal(e505.variance) == Decimal("24")
    assert e505.canonical_location_id == "STORE-015"
    e506 = excs["PO-506"]   # duplicate receipt source ID counted once -> 10 short
    assert Decimal(e506.received_qty) == Decimal("10")
    assert Decimal(e506.variance) == Decimal("10")

    # PO-502 fully received: no exception. PO-503 deadline future: skipped.
    # PO-504 cancelled: skipped.
    assert "PO-502" not in excs and "PO-503" not in excs and "PO-504" not in excs
    reasons = {s["po_line"].split("/")[0]: s["reason"] for s in res.skipped}
    assert "deadline" in reasons["PO-503"]
    assert "CANCELLED" in reasons["PO-504"]

    # evidence + neutrality
    assert e506.evidence_refs["receipt_source_record_ids"] == ["WMS-GRN506-A-L1"]
    assert "not proven supplier fault" in e501.explanation
    assert e501.evidence_refs["rule_version"] == "receipt-shortfall/v1"
