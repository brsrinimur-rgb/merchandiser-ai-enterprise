from __future__ import annotations
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, ConfigDict
from typing import Any, Literal
from integrations.common.registry import register, get, all_connectors
from integrations.common.log_store import write_log, list_logs, get_log, connector_metrics
from integrations.d365.connector import D365Connector
from integrations.mock.connector import MockConnector

register(D365Connector())
register(MockConnector())
router = APIRouter(prefix='/api/integration-hub', tags=['Enterprise Integration Hub'])

class ConnectorMetricsOut(BaseModel):
    last_activity: str | None = None
    last_success: str | None = None
    last_failure: str | None = None
    successful_operations: int = 0
    failed_operations: int = 0
    records_pulled: int = 0
    records_pushed: int = 0

class ConnectorOut(BaseModel):
    key: str
    name: str
    system_type: str
    vendor: str = ''
    category: str = 'ERP'
    description: str = ''
    auth_type: str = ''
    supports_pull: list[str] = Field(default_factory=list)
    supports_push: list[str] = Field(default_factory=list)
    configured: bool = False
    missing: list[str] = Field(default_factory=list)
    mode: str = 'read-only'
    installed: bool = True
    connection_status: Literal['ready','configuration_required','sandbox'] = 'configuration_required'
    metrics: ConnectorMetricsOut

class ConnectorListResponse(BaseModel):
    connectors: list[ConnectorOut]
    total: int
    configured_count: int
    ready_count: int

class HubStatusResponse(BaseModel):
    connectors: list[ConnectorOut]
    configured_count: int
    pull_capabilities: list[str]
    push_capabilities: list[str]
    health: dict[str, Any]

class PushBody(BaseModel):
    payload: dict[str, Any]
    confirmation: str = Field(description="Enter PUSH to confirm live write-back")

class OperationResponse(BaseModel):
    model_config = ConfigDict(extra='allow')
    log_id: int | None = None
    connector: str | None = None
    entity: str | None = None
    direction: str | None = None
    dry_run: bool | None = None
    fetched: int | None = None
    inserted: int | None = None
    updated: int | None = None
    created: bool | None = None
    external_id: str | None = None
    preview: list[dict[str, Any]] | None = None
    payload: dict[str, Any] | None = None
    message: str | None = None

class IntegrationLogOut(BaseModel):
    id: int
    ts: str
    connector: str
    direction: str
    entity: str
    dry_run: bool
    status: str
    records: int
    message: str | None = None
    request: Any = None
    response: Any = None
    retry_of: int | None = None

def _connector_record(c):
    raw = c.info().__dict__
    if raw.get('mode') == 'sandbox':
        status = 'sandbox'
    elif raw.get('configured'):
        status = 'ready'
    else:
        status = 'configuration_required'
    return {**raw, 'connection_status': status, 'metrics': connector_metrics(raw['key'])}

@router.get('/connectors', response_model=ConnectorListResponse, summary='List installed ERP/API connectors')
def connectors():
    rows = [_connector_record(c) for c in all_connectors()]
    return {
        'connectors': rows,
        'total': len(rows),
        'configured_count': sum(1 for x in rows if x['configured']),
        'ready_count': sum(1 for x in rows if x['connection_status'] in ('ready','sandbox')),
    }

@router.get('/status', response_model=HubStatusResponse, summary='Integration Hub health and capabilities')
def status():
    infos = [_connector_record(c) for c in all_connectors()]
    failures = sum(x['metrics']['failed_operations'] for x in infos)
    return {
        'connectors': infos,
        'configured_count': sum(1 for x in infos if x['configured']),
        'pull_capabilities': sorted({e for x in infos for e in x['supports_pull']}),
        'push_capabilities': sorted({e for x in infos for e in x['supports_push']}),
        'health': {
            'status': 'attention' if failures else 'healthy',
            'failed_operations': failures,
            'total_records_pulled': sum(x['metrics']['records_pulled'] for x in infos),
            'total_records_pushed': sum(x['metrics']['records_pushed'] for x in infos),
        },
    }

@router.post('/{connector}/test', summary='Test connector authentication and reachability')
def test(connector: str):
    try:
        c = get(connector)
        result = c.test_connection()
        write_log(connector=connector, direction='test', entity='connection', status='success', response=result)
        return result
    except KeyError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        write_log(connector=connector, direction='test', entity='connection', status='failed', message=str(e))
        raise HTTPException(502, str(e))

@router.post('/{connector}/pull/{entity}', response_model=OperationResponse, summary='Preview or pull ERP data')
def pull(connector: str, entity: str, dry_run: bool = Query(True), top: int = Query(100, ge=1, le=50000), filter_expression: str | None = Query(None)):
    request = {'dry_run': dry_run, 'top': top, 'filter_expression': filter_expression}
    try:
        c = get(connector)
        info = c.info()
        if entity not in info.supports_pull:
            raise RuntimeError(f"{connector} does not support pull for {entity}")
        result = c.pull(entity, dry_run=dry_run, top=top, filter_expression=filter_expression)
        records = result.get('fetched') or result.get('inserted', 0) + result.get('updated', 0)
        log_id = write_log(connector=connector, direction='pull', entity=entity, dry_run=dry_run, status='success', records=records, request=request, response=result)
        return {'log_id': log_id, **result}
    except KeyError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        write_log(connector=connector, direction='pull', entity=entity, dry_run=dry_run, status='failed', message=str(e), request=request)
        raise HTTPException(502, str(e))

@router.post('/{connector}/push/{entity}', response_model=OperationResponse, summary='Push approved data to an ERP')
def push(connector: str, entity: str, body: PushBody):
    if body.confirmation.strip().upper() != 'PUSH':
        raise HTTPException(400, "Live write-back requires confirmation='PUSH'")
    try:
        c = get(connector)
        info = c.info()
        if entity not in info.supports_push:
            raise RuntimeError(f"{connector} does not support push for {entity} in current mode")
        result = c.push(entity, body.payload)
        log_id = write_log(connector=connector, direction='push', entity=entity, status='success', records=1, request=body.payload, response=result)
        return {'log_id': log_id, **result}
    except KeyError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        write_log(connector=connector, direction='push', entity=entity, status='failed', message=str(e), request=body.payload)
        raise HTTPException(502, str(e))

@router.get('/logs', response_model=list[IntegrationLogOut], summary='Search integration audit logs')
def logs(limit: int = Query(100, ge=1, le=1000), connector: str | None = None, status: str | None = None):
    return list_logs(limit=limit, connector=connector, status=status)

@router.post('/retry/{log_id}', summary='Explain safe retry behavior')
def retry(log_id: int):
    row = get_log(log_id)
    if not row:
        raise HTTPException(404, 'Integration log not found')
    raise HTTPException(400, 'Safe retry requires the original structured request. Re-run the pull/push from the Integration Hub after reviewing the failure.')
