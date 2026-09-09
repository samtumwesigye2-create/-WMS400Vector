from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app import auth, conn, now

router=APIRouter(prefix='/v1/ugatu',tags=['UGATU Fulfillment Analytics'])
NOVA_BASE_URL=os.getenv('NOVA_BASE_URL','https://ung-nova-production.up.railway.app').rstrip('/')


def ensure_schema():
    with conn() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS vector_ugatu_fulfillment(
          id UUID PRIMARY KEY,
          delivery_id TEXT UNIQUE NOT NULL,
          tracking_id TEXT,
          planned_at TIMESTAMPTZ NOT NULL,
          completed_at TIMESTAMPTZ,
          expected_qty DOUBLE PRECISION NOT NULL CHECK(expected_qty>=0),
          delivered_qty DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK(delivered_qty>=0),
          status TEXT NOT NULL,
          on_time BOOLEAN,
          exception BOOLEAN NOT NULL DEFAULT FALSE,
          exception_code TEXT,
          created_at TIMESTAMPTZ NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL
        )''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_vector_ugatu_fulfillment_time ON vector_ugatu_fulfillment(planned_at DESC)')


class FulfillmentIn(BaseModel):
    delivery_id:str=Field(min_length=2,max_length=160)
    tracking_id:str|None=Field(default=None,max_length=160)
    planned_at:datetime
    expected_qty:float=Field(ge=0)

class CompleteIn(BaseModel):
    completed_at:datetime|None=None
    delivered_qty:float=Field(ge=0)
    status:str=Field(min_length=2,max_length=40)
    on_time:bool
    exception:bool=False
    exception_code:str|None=Field(default=None,max_length=80)


def _snapshot():
    ensure_schema()
    with conn() as c:
        r=c.execute('''SELECT
          COUNT(*) FILTER(WHERE completed_at IS NOT NULL) completed,
          COUNT(*) FILTER(WHERE status='delivered' AND on_time=TRUE AND delivered_qty>=expected_qty AND exception=FALSE) perfect,
          COUNT(*) FILTER(WHERE status='delivered' AND on_time=TRUE AND delivered_qty>=expected_qty) otif,
          AVG(EXTRACT(EPOCH FROM (completed_at-planned_at))/60.0) FILTER(WHERE completed_at IS NOT NULL) avg_cycle,
          COUNT(*) visible_total,
          COUNT(*) FILTER(WHERE tracking_id IS NOT NULL AND planned_at IS NOT NULL AND (completed_at IS NOT NULL OR status IN ('planned','in_transit'))) visible
        FROM vector_ugatu_fulfillment''').fetchone()
    completed=int(r['completed'] or 0); total=int(r['visible_total'] or 0)
    obs=[]
    if completed:
        obs.extend([
          {'kpi_key':'perfect_order_fulfillment','value':round(100.0*float(r['perfect'] or 0)/completed,6),'entity_id':'enterprise','source_system':'UNG-UGATU','measured_at':now().isoformat()},
          {'kpi_key':'otif','value':round(100.0*float(r['otif'] or 0)/completed,6),'entity_id':'enterprise','source_system':'UNG-UGATU','measured_at':now().isoformat()},
          {'kpi_key':'order_fulfillment_cycle_time','value':round(float(r['avg_cycle'] or 0),6),'entity_id':'enterprise','source_system':'UNG-UGATU','measured_at':now().isoformat()},
        ])
    if total:
        obs.append({'kpi_key':'end_to_end_visibility_coverage','value':round(100.0*float(r['visible'] or 0)/total,6),'entity_id':'enterprise','source_system':'UNG-UGATU','measured_at':now().isoformat()})
    return {'source_system':'UNG-UGATU','completed_deliveries':completed,'total_records':total,'observations':obs,'generated_at':now()}


def _publish(snapshot):
    observations=snapshot.get('observations') or []
    if not observations:return {'status':'no-data','inserted':0,'snapshot':snapshot}
    req=urllib.request.Request(NOVA_BASE_URL+'/v1/supply-chain/observations/bulk',data=json.dumps({'observations':observations}).encode(),method='POST',headers={'Content-Type':'application/json','X-UNG-Permissions':'nova.datasets.write','User-Agent':'UNG-VECTOR/UGATU-KPI'})
    try:
        with urllib.request.urlopen(req,timeout=8) as r:return {'status':'published','response_code':r.status,'nova':json.loads(r.read().decode() or '{}'),'snapshot':snapshot}
    except urllib.error.HTTPError as e: raise HTTPException(502,f'nova_http_{e.code}')
    except Exception as e: raise HTTPException(503,f'nova_unavailable:{type(e).__name__}')

@router.post('/fulfillment',status_code=201)
def create_fulfillment(body:FulfillmentIn,authorization:str|None=Header(None)):
    auth('vector.movements.write',authorization);ensure_schema();ts=now()
    with conn() as c:
        try:
            return c.execute('''INSERT INTO vector_ugatu_fulfillment(id,delivery_id,tracking_id,planned_at,expected_qty,delivered_qty,status,on_time,exception,created_at,updated_at)
            VALUES(%s,%s,%s,%s,%s,0,'planned',NULL,FALSE,%s,%s) RETURNING *''',(str(uuid4()),body.delivery_id,body.tracking_id,body.planned_at,body.expected_qty,ts,ts)).fetchone()
        except Exception as exc:
            if 'unique' in str(exc).lower(): raise HTTPException(409,'delivery_already_exists')
            raise

@router.patch('/fulfillment/{delivery_id}')
def complete_fulfillment(delivery_id:str,body:CompleteIn,authorization:str|None=Header(None)):
    auth('vector.movements.write',authorization);ensure_schema();status=body.status.strip().lower().replace(' ','_')
    if status not in {'delivered','failed','partial','cancelled','in_transit'}: raise HTTPException(422,'invalid_delivery_status')
    completed=(body.completed_at or now()) if status in {'delivered','failed','partial','cancelled'} else None
    with conn() as c:
        row=c.execute('''UPDATE vector_ugatu_fulfillment SET completed_at=%s,delivered_qty=%s,status=%s,on_time=%s,exception=%s,exception_code=%s,updated_at=%s WHERE delivery_id=%s RETURNING *''',(completed,body.delivered_qty,status,body.on_time,body.exception,body.exception_code,now(),delivery_id)).fetchone()
        if not row: raise HTTPException(404,'delivery_not_found')
        return row

@router.get('/fulfillment')
def list_fulfillment(limit:int=250,authorization:str|None=Header(None)):
    auth('vector.movements.read',authorization);ensure_schema();limit=max(1,min(limit,1000))
    with conn() as c:return c.execute('SELECT * FROM vector_ugatu_fulfillment ORDER BY planned_at DESC LIMIT %s',(limit,)).fetchall()

@router.get('/kpis/supply-chain')
def ugatu_kpis(authorization:str|None=Header(None)):
    auth('vector.movements.read',authorization);return _snapshot()

@router.post('/kpis/supply-chain/publish')
def publish_ugatu_kpis(authorization:str|None=Header(None)):
    auth('vector.movements.read',authorization);return _publish(_snapshot())
