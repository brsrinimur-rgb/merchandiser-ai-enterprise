
from __future__ import annotations
from typing import Dict
from .base import IntegrationConnector

_CONNECTORS: Dict[str, IntegrationConnector]={}

def register(connector:IntegrationConnector): _CONNECTORS[connector.key]=connector

def get(key:str)->IntegrationConnector:
    if key not in _CONNECTORS: raise KeyError(f"Unknown connector '{key}'")
    return _CONNECTORS[key]

def all_connectors(): return list(_CONNECTORS.values())
