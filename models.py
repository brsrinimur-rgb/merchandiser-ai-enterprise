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
    Column, Integer, String, Float, Date, ForeignKey, ForeignKeyConstraint
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
    """Per-category replenishment settings a merchandiser can tune -- service
    level target and an optional promotion window with an uplift multiplier.
    One row per category; falls back to global defaults if absent."""
    __tablename__ = "category_config"

    id = Column(Integer, primary_key=True)
    category = Column(String, unique=True, index=True)
    service_level_pct = Column(Float, default=95.0)     # -> z-score via lookup
    promo_start = Column(Date, nullable=True)
    promo_end = Column(Date, nullable=True)
    promo_uplift_pct = Column(Float, default=0.0)       # e.g. 40 = +40% demand during promo window
