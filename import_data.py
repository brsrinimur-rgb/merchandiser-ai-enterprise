"""
Real-data import layer.

Accepts CSV/XLSX uploads for each core entity (stores, suppliers, items,
sales, stock, purchase orders), validates every row (required fields,
numeric/date types, foreign keys against what's already in the DB), and
reports errors *before* anything is written. Two-step flow so a merchandiser
can review before committing:

  1. validate_upload(entity, filename, content, db)
       -> parses + validates, caches the valid rows under a token, returns
          a report: {token, total_rows, valid_count, error_count, errors,
          preview}
  2. commit_upload(token, db)
       -> re-uses the cached valid rows, upserts them by natural key,
          returns {inserted, updated}

Recommended load order for a fresh environment: stores -> suppliers ->
items -> stock / purchase_orders -> sales (later entities reference the
earlier ones by code).

Cached validated batches live in an in-memory dict keyed by a UUID token.
That's fine for a single-process dev/pilot deployment; swap for
Redis or a staging table if this needs to survive restarts or run
multi-worker.
"""
import io
import time
import uuid

import pandas as pd

from models import Store, Supplier, Item, Sale, Stock, PurchaseOrder

_PENDING = {}
_PENDING_TTL_SECONDS = 3600

SCHEMAS = {
    "stores": dict(
        required=["code", "name"], optional=["city", "region"],
        numeric=[], dates=[], fk={}, key=["code"],
        label="Store Master",
    ),
    "suppliers": dict(
        required=["code", "name"], optional=["lead_time_days", "reliability_score"],
        numeric=["lead_time_days", "reliability_score"], dates=[], fk={}, key=["code"],
        label="Supplier Master",
    ),
    "items": dict(
        required=["item_code", "item_name", "category"],
        optional=["brand", "department", "subcategory", "collection", "color", "size",
                  "supplier_code", "cost", "retail_price", "lead_time_days", "moq",
                  "case_pack", "display_min_qty"],
        numeric=["cost", "retail_price", "lead_time_days", "moq", "case_pack", "display_min_qty"],
        dates=[], fk={"supplier_code": "supplier_codes"}, key=["item_code"],
        label="Item Master",
    ),
    "sales": dict(
        required=["date", "store_code", "item_code", "quantity"],
        optional=["sales_value", "discount", "cost", "margin"],
        numeric=["quantity", "sales_value", "discount", "cost", "margin"],
        dates=["date"], fk={"store_code": "store_codes", "item_code": "item_codes"},
        key=["date", "store_code", "item_code"],
        label="POS / Sales",
    ),
    "stock": dict(
        required=["date", "store_code", "item_code", "on_hand"],
        optional=["reserved", "in_transit", "available"],
        numeric=["on_hand", "reserved", "in_transit", "available"],
        dates=["date"], fk={"store_code": "store_codes", "item_code": "item_codes"},
        key=["date", "store_code", "item_code"],
        label="Stock-on-Hand",
    ),
    "purchase_orders": dict(
        required=["po_number", "supplier_code", "item_code", "store_code", "ordered_qty", "order_date"],
        optional=["received_qty", "eta", "received_date", "status"],
        numeric=["ordered_qty", "received_qty"],
        dates=["order_date", "eta", "received_date"],
        fk={"supplier_code": "supplier_codes", "item_code": "item_codes", "store_code": "store_codes"},
        key=["po_number", "item_code", "store_code"],
        label="Open Purchase Orders",
    ),
}


def entity_list():
    return [{"entity": k, "label": v["label"], "required": v["required"], "optional": v["optional"]}
            for k, v in SCHEMAS.items()]


def template_csv(entity: str) -> str:
    schema = SCHEMAS[entity]
    cols = schema["required"] + schema["optional"]
    return ",".join(cols) + "\n"


def _parse_upload(filename: str, content: bytes) -> pd.DataFrame:
    if filename.lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(io.BytesIO(content))
    else:
        df = pd.read_csv(io.BytesIO(content))
    df.columns = [str(c).strip() for c in df.columns]
    return df


def _code_sets(db):
    return {
        "store_codes": {c for (c,) in db.query(Store.code).all()},
        "supplier_codes": {c for (c,) in db.query(Supplier.code).all()},
        "item_codes": {c for (c,) in db.query(Item.item_code).all()},
    }


def _clean_cell(v):
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, str) and v.strip() == "":
        return None
    return v


def validate_dataframe(entity: str, df: pd.DataFrame, code_sets: dict):
    schema = SCHEMAS[entity]
    present = set(df.columns)
    missing_cols = [c for c in schema["required"] if c not in present]
    if missing_cols:
        return [], [{
            "row": 0, "field": ", ".join(missing_cols),
            "message": f"Missing required column(s): {', '.join(missing_cols)}. "
                       f"Download the template to see the expected headers.",
        }]

    all_cols = schema["required"] + schema["optional"]
    valid_rows, errors = [], []

    for idx, raw in df.iterrows():
        rowno = int(idx) + 2  # +1 for 0-index, +1 for header row
        row = {}
        for col in all_cols:
            row[col] = _clean_cell(raw[col]) if col in df.columns else None

        row_errors = []
        for col in schema["required"]:
            if row[col] is None:
                row_errors.append({"row": rowno, "field": col, "message": "required value is missing"})

        for col in schema.get("numeric", []):
            if row[col] is not None:
                try:
                    row[col] = float(row[col])
                except (TypeError, ValueError):
                    row_errors.append({"row": rowno, "field": col, "message": f"'{row[col]}' is not numeric"})
                    row[col] = None

        for col in schema.get("dates", []):
            if row[col] is not None:
                try:
                    row[col] = pd.to_datetime(row[col]).date()
                except Exception:
                    row_errors.append({"row": rowno, "field": col, "message": f"'{row[col]}' is not a valid date"})
                    row[col] = None

        for col, set_name in schema.get("fk", {}).items():
            val = row.get(col)
            if val is not None and val not in code_sets.get(set_name, set()):
                ref = set_name.replace("_codes", "")
                row_errors.append({
                    "row": rowno, "field": col,
                    "message": f"'{val}' not found in {ref} master -- upload {ref} master first",
                })

        if row_errors:
            errors.extend(row_errors)
        else:
            valid_rows.append(row)

    return valid_rows, errors


def validate_upload(entity: str, filename: str, content: bytes, db):
    if entity not in SCHEMAS:
        raise ValueError(f"Unknown entity '{entity}'. Valid: {', '.join(SCHEMAS)}")

    df = _parse_upload(filename, content)
    code_sets = _code_sets(db)
    valid_rows, errors = validate_dataframe(entity, df, code_sets)

    token = str(uuid.uuid4())
    _PENDING[token] = {"entity": entity, "rows": valid_rows, "ts": time.time()}
    _gc_pending()

    return {
        "token": token,
        "entity": entity,
        "total_rows": len(df),
        "valid_count": len(valid_rows),
        "error_count": len(errors),
        "errors": errors[:500],
        "preview": valid_rows[:20],
    }


def _gc_pending():
    now = time.time()
    dead = [t for t, v in _PENDING.items() if now - v["ts"] > _PENDING_TTL_SECONDS]
    for t in dead:
        _PENDING.pop(t, None)


# --------------------------------------------------------------------------
# Commit (upsert) -- one function per entity, they share the same shape
# --------------------------------------------------------------------------

def _upsert_master(db, model, rows, key_fields, field_map, defaults=None):
    defaults = defaults or {}
    inserted = updated = 0
    for r in rows:
        filters = {kf: r[kf] for kf in key_fields}
        existing = db.query(model).filter_by(**filters).first()
        data = {}
        for src, dest in field_map.items():
            val = r.get(src)
            if val is None and dest in defaults:
                val = defaults[dest]
            data[dest] = val
        if existing:
            for k, v in data.items():
                if v is not None:
                    setattr(existing, k, v)
            updated += 1
        else:
            db.add(model(**data))
            inserted += 1
    db.commit()
    return {"inserted": inserted, "updated": updated}


def _commit_stores(db, rows):
    return _upsert_master(db, Store, rows, ["code"],
                           {"code": "code", "name": "name", "city": "city", "region": "region"})


def _commit_suppliers(db, rows):
    return _upsert_master(db, Supplier, rows, ["code"],
                           {"code": "code", "name": "name", "lead_time_days": "lead_time_days",
                            "reliability_score": "reliability_score"},
                           defaults={"lead_time_days": 14, "reliability_score": 0.9})


def _commit_items(db, rows):
    supplier_map = {c: sid for c, sid in db.query(Supplier.code, Supplier.id).all()}
    inserted = updated = 0
    for r in rows:
        existing = db.query(Item).filter(Item.item_code == r["item_code"]).first()
        data = dict(
            item_code=r["item_code"], item_name=r["item_name"], category=r["category"],
            brand=r.get("brand"), department=r.get("department"), subcategory=r.get("subcategory"),
            collection=r.get("collection"), color=r.get("color"), size=r.get("size"),
            supplier_id=supplier_map.get(r.get("supplier_code")),
            cost=r.get("cost"), retail_price=r.get("retail_price"),
            lead_time_days=int(r["lead_time_days"]) if r.get("lead_time_days") is not None else 14,
            moq=int(r["moq"]) if r.get("moq") is not None else 1,
            case_pack=int(r["case_pack"]) if r.get("case_pack") is not None else 1,
            display_min_qty=int(r["display_min_qty"]) if r.get("display_min_qty") is not None else 0,
        )
        if existing:
            for k, v in data.items():
                setattr(existing, k, v)
            updated += 1
        else:
            db.add(Item(**data))
            inserted += 1
    db.commit()
    return {"inserted": inserted, "updated": updated}


def _commit_sales(db, rows):
    store_map = {c: sid for c, sid in db.query(Store.code, Store.id).all()}
    item_lookup = {c: (iid, cost, price) for c, iid, cost, price in
                   db.query(Item.item_code, Item.id, Item.cost, Item.retail_price).all()}
    inserted = updated = 0
    for r in rows:
        store_id = store_map[r["store_code"]]
        item_id, item_cost, item_price = item_lookup[r["item_code"]]
        qty = r["quantity"]
        sales_value = r.get("sales_value") if r.get("sales_value") is not None else qty * (item_price or 0)
        cost_value = r.get("cost") if r.get("cost") is not None else qty * (item_cost or 0)
        discount = r.get("discount") or 0
        margin = r.get("margin") if r.get("margin") is not None else sales_value - cost_value

        existing = db.query(Sale).filter(
            Sale.date == r["date"], Sale.store_id == store_id, Sale.item_id == item_id).first()
        data = dict(date=r["date"], store_id=store_id, item_id=item_id, quantity=int(qty),
                    sales_value=sales_value, discount=discount, cost=cost_value, margin=margin)
        if existing:
            for k, v in data.items():
                setattr(existing, k, v)
            updated += 1
        else:
            db.add(Sale(**data))
            inserted += 1
    db.commit()
    return {"inserted": inserted, "updated": updated}


def _commit_stock(db, rows):
    store_map = {c: sid for c, sid in db.query(Store.code, Store.id).all()}
    item_map = {c: iid for c, iid in db.query(Item.item_code, Item.id).all()}
    inserted = updated = 0
    for r in rows:
        store_id = store_map[r["store_code"]]
        item_id = item_map[r["item_code"]]
        on_hand = r["on_hand"]
        reserved = r.get("reserved") or 0
        in_transit = r.get("in_transit") or 0
        available = r.get("available") if r.get("available") is not None else on_hand - reserved

        existing = db.query(Stock).filter(
            Stock.date == r["date"], Stock.store_id == store_id, Stock.item_id == item_id).first()
        data = dict(date=r["date"], store_id=store_id, item_id=item_id, on_hand=int(on_hand),
                    reserved=int(reserved), in_transit=int(in_transit), available=int(available))
        if existing:
            for k, v in data.items():
                setattr(existing, k, v)
            updated += 1
        else:
            db.add(Stock(**data))
            inserted += 1
    db.commit()
    return {"inserted": inserted, "updated": updated}


def _commit_purchase_orders(db, rows):
    supplier_map = {c: sid for c, sid in db.query(Supplier.code, Supplier.id).all()}
    item_map = {c: iid for c, iid in db.query(Item.item_code, Item.id).all()}
    store_map = {c: sid for c, sid in db.query(Store.code, Store.id).all()}
    inserted = updated = 0
    for r in rows:
        ordered_qty = int(r["ordered_qty"])
        received_qty = int(r.get("received_qty") or 0)
        existing = db.query(PurchaseOrder).filter(
            PurchaseOrder.po_number == r["po_number"],
            PurchaseOrder.item_id == item_map[r["item_code"]],
            PurchaseOrder.store_id == store_map[r["store_code"]],
        ).first()
        data = dict(
            po_number=r["po_number"], supplier_id=supplier_map[r["supplier_code"]],
            item_id=item_map[r["item_code"]], store_id=store_map[r["store_code"]],
            ordered_qty=ordered_qty, received_qty=received_qty, balance_qty=ordered_qty - received_qty,
            order_date=r["order_date"], eta=r.get("eta"), received_date=r.get("received_date"),
            status=r.get("status") or ("received" if received_qty >= ordered_qty and received_qty > 0 else "open"),
        )
        if existing:
            for k, v in data.items():
                setattr(existing, k, v)
            updated += 1
        else:
            db.add(PurchaseOrder(**data))
            inserted += 1
    db.commit()
    return {"inserted": inserted, "updated": updated}


_COMMIT_FN = {
    "stores": _commit_stores,
    "suppliers": _commit_suppliers,
    "items": _commit_items,
    "sales": _commit_sales,
    "stock": _commit_stock,
    "purchase_orders": _commit_purchase_orders,
}


def commit_upload(token: str, db):
    pending = _PENDING.get(token)
    if not pending:
        raise ValueError("Unknown or expired import token -- re-run validation.")
    entity, rows = pending["entity"], pending["rows"]
    if not rows:
        _PENDING.pop(token, None)
        return {"entity": entity, "inserted": 0, "updated": 0, "message": "No valid rows to commit."}
    result = _COMMIT_FN[entity](db, rows)
    _PENDING.pop(token, None)
    return {"entity": entity, **result}
