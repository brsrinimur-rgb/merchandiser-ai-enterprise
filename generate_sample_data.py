"""
Synthetic retail data generator.

Since we don't yet have a real ERP/POS feed, this script builds a realistic
multi-store, multi-category dataset so the whole platform (replenishment,
stock intelligence, forecasting, etc.) can be built and demoed against
something that behaves like real retail data:

- Items span fashion (color/size variants), footwear, accessories, home,
  fragrance -- so variant intelligence has something to chew on.
- Each item is tagged with a velocity archetype (fast / medium / slow / dead)
  so stock intelligence and markdown logic produce meaningful output.
- Categories are tagged growing / stable / declining so category management
  and sales-growth metrics are meaningful.
- A promo window (mirroring an Eid-style peak) is injected so forecasting
  and promotion-lift logic has a real signal to find.
- Purchase orders include some late/short deliveries so supplier
  intelligence has something to score.

Run:  python generate_sample_data.py
Re-running wipes and rebuilds the DB from scratch.
"""
import random
from datetime import date, timedelta

import numpy as np

from database import engine, Base, SessionLocal
from models import Store, Supplier, Item, Sale, Stock, PurchaseOrder, CategoryConfig

random.seed(42)
np.random.seed(42)

TODAY = date(2026, 7, 26)
HISTORY_DAYS = 90
START_DATE = TODAY - timedelta(days=HISTORY_DAYS)

STORES = [
    ("STR-RYP", "Riyadh Park", "Riyadh", "Central"),
    ("STR-FAI", "Faisaliah", "Riyadh", "Central"),
    ("STR-RSM", "Red Sea Mall", "Jeddah", "West"),
    ("STR-COR", "Jeddah Corniche", "Jeddah", "West"),
]

SUPPLIERS = [
    ("SUP-01", "Al Nakheel Textiles", 21, 0.90),
    ("SUP-02", "Gulf Footwear Co.", 30, 0.82),
    ("SUP-03", "Levant Fashion House", 18, 0.93),
    ("SUP-04", "Horizon Accessories", 25, 0.88),
    ("SUP-05", "Noor Home Goods", 35, 0.80),
    ("SUP-06", "Aroma Fragrance Ltd.", 14, 0.95),
    ("SUP-07", "Metro Apparel Group", 20, 0.86),
    ("SUP-08", "Falcon Bags & Leather", 28, 0.84),
]

# category -> (department, subcategories, supplier codes, has_variants, trend)
# trend: growing / stable / declining -- drives a slope applied over the 90 days
CATEGORIES = {
    "Men's Apparel":     ("Apparel",     ["Shirts", "Trousers", "T-Shirts"], ["SUP-01", "SUP-07"], True,  "growing"),
    "Women's Apparel":   ("Apparel",     ["Dresses", "Abayas", "Tops"],       ["SUP-03", "SUP-07"], True,  "declining"),
    "Footwear":          ("Footwear",    ["Sneakers", "Formal", "Sandals"],  ["SUP-02"],            True,  "stable"),
    "Accessories":       ("Accessories", ["Belts", "Sunglasses", "Watches"], ["SUP-04"],            False, "growing"),
    "Bags":              ("Accessories", ["Handbags", "Backpacks"],          ["SUP-08"],            False, "stable"),
    "Home":              ("Home",        ["Decor", "Textiles"],              ["SUP-05"],            False, "declining"),
    "Fragrance":         ("Beauty",      ["Perfume", "Gift Sets"],           ["SUP-06"],            False, "growing"),
}

BRANDS = ["Aurel", "Vantora", "Nordane", "Solstice", "Marbel", "Kestrel", "Halcyon", "Verita"]
COLORS = ["Black", "White", "Navy", "Beige", "Red", "Olive", "Grey"]
SIZES = ["XS", "S", "M", "L", "XL", "XXL"]
SHOE_SIZES = ["39", "40", "41", "42", "43", "44"]

# Promo window mirrors an Eid-style demand spike
PROMO_START = TODAY - timedelta(days=25)
PROMO_END = TODAY - timedelta(days=15)

VELOCITY_PROFILES = {
    # name: (weight among items, base lambda per store per day)
    "fast":   (0.15, 3.2),
    "medium": (0.40, 1.1),
    "slow":   (0.30, 0.35),
    "dead":   (0.15, 0.03),
}


def weighted_choice(options):
    names = list(options.keys())
    weights = [options[n][0] for n in names]
    return random.choices(names, weights=weights, k=1)[0]


def build_items(session):
    items = []
    item_seq = 1
    for category, (dept, subcats, supplier_codes, has_variants, trend) in CATEGORIES.items():
        suppliers = [s for s in session.query(Supplier).filter(Supplier.code.in_(supplier_codes))]
        n_products = 6  # distinct designs per category
        for p in range(n_products):
            subcat = random.choice(subcats)
            brand = random.choice(BRANDS)
            supplier = random.choice(suppliers)
            base_cost = round(random.uniform(30, 350), 2)
            markup = random.uniform(1.8, 3.2)
            retail_price = round(base_cost * markup, 2)
            velocity = weighted_choice(VELOCITY_PROFILES)
            collection = f"{category} {random.choice(['SS26', 'FW25', 'Core'])}"

            variant_colors = random.sample(COLORS, k=3) if has_variants else [None]
            size_list = (SHOE_SIZES if category == "Footwear" else SIZES) if has_variants else [None]
            variant_sizes = random.sample(size_list, k=4) if has_variants else [None]

            case_pack = random.choice([1, 2, 3, 6, 12])
            moq = case_pack * random.choice([2, 4, 6, 10])
            # keep a small amount of stock on the shelf for display purposes even
            # when demand is thin -- fast/medium movers get a real display floor
            display_min_qty = {"fast": 6, "medium": 4, "slow": 2, "dead": 0}[velocity]

            for color in variant_colors:
                for size in variant_sizes:
                    item_code = f"{category[:3].upper()}-{item_seq:04d}"
                    item = Item(
                        item_code=item_code,
                        item_name=f"{brand} {subcat}",
                        brand=brand,
                        department=dept,
                        category=category,
                        subcategory=subcat,
                        collection=collection,
                        color=color,
                        size=size,
                        supplier_id=supplier.id,
                        cost=base_cost,
                        retail_price=retail_price,
                        lead_time_days=supplier.lead_time_days,
                        moq=moq,
                        case_pack=case_pack,
                        display_min_qty=display_min_qty,
                    )
                    item._velocity = velocity          # transient attrs, not persisted
                    item._trend = trend
                    session.add(item)
                    items.append(item)
                    item_seq += 1
    session.commit()
    return items


def daily_lambda(item, day_index, store_bias):
    """Expected units sold for one item/store/day."""
    base = VELOCITY_PROFILES[item._velocity][1] * store_bias

    # trend slope over the window
    slope = {"growing": 0.010, "stable": 0.0, "declining": -0.008}[item._trend]
    trend_factor = max(0.15, 1 + slope * day_index)

    # weekend bump (Thu/Fri weekend in KSA context -> use Fri/Sat as peak, weekday index 4,5)
    dow = (START_DATE + timedelta(days=day_index)).weekday()
    weekend_factor = 1.35 if dow in (4, 5) else 1.0

    # promo window bump
    d = START_DATE + timedelta(days=day_index)
    promo_factor = 1.9 if PROMO_START <= d <= PROMO_END else 1.0

    return base * trend_factor * weekend_factor * promo_factor


def build_sales_and_stock(session, items, stores):
    sales_rows = []
    stock_rows = []

    store_bias = {s.id: random.uniform(0.7, 1.4) for s in stores}

    # opening stock: enough for ~35 days of average demand
    stock_state = {}
    for item in items:
        for store in stores:
            avg_lambda = VELOCITY_PROFILES[item._velocity][1] * store_bias[store.id]
            opening = int(max(4, avg_lambda * random.uniform(25, 45)))
            stock_state[(item.id, store.id)] = opening

    for day_index in range(HISTORY_DAYS + 1):
        d = START_DATE + timedelta(days=day_index)
        for item in items:
            for store in stores:
                lam = daily_lambda(item, day_index, store_bias[store.id])
                qty_sold = int(np.random.poisson(lam))
                on_hand_before = stock_state[(item.id, store.id)]
                qty_sold = min(qty_sold, on_hand_before)  # can't sell more than on hand

                discount_rate = 0.0
                if PROMO_START <= d <= PROMO_END:
                    discount_rate = 0.10

                if qty_sold > 0:
                    sales_value = round(qty_sold * item.retail_price * (1 - discount_rate), 2)
                    cost_value = round(qty_sold * item.cost, 2)
                    sales_rows.append(Sale(
                        date=d, store_id=store.id, item_id=item.id,
                        quantity=qty_sold, sales_value=sales_value,
                        discount=round(discount_rate * qty_sold * item.retail_price, 2),
                        cost=cost_value, margin=round(sales_value - cost_value, 2),
                    ))

                on_hand_after = on_hand_before - qty_sold

                # occasional replenishment trickle (simulates POs landing) so
                # stock doesn't just monotonically decay to zero for every SKU
                restock_cycle = {"fast": 9, "medium": 12, "slow": 20, "dead": 45}[item._velocity]
                if day_index % restock_cycle == 0 and day_index > 0:
                    restock = int(max(VELOCITY_PROFILES[item._velocity][1], 0.2) * store_bias[store.id]
                                  * random.uniform(18, 28))
                    on_hand_after += restock

                stock_state[(item.id, store.id)] = max(on_hand_after, 0)

                stock_rows.append(Stock(
                    date=d, store_id=store.id, item_id=item.id,
                    on_hand=stock_state[(item.id, store.id)], reserved=0,
                    in_transit=0, available=stock_state[(item.id, store.id)],
                ))

        # batch insert per day to keep memory sane
        session.bulk_save_objects(sales_rows)
        session.bulk_save_objects(stock_rows)
        sales_rows, stock_rows = [], []

    session.commit()


def build_purchase_orders(session, items, stores):
    po_seq = 1
    pos = []
    for item in items:
        supplier = item.supplier
        # 2-4 historical/open POs per item across random stores
        for _ in range(random.randint(2, 4)):
            store = random.choice(stores)
            order_date = START_DATE + timedelta(days=random.randint(0, HISTORY_DAYS))
            planned_lead = supplier.lead_time_days
            actual_lead = int(planned_lead * random.uniform(0.8, 1.6))  # some run late
            eta = order_date + timedelta(days=planned_lead)
            received_date = order_date + timedelta(days=actual_lead)

            ordered_qty = max(item.moq, int(VELOCITY_PROFILES[item._velocity][1] * random.uniform(20, 40)))
            fill_rate = random.uniform(0.75, 1.0)
            received_qty = int(ordered_qty * fill_rate)

            if received_date <= TODAY:
                status = "received" if received_qty >= ordered_qty else "partial"
                recv_date = received_date
            else:
                status = "open"
                received_qty = 0
                recv_date = None

            pos.append(PurchaseOrder(
                po_number=f"PO-{po_seq:05d}",
                supplier_id=supplier.id,
                item_id=item.id,
                store_id=store.id,
                ordered_qty=ordered_qty,
                received_qty=received_qty,
                balance_qty=ordered_qty - received_qty,
                order_date=order_date,
                eta=eta,
                received_date=recv_date,
                status=status,
            ))
            po_seq += 1
    session.bulk_save_objects(pos)
    session.commit()


def main():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()

    print("Creating stores & suppliers...")
    stores = [Store(code=c, name=n, city=city, region=r) for c, n, city, r in STORES]
    suppliers = [Supplier(code=c, name=n, lead_time_days=lt, reliability_score=rel) for c, n, lt, rel in SUPPLIERS]
    session.add_all(stores)
    session.add_all(suppliers)
    session.commit()

    print("Building item master...")
    items = build_items(session)
    print(f"  {len(items)} SKUs created")

    print("Simulating 90 days of sales & stock (this generates ~2 files worth of rows, may take a bit)...")
    build_sales_and_stock(session, items, stores)

    print("Generating purchase orders...")
    build_purchase_orders(session, items, stores)

    print("Seeding default category configs (service level + one demo promo)...")
    configs = []
    for category in CATEGORIES:
        configs.append(CategoryConfig(
            category=category,
            service_level_pct=98.0 if category in ("Footwear", "Fragrance") else 95.0,
        ))
    # give one growing category an upcoming promo window so the uplift logic
    # in replenishment has something real to apply
    for c in configs:
        if c.category == "Men's Apparel":
            c.promo_start = TODAY + timedelta(days=10)
            c.promo_end = TODAY + timedelta(days=24)
            c.promo_uplift_pct = 35.0
    session.add_all(configs)
    session.commit()

    print("Done. Database ready at merchandiser.db")


if __name__ == "__main__":
    main()
