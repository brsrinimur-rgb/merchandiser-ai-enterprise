from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

@dataclass
class ConnectorInfo:
    key: str
    name: str
    system_type: str
    supports_pull: list[str] = field(default_factory=list)
    supports_push: list[str] = field(default_factory=list)
    configured: bool = False
    missing: list[str] = field(default_factory=list)
    mode: str = "read-only"
    description: str = ""
    vendor: str = ""
    auth_type: str = ""
    category: str = "ERP"
    installed: bool = True

class IntegrationConnector(ABC):
    key: str
    @abstractmethod
    def info(self) -> ConnectorInfo: ...
    @abstractmethod
    def test_connection(self) -> dict[str, Any]: ...
    @abstractmethod
    def pull(self, entity: str, *, dry_run: bool = True, top: int | None = 100,
             filter_expression: str | None = None) -> dict[str, Any]: ...
    @abstractmethod
    def push(self, entity: str, payload: dict[str, Any]) -> dict[str, Any]: ...
