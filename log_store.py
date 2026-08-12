
from __future__ import annotations
import json, sqlite3, threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DB = Path(__file__).resolve().parents[2] / "integration_hub.db"
_LOCK = threading.Lock()

def _conn():
    c=sqlite3.connect(_DB)
    c.row_factory=sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS integration_logs(
      id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, connector TEXT NOT NULL,
      direction TEXT NOT NULL, entity TEXT NOT NULL, dry_run INTEGER NOT NULL DEFAULT 0,
      status TEXT NOT NULL, records INTEGER NOT NULL DEFAULT 0, message TEXT,
      request_json TEXT, response_json TEXT, retry_of INTEGER)""")
    c.commit(); return c

def write_log(*, connector:str,direction:str,entity:str,status:str,dry_run:bool=False,
              records:int=0,message:str|None=None,request:Any=None,response:Any=None,retry_of:int|None=None)->int:
    with _LOCK:
        c=_conn(); cur=c.execute("""INSERT INTO integration_logs
        (ts,connector,direction,entity,dry_run,status,records,message,request_json,response_json,retry_of)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(
          datetime.now(timezone.utc).isoformat(),connector,direction,entity,int(dry_run),status,int(records or 0),
          message,json.dumps(request,default=str) if request is not None else None,
          json.dumps(response,default=str) if response is not None else None,retry_of))
        c.commit(); i=cur.lastrowid; c.close(); return i

def list_logs(limit:int=100,connector:str|None=None,status:str|None=None):
    c=_conn(); q="SELECT * FROM integration_logs WHERE 1=1"; p=[]
    if connector: q+=" AND connector=?"; p.append(connector)
    if status: q+=" AND status=?"; p.append(status)
    q+=" ORDER BY id DESC LIMIT ?"; p.append(limit)
    rows=[dict(r) for r in c.execute(q,p).fetchall()]; c.close()
    for r in rows:
        for k in ('request_json','response_json'):
            try:r[k[:-5]]=json.loads(r[k]) if r[k] else None
            except Exception:r[k[:-5]]=r[k]
            r.pop(k,None)
        r['dry_run']=bool(r['dry_run'])
    return rows

def get_log(log_id:int):
    c=_conn(); r=c.execute("SELECT * FROM integration_logs WHERE id=?",(log_id,)).fetchone(); c.close()
    return dict(r) if r else None

def connector_metrics(connector: str):
    c = _conn()
    rows = c.execute("SELECT * FROM integration_logs WHERE connector=? ORDER BY id DESC", (connector,)).fetchall()
    c.close()
    rows = [dict(r) for r in rows]
    successes = [r for r in rows if r.get('status') == 'success']
    failures = [r for r in rows if r.get('status') == 'failed']
    pulls = [r for r in successes if r.get('direction') == 'pull' and not r.get('dry_run')]
    pushes = [r for r in successes if r.get('direction') == 'push']
    return {
        'last_activity': rows[0]['ts'] if rows else None,
        'last_success': successes[0]['ts'] if successes else None,
        'last_failure': failures[0]['ts'] if failures else None,
        'successful_operations': len(successes),
        'failed_operations': len(failures),
        'records_pulled': sum(int(r.get('records') or 0) for r in pulls),
        'records_pushed': sum(int(r.get('records') or 0) for r in pushes),
    }
