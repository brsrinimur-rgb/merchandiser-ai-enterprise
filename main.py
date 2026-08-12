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
import workflow
from database import SessionLocal
from integrations.d365.router import router as d365_router
from integrations.hub_router import router as integration_hub_router

app = FastAPI(
    title="Merchandiser AI Enterprise v11",
    version="11.0.0",
    description="Enterprise Retail Planning, Buying, Inventory Optimization and Microsoft D365 Integration Platform",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(d365_router)
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


@app.get("/api/otb")
def otb():
    return clean(analytics.category_otb(_cache["dfs"], _cache["m"]))


@app.get("/api/capital-optimizer")
def capital_optimizer():
    otb_rows = analytics.category_otb(_cache["dfs"], _cache["m"])
    return clean(analytics.inventory_capital_optimizer(_cache["dfs"], _cache["m"], otb_rows=otb_rows))


@app.get("/api/health")
def health():
    return {"status": "ok"}


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
    monthly_budget: float = Form(None),
    closing_stock_target: str = Form(None),  # string so "" means "clear it"
):
    db = SessionLocal()
    try:
        p_start = date.fromisoformat(promo_start) if promo_start else None
        p_end = date.fromisoformat(promo_end) if promo_end else None
        clear_target = closing_stock_target is not None and closing_stock_target.strip() == ""
        target_val = float(closing_stock_target) if closing_stock_target not in (None, "") else None
        analytics.save_category_config(
            db, category, service_level_pct=service_level_pct,
            promo_start=p_start, promo_end=p_end, promo_uplift_pct=promo_uplift_pct,
            monthly_budget=monthly_budget, closing_stock_target=target_val,
            clear_closing_stock_target=clear_target,
        )
    finally:
        db.close()
    build_cache()
    return clean(_cache["cat_config"].get(category, {}))


# --------------------------------------------------------------------------
# Approval & Decision Workflow (v4)
#
#   AI Recommendation -> Merchandiser Review -> Buyer Review -> Final Approval -> PO Ready
#                                             \-> Rejected (from any stage)
# --------------------------------------------------------------------------

@app.post("/api/workflow/sync")
def workflow_sync():
    """Pull the Capital Optimizer's current recommendations into the review
    queue. Safe to call repeatedly -- store/items already in an open decision
    are left alone."""
    otb_rows = analytics.category_otb(_cache["dfs"], _cache["m"])
    result = analytics.inventory_capital_optimizer(_cache["dfs"], _cache["m"], otb_rows=otb_rows)
    db = SessionLocal()
    try:
        summary = workflow.sync_recommendations(db, result)
    finally:
        db.close()
    return clean(summary)


@app.get("/api/workflow/decisions")
def workflow_decisions(status: str = Query(None)):
    db = SessionLocal()
    try:
        return clean(workflow.list_decisions(db, status=status))
    finally:
        db.close()


@app.get("/api/workflow/decisions/{decision_id}")
def workflow_decision_detail(decision_id: int):
    db = SessionLocal()
    try:
        decision = workflow.get_decision(db, decision_id)
        if not decision:
            raise HTTPException(status_code=404, detail=f"Decision {decision_id} not found")
        return clean({
            "decision": workflow.decision_to_dict(decision),
            "history": workflow.get_history(db, decision_id),
        })
    finally:
        db.close()


@app.post("/api/workflow/decisions/{decision_id}/action")
def workflow_action(
    decision_id: int,
    stage: str = Form(...),
    action: str = Form(...),
    actor: str = Form(...),
    qty: int = Form(None),
    reason: str = Form(None),
):
    db = SessionLocal()
    try:
        result = workflow.apply_action(db, decision_id, stage, action, actor, qty=qty, reason=reason)
    except workflow.WorkflowError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        db.close()
    # a decision reaching po_ready creates a real PurchaseOrder row, which
    # changes commitments/incoming-stock everywhere else -- keep it consistent
    build_cache()
    return clean(result)


@app.get("/api/workflow/summary")
def workflow_summary_endpoint():
    db = SessionLocal()
    try:
        return clean(workflow.workflow_summary(db))
    finally:
        db.close()


# --------------------------------------------------------------------------
# v6 Decision Experience: Digital Twin, Product 360, Store Performance
# --------------------------------------------------------------------------

@app.get("/api/v6/product-options")
def v6_product_options():
    m = _cache["m"]
    items = (
        m[["item_id", "item_code", "item_name", "category"]]
        .drop_duplicates("item_id")
        .sort_values(["category", "item_name"])
    )
    stores = _cache["dfs"]["stores"][["store_id", "store_name"]].sort_values("store_name")
    return clean({"items": items.to_dict("records"), "stores": stores.to_dict("records")})


@app.get("/api/v6/digital-twin")
def v6_digital_twin(item_id: int, store_id: int, proposed_qty: int = Query(..., ge=0)):
    m = _cache["m"]
    match = m[(m["item_id"] == item_id) & (m["store_id"] == store_id)]
    if match.empty:
        raise HTTPException(status_code=404, detail="Item/store inventory position not found")
    r = match.iloc[0]
    current_stock = float(r.get("on_hand", 0) or 0)
    incoming = float(r.get("incoming_stock", 0) or 0)
    ads = float(r.get("ads_30", 0) or 0)
    cost = float(r.get("cost", 0) or 0)
    price = float(r.get("retail_price", 0) or 0)
    forecast_30 = float(r.get("forecast_demand_30d", ads * 30) or 0)
    projected_available = current_stock + incoming + proposed_qty
    projected_sales_units = min(projected_available, forecast_30)
    projected_revenue = projected_sales_units * price
    projected_cogs = projected_sales_units * cost
    projected_margin = projected_revenue - projected_cogs
    closing_units = max(projected_available - forecast_30, 0)
    cover_days = projected_available / ads if ads > 0 else 999
    sell_through = projected_sales_units / projected_available * 100 if projected_available > 0 else 0
    working_capital = proposed_qty * cost
    category = r.get("category")
    otb_rows = analytics.category_otb(_cache["dfs"], m)
    otb = next((x for x in otb_rows if x["category"] == category), None)
    available_otb = otb.get("available_otb") if otb else None
    risk = "Low"
    if cover_days < float(r.get("lead_time_days", 14) or 14): risk = "Stock-out risk"
    elif cover_days > 90: risk = "Overstock risk"
    elif available_otb is not None and working_capital > available_otb: risk = "OTB exception"
    confidence = max(55, min(98, round(92 - min(float(r.get("days_out_of_stock_30",0) or 0),20)*1.5 - min(float(r.get("lt_std_actual",0) or 0),15))))
    baseline_qty = int(r.get("suggested_order_qty", 0) or 0)
    return clean({
        "item_id": item_id, "item_code": r.get("item_code"), "item_name": r.get("item_name"),
        "store_id": store_id, "store_name": r.get("store_name"), "category": category,
        "baseline_recommended_qty": baseline_qty, "proposed_qty": proposed_qty,
        "current_stock": current_stock, "incoming_stock": incoming,
        "forecast_30d_units": round(forecast_30,1), "projected_sales_units": round(projected_sales_units,1),
        "projected_closing_units": round(closing_units,1), "projected_cover_days": round(cover_days,1),
        "projected_sell_through_pct": round(sell_through,1), "working_capital_impact": round(working_capital,2),
        "projected_revenue": round(projected_revenue,2), "projected_gross_margin": round(projected_margin,2),
        "available_otb": available_otb, "risk": risk, "confidence_pct": confidence,
        "explanation": [
            f"Demand rate is {ads:.2f} units/day based on stockout-corrected recent sales.",
            f"Lead time is {float(r.get('lt_mean_actual', r.get('lead_time_days',14)) or 14):.1f} days with safety stock {float(r.get('safety_stock',0) or 0):.0f} units.",
            f"The proposed order uses SAR {working_capital:,.0f} of working capital and results in {cover_days:.1f} days of cover.",
            f"Expected 30-day sell-through is {sell_through:.1f}% with projected gross margin SAR {projected_margin:,.0f}."
        ]
    })


@app.get("/api/v6/product-360/{item_id}")
def v6_product_360(item_id: int):
    m = _cache["m"]
    rows = m[m["item_id"] == item_id].copy()
    if rows.empty:
        raise HTTPException(status_code=404, detail="Item not found")
    r = rows.iloc[0]
    sales = _cache["dfs"]["sales"]
    sales = sales[sales["item_id"] == item_id].copy()
    if not sales.empty:
        trend = sales.groupby("date").agg(quantity=("quantity","sum"), sales_value=("sales_value","sum"), margin=("margin","sum")).reset_index().tail(45)
    else:
        trend = sales
    store_positions = rows[["store_id","store_name","on_hand","incoming_stock","ads_30","weeks_of_cover","stock_status","suggested_order_qty","emergency_order_qty","sales_value_30","margin_30"]].copy()
    for c in ["ads_30","weeks_of_cover","sales_value_30","margin_30"]:
        store_positions[c] = store_positions[c].replace([np.inf,-np.inf], None)
    return clean({
        "product": {k: r.get(k) for k in ["item_id","item_code","item_name","brand","category","subcategory","collection","color","size","cost","retail_price","lead_time_days","moq","case_pack"]},
        "summary": {
            "sales_30d": float(rows["sales_value_30"].sum()), "margin_30d": float(rows["margin_30"].sum()),
            "stock_units": float(rows["on_hand"].sum()), "stock_value": float(rows["stock_value"].sum()),
            "suggested_order_qty": int(rows["suggested_order_qty"].sum()), "emergency_qty": int(rows["emergency_order_qty"].sum()),
            "stores_at_risk": int(((rows["out_of_stock_risk"] == True) | (rows["stock_status"] == "understock")).sum())
        },
        "sales_trend": trend.to_dict("records") if not trend.empty else [],
        "store_positions": store_positions.sort_values("weeks_of_cover").to_dict("records")
    })


@app.get("/api/v6/store-performance")
def v6_store_performance():
    m = _cache["m"].copy()
    grouped = m.groupby(["store_id","store_name"], as_index=False).agg(
        sales_value_30=("sales_value_30","sum"), margin_30=("margin_30","sum"), stock_value=("stock_value","sum"),
        qty_30=("qty_30","sum"), qty_prior_30=("qty_prior_30","sum"), stockout_risks=("out_of_stock_risk","sum"),
        dead_value=("stock_value", lambda s: float(s[m.loc[s.index,"stock_status"]=="dead"].sum())),
        overstock_value=("stock_value", lambda s: float(s[m.loc[s.index,"stock_status"]=="overstock"].sum()))
    )
    grouped["growth_pct"] = np.where(grouped["qty_prior_30"]>0,(grouped["qty_30"]-grouped["qty_prior_30"])/grouped["qty_prior_30"]*100,0)
    grouped["margin_pct"] = np.where(grouped["sales_value_30"]>0,grouped["margin_30"]/grouped["sales_value_30"]*100,0)
    sales_rank = grouped["sales_value_30"].rank(pct=True)*100
    margin_rank = grouped["margin_pct"].rank(pct=True)*100
    risk_penalty = np.minimum(grouped["stockout_risks"]*3 + grouped["overstock_value"]/grouped["stock_value"].replace(0,1)*30,40)
    grouped["ai_score"] = (sales_rank*.45 + margin_rank*.35 + 20 - risk_penalty).clip(0,100).round(1)
    return clean(grouped.sort_values("ai_score",ascending=False).to_dict("records"))


# --------------------------------------------------------------------------
# v7 Decision Experience: executive intelligence, explainability, scenarios,
# and a unified decision timeline. These endpoints are read-only simulations.
# --------------------------------------------------------------------------

@app.get("/api/v7/executive-intelligence")
def v7_executive_intelligence():
    d = analytics.executive_dashboard(_cache["dfs"], _cache["m"])
    m = _cache["m"]
    otb_rows = analytics.category_otb(_cache["dfs"], m)
    capital = analytics.inventory_capital_optimizer(_cache["dfs"], m, otb_rows=otb_rows)
    sales = d["sales"]; stock = d["stock"]; buying = d["buying"]
    stock_value = float(stock.get("stock_value") or 0)
    dead = float(stock.get("dead_stock_value") or 0)
    over = float(stock.get("overstock_value") or 0)
    oos = int(stock.get("out_of_stock_risk_count") or 0)
    margin = float(sales.get("gross_margin_pct") or 0)
    growth = float(sales.get("growth_pct") or 0)
    available_otb = buying.get("open_to_buy")
    inventory_health = max(0, min(100, 100 - ((dead + over) / max(stock_value, 1) * 70) - min(oos, 50) * .6))
    financial_health = max(0, min(100, margin + 35 + max(-10, min(growth, 20)) * .5))
    supply_health = max(0, min(100, 100 - min(oos, 80) * .9))
    otb_health = 82 if available_otb is None else max(0, min(100, 55 + (1 if available_otb >= 0 else -1) * min(abs(float(available_otb))/1_000_000*8, 35)))
    business_health = round(financial_health*.30 + inventory_health*.30 + supply_health*.20 + otb_health*.20, 1)
    potential_recovery = dead*.55 + over*.18 + float(capital["summary"].get("capital_freed_by_transfer") or 0)
    revenue_at_risk = float(m.loc[m["out_of_stock_risk"] == True, "ads_30"].mul(m.loc[m["out_of_stock_risk"] == True, "retail_price"]).sum() * 30)
    return clean({
        "health": {"business": business_health, "financial": round(financial_health,1), "inventory": round(inventory_health,1), "supply_chain": round(supply_health,1), "otb": round(otb_health,1)},
        "capital": {"revenue_at_risk": round(revenue_at_risk,2), "inventory_at_risk": round(dead+over,2), "cash_locked": round(stock_value,2), "potential_recovery": round(potential_recovery,2), "otb_available": available_otb},
        "priorities": {"emergency_orders": int((m["emergency_order_qty"]>0).sum()), "otb_exceptions": int(capital["summary"].get("exception_count") or 0), "transfer_opportunities": len(analytics.store_transfer_suggestions(m)), "supplier_risks": len([x for x in analytics.supplier_intelligence(_cache["dfs"]) if (x.get("supplier_score") or 0)<70])},
        "briefing": f"Sales are {growth:.1f}% versus the prior 30 days at {margin:.1f}% gross margin. {oos} store-SKU positions are at stock-out risk. Transfer-first optimization can avoid SAR {float(capital['summary'].get('capital_freed_by_transfer') or 0):,.0f} of new buying, while dead and overstock inventory total SAR {dead+over:,.0f}."
    })

@app.get("/api/v7/explainability")
def v7_explainability(item_id: int, store_id: int):
    m=_cache["m"]
    q=m[(m["item_id"]==item_id)&(m["store_id"]==store_id)]
    if q.empty: raise HTTPException(status_code=404, detail="Item/store position not found")
    r=q.iloc[0]
    qty=int(r.get("suggested_order_qty",0) or 0)
    revenue=qty*float(r.get("retail_price",0) or 0)
    margin=qty*max(float(r.get("retail_price",0) or 0)-float(r.get("cost",0) or 0),0)
    confidence=max(55,min(98,round(95-min(float(r.get("days_out_of_stock_30",0) or 0),20)*1.4-min(float(r.get("lt_std_actual",0) or 0),15)*1.2)))
    return clean({
      "item_code":r.get("item_code"),"item_name":r.get("item_name"),"store_name":r.get("store_name"),"category":r.get("category"),
      "recommendation":f"Order {qty} units" if qty else "No new order required","confidence_pct":confidence,
      "drivers":{"sales_growth_pct":r.get("growth_pct"),"current_stock":r.get("on_hand"),"incoming_stock":r.get("incoming_stock"),"lead_time_days":r.get("lt_mean_actual"),"safety_stock":r.get("safety_stock"),"forecast_30d":r.get("forecast_demand_30d"),"service_level_pct":r.get("service_level_pct"),"promotion_multiplier":r.get("promo_mult_lt"),"days_out_of_stock_30":r.get("days_out_of_stock_30")},
      "financial_impact":{"working_capital":qty*float(r.get("cost",0) or 0),"potential_revenue":revenue,"potential_margin":margin},
      "reasoning":[f"Demand is {float(r.get('ads_30',0) or 0):.2f} units/day after correcting for stock-out days.",f"Inventory position is {float(r.get('inventory_position',0) or 0):.0f} units versus a reorder point of {float(r.get('reorder_point',0) or 0):.1f}.",f"The engine applies a {float(r.get('service_level_pct',95) or 95):.1f}% service target, observed lead-time variability, case-pack and MOQ controls."]
    })

@app.get("/api/v7/digital-twin/compare")
def v7_digital_twin_compare(item_id:int, store_id:int):
    m=_cache["m"]; q=m[(m["item_id"]==item_id)&(m["store_id"]==store_id)]
    if q.empty: raise HTTPException(status_code=404, detail="Item/store inventory position not found")
    base=int(q.iloc[0].get("suggested_order_qty",0) or 0)
    scenarios=[("No Buy",0),("Recommended",base),("Increase 20%",round(base*1.2)),("Reduce 15%",round(base*.85))]
    return clean({"baseline_qty":base,"scenarios":[{"name":n,**v6_digital_twin(item_id,store_id,max(0,int(qty)))} for n,qty in scenarios]})

@app.get("/api/v7/decision-timeline")
def v7_decision_timeline(limit:int=Query(25,ge=1,le=100)):
    db=SessionLocal()
    try:
        decisions=workflow.list_decisions(db)[:limit]
        events=[]
        for d in decisions:
            hist=workflow.get_history(db,d["id"])
            for h in hist:
                events.append({"decision_id":d["id"],"item_code":d.get("item_code"),"item_name":d.get("item_name"),"store_name":d.get("store_name"),"stage":h.get("stage"),"action":h.get("action"),"actor":h.get("actor"),"reason":h.get("reason"),"timestamp":h.get("timestamp"),"qty":h.get("new_qty")})
        events.sort(key=lambda x:x.get("timestamp") or "", reverse=True)
        return clean(events[:limit])
    finally: db.close()
