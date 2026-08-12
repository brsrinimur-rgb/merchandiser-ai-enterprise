"""Small, production-oriented OData client for D365 Finance & Supply Chain."""
from __future__ import annotations

from typing import Any, Dict, Iterable, Iterator, Optional
from urllib.parse import urljoin

import requests

from .auth import D365TokenProvider
from .config import D365Settings


class D365ApiError(RuntimeError):
    def __init__(self, status_code: int, message: str, response_text: str = ""):
        super().__init__(f"D365 API {status_code}: {message}")
        self.status_code = status_code
        self.response_text = response_text


class D365Client:
    def __init__(self, settings: D365Settings):
        self.settings = settings
        self.tokens = D365TokenProvider(settings)
        self.session = requests.Session()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.tokens.get_access_token()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "OData-MaxVersion": "4.0",
            "OData-Version": "4.0",
        }

    def _raise_for_response(self, response: requests.Response) -> None:
        if response.ok:
            return
        try:
            body = response.json()
            message = body.get("error", {}).get("message") or body.get("Message") or response.reason
        except Exception:
            message = response.reason
        raise D365ApiError(response.status_code, str(message), response.text[:2000])

    def test_connection(self) -> dict[str, Any]:
        url = f"{self.settings.data_url}/$metadata"
        response = self.session.get(
            url,
            headers=self._headers(),
            timeout=self.settings.timeout_seconds,
            verify=self.settings.verify_ssl,
        )
        self._raise_for_response(response)
        return {"connected": True, "status_code": response.status_code, "resource_url": self.settings.resource_url}

    def iter_entity(
        self,
        entity: str,
        *,
        select: Optional[Iterable[str]] = None,
        filter_expression: Optional[str] = None,
        top: Optional[int] = None,
        page_size: int = 5000,
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> Iterator[dict[str, Any]]:
        url = f"{self.settings.data_url}/{entity}"
        params: Dict[str, Any] = {"$top": min(page_size, top) if top else page_size}
        if self.settings.company:
            params["cross-company"] = "true"
        if select:
            params["$select"] = ",".join(select)
        if filter_expression:
            params["$filter"] = filter_expression
        if extra_params:
            params.update(extra_params)

        yielded = 0
        while url:
            response = self.session.get(
                url,
                headers=self._headers(),
                params=params,
                timeout=self.settings.timeout_seconds,
                verify=self.settings.verify_ssl,
            )
            self._raise_for_response(response)
            body = response.json()
            rows = body.get("value", [])
            for row in rows:
                yield row
                yielded += 1
                if top is not None and yielded >= top:
                    return
            next_link = body.get("@odata.nextLink")
            url = next_link if next_link else ""
            params = None  # nextLink already contains query parameters

    def get_entity(self, entity: str, **kwargs: Any) -> list[dict[str, Any]]:
        return list(self.iter_entity(entity, **kwargs))

    def create(self, entity: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.settings.data_url}/{entity}"
        response = self.session.post(
            url,
            headers=self._headers(),
            json=payload,
            timeout=self.settings.timeout_seconds,
            verify=self.settings.verify_ssl,
        )
        self._raise_for_response(response)
        return response.json() if response.content else {"created": True}
