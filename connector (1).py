
from __future__ import annotations
from integrations.common.base import ConnectorInfo, IntegrationConnector

class MockConnector(IntegrationConnector):
    key='mock'
    def info(self):
        return ConnectorInfo(key=self.key,name='Demo / Sandbox Connector',system_type='mock',
          supports_pull=['stores','suppliers','items','stock','sales','purchase_orders'],
          supports_push=['purchase_orders','transfer_orders'],configured=True,mode='sandbox',
          description='Safe demo connector for testing pull, push, logs and UI without touching a live ERP.',
          vendor='Merchandiser AI',auth_type='None',category='Test')
    def test_connection(self): return {'connected':True,'connector':'mock','message':'Sandbox connector is ready'}
    def pull(self,entity,*,dry_run=True,top=100,filter_expression=None):
        rows=[{'external_id':f'{entity.upper()}-{i:04d}','name':f'Demo {entity} {i}'} for i in range(1,min(top or 10,10)+1)]
        return {'connector':'mock','entity':entity,'direction':'pull','dry_run':dry_run,'fetched':len(rows),'preview':rows}
    def push(self,entity,payload):
        return {'connector':'mock','entity':entity,'direction':'push','created':True,'external_id':'MOCK-000001','payload':payload}
