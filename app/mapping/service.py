"""Canonical mapping: resolves source identifiers to canonical identities
and converts quantities into the declared base UOM.

Mapping is data-driven (the alias tables), never name similarity.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from app.db.models import LocationAlias, ProductAlias


def resolve_product(session: Session, source_system: str, external_item_id: str) -> ProductAlias | None:
    return session.get(ProductAlias, (source_system, external_item_id))


def resolve_location(session: Session, source_system: str, external_location_id: str) -> LocationAlias | None:
    if not external_location_id:
        return None
    return session.get(LocationAlias, (source_system, external_location_id))


def to_base_qty(qty: Decimal, uom: str, alias: ProductAlias) -> Decimal:
    """Convert a source quantity into the product's base UOM.

    Raises ValueError if the row's UOM disagrees with the alias row's declared
    source UOM: we never guess a conversion the mapping data does not declare.
    """
    if alias is None:
        raise ValueError("no product alias")
    if uom != alias.source_uom:
        raise ValueError(
            f"uom mismatch: row is {uom}, alias for {alias.external_item_id} declares {alias.source_uom}"
        )
    return Decimal(qty) * Decimal(alias.to_base_factor)
