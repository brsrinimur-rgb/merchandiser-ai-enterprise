"""
Merchandiser AI 360 -- decision engine.

Every function below takes a `db` (SQLAlchemy session) and returns plain
Python dicts/lists (JSON-ready) so FastAPI can serve them directly.

The engine is built around one core object: the *metrics frame* -- one row
per (store, item) with current stock, sales velocity, variability, and
incoming supply. Every module (replenishment, stock intelligence, transfers,
variants, markdown, action center) is a derivation of that frame, so the
numbers stay consistent across screens.
"""
import math
from datetime import timedelta

import numpy as np
import pandas as pd

from models import Store, Supplier, Item, Sale, Stock, PurchaseOrder, CategoryConfig

RECENT_DAYS = 30
DEAD_STOCK_DAYS = 60
OVERSTOCK_WEEKS = 8
UNDERSTOCK_WEEKS = 1.5
TARGET_COVER_WEEKS = 4  # desired cover used for transfer & OTB sizing


# --------------------------------------------------------------------------
# Data loading
# --------------------------------------------------------------------------

def _read_sql(query, db):
    return pd.read_sql(query.statement, db.bind)


def load_frames(db):
    items = _read_sql(db.query(Item), db)
    stores = _read_sql(db.query(Store), db)
    suppliers = _read_sql(db.query(Supplier), db)
    sales = _read_sql(db.query(Sale), db)
    stock = _read_sql(db.query(Stock), db)
    po = _read_sql(db.query(PurchaseOrder), db)

    for df, col in [(sales, "date"), (stock, "date"), (po, "order_date"), (po, "eta")]:
        df[col] = pd.to_datetime(df[col])
    po["received_date"] = pd.to_datetime(po["received_date"])

    items = items.rename(columns={"id": "item_id"})
    stores = stores.rename(columns={"id": "store_id", "name": "store_name"})
    suppliers = suppliers.rename(columns={"id": "supplier_id", "name": "supplier_name"})

    return dict(items=items, stores=stores, suppliers=suppliers, sales=sales, stock=stock, po=po)


def _today(dfs):
    if len(dfs["sales"]):
        return dfs["sales"]["date"].max()
    return pd.Timestamp.today().normalize()


# --------------------------------------------------------------------------
# v2 replenishment helpers: service-level z-scores, lead-time variability,
# seasonality, and promotion uplift
# --------------------------------------------------------------------------

def norm_ppf(p):
    """Inverse standard-normal CDF (Acklam's rational approximation).
    Lets us turn a service-level target (e.g. 97.5%) into a z-score without
    a scipy dependency."""
    if p <= 0:
        return -8.0
    if p >= 1:
        return 8.0
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    p_low, p_high = 0.02425, 1 - 0.02425
    if p < p_low:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p <= p_high:
        q = p - 0.5
        r = q * q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)


_CATEGORY_CONFIG_CACHE = {}


def set_category_config(mapping):
    """mapping: {category: {service_level_pct, promo_start, promo_end, promo_uplift_pct}}"""
    _CATEGORY_CONFIG_CACHE.clear()
    _CATEGORY_CONFIG_CACHE.update(mapping)


def _category_config():
    return _CATEGORY_CONFIG_CACHE


DEFAULT_SERVICE_LEVEL_PCT = 95.0


def supplier_lead_time_stats(dfs):
    """Mean & std actual lead time per supplier, from received PO history.
    Falls back to the supplier's stated lead_time_days when there isn't
    enough history yet."""
    po = dfs["po"]
    received = po[po["received_date"].notna()].copy()
    if received.empty:
        return pd.DataFrame(columns=["supplier_id", "lt_mean_actual", "lt_std_actual"])
    received["actual_lead"] = (received["received_date"] - received["order_date"]).dt.days
    stats = received.groupby("supplier_id")["actual_lead"].agg(
        lt_mean_actual="mean", lt_std_actual="std"
    ).reset_index()
    stats["lt_std_actual"] = stats["lt_std_actual"].fillna(0)
    return stats


def weekday_seasonal_index(dfs):
    """Day-of-week demand index (Monday=0 ... Sunday=6) relative to the
    overall daily average, computed from the full sales history. A simple,
    transparent seasonality signal -- swap for a per-category or holiday-
    calendar model once there's enough real history."""
    sales = dfs["sales"]
    if sales.empty:
        return {i: 1.0 for i in range(7)}
    daily_total = sales.groupby("date")["quantity"].sum()
    by_dow = daily_total.groupby(daily_total.index.dayofweek).mean()
    overall_mean = daily_total.mean() or 1.0
    idx = (by_dow / overall_mean).reindex(range(7)).fillna(1.0)
    return idx.to_dict()


def _seasonal_mult_for_horizon(today, horizon_days, weekday_idx):
    if horizon_days <= 0:
        return 1.0
    future_dates = pd.date_range(today + pd.Timedelta(days=1), periods=int(horizon_days), freq="D")
    mults = [weekday_idx.get(int(d.dayofweek), 1.0) for d in future_dates]
    return float(np.mean(mults)) if mults else 1.0


def _promo_multiplier(category, horizon_days, today, promo_cfg):
    cfg = promo_cfg.get(category)
    if not cfg or not cfg.get("promo_start") or not cfg.get("promo_uplift_pct"):
        return 1.0
    p_start, p_end = cfg.get("promo_start"), cfg.get("promo_end")
    if p_start is None or p_end is None or horizon_days <= 0:
        return 1.0
    p_start, p_end = pd.Timestamp(p_start), pd.Timestamp(p_end)
    window_start = today + pd.Timedelta(days=1)
    window_end = today + pd.Timedelta(days=int(horizon_days))
    overlap_start = max(window_start, p_start)
    overlap_end = min(window_end, p_end)
    overlap_days = (overlap_end - overlap_start).days + 1
    if overlap_days <= 0:
        return 1.0
    frac = overlap_days / horizon_days
    return 1 + (cfg["promo_uplift_pct"] / 100.0) * frac


# --------------------------------------------------------------------------
# Core metrics frame: one row per (store, item)
# --------------------------------------------------------------------------

def build_metrics_frame(dfs):
    items, stores, sales, stock, po = dfs["items"], dfs["stores"], dfs["sales"], dfs["stock"], dfs["po"]
    today = _today(dfs)

    # --- latest stock snapshot per store/item ---
    latest_stock = (
        stock.sort_values("date")
        .groupby(["store_id", "item_id"], as_index=False)
        .last()[["store_id", "item_id", "date", "on_hand"]]
        .rename(columns={"date": "stock_date"})
    )

    # --- full grid of store x item so zero-sale combos aren't dropped ---
    grid = stores[["store_id"]].merge(items[["item_id"]], how="cross")

    # --- daily sales for last N days, filled with zeros for std/mean ---
    window_start = today - timedelta(days=RECENT_DAYS - 1)
    recent_sales = sales[sales["date"] >= window_start]
    daily = recent_sales.groupby(["store_id", "item_id", "date"])["quantity"].sum().reset_index()

    date_range = pd.date_range(window_start, today, freq="D")
    full_idx = grid.merge(pd.DataFrame({"date": date_range}), how="cross")
    daily_full = full_idx.merge(daily, on=["store_id", "item_id", "date"], how="left")
    daily_full["quantity"] = daily_full["quantity"].fillna(0)

    # --- daily stock-on-hand for the same window, to flag stockout days.
    # Raw sales during an out-of-stock day understate true demand, so we
    # correct average daily sales to a "days actually in stock" basis
    # instead of a flat /30, and separately track the raw figure too. ---
    stock_recent = stock[stock["date"] >= window_start]
    daily_stock = stock_recent.groupby(["store_id", "item_id", "date"])["on_hand"].max().reset_index()
    daily_full = daily_full.merge(daily_stock, on=["store_id", "item_id", "date"], how="left")
    daily_full["on_hand"] = daily_full["on_hand"].fillna(1)  # no snapshot -> assume in stock
    daily_full["in_stock_day"] = daily_full["on_hand"] > 0

    vel = daily_full.groupby(["store_id", "item_id"]).agg(
        std_30=("quantity", "std"), qty_30=("quantity", "sum"),
        days_in_stock=("in_stock_day", "sum"), days_total=("in_stock_day", "count"),
    ).reset_index()
    vel["std_30"] = vel["std_30"].fillna(0)
    vel["ads_30_raw"] = vel["qty_30"] / vel["days_total"].clip(lower=1)
    vel["days_in_stock"] = vel["days_in_stock"].clip(lower=1)
    vel["ads_30"] = vel["qty_30"] / vel["days_in_stock"]           # lost-sales corrected
    vel["days_out_of_stock_30"] = vel["days_total"] - vel["days_in_stock"]

    # --- 60/90 day totals for trend/growth & dead-stock detection ---
    def window_sum(days, col_name):
        w_start = today - timedelta(days=days - 1)
        w = sales[sales["date"] >= w_start].groupby(["store_id", "item_id"])["quantity"].sum()
        return w.rename(col_name).reset_index()

    qty_60 = window_sum(60, "qty_60")
    qty_90 = window_sum(90, "qty_90")

    prior_30_start = today - timedelta(days=59)
    prior_30_end = today - timedelta(days=30)
    prior_30 = sales[(sales["date"] >= prior_30_start) & (sales["date"] <= prior_30_end)]
    prior_30 = prior_30.groupby(["store_id", "item_id"])["quantity"].sum().rename("qty_prior_30").reset_index()

    last_sale = sales.groupby(["store_id", "item_id"])["date"].max().rename("last_sale_date").reset_index()

    sales_value_30 = recent_sales.groupby(["store_id", "item_id"]).agg(
        sales_value_30=("sales_value", "sum"), margin_30=("margin", "sum")
    ).reset_index()

    # --- incoming stock: open PO balance ---
    open_po = po[po["status"] == "open"]
    incoming = open_po.groupby(["store_id", "item_id"])["balance_qty"].sum().rename("incoming_stock").reset_index()

    # --- supplier lead-time variability (mean + std of actual lead time) ---
    lt_stats = supplier_lead_time_stats(dfs)

    # --- assemble ---
    m = grid.merge(items, on="item_id", how="left")
    m = m.merge(stores, on="store_id", how="left")
    m = m.merge(latest_stock, on=["store_id", "item_id"], how="left")
    m = m.merge(vel, on=["store_id", "item_id"], how="left")
    m = m.merge(qty_60, on=["store_id", "item_id"], how="left")
    m = m.merge(qty_90, on=["store_id", "item_id"], how="left")
    m = m.merge(prior_30, on=["store_id", "item_id"], how="left")
    m = m.merge(last_sale, on=["store_id", "item_id"], how="left")
    m = m.merge(sales_value_30, on=["store_id", "item_id"], how="left")
    m = m.merge(incoming, on=["store_id", "item_id"], how="left")
    m = m.merge(lt_stats, on="supplier_id", how="left")

    fill0 = ["on_hand", "ads_30", "ads_30_raw", "std_30", "qty_30", "qty_60", "qty_90", "qty_prior_30",
             "sales_value_30", "margin_30", "incoming_stock", "days_out_of_stock_30"]
    for c in fill0:
        m[c] = m[c].fillna(0)

    m["days_since_sale"] = (today - m["last_sale_date"]).dt.days
    m["days_since_sale"] = m["days_since_sale"].fillna(9999).astype(int)

    m["weekly_velocity"] = m["ads_30"] * 7
    m["weeks_of_cover"] = np.where(m["weekly_velocity"] > 0, m["on_hand"] / m["weekly_velocity"], np.inf)
    m["stock_value"] = m["on_hand"] * m["cost"]

    lead_time = m["lead_time_days"].fillna(14)
    # lead-time mean/std: prefer supplier's observed history, fall back to the
    # item's stated lead time with zero observed variability
    m["lt_mean_actual"] = m["lt_mean_actual"].fillna(lead_time)
    m["lt_std_actual"] = m["lt_std_actual"].fillna(0)

    # --- service level -> z-score, per category (falls back to global default) ---
    cfg = _category_config()
    unique_categories = m["category"].dropna().unique()
    z_by_category = {
        cat: norm_ppf(cfg.get(cat, {}).get("service_level_pct", DEFAULT_SERVICE_LEVEL_PCT) / 100.0)
        for cat in unique_categories
    }
    m["service_level_pct"] = m["category"].map(
        lambda c: cfg.get(c, {}).get("service_level_pct", DEFAULT_SERVICE_LEVEL_PCT)
    )
    m["z_score"] = m["category"].map(z_by_category).fillna(norm_ppf(DEFAULT_SERVICE_LEVEL_PCT / 100.0))

    # --- seasonality (day-of-week index applied over each row's specific horizon) ---
    weekday_idx = weekday_seasonal_index(dfs)
    seasonal_mult_30 = _seasonal_mult_for_horizon(today, 30, weekday_idx)
    unique_lts = pd.Series(lead_time.unique())
    lt_season_map = {lt: _seasonal_mult_for_horizon(today, lt, weekday_idx) for lt in unique_lts}
    m["seasonal_mult_lt"] = lead_time.map(lt_season_map)
    m["seasonal_mult_30"] = seasonal_mult_30

    # --- promotion uplift (category-level window from CategoryConfig) ---
    m["promo_mult_lt"] = [
        _promo_multiplier(cat, lt, today, cfg) for cat, lt in zip(m["category"], lead_time)
    ]
    m["promo_mult_30"] = [_promo_multiplier(cat, 30, today, cfg) for cat in m["category"]]

    # --- demand over the lead time & 30-day planning horizon, seasonality + promo adjusted ---
    m["lead_time_demand"] = m["ads_30"] * lead_time * m["seasonal_mult_lt"] * m["promo_mult_lt"]
    m["forecast_demand_30d"] = m["ads_30"] * 30 * m["seasonal_mult_30"] * m["promo_mult_30"]

    # --- dynamic safety stock: combined demand + lead-time variability
    # SS = z * sqrt( LT_mean * sigma_d^2  +  ADS^2 * sigma_LT^2 ) ---
    m["safety_stock"] = (
        m["z_score"] * np.sqrt(
            (m["lt_mean_actual"] * m["std_30"] ** 2).clip(lower=0)
            + (m["ads_30"] ** 2 * m["lt_std_actual"] ** 2).clip(lower=0)
        )
    ).round().clip(lower=0)

    m["reorder_point"] = m["lead_time_demand"] + m["safety_stock"]
    m["inventory_position"] = m["on_hand"] + m["incoming_stock"]

    gap = (m["reorder_point"] + m["forecast_demand_30d"]) - m["inventory_position"]
    m["suggested_order_qty"] = gap.clip(lower=0)

    def round_to_pack(row):
        """Round up to a case-pack multiple, then make sure that's still >= MOQ
        (also expressed in whole case packs)."""
        case_pack = int(row["case_pack"]) if row["case_pack"] and row["case_pack"] > 0 else 1
        moq = row["moq"] if row["moq"] and row["moq"] > 0 else 1
        qty = row["suggested_order_qty"]
        if qty <= 0:
            return 0
        packs = math.ceil(qty / case_pack)
        qty_rounded = packs * case_pack
        if qty_rounded < moq:
            qty_rounded = math.ceil(moq / case_pack) * case_pack
        return int(qty_rounded)

    m["suggested_order_qty"] = m.apply(round_to_pack, axis=1)

    # --- minimum display quantity: keep a shelf-presence floor even when
    # pure demand math wouldn't justify an order (rounded to case pack) ---
    def display_topup(row):
        target = row["display_min_qty"] or 0
        projected = row["on_hand"] + row["incoming_stock"] + row["suggested_order_qty"]
        if projected >= target:
            return 0
        case_pack = int(row["case_pack"]) if row["case_pack"] and row["case_pack"] > 0 else 1
        return int(math.ceil((target - projected) / case_pack) * case_pack)

    m["display_topup_qty"] = m.apply(display_topup, axis=1)
    m["suggested_order_qty"] = m["suggested_order_qty"] + m["display_topup_qty"]

    growing_category = m["category"].map(_category_trend_lookup())
    m["extra_order_qty"] = np.where(
        m["category"].map(_category_trend_lookup()) == "growing",
        (m["suggested_order_qty"] * 0.20).round(),
        0,
    ).astype(int)

    emergency_gap = m["lead_time_demand"] - m["inventory_position"]
    m["emergency_order_qty"] = emergency_gap.clip(lower=0).round().astype(int)

    # classification
    def classify(row):
        if row["on_hand"] > 0 and row["days_since_sale"] >= DEAD_STOCK_DAYS and row["qty_90"] == 0:
            return "dead"
        if row["weeks_of_cover"] < UNDERSTOCK_WEEKS:
            return "understock"
        if row["weeks_of_cover"] > OVERSTOCK_WEEKS:
            return "overstock"
        if row["ads_30"] >= m["ads_30"].quantile(0.85):
            return "fast_moving"
        if row["ads_30"] <= m["ads_30"].quantile(0.30):
            return "slow_moving"
        return "normal"

    m["stock_status"] = m.apply(classify, axis=1)
    m["out_of_stock_risk"] = (m["on_hand"] <= 0) & (m["ads_30"] > 0)

    m["growth_pct"] = np.where(
        m["qty_prior_30"] > 0,
        ((m["qty_30"] - m["qty_prior_30"]) / m["qty_prior_30"] * 100).round(1),
        np.where(m["qty_30"] > 0, 100.0, 0.0),
    )

    return m


_CATEGORY_TREND_CACHE = {}


def set_category_trend_lookup(mapping):
    _CATEGORY_TREND_CACHE.clear()
    _CATEGORY_TREND_CACHE.update(mapping)


def _category_trend_lookup():
    return _CATEGORY_TREND_CACHE


# --------------------------------------------------------------------------
# 1. Sales Intelligence
# --------------------------------------------------------------------------

def sales_intelligence(dfs, m):
    today = _today(dfs)

    by_item = m.groupby(["item_id", "item_code", "item_name", "category", "brand"], as_index=False).agg(
        qty_30=("qty_30", "sum"), sales_value_30=("sales_value_30", "sum"),
        margin_30=("margin_30", "sum"), qty_prior_30=("qty_prior_30", "sum"),
    )
    by_item["growth_pct"] = np.where(
        by_item["qty_prior_30"] > 0,
        ((by_item["qty_30"] - by_item["qty_prior_30"]) / by_item["qty_prior_30"] * 100).round(1),
        0.0,
    )
    best_sellers = by_item.sort_values("sales_value_30", ascending=False).head(15)
    worst_sellers = by_item[by_item["qty_30"] >= 0].sort_values(["qty_30", "sales_value_30"]).head(15)

    store_perf = m.groupby(["store_id", "store_name"], as_index=False).agg(
        sales_value_30=("sales_value_30", "sum"), margin_30=("margin_30", "sum"),
        qty_30=("qty_30", "sum"), qty_prior_30=("qty_prior_30", "sum"),
    )
    store_perf["growth_pct"] = np.where(
        store_perf["qty_prior_30"] > 0,
        ((store_perf["qty_30"] - store_perf["qty_prior_30"]) / store_perf["qty_prior_30"] * 100).round(1), 0.0,
    )

    cat_perf = m.groupby("category", as_index=False).agg(
        sales_value_30=("sales_value_30", "sum"), margin_30=("margin_30", "sum"),
        qty_30=("qty_30", "sum"), qty_prior_30=("qty_prior_30", "sum"),
    )
    cat_perf["growth_pct"] = np.where(
        cat_perf["qty_prior_30"] > 0,
        ((cat_perf["qty_30"] - cat_perf["qty_prior_30"]) / cat_perf["qty_prior_30"] * 100).round(1), 0.0,
    )
    cat_perf["margin_pct"] = np.where(
        cat_perf["sales_value_30"] > 0, (cat_perf["margin_30"] / cat_perf["sales_value_30"] * 100).round(1), 0.0
    )

    brand_perf = m.groupby("brand", as_index=False).agg(
        sales_value_30=("sales_value_30", "sum"), margin_30=("margin_30", "sum"), qty_30=("qty_30", "sum"),
    ).sort_values("sales_value_30", ascending=False)

    return {
        "best_sellers": best_sellers.to_dict("records"),
        "worst_sellers": worst_sellers.to_dict("records"),
        "store_performance": store_perf.sort_values("sales_value_30", ascending=False).to_dict("records"),
        "category_performance": cat_perf.sort_values("sales_value_30", ascending=False).to_dict("records"),
        "brand_performance": brand_perf.to_dict("records"),
        "margin_contribution": cat_perf[["category", "margin_30", "margin_pct"]].sort_values(
            "margin_30", ascending=False).to_dict("records"),
    }


# --------------------------------------------------------------------------
# 2. Auto Replenishment
# --------------------------------------------------------------------------

def auto_replenishment(m, only_action=True):
    cols = ["store_id", "store_name", "item_id", "item_code", "item_name", "category", "brand",
            "ads_30", "ads_30_raw", "days_out_of_stock_30",
            "forecast_demand_30d", "lead_time_demand", "safety_stock", "on_hand",
            "incoming_stock", "suggested_order_qty", "extra_order_qty", "emergency_order_qty",
            "display_topup_qty", "moq", "case_pack", "service_level_pct",
            "lt_mean_actual", "lt_std_actual", "seasonal_mult_lt", "promo_mult_lt"]
    out = m[cols].copy()
    for c in ["ads_30", "ads_30_raw", "forecast_demand_30d", "lead_time_demand", "safety_stock",
              "lt_mean_actual", "lt_std_actual", "seasonal_mult_lt", "promo_mult_lt"]:
        out[c] = out[c].round(2)
    if only_action:
        out = out[(out["suggested_order_qty"] > 0) | (out["emergency_order_qty"] > 0)]
    return out.sort_values("emergency_order_qty", ascending=False).to_dict("records")


# --------------------------------------------------------------------------
# 3. Stock Intelligence
# --------------------------------------------------------------------------

def stock_intelligence(m):
    cols = ["store_id", "store_name", "item_id", "item_code", "item_name", "category",
            "on_hand", "ads_30", "weeks_of_cover", "days_since_sale", "stock_value",
            "stock_status", "out_of_stock_risk"]
    out = m[cols].copy()
    out["weeks_of_cover"] = out["weeks_of_cover"].replace(np.inf, 999).round(1)

    summary = m["stock_status"].value_counts().to_dict()
    summary["out_of_stock_risk_count"] = int(m["out_of_stock_risk"].sum())
    summary["dead_stock_value"] = float(m.loc[m["stock_status"] == "dead", "stock_value"].sum())
    summary["overstock_value"] = float(m.loc[m["stock_status"] == "overstock", "stock_value"].sum())
    summary["total_stock_value"] = float(m["stock_value"].sum())

    return {
        "detail": out.sort_values("stock_value", ascending=False).to_dict("records"),
        "summary": summary,
    }


# --------------------------------------------------------------------------
# 4. Category Management
# --------------------------------------------------------------------------

def category_management(m):
    cat = m.groupby("category", as_index=False).agg(
        sales_value_30=("sales_value_30", "sum"), margin_30=("margin_30", "sum"),
        qty_30=("qty_30", "sum"), qty_prior_30=("qty_prior_30", "sum"),
        stock_value=("stock_value", "sum"), weeks_of_cover=("weeks_of_cover", lambda s: s.replace(np.inf, 999).mean()),
    )
    cat["growth_pct"] = np.where(
        cat["qty_prior_30"] > 0, ((cat["qty_30"] - cat["qty_prior_30"]) / cat["qty_prior_30"] * 100).round(1), 0.0
    )
    cat["margin_pct"] = np.where(cat["sales_value_30"] > 0, (cat["margin_30"] / cat["sales_value_30"] * 100).round(1), 0.0)
    cat["weeks_of_cover"] = cat["weeks_of_cover"].round(1)

    def recommend(row):
        if row["growth_pct"] >= 15 and row["weeks_of_cover"] < TARGET_COVER_WEEKS:
            return "Increase buying"
        if row["growth_pct"] <= -10 and row["weeks_of_cover"] > OVERSTOCK_WEEKS:
            return "Reduce buying / clear stock"
        if row["weeks_of_cover"] > OVERSTOCK_WEEKS:
            return "Too much stock -- slow buying"
        if row["growth_pct"] >= 15:
            return "Growing -- watch supply"
        if row["growth_pct"] <= -10:
            return "Declining -- review assortment"
        return "Maintain"

    cat["recommendation"] = cat.apply(recommend, axis=1)
    return cat.sort_values("sales_value_30", ascending=False).to_dict("records")


# --------------------------------------------------------------------------
# 5. Store-to-Store Transfer
# --------------------------------------------------------------------------

def store_transfer_suggestions(m):
    suggestions = []
    for item_id, grp in m.groupby("item_id"):
        if len(grp) < 2:
            continue
        grp = grp.copy()
        desired_units = TARGET_COVER_WEEKS * grp["weekly_velocity"]
        grp["surplus"] = (grp["on_hand"] - desired_units).clip(lower=0)
        grp["shortage"] = (desired_units - grp["on_hand"]).clip(lower=0)

        donors = grp[grp["surplus"] > 0].sort_values("surplus", ascending=False)
        receivers = grp[(grp["shortage"] > 0) & (grp["weekly_velocity"] > 0)].sort_values("shortage", ascending=False)

        for _, r in receivers.iterrows():
            need = r["shortage"]
            for di in donors.index:
                if need <= 0:
                    break
                available = donors.loc[di, "surplus"]
                if available <= 0 or donors.loc[di, "store_id"] == r["store_id"]:
                    continue
                qty = int(min(available, need))
                if qty <= 0:
                    continue
                suggestions.append({
                    "item_id": int(item_id), "item_code": r["item_code"], "item_name": r["item_name"],
                    "category": r["category"],
                    "from_store": donors.loc[di, "store_name"], "to_store": r["store_name"],
                    "transfer_qty": qty,
                    "reason": f"{donors.loc[di, 'store_name']} has {int(donors.loc[di, 'on_hand'])} units "
                              f"({donors.loc[di, 'weeks_of_cover']:.1f} wks cover) while {r['store_name']} "
                              f"sells {r['weekly_velocity']:.1f}/wk with only {r['weeks_of_cover']:.1f} wks cover.",
                })
                donors.loc[di, "surplus"] -= qty
                need -= qty

    suggestions.sort(key=lambda x: x["transfer_qty"], reverse=True)
    return suggestions[:100]


# --------------------------------------------------------------------------
# 6. Purchase Planning
# --------------------------------------------------------------------------

def purchase_planning(dfs, m, budget_by_category=None):
    m = m.copy()
    m["order_value"] = m["suggested_order_qty"] * m["cost"]
    m["below_moq"] = (m["suggested_order_qty"] > 0) & (m["suggested_order_qty"] < m["moq"])

    item_supplier = dfs["items"][["item_id", "supplier_id"]]
    supplier_names = dfs["suppliers"][["supplier_id", "supplier_name", "lead_time_days"]].rename(
        columns={"lead_time_days": "supplier_lead_time_days"}
    )
    by_supplier = m.drop(columns=["supplier_id"], errors="ignore").merge(item_supplier, on="item_id", how="left")
    by_supplier = by_supplier.merge(supplier_names, on="supplier_id", how="left")
    supplier_plan = by_supplier.groupby(["supplier_id", "supplier_name"], as_index=False).agg(
        suggested_qty=("suggested_order_qty", "sum"), order_value=("order_value", "sum"),
        lead_time_days=("supplier_lead_time_days", "first"),
    ).sort_values("order_value", ascending=False)

    category_plan = m.groupby("category", as_index=False).agg(
        suggested_qty=("suggested_order_qty", "sum"), order_value=("order_value", "sum"),
    )
    if budget_by_category:
        category_plan["budget"] = category_plan["category"].map(budget_by_category).fillna(0)
        category_plan["open_to_buy"] = (category_plan["budget"] - category_plan["order_value"]).round(2)
    else:
        category_plan["budget"] = None
        category_plan["open_to_buy"] = None

    priority = m[m["suggested_order_qty"] > 0].copy()
    priority["priority_score"] = (
        priority["emergency_order_qty"] * 3 + priority["out_of_stock_risk"].astype(int) * 50
        + priority["growth_pct"].clip(lower=0) * 0.5
    )
    priority = priority.sort_values("priority_score", ascending=False).head(30)
    priority_cols = ["item_code", "item_name", "store_name", "category", "suggested_order_qty",
                      "emergency_order_qty", "order_value", "below_moq", "moq"]

    stop_buy = m[m["stock_status"] == "dead"][["item_code", "item_name", "store_name", "category",
                                                 "on_hand", "stock_value", "days_since_sale"]]

    return {
        "supplier_plan": supplier_plan.to_dict("records"),
        "category_plan": category_plan.to_dict("records"),
        "purchase_priority": priority[priority_cols].to_dict("records"),
        "stop_buy_list": stop_buy.sort_values("stock_value", ascending=False).head(30).to_dict("records"),
        "moq_flags": m[m["below_moq"]][["item_code", "item_name", "store_name", "suggested_order_qty", "moq"]]
            .to_dict("records"),
    }


# --------------------------------------------------------------------------
# 7. Size / Color / Variant Intelligence
# --------------------------------------------------------------------------

def variant_intelligence(m):
    variants = m[m["color"].notna() | m["size"].notna()].copy()
    if variants.empty:
        return {"by_color": [], "by_size": [], "broken_size_range": [], "note": "No variant items in catalogue."}

    by_color = variants[variants["color"].notna()].groupby(["category", "color"], as_index=False).agg(
        qty_30=("qty_30", "sum"), sales_value_30=("sales_value_30", "sum"),
    ).sort_values("sales_value_30", ascending=False)

    by_size = variants[variants["size"].notna()].groupby(["category", "size"], as_index=False).agg(
        qty_30=("qty_30", "sum"), sales_value_30=("sales_value_30", "sum"), on_hand=("on_hand", "sum"),
    ).sort_values("sales_value_30", ascending=False)

    broken = []
    # group by the exact product (item_name) + color + store so "missing size"
    # means missing *for that specific product*, not just anything sharing a
    # collection label
    key_cols = ["item_name", "color", "store_id"]
    for key, grp in variants[variants["size"].notna()].groupby(key_cols):
        if grp["size"].nunique() < 2:
            continue
        in_stock = grp[grp["on_hand"] > 0]
        out_of_stock = grp[grp["on_hand"] <= 0]
        # a size only counts as "missing" if it isn't also in stock under a
        # different SKU in the same product/color/store group
        missing_sizes = sorted(set(out_of_stock["size"]) - set(in_stock["size"]))
        if in_stock.empty or not missing_sizes:
            continue
        sells_elsewhere = grp[grp["qty_90"] > 0]
        if sells_elsewhere.empty:
            continue
        row0 = grp.iloc[0]
        broken.append({
            "item_name": key[0], "color": key[1], "store_name": row0["store_name"],
            "collection": row0["collection"],
            "missing_sizes": missing_sizes,
            "available_sizes": sorted(in_stock["size"].unique().tolist()),
        })

    return {
        "by_color": by_color.to_dict("records"),
        "by_size": by_size.to_dict("records"),
        "broken_size_range": broken[:50],
    }


# --------------------------------------------------------------------------
# 8. Markdown & Promotion AI
# --------------------------------------------------------------------------

def markdown_promotion(m):
    candidates = m[(m["stock_status"].isin(["dead", "slow_moving"])) & (m["on_hand"] > 0)].copy()

    def markdown_pct(row):
        d = row["days_since_sale"]
        if row["stock_status"] == "dead" or d >= 120:
            return 40
        if d >= 90:
            return 30
        if d >= 60:
            return 20
        return 10

    candidates["recommended_markdown_pct"] = candidates.apply(markdown_pct, axis=1)
    candidates["tied_up_value"] = candidates["stock_value"]
    candidates = candidates.sort_values("tied_up_value", ascending=False)

    do_not_discount = m[
        (m["stock_status"] == "fast_moving") | ((m["growth_pct"] >= 20) & (m["weeks_of_cover"] < TARGET_COVER_WEEKS))
    ][["item_code", "item_name", "store_name", "category", "growth_pct", "weeks_of_cover"]].head(30)

    # naive bundle idea: pair a markdown candidate with the category's best seller
    bundles = []
    best_by_cat = m.sort_values("sales_value_30", ascending=False).groupby("category").first()
    for _, row in candidates.head(20).iterrows():
        if row["category"] in best_by_cat.index:
            partner = best_by_cat.loc[row["category"]]
            bundles.append({
                "slow_item": row["item_name"], "slow_item_code": row["item_code"],
                "pair_with": partner["item_name"], "pair_with_code": partner["item_code"],
                "category": row["category"],
            })

    cols = ["item_code", "item_name", "store_name", "category", "on_hand", "days_since_sale",
            "stock_value", "recommended_markdown_pct"]
    return {
        "markdown_candidates": candidates[cols].head(50).to_dict("records"),
        "do_not_discount": do_not_discount.to_dict("records"),
        "bundle_opportunities": bundles,
        "promotion_priority": candidates[cols].head(15).to_dict("records"),
    }


# --------------------------------------------------------------------------
# 9. Supplier Intelligence
# --------------------------------------------------------------------------

def supplier_intelligence(dfs):
    po, suppliers = dfs["po"].copy(), dfs["suppliers"]
    if po.empty:
        return []

    po["promised_lead"] = (po["eta"] - po["order_date"]).dt.days
    received = po[po["received_date"].notna()].copy()
    received["actual_lead"] = (received["received_date"] - received["order_date"]).dt.days
    received["delay_days"] = received["actual_lead"] - received["promised_lead"]
    received["on_time"] = received["delay_days"] <= 0

    agg = po.groupby("supplier_id").agg(
        ordered_qty=("ordered_qty", "sum"), received_qty=("received_qty", "sum"), po_count=("po_number", "count"),
    ).reset_index()
    agg["fill_rate"] = (agg["received_qty"] / agg["ordered_qty"]).round(3)

    delay_agg = received.groupby("supplier_id").agg(
        avg_delay_days=("delay_days", "mean"), on_time_pct=("on_time", "mean"),
    ).reset_index()
    delay_agg["avg_delay_days"] = delay_agg["avg_delay_days"].round(1)
    delay_agg["on_time_pct"] = (delay_agg["on_time_pct"] * 100).round(1)

    out = suppliers.merge(agg, on="supplier_id", how="left").merge(delay_agg, on="supplier_id", how="left")
    out["fill_rate"] = out["fill_rate"].fillna(0)
    out["on_time_pct"] = out["on_time_pct"].fillna(0)
    out["avg_delay_days"] = out["avg_delay_days"].fillna(0)

    out["supplier_score"] = (
        out["fill_rate"] * 40 + (out["on_time_pct"] / 100) * 40 + out["reliability_score"] * 20
    ).round(1)

    cols = ["supplier_id", "supplier_name", "lead_time_days", "po_count", "fill_rate",
            "on_time_pct", "avg_delay_days", "reliability_score", "supplier_score"]
    return out[cols].sort_values("supplier_score", ascending=False).to_dict("records")


# --------------------------------------------------------------------------
# 10. AI Forecasting
# --------------------------------------------------------------------------

def ai_forecasting(dfs, m):
    sales = dfs["sales"]
    today = _today(dfs)
    daily_total = sales.groupby("date")["quantity"].sum().reindex(
        pd.date_range(sales["date"].min(), today, freq="D"), fill_value=0
    )

    recent = daily_total.tail(30)
    x = np.arange(len(recent))
    if recent.sum() > 0 and len(recent) > 1:
        slope, intercept = np.polyfit(x, recent.values, 1)
    else:
        slope, intercept = 0.0, recent.mean() if len(recent) else 0.0

    def project(days):
        future_x = np.arange(len(recent), len(recent) + days)
        vals = np.clip(slope * future_x + intercept, 0, None)
        return float(vals.sum())

    forecast_overall = {
        "next_7_days_units": round(project(7), 1),
        "next_30_days_units": round(project(30), 1),
        "next_90_days_units": round(project(90), 1),
        "trend": "growing" if slope > 0.05 else ("declining" if slope < -0.05 else "stable"),
    }

    # per category, same simple trend method but on 30-day average velocity
    cat_forecast = m.groupby("category", as_index=False).agg(ads_30=("ads_30", "sum"))
    cat_forecast["next_7_days_units"] = (cat_forecast["ads_30"] * 7).round(1)
    cat_forecast["next_30_days_units"] = (cat_forecast["ads_30"] * 30).round(1)
    cat_forecast["next_90_days_units"] = (cat_forecast["ads_30"] * 90).round(1)

    # seasonal signal: lift observed during the promo window baked into the sample data
    promo_mask = (sales["date"] >= today - timedelta(days=25)) & (sales["date"] <= today - timedelta(days=15))
    baseline_mask = (sales["date"] < today - timedelta(days=25)) & (sales["date"] >= today - timedelta(days=45))
    promo_avg = sales[promo_mask].groupby("date")["quantity"].sum().mean() or 0
    baseline_avg = sales[baseline_mask].groupby("date")["quantity"].sum().mean() or 0
    lift_pct = round(((promo_avg - baseline_avg) / baseline_avg * 100), 1) if baseline_avg else 0.0

    return {
        "overall": forecast_overall,
        "by_category": cat_forecast.drop(columns=["ads_30"]).to_dict("records"),
        "seasonal_signal": {
            "observed_window": "last promo window (e.g. Ramadan/Eid style peak)",
            "demand_lift_pct": lift_pct,
            "note": "Apply a similar uplift multiplier when planning buys ahead of the next known "
                    "promotional or religious-calendar peak. Confirm exact dates against next year's "
                    "calendar before finalizing purchase quantities.",
        },
    }


# --------------------------------------------------------------------------
# 11. AI Action Center
# --------------------------------------------------------------------------

def ai_action_center(dfs, m):
    actions = []

    emergencies = m[m["emergency_order_qty"] > 0]
    for _, r in emergencies.iterrows():
        actions.append({
            "priority": "Critical", "item": r["item_code"], "item_name": r["item_name"],
            "store": r["store_name"], "category": r["category"],
            "problem": f"{r['weeks_of_cover']:.1f} weeks cover, will stock out before lead time",
            "ai_action": f"Order {int(r['emergency_order_qty'])} (emergency)",
            "impact_value": round(r["emergency_order_qty"] * r["retail_price"], 2),
        })

    oos = m[m["out_of_stock_risk"] & (m["emergency_order_qty"] == 0)]
    for _, r in oos.iterrows():
        actions.append({
            "priority": "Critical", "item": r["item_code"], "item_name": r["item_name"],
            "store": r["store_name"], "category": r["category"],
            "problem": "Out of stock, still selling",
            "ai_action": f"Order {int(max(r['suggested_order_qty'], r['moq']))}",
            "impact_value": round(r["ads_30"] * r["retail_price"] * 30, 2),
        })

    transfers = store_transfer_suggestions(m)
    for t in transfers[:25]:
        actions.append({
            "priority": "High", "item": t["item_code"], "item_name": t["item_name"],
            "store": t["to_store"], "category": t["category"],
            "problem": f"Excess at {t['from_store']}, shortage at {t['to_store']}",
            "ai_action": f"Transfer {t['transfer_qty']} from {t['from_store']}",
            "impact_value": None,
        })

    dead = m[(m["stock_status"] == "dead") & (m["stock_value"] > 0)].sort_values("stock_value", ascending=False).head(20)
    for _, r in dead.iterrows():
        actions.append({
            "priority": "Medium", "item": r["item_code"], "item_name": r["item_name"],
            "store": r["store_name"], "category": r["category"],
            "problem": f"No sale in {int(r['days_since_sale'])} days",
            "ai_action": "Markdown",
            "impact_value": round(r["stock_value"], 2),
        })

    cat = category_management(m)
    for c in cat:
        if c["recommendation"] == "Increase buying":
            actions.append({
                "priority": "High", "item": "Category", "item_name": c["category"],
                "store": "All Stores", "category": c["category"],
                "problem": f"Sales +{c['growth_pct']}%",
                "ai_action": "Increase buying",
                "impact_value": round(c["sales_value_30"], 2),
            })
        elif c["recommendation"] == "Reduce buying / clear stock":
            actions.append({
                "priority": "Medium", "item": "Category", "item_name": c["category"],
                "store": "All Stores", "category": c["category"],
                "problem": f"Sales {c['growth_pct']}%, {c['weeks_of_cover']} wks cover",
                "ai_action": "Reduce buying / clear stock",
                "impact_value": round(c["stock_value"], 2),
            })

    priority_rank = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
    actions.sort(key=lambda a: (priority_rank.get(a["priority"], 9), -(a["impact_value"] or 0)))
    return actions[:150]


# --------------------------------------------------------------------------
# 12. Executive Dashboard
# --------------------------------------------------------------------------

def executive_dashboard(dfs, m):
    total_sales_30 = float(m["sales_value_30"].sum())
    total_margin_30 = float(m["margin_30"].sum())
    qty_30, qty_prior = m["qty_30"].sum(), m["qty_prior_30"].sum()
    growth = round(((qty_30 - qty_prior) / qty_prior * 100), 1) if qty_prior else 0.0

    cat_perf = m.groupby("category")["sales_value_30"].sum().sort_values(ascending=False)
    store_perf = m.groupby("store_name")["sales_value_30"].sum().sort_values(ascending=False)

    stock_value = float(m["stock_value"].sum())
    avg_cover = float(m.loc[np.isfinite(m["weeks_of_cover"]), "weeks_of_cover"].mean())
    dead_value = float(m.loc[m["stock_status"] == "dead", "stock_value"].sum())
    overstock_value = float(m.loc[m["stock_status"] == "overstock", "stock_value"].sum())
    oos_count = int(m["out_of_stock_risk"].sum())

    recommended_purchase_value = float((m["suggested_order_qty"] * m["cost"]).sum())
    extra_order_value = float((m["extra_order_qty"] * m["cost"]).sum())

    open_po = dfs["po"][dfs["po"]["status"] == "open"]
    supplier_commitments = float((open_po["balance_qty"] * 0).sum())  # placeholder, replaced below
    if not open_po.empty:
        po_items = dfs["items"][["item_id", "cost"]]
        open_po_val = open_po.merge(po_items, on="item_id", how="left")
        supplier_commitments = float((open_po_val["balance_qty"] * open_po_val["cost"]).sum())

    actions = ai_action_center(dfs, m)
    critical = [a for a in actions if a["priority"] == "Critical"]
    high = [a for a in actions if a["priority"] == "High"]

    return {
        "sales": {
            "total_sales_30d": round(total_sales_30, 2),
            "growth_pct": growth,
            "gross_margin_pct": round((total_margin_30 / total_sales_30 * 100), 1) if total_sales_30 else 0.0,
            "top_category": cat_perf.index[0] if len(cat_perf) else None,
            "top_store": store_perf.index[0] if len(store_perf) else None,
        },
        "stock": {
            "stock_value": round(stock_value, 2),
            "avg_weeks_of_cover": round(avg_cover, 1) if not math.isnan(avg_cover) else 0,
            "dead_stock_value": round(dead_value, 2),
            "overstock_value": round(overstock_value, 2),
            "out_of_stock_risk_count": oos_count,
        },
        "buying": {
            "recommended_purchase_value": round(recommended_purchase_value, 2),
            "extra_order_value": round(extra_order_value, 2),
            "open_to_buy": None,
            "supplier_commitments": round(supplier_commitments, 2),
        },
        "ai": {
            "critical_actions": len(critical),
            "high_priority_actions": len(high),
            "growth_opportunities": len([a for a in actions if a["ai_action"] == "Increase buying"]),
            "top_recommended_decisions": actions[:5],
        },
    }


# --------------------------------------------------------------------------
# Orchestrator used by main.py to avoid recomputing the metrics frame per call
# --------------------------------------------------------------------------

def category_trend_map(dfs):
    """Best-effort growing/declining tag per category, derived from the data
    itself (used to size the 'extra order qty' buffer)."""
    m_light = dfs["sales"].merge(dfs["items"][["item_id", "category"]], on="item_id", how="left")
    today = m_light["date"].max()
    recent = m_light[m_light["date"] >= today - timedelta(days=29)].groupby("category")["quantity"].sum()
    prior = m_light[(m_light["date"] < today - timedelta(days=29)) &
                     (m_light["date"] >= today - timedelta(days=59))].groupby("category")["quantity"].sum()
    trend = {}
    for cat in recent.index:
        r, p = recent.get(cat, 0), prior.get(cat, 0)
        if p == 0:
            trend[cat] = "growing" if r > 0 else "stable"
        else:
            g = (r - p) / p
            trend[cat] = "growing" if g >= 0.15 else ("declining" if g <= -0.10 else "stable")
    return trend


def load_category_config(db):
    """Read CategoryConfig rows into the {category: {...}} shape
    set_category_config() expects. Categories with no row fall back to
    DEFAULT_SERVICE_LEVEL_PCT / no promo inside the lookup helpers."""
    rows = db.query(CategoryConfig).all()
    return {
        r.category: {
            "service_level_pct": r.service_level_pct,
            "promo_start": r.promo_start,
            "promo_end": r.promo_end,
            "promo_uplift_pct": r.promo_uplift_pct,
        }
        for r in rows
    }


def save_category_config(db, category, service_level_pct=None, promo_start=None,
                          promo_end=None, promo_uplift_pct=None):
    row = db.query(CategoryConfig).filter(CategoryConfig.category == category).first()
    if not row:
        row = CategoryConfig(category=category)
        db.add(row)
    if service_level_pct is not None:
        row.service_level_pct = service_level_pct
    if promo_start is not None:
        row.promo_start = promo_start
    if promo_end is not None:
        row.promo_end = promo_end
    if promo_uplift_pct is not None:
        row.promo_uplift_pct = promo_uplift_pct
    db.commit()
    return row
