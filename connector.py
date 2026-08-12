
from __future__ import annotations
import os
from integrations.common.base import ConnectorInfo, IntegrationConnector
from database import SessionLocal
from .client import D365Client
from .config import ENTITY_NAMES, get_settings
from .service import sync_entity

class D365Connector(IntegrationConnector):
    key='d365'
    def info(self):
        s=get_settings(); missing=s.missing(); write=os.getenv('D365_ENABLE_WRITEBACK','false').lower() in {'1','true','yes'}
        return ConnectorInfo(key=self.key,name='Microsoft Dynamics 365 Finance & Supply Chain',system_type='d365_fscm',
          supports_pull=[k for k in ENTITY_NAMES if k!='purchase_order_headers'],
          supports_push=['purchase_orders','transfer_orders'] if write else [],
          configured=not bool(missing),missing=missing,mode='read/write' if write else 'read-only',
          description='Pull planning data from D365 Finance & Supply Chain and optionally push approved purchase/transfer orders.',
          vendor='Microsoft',auth_type='Microsoft Entra ID OAuth 2.0',category='ERP')
    def test_connection(self):
        s=get_settings()
        if s.missing(): raise RuntimeError('Missing configuration: '+', '.join(s.missing()))
        return D365Client(s).test_connection()
    def pull(self,entity,*,dry_run=True,top=100,filter_expression=None):
        db=SessionLocal()
        try:return sync_entity(db,entity,dry_run=dry_run,top=top,filter_expression=filter_expression)
        finally:db.close()
    def push(self,entity,payload):
        if os.getenv('D365_ENABLE_WRITEBACK','false').lower() not in {'1','true','yes'}:
            raise RuntimeError('D365 write-back is disabled. Set D365_ENABLE_WRITEBACK=true only after UAT approval.')
        s=get_settings(); client=D365Client(s)
        if entity=='purchase_orders':
            header_entity=ENTITY_NAMES['purchase_order_headers']; line_entity=ENTITY_NAMES['purchase_orders']
            header=client.create(header_entity,payload['header'])
            line=client.create(line_entity,payload['line'])
            return {'created':True,'header':header,'line':line}
        if entity=='transfer_orders':
            target=os.getenv('D365_ENTITY_TRANSFER_ORDERS','TransferOrderHeaders')
            return client.create(target,payload)
        raise RuntimeError(f"Unsupported D365 push entity '{entity}'")
