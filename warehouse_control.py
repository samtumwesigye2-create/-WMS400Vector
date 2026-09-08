from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app import auth, conn

router = APIRouter(prefix="/v1/warehouse-control", tags=["Warehouse Control"])


def now():
    return datetime.now(timezone.utc)


def ensure_schema():
    with conn() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS vector_capacity(
            location_code TEXT NOT NULL,
            stage TEXT NOT NULL,
            max_units INTEGER NOT NULL CHECK(max_units>0),
            active_workers INTEGER NOT NULL DEFAULT 0 CHECK(active_workers>=0),
            target_units_per_hour DOUBLE PRECISION NOT NULL DEFAULT 0,
            current_units INTEGER NOT NULL DEFAULT 0 CHECK(current_units>=0),
            updated_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY(location_code,stage)
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS vector_fulfillment_links(
            id UUID PRIMARY KEY,
            movement_id UUID UNIQUE NOT NULL,
            procure_order_id TEXT NOT NULL,
            inbound_asn_id TEXT,
            created_at TIMESTAMPTZ NOT NULL
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS vector_returns(
            id UUID PRIMARY KEY,
            original_reference TEXT NOT NULL,
            sku TEXT NOT NULL,
            quantity INTEGER NOT NULL CHECK(quantity>0),
            location_code TEXT NOT NULL,
            stage TEXT NOT NULL,
            grade TEXT,
            disposition TEXT,
            notes TEXT,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL
        )''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_vector_returns_ref ON vector_returns(original_reference)')


class CapacityIn(BaseModel):
    location_code: str = Field(min_length=1, max_length=64)
    stage: str = Field(min_length=2, max_length=32)
    max_units: int = Field(gt=0)
    active_workers: int = Field(ge=0, default=0)
    target_units_per_hour: float = Field(ge=0, default=0)
    current_units: int = Field(ge=0, default=0)


class FulfillmentLinkIn(BaseModel):
    movement_id: str
    procure_order_id: str = Field(min_length=2, max_length=120)
    inbound_asn_id: str | None = Field(default=None, max_length=120)


class ReturnIn(BaseModel):
    original_reference: str = Field(min_length=2, max_length=120)
    sku: str = Field(min_length=1, max_length=120)
    quantity: int = Field(gt=0)
    location_code: str = Field(min_length=1, max_length=64)
    notes: str = Field(default="", max_length=500)


class ReturnStageIn(BaseModel):
    stage: str = Field(min_length=2, max_length=32)
    grade: str | None = Field(default=None, max_length=32)
    disposition: str | None = Field(default=None, max_length=32)
    notes: str | None = Field(default=None, max_length=500)


@router.put('/capacity')
def upsert_capacity(body: CapacityIn, authorization: str | None = Header(None)):
    auth('vector.inventory.write', authorization)
    ensure_schema()
    stage = body.stage.strip().lower().replace(' ', '_')
    allowed = {'receive','store','pick','pack','dispatch'}
    if stage not in allowed: raise HTTPException(422, 'invalid_warehouse_stage')
    with conn() as c:
        if not c.execute('SELECT code FROM vector_locations WHERE code=%s', (body.location_code,)).fetchone():
            raise HTTPException(404, 'location_not_found')
        row = c.execute('''INSERT INTO vector_capacity(location_code,stage,max_units,active_workers,target_units_per_hour,current_units,updated_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(location_code,stage) DO UPDATE SET max_units=EXCLUDED.max_units,active_workers=EXCLUDED.active_workers,
            target_units_per_hour=EXCLUDED.target_units_per_hour,current_units=EXCLUDED.current_units,updated_at=EXCLUDED.updated_at
            RETURNING *''', (body.location_code,stage,body.max_units,body.active_workers,body.target_units_per_hour,body.current_units,now())).fetchone()
    utilization = round((row['current_units'] / row['max_units']) * 100, 2) if row['max_units'] else 0
    return {**row, 'utilization_pct': utilization, 'capacity_state': 'critical' if utilization >= 95 else 'high' if utilization >= 80 else 'normal'}


@router.get('/capacity')
def list_capacity(location_code: str | None = None, authorization: str | None = Header(None)):
    auth('vector.inventory.read', authorization)
    ensure_schema()
    with conn() as c:
        rows = c.execute('SELECT * FROM vector_capacity WHERE location_code=%s ORDER BY stage', (location_code,)).fetchall() if location_code else c.execute('SELECT * FROM vector_capacity ORDER BY location_code,stage').fetchall()
    result=[]
    for row in rows:
        util=round((row['current_units']/row['max_units'])*100,2) if row['max_units'] else 0
        result.append({**row,'utilization_pct':util,'capacity_state':'critical' if util>=95 else 'high' if util>=80 else 'normal'})
    return result


@router.post('/fulfillment-links', status_code=201)
def link_dispatch_to_procure(body: FulfillmentLinkIn, authorization: str | None = Header(None)):
    auth('vector.movements.write', authorization)
    ensure_schema()
    with conn() as c:
        movement = c.execute('SELECT id,movement_type,reference FROM vector_movements WHERE id=%s', (body.movement_id,)).fetchone()
        if not movement: raise HTTPException(404, 'movement_not_found')
        if movement['movement_type'] != 'dispatch': raise HTTPException(409, 'movement_is_not_dispatch')
        try:
            return c.execute('INSERT INTO vector_fulfillment_links VALUES(%s,%s,%s,%s,%s) RETURNING *', (str(uuid4()),body.movement_id,body.procure_order_id,body.inbound_asn_id,now())).fetchone()
        except Exception as exc:
            if 'unique' in str(exc).lower(): raise HTTPException(409, 'dispatch_already_linked')
            raise


@router.get('/fulfillment-links')
def fulfillment_links(procure_order_id: str | None = None, authorization: str | None = Header(None)):
    auth('vector.movements.read', authorization)
    ensure_schema()
    with conn() as c:
        if procure_order_id:
            return c.execute('SELECT * FROM vector_fulfillment_links WHERE procure_order_id=%s ORDER BY created_at DESC', (procure_order_id,)).fetchall()
        return c.execute('SELECT * FROM vector_fulfillment_links ORDER BY created_at DESC LIMIT 250').fetchall()


@router.post('/returns', status_code=201)
def create_return(body: ReturnIn, authorization: str | None = Header(None)):
    auth('vector.movements.write', authorization)
    ensure_schema()
    with conn() as c:
        if not c.execute('SELECT code FROM vector_locations WHERE code=%s', (body.location_code,)).fetchone(): raise HTTPException(404, 'location_not_found')
        ts=now()
        return c.execute('''INSERT INTO vector_returns(id,original_reference,sku,quantity,location_code,stage,grade,disposition,notes,created_at,updated_at)
            VALUES(%s,%s,%s,%s,%s,'received',NULL,NULL,%s,%s,%s) RETURNING *''', (str(uuid4()),body.original_reference,body.sku,body.quantity,body.location_code,body.notes,ts,ts)).fetchone()


@router.patch('/returns/{return_id}')
def advance_return(return_id: str, body: ReturnStageIn, authorization: str | None = Header(None)):
    auth('vector.movements.write', authorization)
    ensure_schema()
    stage=body.stage.strip().lower().replace(' ','_')
    if stage not in {'received','inspected','graded','routed'}: raise HTTPException(422,'invalid_return_stage')
    if stage=='routed' and body.disposition not in {'repair','resell','recycle'}: raise HTTPException(422,'disposition_required')
    with conn() as c:
        current=c.execute('SELECT * FROM vector_returns WHERE id=%s',(return_id,)).fetchone()
        if not current: raise HTTPException(404,'return_not_found')
        order={'received':0,'inspected':1,'graded':2,'routed':3}
        if order[stage] < order[current['stage']]: raise HTTPException(409,'return_stage_cannot_move_backward')
        return c.execute('''UPDATE vector_returns SET stage=%s,grade=COALESCE(%s,grade),disposition=COALESCE(%s,disposition),
            notes=COALESCE(%s,notes),updated_at=%s WHERE id=%s RETURNING *''', (stage,body.grade,body.disposition,body.notes,now(),return_id)).fetchone()


@router.get('/returns')
def list_returns(stage: str | None = None, authorization: str | None = Header(None)):
    auth('vector.movements.read', authorization)
    ensure_schema()
    with conn() as c:
        if stage:return c.execute('SELECT * FROM vector_returns WHERE stage=%s ORDER BY created_at DESC',(stage,)).fetchall()
        return c.execute('SELECT * FROM vector_returns ORDER BY created_at DESC LIMIT 250').fetchall()


@router.get('/summary')
def warehouse_control_summary(authorization: str | None = Header(None)):
    auth('vector.inventory.read', authorization)
    ensure_schema()
    with conn() as c:
        return {
            'capacity_rows': c.execute('SELECT COUNT(*) n FROM vector_capacity').fetchone()['n'],
            'high_capacity_stages': c.execute('SELECT COUNT(*) n FROM vector_capacity WHERE current_units::double precision/max_units >= 0.8').fetchone()['n'],
            'dispatch_po_links': c.execute('SELECT COUNT(*) n FROM vector_fulfillment_links').fetchone()['n'],
            'open_returns': c.execute("SELECT COUNT(*) n FROM vector_returns WHERE stage!='routed'").fetchone()['n'],
            'routed_returns': c.execute("SELECT COUNT(*) n FROM vector_returns WHERE stage='routed'").fetchone()['n'],
        }
