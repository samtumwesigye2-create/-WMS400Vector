from datetime import datetime, timezone
from uuid import uuid4
from fastapi import Header, HTTPException
from pydantic import BaseModel, Field

def _now(): return datetime.now(timezone.utc)

def init_manufacturing(conn):
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS vector_bom(id UUID PRIMARY KEY,parent_sku TEXT NOT NULL,component_sku TEXT NOT NULL,qty_per NUMERIC NOT NULL CHECK(qty_per>0),UNIQUE(parent_sku,component_sku))""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_work_centers(id UUID PRIMARY KEY,code TEXT UNIQUE NOT NULL,name TEXT NOT NULL,capacity_per_day NUMERIC NOT NULL DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_routings(id UUID PRIMARY KEY,sku TEXT NOT NULL,operation_no INTEGER NOT NULL,work_center TEXT NOT NULL,description TEXT NOT NULL,minutes_per_unit NUMERIC NOT NULL DEFAULT 0,UNIQUE(sku,operation_no))""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_production_orders(id UUID PRIMARY KEY,sku TEXT NOT NULL,quantity INTEGER NOT NULL CHECK(quantity>0),strategy TEXT NOT NULL,status TEXT NOT NULL,due_date DATE NULL,created_at TIMESTAMPTZ NOT NULL,completed_qty INTEGER NOT NULL DEFAULT 0,scrap_qty INTEGER NOT NULL DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_mps(id UUID PRIMARY KEY,sku TEXT NOT NULL,quantity INTEGER NOT NULL CHECK(quantity>0),period_start DATE NOT NULL,due_date DATE NOT NULL,status TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL)""")

class BOMIn(BaseModel): parent_sku:str; component_sku:str; qty_per:float=Field(gt=0)
class WorkCenterIn(BaseModel): code:str; name:str; capacity_per_day:float=Field(ge=0)
class RoutingIn(BaseModel): sku:str; operation_no:int=Field(gt=0); work_center:str; description:str; minutes_per_unit:float=Field(ge=0)
class MPSIn(BaseModel): sku:str; quantity:int=Field(gt=0); period_start:str; due_date:str
class ProductionOrderIn(BaseModel): sku:str; quantity:int=Field(gt=0); strategy:str='MTS'; due_date:str|None=None
class ConfirmIn(BaseModel): completed_qty:int=Field(ge=0); scrap_qty:int=Field(ge=0)

def install_manufacturing_routes(app,conn,auth):
    @app.post('/v1/manufacturing/bom',status_code=201)
    def bom(b:BOMIn,authorization:str|None=Header(None)):
        auth('vector.manufacturing.write',authorization)
        with conn() as c:return c.execute("INSERT INTO vector_bom VALUES(%s,%s,%s,%s) ON CONFLICT(parent_sku,component_sku) DO UPDATE SET qty_per=EXCLUDED.qty_per RETURNING *",(str(uuid4()),b.parent_sku,b.component_sku,b.qty_per)).fetchone()

    @app.get('/v1/manufacturing/bom/{sku}')
    def bom_get(sku:str,authorization:str|None=Header(None)):
        auth('vector.manufacturing.read',authorization)
        with conn() as c:return c.execute('SELECT * FROM vector_bom WHERE parent_sku=%s ORDER BY component_sku',(sku,)).fetchall()

    @app.post('/v1/manufacturing/work-centers',status_code=201)
    def work_center(b:WorkCenterIn,authorization:str|None=Header(None)):
        auth('vector.manufacturing.write',authorization)
        with conn() as c:return c.execute("INSERT INTO vector_work_centers VALUES(%s,%s,%s,%s) ON CONFLICT(code) DO UPDATE SET name=EXCLUDED.name,capacity_per_day=EXCLUDED.capacity_per_day RETURNING *",(str(uuid4()),b.code,b.name,b.capacity_per_day)).fetchone()

    @app.post('/v1/manufacturing/routings',status_code=201)
    def routing(b:RoutingIn,authorization:str|None=Header(None)):
        auth('vector.manufacturing.write',authorization)
        with conn() as c:return c.execute("INSERT INTO vector_routings VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(sku,operation_no) DO UPDATE SET work_center=EXCLUDED.work_center,description=EXCLUDED.description,minutes_per_unit=EXCLUDED.minutes_per_unit RETURNING *",(str(uuid4()),b.sku,b.operation_no,b.work_center,b.description,b.minutes_per_unit)).fetchone()

    @app.post('/v1/manufacturing/mps',status_code=201)
    def mps(b:MPSIn,authorization:str|None=Header(None)):
        auth('vector.manufacturing.write',authorization)
        with conn() as c:return c.execute("INSERT INTO vector_mps VALUES(%s,%s,%s,%s,%s,'planned',%s) RETURNING *",(str(uuid4()),b.sku,b.quantity,b.period_start,b.due_date,_now())).fetchone()

    @app.get('/v1/manufacturing/mrp/{sku}')
    def mrp(sku:str,required_qty:int=1,authorization:str|None=Header(None)):
        auth('vector.manufacturing.read',authorization)
        with conn() as c:
            bom=c.execute('SELECT component_sku,qty_per FROM vector_bom WHERE parent_sku=%s',(sku,)).fetchall()
            out=[]
            for x in bom:
                need=float(x['qty_per'])*required_qty
                have=c.execute('SELECT COALESCE(sum(quantity),0) q FROM vector_inventory WHERE sku=%s',(x['component_sku'],)).fetchone()['q']
                out.append({'component_sku':x['component_sku'],'gross_requirement':need,'on_hand':float(have),'net_requirement':max(0,need-float(have))})
            return {'sku':sku,'required_qty':required_qty,'components':out}

    @app.post('/v1/manufacturing/orders',status_code=201)
    def create_order(b:ProductionOrderIn,authorization:str|None=Header(None)):
        auth('vector.manufacturing.write',authorization)
        strategy=b.strategy.upper()
        if strategy not in {'MTO','MTS'}:raise HTTPException(400,'strategy_must_be_MTO_or_MTS')
        with conn() as c:return c.execute("INSERT INTO vector_production_orders VALUES(%s,%s,%s,%s,'planned',%s,%s,0,0) RETURNING *",(str(uuid4()),b.sku,b.quantity,strategy,b.due_date,_now())).fetchone()

    @app.post('/v1/manufacturing/orders/{order_id}/release')
    def release(order_id:str,authorization:str|None=Header(None)):
        auth('vector.manufacturing.write',authorization)
        with conn() as c:
            row=c.execute("UPDATE vector_production_orders SET status='released' WHERE id=%s AND status='planned' RETURNING *",(order_id,)).fetchone()
            if not row:raise HTTPException(409,'order_not_planned')
            return row

    @app.post('/v1/manufacturing/orders/{order_id}/confirm')
    def confirm(order_id:str,b:ConfirmIn,authorization:str|None=Header(None)):
        auth('vector.manufacturing.write',authorization)
        with conn() as c:
            row=c.execute("UPDATE vector_production_orders SET completed_qty=completed_qty+%s,scrap_qty=scrap_qty+%s,status=CASE WHEN completed_qty+%s>=quantity THEN 'completed' ELSE 'in_progress' END WHERE id=%s AND status IN ('released','in_progress') RETURNING *",(b.completed_qty,b.scrap_qty,b.completed_qty,order_id)).fetchone()
            if not row:raise HTTPException(409,'order_not_released')
            return row

    @app.get('/v1/manufacturing/orders')
    def orders(authorization:str|None=Header(None)):
        auth('vector.manufacturing.read',authorization)
        with conn() as c:return c.execute('SELECT * FROM vector_production_orders ORDER BY created_at DESC').fetchall()
