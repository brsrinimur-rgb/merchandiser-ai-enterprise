"""
Core retail data model -- the standard structure the whole platform builds on.

Store            physical / online selling locations
Supplier         vendors who supply items
Item             Item Master (product catalogue, one row per SKU/variant)
Sale             one row per store/item/day (aggregated daily transactions)
Stock            one row per store/item/day (daily stock snapshot)
PurchaseOrder    one row per PO line (supplier -> item -> store)

This schema is intentionally close to what an ERP (e.g. Dynamics 365) already
exposes, so a real integration later is a mapping exercise, not a redesign.
"""
from sqlalchemy import (
    Column, Integer, String, Float, Date, DateTime, Text, ForeignKey, ForeignKeyConstraint
)
from sqlalchemy.orm import relationship
from database import Base


class Store(Base):
    __tablename__ = "stores"

    id = Column(Integer, primary_key=True)
    code = Column(String, unique=True, index=True)
    name = Column(String)
    city = Column(String)
    region = Column(String)


class Supplier(Base):
    __tablename__ = "suppliers"

    id = Column(Integer, primary_key=True)
    code = Column(String, unique=True, index=True)
    name = Column(String)
    lead_time_days = Column(Integer, default=14)
    reliability_score = Column(Float, default=0.9)  # 0-1, updated by supplier intelligence


class Item(Base):
    """Item Master."""
    __tablename__ = "items"

    id = Column(Integer, primary_key=True)
    item_code = Column(String, unique=True, index=True)
    item_name = Column(String)
    brand = Column(String)
    department = Column(String)
    category = Column(String, index=True)
    subcategory = Column(String)
    collection = Column(String)
    color = Column(String)
    size = Column(String)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"))
    cost = Column(Float)
    retail_price = Column(Float)
    lead_time_days = Column(Integer, default=14)
    moq = Column(Integer, default=1)
    case_pack = Column(Integer, default=1)          # order in multiples of this (carton size)
    display_min_qty = Column(Integer, default=0)    # floor stock to keep on shelf regardless of demand

    supplier = relationship("Supplier")


class Sale(Base):
    """Daily sales fact, one row per store/item/day."""
    __tablename__ = "sales"

    id = Column(Integer, primary_key=True)
    date = Column(Date, index=True)
    store_id = Column(Integer, ForeignKey("stores.id"), index=True)
    item_id = Column(Integer, ForeignKey("items.id"), index=True)
    quantity = Column(Integer)
    sales_value = Column(Float)
    discount = Column(Float, default=0)
    cost = Column(Float)
    margin = Column(Float)


class Stock(Base):
    """Daily stock snapshot, one row per store/item/day."""
    __tablename__ = "stock"

    id = Column(Integer, primary_key=True)
    date = Column(Date, index=True)
    store_id = Column(Integer, ForeignKey("stores.id"), index=True)
    item_id = Column(Integer, ForeignKey("items.id"), index=True)
    on_hand = Column(Integer)
    reserved = Column(Integer, default=0)
    in_transit = Column(Integer, default=0)
    available = Column(Integer)


class PurchaseOrder(Base):
    """One row per PO line."""
    __tablename__ = "purchase_orders"

    id = Column(Integer, primary_key=True)
    po_number = Column(String, index=True)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"))
    item_id = Column(Integer, ForeignKey("items.id"))
    store_id = Column(Integer, ForeignKey("stores.id"))
    ordered_qty = Column(Integer)
    received_qty = Column(Integer, default=0)
    balance_qty = Column(Integer)
    order_date = Column(Date)
    eta = Column(Date)
    received_date = Column(Date, nullable=True)
    status = Column(String, default="open")  # open, partial, received, cancelled


class CategoryConfig(Base):
    """Per-category settings a merchandiser/buyer can tune -- replenishment
    service level, an optional promotion window, and Open-to-Buy planning
    inputs (rolling monthly budget + closing-stock target). One row per
    category; falls back to sensible defaults if absent."""
    __tablename__ = "category_config"

    id = Column(Integer, primary_key=True)
    category = Column(String, unique=True, index=True)
    service_level_pct = Column(Float, default=95.0)     # -> z-score via lookup
    promo_start = Column(Date, nullable=True)
    promo_end = Column(Date, nullable=True)
    promo_uplift_pct = Column(Float, default=0.0)       # e.g. 40 = +40% demand during promo window
    monthly_budget = Column(Float, default=0.0)         # rolling-30-day purchase budget, cost basis; 0/None = unlimited
    closing_stock_target = Column(Float, nullable=True)  # target period-end stock value, cost basis; None = "maintain current"


class PurchaseDecision(Base):
    """One row per store/item recommendation that has entered the approval
    workflow (pulled in from the Capital Optimizer). This is the queue --
    current status, current quantity, and where it sits in the pipeline:

        pending_merchandiser -> pending_buyer -> pending_final -> po_ready
                             \\-> rejected  (from any stage)

    Every edit and every stage transition is logged as a DecisionHistory
    row, not overwritten -- that's the audit trail. When a decision reaches
    po_ready, a real PurchaseOrder row is created from it, closing the loop
    back into the operational data model."""
    __tablename__ = "purchase_decisions"

    id = Column(Integer, primary_key=True)
    store_id = Column(Integer, ForeignKey("stores.id"))
    item_id = Column(Integer, ForeignKey("items.id"))
    category = Column(String, index=True)          # denormalized for fast queue filtering/reporting

    ai_recommended_qty = Column(Integer)            # what the Capital Optimizer originally proposed
    ai_recommended_value = Column(Float)
    current_qty = Column(Integer)                   # current qty after any merchandiser/buyer edits
    current_value = Column(Float)

    source_exception_flag = Column(String, nullable=True)   # carried over from the optimizer, if any
    is_emergency = Column(String, default="false")           # "true"/"false" (kept as string for sqlite simplicity)

    status = Column(String, default="pending_merchandiser", index=True)
    otb_impact_value = Column(Float, default=0.0)   # current_value -- what this decision consumes from OTB if approved

    po_number = Column(String, nullable=True)        # set once status reaches po_ready

    created_at = Column(DateTime)
    updated_at = Column(DateTime)

    store = relationship("Store")
    item = relationship("Item")


class DecisionHistory(Base):
    """Audit trail: one immutable row per action taken on a PurchaseDecision --
    who did it, when, what changed, and why."""
    __tablename__ = "decision_history"

    id = Column(Integer, primary_key=True)
    decision_id = Column(Integer, ForeignKey("purchase_decisions.id"), index=True)

    actor = Column(String)                # free-text name/handle -- no auth system yet, see README
    stage = Column(String)                # "merchandiser" | "buyer" | "final" | "system"
    action = Column(String)               # "created" | "edit" | "approve" | "reject" | "po_created"

    from_status = Column(String)
    to_status = Column(String)
    previous_qty = Column(Integer, nullable=True)
    new_qty = Column(Integer, nullable=True)
    reason = Column(Text, nullable=True)
    otb_impact_value = Column(Float, nullable=True)

    timestamp = Column(DateTime)

    decision = relationship("PurchaseDecision")
