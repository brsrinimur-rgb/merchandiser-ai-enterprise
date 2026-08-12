# Merchandiser AI Enterprise v6

## What is new

- Retail Digital Twin: simulate a proposed SKU/store purchase before approval.
- Product 360: sales trend, inventory, margin, forecast/replenishment and store positions.
- Store Performance: AI score, sales, margin, growth, dead stock, overstock and stock-out risk.
- Explainable decision results: demand, lead time, safety stock, working capital, OTB, projected revenue and margin.
- All v5 dashboards, imports, OTB, capital optimizer, forecasting and approval workflow remain available.

## Start on Windows

Double-click `run_all.bat` and keep both command windows open.

- Application: http://127.0.0.1:5500
- API docs: http://127.0.0.1:8000/docs
- Health: http://127.0.0.1:8000/api/health

## v6 API endpoints

- `GET /api/v6/product-options`
- `GET /api/v6/digital-twin?item_id=...&store_id=...&proposed_qty=...`
- `GET /api/v6/product-360/{item_id}`
- `GET /api/v6/store-performance`

## Important

The Digital Twin is a transparent planning simulation. It does not post a PO or alter stock. Approval and PO creation continue through the governed workflow.
