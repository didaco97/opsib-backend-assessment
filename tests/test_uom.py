"""Unit tests: UOM conversion and mapping rules."""
from decimal import Decimal

import pytest

from app.db.models import ProductAlias
from app.mapping.service import to_base_qty


def alias(factor, source_uom="BOX", base_uom="EA"):
    return ProductAlias(source_system="ERP", external_item_id="ERP-BOX-12",
                        canonical_product_id="SKU-002", source_uom=source_uom,
                        base_uom=base_uom, to_base_factor=factor, import_id="t")


def test_box_converts_to_ea():
    assert to_base_qty(Decimal("10"), "BOX", alias(12)) == Decimal("120")


def test_factor_one_is_identity():
    assert to_base_qty(Decimal("96"), "EA", alias(1, "EA")) == Decimal("96")


def test_uom_mismatch_is_rejected_not_guessed():
    with pytest.raises(ValueError, match="uom mismatch"):
        to_base_qty(Decimal("10"), "EA", alias(12, "BOX"))


def test_missing_alias_is_rejected():
    with pytest.raises(ValueError, match="no product alias"):
        to_base_qty(Decimal("10"), "EA", None)
