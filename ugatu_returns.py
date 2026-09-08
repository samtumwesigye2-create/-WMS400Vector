from datetime import datetime, timezone
from uuid import uuid4
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from app import auth, conn
from warehouse_control import ensure_schema

router=APIRouter(prefix='/v1/ugatu',tags=['UGATU Reverse Logistics'])
def now():return datetime.now(timezone.utc)
class FailedDeliveryIn(BaseModel):
    delivery_id:str=Field(min_length=2,max_length=120)
    tracking_id:str=Field(min_length=2,max_length=120)
    sku:str=Field(min_length=1,max_length=120)
    quantity:int=Field(gt=0)
    return_location:str=Field(min_length=1,max_length=64)
    reason:str=Field(min_length=2,max_length=240)
    pickup_id:str|None=Field(default=None,max_length=120)
    notes:str=''

def schema():
    ensure_schema()
    with conn() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS vector_ugatu_return_links(id UUID PRIMARY KEY,delivery_id TEXT UNIQUE NOT NULL,pickup_id TEXT,tracking_id TEXT NOT NULL,return_id UUID UNIQUE NOT NULL,reason TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL)''')

@router.post('/failed-deliveries',status_code=201)
def failed_delivery(body:FailedDeliveryIn,authorization:str|None=Header(None)):
    auth('vector.movements.write',authorization);schema()
    with conn() as c:
        prior=c.execute('''SELECT l.*,r.stage,r.grade,r.disposition,r.location_code FROM vector_ugatu_return_links l JOIN vector_returns r ON r.id=l.return_id WHERE l.delivery_id=%s''',(body.delivery_id,)).fetchone()
        if prior:return {'accepted':True,'duplicate':True,'link':prior}
        if not c.execute('SELECT code FROM vector_locations WHERE code=%s',(body.return_location,)).fetchone():raise HTTPException(404,'return_location_not_found')
        ts=now();rid=str(uuid4());notes=('UGATU failed delivery: '+body.reason+(' | '+body.notes if body.notes else ''))[:500]
        ret=c.execute('''INSERT INTO vector_returns(id,original_reference,sku,quantity,location_code,stage,grade,disposition,notes,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,'received',NULL,NULL,%s,%s,%s) RETURNING *''',(rid,body.tracking_id,body.sku,body.quantity,body.return_location,notes,ts,ts)).fetchone()
        link=c.execute('''INSERT INTO vector_ugatu_return_links(id,delivery_id,pickup_id,tracking_id,return_id,reason,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *''',(str(uuid4()),body.delivery_id,body.pickup_id,body.tracking_id,rid,body.reason,ts)).fetchone()
        return {'accepted':True,'duplicate':False,'return':ret,'link':link}

@router.get('/returns/{delivery_id}')
def return_for_delivery(delivery_id:str,authorization:str|None=Header(None)):
    auth('vector.movements.read',authorization);schema()
    with conn() as c:
        r=c.execute('''SELECT l.*,r.stage,r.grade,r.disposition,r.location_code,r.sku,r.quantity,r.updated_at FROM vector_ugatu_return_links l JOIN vector_returns r ON r.id=l.return_id WHERE l.delivery_id=%s''',(delivery_id,)).fetchone()
    if not r:raise HTTPException(404,'delivery_return_not_found')
    return r
