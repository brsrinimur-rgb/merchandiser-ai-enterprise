"""
Merchandiser AI 360 -- API layer.

Thin FastAPI wrapper around analytics.py. The metrics frame (one row per
store/item, with velocity, stock, and replenishment math already computed)
is built once at startup and cached; call POST /api/refresh after loading
new data into the DB to recompute it.
"""
import math
from datetime import date

import numpy as np
import pandas as pd
from fastapi import FastAPI, Query, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

import analytics
import import_data
from database import SessionLocal
from integration_hub import router as integration_hub_router

app = FastAPI(
    title="Merchandiser AI 360 Enterprise",
    description="Retail planning, decision intelligence and ERP Integration Hub",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(integration_hub_router)

_cache = {}


def clean(obj):
    """Recursively convert numpy/pandas types to plain JSON-safe Python types."""
    if isinstance(obj, dict):
        return {k: clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        v = float(obj)
        return None if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, pd.Timestamp):
        return obj.strftime("%Y-%m-%d")
    if obj is pd.NaT:
        return None
    try:
        if pd.isna(obj):
            return None
    except (TypeError, ValueError):
        pass
    return obj


def build_cache():
    db = SessionLocal()
    try:
        dfs = analytics.load_frames(db)
        trend_map = analytics.category_trend_map(dfs)
        analytics.set_category_trend_lookup(trend_map)
        cat_config = analytics.load_category_config(db)
        analytics.set_category_config(cat_config)
        m = analytics.build_metrics_frame(dfs)
        _cache["dfs"] = dfs
        _cache["m"] = m
        _cache["trend_map"] = trend_map
        _cache["cat_config"] = cat_config
    finally:
        db.close()


@app.on_event("startup")
def startup():
    build_cache()


@app.post("/api/refresh")
def refresh():
    build_cache()
    return {"status": "refreshed", "rows": len(_cache["m"])}


@app.get("/api/meta")
def meta():
    dfs = _cache["dfs"]
    return clean({
        "stores": dfs["stores"][["store_id", "store_name", "city", "region"]].to_dict("records"),
        "categories": sorted(dfs["items"]["category"].dropna().unique().tolist()),
        "brands": sorted(dfs["items"]["brand"].dropna().unique().tolist()),
        "as_of": analytics._today(dfs).strftime("%Y-%m-%d"),
        "sku_count": int(dfs["items"]["item_id"].nunique()),
    })


@app.get("/api/dashboard")
def dashboard():
    return clean(analytics.executive_dashboard(_cache["dfs"], _cache["m"]))


@app.get("/api/sales-intelligence")
def sales_intel():
    return clean(analytics.sales_intelligence(_cache["dfs"], _cache["m"]))


@app.get("/api/replenishment")
def replenishment(only_action: bool = Query(True)):
    return clean(analytics.auto_replenishment(_cache["m"], only_action=only_action))


@app.get("/api/stock-intelligence")
def stock_intel():
    return clean(analytics.stock_intelligence(_cache["m"]))


@app.get("/api/category-management")
def category_mgmt():
    return clean(analytics.category_management(_cache["m"]))


@app.get("/api/transfers")
def transfers():
    return clean(analytics.store_transfer_suggestions(_cache["m"]))


@app.get("/api/purchase-planning")
def purchase_planning():
    return clean(analytics.purchase_planning(_cache["dfs"], _cache["m"]))


@app.get("/api/variant-intelligence")
def variant_intel():
    return clean(analytics.variant_intelligence(_cache["m"]))


@app.get("/api/markdown-promotion")
def markdown_promo():
    return clean(analytics.markdown_promotion(_cache["m"]))


@app.get("/api/supplier-intelligence")
def supplier_intel():
    return clean(analytics.supplier_intelligence(_cache["dfs"]))


@app.get("/api/forecasting")
def forecasting():
    return clean(analytics.ai_forecasting(_cache["dfs"], _cache["m"]))


@app.get("/api/action-center")
def action_center():
    return clean(analytics.ai_action_center(_cache["dfs"], _cache["m"]))


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "service": "Merchandiser AI 360 Enterprise API",
        "version": "1.0.0",
        "cached_rows": len(_cache.get("m", [])),
    }


# --------------------------------------------------------------------------
# Real-data import layer
# --------------------------------------------------------------------------

@app.get("/api/import/entities")
def import_entities():
    """List of importable entities with their required/optional columns --
    drives the entity picker + column hints in the Data Import screen."""
    return clean(import_data.entity_list())


@app.get("/api/import/template/{entity}", response_class=PlainTextResponse)
def import_template(entity: str):
    if entity not in import_data.SCHEMAS:
        raise HTTPException(status_code=404, detail=f"Unknown entity '{entity}'")
    return import_data.template_csv(entity)


@app.post("/api/import/{entity}/validate")
async def import_validate(entity: str, file: UploadFile = File(...)):
    if entity not in import_data.SCHEMAS:
        raise HTTPException(status_code=404, detail=f"Unknown entity '{entity}'")
    content = await file.read()
    db = SessionLocal()
    try:
        report = import_data.validate_upload(entity, file.filename, content, db)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        db.close()
    return clean(report)


@app.post("/api/import/commit")
def import_commit(token: str = Form(...)):
    db = SessionLocal()
    try:
        result = import_data.commit_upload(token, db)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        db.close()
    # data changed -- recompute the metrics frame so every screen reflects it
    build_cache()
    return clean(result)


# --------------------------------------------------------------------------
# Replenishment configuration (service level targets + promotions)
# --------------------------------------------------------------------------

@app.get("/api/config/categories")
def get_category_config():
    return clean(_cache.get("cat_config", {}))


@app.post("/api/config/categories/{category}")
def set_category_config_endpoint(
    category: str,
    service_level_pct: float = Form(None),
    promo_start: str = Form(None),
    promo_end: str = Form(None),
    promo_uplift_pct: float = Form(None),
):
    db = SessionLocal()
    try:
        p_start = date.fromisoformat(promo_start) if promo_start else None
        p_end = date.fromisoformat(promo_end) if promo_end else None
        analytics.save_category_config(
            db, category, service_level_pct=service_level_pct,
            promo_start=p_start, promo_end=p_end, promo_uplift_pct=promo_uplift_pct,
        )
    finally:
        db.close()
    build_cache()
    return clean(_cache["cat_config"].get(category, {}))
