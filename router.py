"""FastAPI routes for D365 connection, dry-run and controlled synchronization."""
from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Query

from database import SessionLocal

from .client import D365Client
from .config import ENTITY_NAMES, get_settings
from .service import D365SyncError, build_purchase_order_payload, sync_entity

router = APIRouter(prefix="/api/integrations/d365", tags=["D365 Integration"])


@router.get("/status")
def status():
    settings = get_settings()
    return {
        "configured": not bool(settings.missing()),
        "missing": settings.missing(),
        "resource_url": settings.resource_url or None,
        "company": settings.company or None,
        "entities": ENTITY_NAMES,
        "mode": "read-only until explicitly enabled",
    }


@router.post("/test")
def test_connection():
    settings = get_settings()
    if settings.missing():
        raise HTTPException(status_code=400, detail=f"Missing configuration: {', '.join(settings.missing())}")
    try:
        return D365Client(settings).test_connection()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/sync/{entity}")
def sync(
    entity: str,
    dry_run: bool = Query(True, description="Keep true until mapping preview is approved"),
    top: int | None = Query(100, ge=1, le=50000),
    filter_expression: str | None = Query(None, description="Optional OData $filter"),
):
    if entity not in ENTITY_NAMES or entity == "purchase_order_headers":
        raise HTTPException(status_code=404, detail=f"Supported entities: {', '.join(k for k in ENTITY_NAMES if k != 'purchase_order_headers')}")
    db = SessionLocal()
    try:
        return sync_entity(db, entity, dry_run=dry_run, top=top, filter_expression=filter_expression)
    except D365SyncError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    finally:
        db.close()


@router.get("/po-payload/{decision_id}")
def po_payload(decision_id: int):
    db = SessionLocal()
    try:
        return build_purchase_order_payload(db, decision_id)
    except D365SyncError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        db.close()
