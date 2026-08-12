"""D365 sync orchestration and safe upserts into the existing application model."""
from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from models import Store, Supplier, Item, Sale, Stock, PurchaseOrder, PurchaseDecision

from .client import D365Client
from .config import ENTITY_NAMES, get_settings
from .mappings import MAPPERS, REQUIRED_KEYS


class D365SyncError(RuntimeError):
    pass


def _validate_rows(entity: str, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    valid, errors = [], []
    required = REQUIRED_KEYS[entity]
    for index, row in enumerate(rows, start=1):
        missing = [key for key in required if row.get(key) in (None, "")]
        if missing:
            errors.append({"row": index, "missing": missing, "data": row})
        else:
            valid.append(row)
    return valid, errors


def fetch_and_map(entity: str, *, top: int | None = None, filter_expression: str | None = None) -> dict[str, Any]:
    if entity not in MAPPERS:
        raise D365SyncError(f"Unsupported entity '{entity}'")
    settings = get_settings()
    missing = settings.missing()
    if missing:
        raise D365SyncError(f"Missing D365 configuration: {', '.join(missing)}")
    source_entity = ENTITY_NAMES[entity]
    client = D365Client(settings)
    raw = client.get_entity(source_entity, top=top, filter_expression=filter_expression)
    mapped = [MAPPERS[entity](row) for row in raw]
    valid, errors = _validate_rows(entity, mapped)
    return {
        "entity": entity,
        "source_entity": source_entity,
        "fetched": len(raw),
        "valid": len(valid),
        "invalid": len(errors),
        "rows": valid,
        "errors": errors[:100],
    }


def _upsert_store(db: Session, row: dict[str, Any]) -> str:
    obj = db.query(Store).filter(Store.code == row["code"]).first()
    state = "updated" if obj else "inserted"
    if not obj:
        obj = Store(code=row["code"])
        db.add(obj)
    obj.name, obj.city, obj.region = row["name"], row.get("city"), row.get("region")
    return state


def _upsert_supplier(db: Session, row: dict[str, Any]) -> str:
    obj = db.query(Supplier).filter(Supplier.code == row["code"]).first()
    state = "updated" if obj else "inserted"
    if not obj:
        obj = Supplier(code=row["code"])
        db.add(obj)
    obj.name = row["name"]
    obj.lead_time_days = row.get("lead_time_days", 14)
    obj.reliability_score = row.get("reliability_score", 0.9)
    return state


def _upsert_item(db: Session, row: dict[str, Any]) -> str:
    supplier = None
    if row.get("supplier_code"):
        supplier = db.query(Supplier).filter(Supplier.code == row["supplier_code"]).first()
    obj = db.query(Item).filter(Item.item_code == row["item_code"]).first()
    state = "updated" if obj else "inserted"
    if not obj:
        obj = Item(item_code=row["item_code"])
        db.add(obj)
    for key in ["item_name", "brand", "department", "category", "subcategory", "collection", "color", "size",
                "cost", "retail_price", "lead_time_days", "moq", "case_pack", "display_min_qty"]:
        setattr(obj, key, row.get(key))
    if supplier:
        obj.supplier_id = supplier.id
    return state


def _resolve_store_item(db: Session, row: dict[str, Any]) -> tuple[Store, Item]:
    store = db.query(Store).filter(Store.code == row["store_code"]).first()
    item = db.query(Item).filter(Item.item_code == row["item_code"]).first()
    if not store or not item:
        raise D365SyncError(f"Master missing for store={row['store_code']} item={row['item_code']}")
    return store, item


def _upsert_sale(db: Session, row: dict[str, Any]) -> str:
    store, item = _resolve_store_item(db, row)
    obj = db.query(Sale).filter(Sale.date == row["date"], Sale.store_id == store.id, Sale.item_id == item.id).first()
    state = "updated" if obj else "inserted"
    if not obj:
        obj = Sale(date=row["date"], store_id=store.id, item_id=item.id)
        db.add(obj)
    obj.quantity = int(row.get("quantity", 0))
    obj.sales_value = float(row.get("sales_value", 0))
    obj.discount = float(row.get("discount", 0))
    obj.cost = float(row.get("cost", 0))
    obj.margin = float(row.get("margin", obj.sales_value - obj.cost))
    return state


def _upsert_stock(db: Session, row: dict[str, Any]) -> str:
    store, item = _resolve_store_item(db, row)
    obj = db.query(Stock).filter(Stock.date == row["date"], Stock.store_id == store.id, Stock.item_id == item.id).first()
    state = "updated" if obj else "inserted"
    if not obj:
        obj = Stock(date=row["date"], store_id=store.id, item_id=item.id)
        db.add(obj)
    obj.on_hand = int(row.get("on_hand", 0))
    obj.reserved = int(row.get("reserved", 0))
    obj.in_transit = int(row.get("in_transit", 0))
    obj.available = int(row.get("available", obj.on_hand - obj.reserved))
    return state


def _upsert_po(db: Session, row: dict[str, Any]) -> str:
    store, item = _resolve_store_item(db, row)
    supplier = db.query(Supplier).filter(Supplier.code == row["supplier_code"]).first()
    if not supplier:
        raise D365SyncError(f"Supplier missing: {row['supplier_code']}")
    obj = db.query(PurchaseOrder).filter(
        PurchaseOrder.po_number == row["po_number"],
        PurchaseOrder.item_id == item.id,
        PurchaseOrder.store_id == store.id,
    ).first()
    state = "updated" if obj else "inserted"
    if not obj:
        obj = PurchaseOrder(po_number=row["po_number"], item_id=item.id, store_id=store.id)
        db.add(obj)
    ordered = int(row.get("ordered_qty", 0))
    received = int(row.get("received_qty", 0))
    obj.supplier_id = supplier.id
    obj.ordered_qty = ordered
    obj.received_qty = received
    obj.balance_qty = max(0, ordered - received)
    obj.order_date = row.get("order_date") or date.today()
    obj.eta = row.get("eta")
    obj.received_date = row.get("received_date")
    obj.status = row.get("status") or ("received" if received >= ordered and ordered > 0 else "open")
    return state


_UPSERT = {
    "stores": _upsert_store,
    "suppliers": _upsert_supplier,
    "items": _upsert_item,
    "sales": _upsert_sale,
    "stock": _upsert_stock,
    "purchase_orders": _upsert_po,
}


def sync_entity(db: Session, entity: str, *, dry_run: bool = True, top: int | None = None,
                filter_expression: str | None = None) -> dict[str, Any]:
    result = fetch_and_map(entity, top=top, filter_expression=filter_expression)
    if dry_run:
        return {**result, "dry_run": True, "preview": result.pop("rows")[:25]}

    inserted = updated = failed = 0
    failures: list[dict[str, Any]] = []
    for row in result["rows"]:
        try:
            state = _UPSERT[entity](db, row)
            inserted += state == "inserted"
            updated += state == "updated"
        except Exception as exc:
            failed += 1
            failures.append({"error": str(exc), "row": row})
    if failed:
        db.rollback()
        raise D365SyncError(f"Sync aborted; {failed} row(s) failed. First error: {failures[0]['error']}")
    db.commit()
    return {
        "entity": entity,
        "source_entity": result["source_entity"],
        "dry_run": False,
        "fetched": result["fetched"],
        "inserted": inserted,
        "updated": updated,
        "invalid": result["invalid"],
        "validation_errors": result["errors"],
    }


def build_purchase_order_payload(db: Session, decision_id: int) -> dict[str, Any]:
    """Build a D365 PO write-back payload from an approved decision.

    The field names are a template. Confirm your D365 legal entity, vendor,
    warehouse, site, currency, delivery terms and number-sequence policy before
    enabling POST in production.
    """
    decision = db.query(PurchaseDecision).filter(PurchaseDecision.id == decision_id).first()
    if not decision:
        raise D365SyncError(f"Decision {decision_id} not found")
    if decision.status != "po_ready":
        raise D365SyncError("Only po_ready decisions can be prepared for D365 write-back")
    item, store = decision.item, decision.store
    supplier = item.supplier if item else None
    if not item or not store or not supplier:
        raise D365SyncError("Decision is missing item, store or supplier master data")
    return {
        "header": {
            "dataAreaId": get_settings().company,
            "OrderVendorAccountNumber": supplier.code,
            "PurchaseOrderName": f"Merchandiser AI decision {decision.id}",
            "DeliveryWarehouseId": store.code,
        },
        "line": {
            "dataAreaId": get_settings().company,
            "PurchaseOrderNumber": decision.po_number,
            "ItemNumber": item.item_code,
            "OrderedPurchaseQuantity": decision.current_qty,
            "ReceivingWarehouseId": store.code,
            "PurchasePrice": item.cost,
        },
        "warning": "Template only: validate required D365 fields and number-sequence behavior before enabling live write-back.",
    }
