"""Enterprise connector registry with safe pull/push controls.

D365 is configuration-aware and remains read-only unless write-back is explicitly
enabled. The Sandbox connector makes the full UI and API testable without secrets.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/integration-hub", tags=["Enterprise Integration Hub"])
LOG_FILE = Path(__file__).resolve().parent / "integration_logs.json"


class ConnectorMetrics(BaseModel):
    last_successful_sync: str | None = None
    failed_operations: int = 0
    records_pulled: int = 0
    records_pushed: int = 0


class Connector(BaseModel):
    id: str
    name: str
    vendor: str
    status: Literal["ready", "sandbox", "configuration_required"]
    mode: Literal["live", "sandbox"]
    authentication: str
    pull_enabled: bool
    push_enabled: bool
    pull_entities: list[str]
    push_entities: list[str]
    missing_configuration: list[str] = Field(default_factory=list)
    metrics: ConnectorMetrics = Field(default_factory=ConnectorMetrics)


class ConnectorListResponse(BaseModel):
    connectors: list[Connector]
    total: int


class TestResponse(BaseModel):
    connector: str
    success: bool
    status: str
    message: str
    tested_at: str


class SyncResponse(BaseModel):
    connector: str
    entity: str
    direction: Literal["pull", "push"]
    dry_run: bool
    status: str
    records: int
    data: list[dict[str, Any]] = Field(default_factory=list)
    message: str
    completed_at: str


class PushRequest(BaseModel):
    confirmation: str = Field(description="Must be PUSH for a live operation")
    dry_run: bool = True
    records: list[dict[str, Any]] = Field(default_factory=list)


class LogEntry(BaseModel):
    timestamp: str
    connector: str
    entity: str
    direction: str
    status: str
    records: int
    message: str


class LogResponse(BaseModel):
    logs: list[LogEntry]
    total: int


D365_PULL = ["products", "inventory", "sales", "vendors", "purchase-orders", "transfer-orders", "stores", "warehouses"]
D365_PUSH = ["purchase-orders", "transfer-orders"]
SANDBOX_ROWS = {
    "products": [{"item_number": "SKU-1001", "name": "Demo Product", "category": "Accessories"}],
    "inventory": [{"item_number": "SKU-1001", "store": "Riyadh", "available_qty": 18}],
    "sales": [{"item_number": "SKU-1001", "store": "Riyadh", "quantity": 3, "sales_amount": 1250}],
    "vendors": [{"vendor_account": "V-001", "name": "Demo Supplier"}],
    "purchase-orders": [{"po_number": "PO-DEMO-001", "status": "Open", "amount": 9500}],
    "transfer-orders": [{"transfer_number": "TO-DEMO-001", "from": "Jeddah", "to": "Riyadh"}],
    "stores": [{"store_number": "601", "name": "Riyadh Demo Store"}],
    "warehouses": [{"warehouse": "WH-RUH", "name": "Riyadh DC"}],
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_logs() -> list[dict[str, Any]]:
    try:
        return json.loads(LOG_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def write_log(connector: str, entity: str, direction: str, status: str, records: int, message: str) -> None:
    logs = read_logs()
    logs.insert(0, {"timestamp": now(), "connector": connector, "entity": entity, "direction": direction,
                    "status": status, "records": records, "message": message})
    LOG_FILE.write_text(json.dumps(logs[:500], indent=2), encoding="utf-8")


def d365_config() -> tuple[dict[str, str], list[str]]:
    keys = ["D365_TENANT_ID", "D365_CLIENT_ID", "D365_CLIENT_SECRET", "D365_RESOURCE_URL", "D365_COMPANY"]
    cfg = {key: os.getenv(key, "").strip() for key in keys}
    return cfg, [key for key, value in cfg.items() if not value]


def metrics_for(connector_id: str) -> ConnectorMetrics:
    rows = [row for row in read_logs() if row.get("connector") == connector_id]
    successful = [row for row in rows if row.get("status") == "success"]
    return ConnectorMetrics(
        last_successful_sync=successful[0]["timestamp"] if successful else None,
        failed_operations=sum(row.get("status") == "failed" for row in rows),
        records_pulled=sum(row.get("records", 0) for row in successful if row.get("direction") == "pull"),
        records_pushed=sum(row.get("records", 0) for row in successful if row.get("direction") == "push"),
    )


def connector_rows() -> list[Connector]:
    _, missing = d365_config()
    writeback = os.getenv("D365_ENABLE_WRITEBACK", "false").lower() == "true"
    return [
        Connector(id="sandbox", name="Sandbox Connector", vendor="Merchandiser AI", status="sandbox", mode="sandbox",
                  authentication="None", pull_enabled=True, push_enabled=True,
                  pull_entities=list(SANDBOX_ROWS), push_entities=D365_PUSH, metrics=metrics_for("sandbox")),
        Connector(id="d365", name="Microsoft Dynamics 365 Finance & Supply Chain", vendor="Microsoft",
                  status="configuration_required" if missing else "ready", mode="live",
                  authentication="OAuth 2.0 / Microsoft Entra ID", pull_enabled=not missing,
                  push_enabled=not missing and writeback, pull_entities=D365_PULL, push_entities=D365_PUSH,
                  missing_configuration=missing, metrics=metrics_for("d365")),
    ]


def json_request(url: str, data: dict[str, str] | None = None, token: str | None = None) -> dict[str, Any]:
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers), timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        raise HTTPException(status_code=502, detail=f"D365 returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach D365: {exc.reason}") from exc


def d365_token(cfg: dict[str, str]) -> str:
    url = f"https://login.microsoftonline.com/{cfg['D365_TENANT_ID']}/oauth2/v2.0/token"
    result = json_request(url, {"grant_type": "client_credentials", "client_id": cfg["D365_CLIENT_ID"],
                                "client_secret": cfg["D365_CLIENT_SECRET"],
                                "scope": f"{cfg['D365_RESOURCE_URL'].rstrip('/')}/.default"})
    if "access_token" not in result:
        raise HTTPException(status_code=502, detail="Microsoft Entra ID did not return an access token")
    return result["access_token"]


ENTITY_MAP = {
    "products": "ReleasedProductsV2", "inventory": "InventOnHand",
    "sales": "RetailTransactionSalesTrans", "vendors": "VendorsV2",
    "purchase-orders": "PurchaseOrderHeadersV2", "transfer-orders": "TransferOrderHeaders",
    "stores": "RetailStores", "warehouses": "Warehouses",
}


@router.get("/connectors", response_model=ConnectorListResponse)
def connectors() -> ConnectorListResponse:
    rows = connector_rows()
    return ConnectorListResponse(connectors=rows, total=len(rows))


@router.get("/status", response_model=ConnectorListResponse)
def status() -> ConnectorListResponse:
    return connectors()


@router.post("/{connector}/test", response_model=TestResponse)
def test_connection(connector: Literal["sandbox", "d365"]) -> TestResponse:
    if connector == "sandbox":
        return TestResponse(connector=connector, success=True, status="connected", message="Sandbox is ready", tested_at=now())
    cfg, missing = d365_config()
    if missing:
        return TestResponse(connector=connector, success=False, status="configuration_required",
                            message="Missing: " + ", ".join(missing), tested_at=now())
    d365_token(cfg)
    return TestResponse(connector=connector, success=True, status="connected", message="D365 authentication succeeded", tested_at=now())


@router.post("/{connector}/pull/{entity}", response_model=SyncResponse)
def pull(connector: Literal["sandbox", "d365"], entity: str, dry_run: bool = True,
         max_records: int = Query(100, ge=1, le=5000), odata_filter: str | None = None) -> SyncResponse:
    if entity not in D365_PULL:
        raise HTTPException(status_code=404, detail=f"Unsupported entity: {entity}")
    if connector == "sandbox":
        data = (SANDBOX_ROWS.get(entity) or [])[:max_records]
    else:
        cfg, missing = d365_config()
        if missing:
            raise HTTPException(status_code=400, detail="D365 configuration is incomplete: " + ", ".join(missing))
        params = {"$top": str(max_records), "$filter": f"dataAreaId eq '{cfg['D365_COMPANY']}'"}
        if odata_filter:
            params["$filter"] += f" and ({odata_filter})"
        url = f"{cfg['D365_RESOURCE_URL'].rstrip('/')}/data/{ENTITY_MAP[entity]}?{urllib.parse.urlencode(params)}"
        data = json_request(url, token=d365_token(cfg)).get("value", [])
    message = "Preview completed; no data was committed" if dry_run else "Pull completed"
    write_log(connector, entity, "pull", "success", len(data), message)
    return SyncResponse(connector=connector, entity=entity, direction="pull", dry_run=dry_run,
                        status="success", records=len(data), data=data[:100], message=message, completed_at=now())


@router.post("/{connector}/push/{entity}", response_model=SyncResponse)
def push(connector: Literal["sandbox", "d365"], entity: str, request: PushRequest) -> SyncResponse:
    if entity not in D365_PUSH:
        raise HTTPException(status_code=404, detail=f"Unsupported push entity: {entity}")
    if not request.dry_run and request.confirmation != "PUSH":
        raise HTTPException(status_code=400, detail="Type PUSH to confirm a live operation")
    if connector == "d365" and not request.dry_run:
        _, missing = d365_config()
        if missing:
            raise HTTPException(status_code=400, detail="D365 configuration is incomplete")
        if os.getenv("D365_ENABLE_WRITEBACK", "false").lower() != "true":
            raise HTTPException(status_code=403, detail="D365 write-back is disabled. Set D365_ENABLE_WRITEBACK=true after UAT approval.")
        raise HTTPException(status_code=501, detail="Live D365 write-back requires approved entity field mappings; use dry-run until configured.")
    message = "Push payload validated; nothing was written" if request.dry_run else "Sandbox push completed"
    write_log(connector, entity, "push", "success", len(request.records), message)
    return SyncResponse(connector=connector, entity=entity, direction="push", dry_run=request.dry_run,
                        status="success", records=len(request.records), data=request.records[:100],
                        message=message, completed_at=now())


@router.get("/logs", response_model=LogResponse)
def logs(connector: str | None = None, status: str | None = None) -> LogResponse:
    rows = read_logs()
    if connector:
        rows = [row for row in rows if row.get("connector") == connector]
    if status:
        rows = [row for row in rows if row.get("status") == status]
    return LogResponse(logs=rows, total=len(rows))
