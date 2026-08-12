"""D365 connection and entity configuration loaded from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict

try:
    from dotenv import load_dotenv
except ImportError:  # package can still be imported before dependencies are installed
    def load_dotenv(*args, **kwargs):
        return False

_BACKEND_DIR = Path(__file__).resolve().parents[2]
load_dotenv(_BACKEND_DIR / ".env")


@dataclass(frozen=True)
class D365Settings:
    tenant_id: str
    client_id: str
    client_secret: str
    resource_url: str
    company: str
    timeout_seconds: int = 60
    verify_ssl: bool = True

    @property
    def authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}"

    @property
    def scope(self) -> list[str]:
        return [f"{self.resource_url.rstrip('/')}/.default"]

    @property
    def data_url(self) -> str:
        return f"{self.resource_url.rstrip('/')}/data"

    def missing(self) -> list[str]:
        values = {
            "D365_TENANT_ID": self.tenant_id,
            "D365_CLIENT_ID": self.client_id,
            "D365_CLIENT_SECRET": self.client_secret,
            "D365_RESOURCE_URL": self.resource_url,
        }
        return [k for k, v in values.items() if not v]


ENTITY_NAMES: Dict[str, str] = {
    "stores": os.getenv("D365_ENTITY_STORES", "RetailStores"),
    "suppliers": os.getenv("D365_ENTITY_SUPPLIERS", "VendorsV2"),
    "items": os.getenv("D365_ENTITY_ITEMS", "ReleasedProductsV2"),
    "sales": os.getenv("D365_ENTITY_SALES", "RetailTransactions"),
    "stock": os.getenv("D365_ENTITY_STOCK", "InventOnHand"),
    "purchase_orders": os.getenv("D365_ENTITY_PURCHASE_ORDERS", "PurchaseOrderLinesV2"),
    "purchase_order_headers": os.getenv("D365_ENTITY_PO_HEADERS", "PurchaseOrderHeadersV2"),
}


def get_settings() -> D365Settings:
    return D365Settings(
        tenant_id=os.getenv("D365_TENANT_ID", "").strip(),
        client_id=os.getenv("D365_CLIENT_ID", "").strip(),
        client_secret=os.getenv("D365_CLIENT_SECRET", "").strip(),
        resource_url=os.getenv("D365_RESOURCE_URL", "").strip().rstrip("/"),
        company=os.getenv("D365_COMPANY", "").strip(),
        timeout_seconds=int(os.getenv("D365_TIMEOUT_SECONDS", "60")),
        verify_ssl=os.getenv("D365_VERIFY_SSL", "true").lower() not in {"0", "false", "no"},
    )
