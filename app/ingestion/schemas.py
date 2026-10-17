"""Typed row validation for each dataset (pydantic v2)."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, field_validator


class _AwareDatetime(BaseModel):
    @field_validator("*", mode="after")
    @classmethod
    def _noop(cls, v):
        return v


def _aware(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("timestamp must carry an explicit UTC offset")
    return v


class ProductAliasRow(BaseModel):
    source_system: str
    external_item_id: str
    canonical_product_id: str
    source_uom: str
    base_uom: str
    to_base_factor: Decimal


class LocationAliasRow(BaseModel):
    source_system: str
    external_location_id: str
    canonical_location_id: str
    region_id: str


class POLineRow(BaseModel):
    source_record_id: str
    po_number: str
    line_number: int
    supplier_code: str
    location_code: str
    item_code: str
    ordered_qty: Decimal
    ordered_uom: str
    status: str
    expected_receipt_by: datetime

    _check = field_validator("expected_receipt_by")(_aware)


class ReceiptLineRow(BaseModel):
    source_record_id: str
    receipt_number: str
    receipt_line: int
    po_number: str
    po_line_number: int
    location_code: str
    product_ref: str
    accepted_qty: Decimal
    uom: str
    received_at: datetime
    posting_status: str

    _check = field_validator("received_at")(_aware)


class EmployeeRow(BaseModel):
    source_employee_id: str
    display_name: str
    employment_status: str
    role: str
    location_code: str | None = None
    manager_employee_id: str | None = None
    region_id: str | None = None


class WorkforceStatusRow(BaseModel):
    source_employee_id: str
    availability_status: str
    shift_start: datetime
    shift_end: datetime
    observed_at: datetime

    _c1 = field_validator("shift_start")(_aware)
    _c2 = field_validator("shift_end")(_aware)
    _c3 = field_validator("observed_at")(_aware)


DATASET_SCHEMAS = {
    "product_aliases": ProductAliasRow,
    "location_aliases": LocationAliasRow,
    "purchase_order_lines": POLineRow,
    "goods_receipt_lines": ReceiptLineRow,
    "employees": EmployeeRow,
    "workforce_status": WorkforceStatusRow,
}
