"""Editable D365-to-Merchandiser mappings.

D365 entity and field names differ by implementation/customization. These
aliases deliberately accept several common names. Adjust this file after
checking your environment's /data/$metadata or Data Management entities.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable


def first(row: dict[str, Any], names: Iterable[str], default: Any = None) -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return default


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def as_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def map_store(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": str(first(row, ["StoreNumber", "RetailStoreId", "StoreId", "WarehouseId", "InventLocationId"], "")).strip(),
        "name": str(first(row, ["StoreName", "Name", "Description"], "")).strip(),
        "city": first(row, ["City", "AddressCity"]),
        "region": first(row, ["Region", "State", "CountryRegionId"]),
    }


def map_supplier(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": str(first(row, ["VendorAccountNumber", "VendorAccount", "AccountNum"], "")).strip(),
        "name": str(first(row, ["VendorOrganizationName", "VendorName", "Name"], "")).strip(),
        "lead_time_days": as_int(first(row, ["DefaultDeliveryDays", "LeadTimeDays"], 14), 14),
        "reliability_score": as_float(first(row, ["ReliabilityScore"], 0.9), 0.9),
    }


def map_item(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "item_code": str(first(row, ["ItemNumber", "ProductNumber", "ItemId"], "")).strip(),
        "item_name": str(first(row, ["ProductName", "SearchName", "ItemName", "Name"], "")).strip(),
        "brand": first(row, ["BrandId", "Brand", "RetailBrand"]),
        "department": first(row, ["RetailDepartmentName", "Department", "ProductDimensionGroupName"]),
        "category": first(row, ["RetailProductCategoryName", "CategoryName", "Category"], "Unassigned"),
        "subcategory": first(row, ["RetailProductSubcategoryName", "Subcategory"]),
        "collection": first(row, ["Collection", "Season"]),
        "color": first(row, ["ProductColorId", "ColorId", "Color"]),
        "size": first(row, ["ProductSizeId", "SizeId", "Size"]),
        "supplier_code": first(row, ["PrimaryVendorAccountNumber", "VendorAccountNumber", "PrimaryVendor"]),
        "cost": as_float(first(row, ["ProductCost", "CostPrice", "StandardCost"], 0)),
        "retail_price": as_float(first(row, ["RetailPrice", "SalesPrice", "Price"], 0)),
        "lead_time_days": as_int(first(row, ["PurchaseLeadTimeDays", "LeadTimeDays"], 14), 14),
        "moq": max(1, as_int(first(row, ["MinimumOrderQuantity", "MOQ"], 1), 1)),
        "case_pack": max(1, as_int(first(row, ["MultipleQty", "CasePack", "OrderMultiple"], 1), 1)),
        "display_min_qty": max(0, as_int(first(row, ["DisplayMinimumQuantity", "DisplayMinQty"], 0), 0)),
    }


def map_stock(row: dict[str, Any]) -> dict[str, Any]:
    on_hand = as_int(first(row, ["AvailableOnHandQuantity", "OnHandQuantity", "PhysicalInventory"], 0))
    reserved = as_int(first(row, ["ReservedOnHandQuantity", "ReservedQuantity", "ReservPhysical"], 0))
    return {
        "date": as_date(first(row, ["AsOfDate", "SnapshotDate", "ModifiedDateTime"])) or date.today(),
        "store_code": str(first(row, ["StoreNumber", "WarehouseId", "InventoryWarehouseId", "InventLocationId"], "")).strip(),
        "item_code": str(first(row, ["ItemNumber", "ProductNumber", "ItemId"], "")).strip(),
        "on_hand": on_hand,
        "reserved": reserved,
        "in_transit": as_int(first(row, ["OrderedInTotalQuantity", "InTransitQuantity", "OrderedSum"], 0)),
        "available": as_int(first(row, ["AvailableOnHandQuantity", "AvailableQuantity"], on_hand - reserved)),
    }


def map_sale(row: dict[str, Any]) -> dict[str, Any]:
    qty = as_int(first(row, ["Quantity", "SalesQuantity", "Qty"], 0))
    sales_value = as_float(first(row, ["NetAmount", "SalesAmount", "LineAmount"], 0))
    cost = as_float(first(row, ["CostAmount", "CostValue"], 0))
    return {
        "date": as_date(first(row, ["TransactionDate", "BusinessDate", "Date"])) or date.today(),
        "store_code": str(first(row, ["StoreNumber", "RetailStoreId", "StoreId"], "")).strip(),
        "item_code": str(first(row, ["ItemNumber", "ProductNumber", "ItemId"], "")).strip(),
        "quantity": qty,
        "sales_value": sales_value,
        "discount": as_float(first(row, ["DiscountAmount", "TotalDiscount"], 0)),
        "cost": cost,
        "margin": as_float(first(row, ["MarginAmount"], sales_value - cost)),
    }


def map_purchase_order(row: dict[str, Any]) -> dict[str, Any]:
    ordered = as_int(first(row, ["OrderedPurchaseQuantity", "OrderedQuantity", "PurchaseQuantity"], 0))
    received = as_int(first(row, ["ReceivedPurchaseQuantity", "ReceivedQuantity"], 0))
    return {
        "po_number": str(first(row, ["PurchaseOrderNumber", "PurchaseOrderId", "PurchId"], "")).strip(),
        "supplier_code": str(first(row, ["OrderVendorAccountNumber", "VendorAccountNumber", "OrderAccount"], "")).strip(),
        "item_code": str(first(row, ["ItemNumber", "ProductNumber", "ItemId"], "")).strip(),
        "store_code": str(first(row, ["ReceivingWarehouseId", "WarehouseId", "InventLocationId"], "")).strip(),
        "ordered_qty": ordered,
        "received_qty": received,
        "order_date": as_date(first(row, ["OrderDate", "PurchaseOrderDate", "CreatedDateTime"])) or date.today(),
        "eta": as_date(first(row, ["ConfirmedDeliveryDate", "RequestedDeliveryDate", "DeliveryDate"])),
        "received_date": as_date(first(row, ["ReceiptDate", "ReceivedDate"])),
        "status": str(first(row, ["PurchaseOrderStatus", "DocumentStatus", "Status"], "open")).lower(),
    }


MAPPERS = {
    "stores": map_store,
    "suppliers": map_supplier,
    "items": map_item,
    "sales": map_sale,
    "stock": map_stock,
    "purchase_orders": map_purchase_order,
}

REQUIRED_KEYS = {
    "stores": ["code", "name"],
    "suppliers": ["code", "name"],
    "items": ["item_code", "item_name", "category"],
    "sales": ["date", "store_code", "item_code", "quantity"],
    "stock": ["date", "store_code", "item_code", "on_hand"],
    "purchase_orders": ["po_number", "supplier_code", "item_code", "store_code", "ordered_qty", "order_date"],
}
