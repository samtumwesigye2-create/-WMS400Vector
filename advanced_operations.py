from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4
from fastapi import Header, HTTPException
from pydantic import BaseModel, Field

def _now(): return datetime.now(timezone.utc)

def init_advanced_ops(conn):
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS vector_shifts(
          id UUID PRIMARY KEY, code TEXT UNIQUE NOT NULL, work_center TEXT NOT NULL,
          start_time TEXT NOT NULL, end_time TEXT NOT NULL, available_minutes INTEGER NOT NULL,
          active BOOLEAN NOT NULL DEFAULT TRUE)""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_machine_downtime(
          id UUID PRIMARY KEY, work_center TEXT NOT NULL, starts_at TIMESTAMPTZ NOT NULL,
          ends_at TIMESTAMPTZ NOT NULL, reason TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_shopfloor_events(
          id UUID PRIMARY KEY, production_order_id UUID NOT NULL, operation_no INTEGER NOT NULL,
          event_type TEXT NOT NULL, labor_minutes NUMERIC NOT NULL DEFAULT 0,
          machine_minutes NUMERIC NOT NULL DEFAULT 0, quantity NUMERIC NOT NULL DEFAULT 0,
          created_at TIMESTAMPTZ NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_nonconformance(
          id UUID PRIMARY KEY, sku TEXT NOT NULL, reference TEXT NULL, defect TEXT NOT NULL,
          severity TEXT NOT NULL, status TEXT NOT NULL, corrective_action TEXT NULL,
          created_at TIMESTAMPTZ NOT NULL, closed_at TIMESTAMPTZ NULL)""")
        c.execute("ALTER TABLE vector_carriers ADD COLUMN IF NOT EXISTS quality_score NUMERIC NOT NULL DEFAULT 0")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_transport_exceptions(
          id UUID PRIMARY KEY, shipment_id UUID NOT NULL, exception_type TEXT NOT NULL,
          details TEXT NOT NULL, status TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL,
          resolved_at TIMESTAMPTZ NULL)""")

class ShiftIn(BaseModel):
    code:str; work_center:str; start_time:str; end_time:str; available_minutes:int=Field(gt=0)
class DowntimeIn(BaseModel):
    work_center:str; starts_at:str; ends_at:str; reason:str
class ShopfloorEventIn(BaseModel):
    production_order_id:str; operation_no:int=Field(gt=0); event_type:str
    labor_minutes:float=Field(default=0,ge=0); machine_minutes:float=Field(default=0,ge=0); quantity:float=Field(default=0,ge=0)
class NCIn(BaseModel):
    sku:str; reference:str|None=None; defect:str; severity:str='medium'
class TransportExceptionIn(BaseModel):
    shipment_id:str; exception_type:str; details:str

def install_advanced_ops_routes(app,conn,auth):
    @app.post('/v1/scheduling/shifts',status_code=201)
    def shift(b:ShiftIn,authorization:str|None=Header(None)):
        auth('vector.scheduling.write',authorization)
        with conn() as c:return c.execute("INSERT INTO vector_shifts VALUES(%s,%s,%s,%s,%s,%s,TRUE) ON CONFLICT(code) DO UPDATE SET work_center=EXCLUDED.work_center,start_time=EXCLUDED.start_time,end_time=EXCLUDED.end_time,available_minutes=EXCLUDED.available_minutes,active=TRUE RETURNING *",(str(uuid4()),b.code,b.work_center,b.start_time,b.end_time,b.available_minutes)).fetchone()

    @app.post('/v1/scheduling/downtime',status_code=201)
    def downtime(b:DowntimeIn,authorization:str|None=Header(None)):
        auth('vector.scheduling.write',authorization)
        with conn() as c:return c.execute("INSERT INTO vector_machine_downtime VALUES(%s,%s,%s,%s,%s) RETURNING *",(str(uuid4()),b.work_center,b.starts_at,b.ends_at,b.reason)).fetchone()

    @app.get('/v1/scheduling/finite/{sku}')
    def finite_schedule(sku:str,quantity:int=1,authorization:str|None=Header(None)):
        auth('vector.scheduling.read',authorization)
        with conn() as c:
            ops=c.execute('SELECT operation_no,work_center,minutes_per_unit FROM vector_routings WHERE sku=%s ORDER BY operation_no',(sku,)).fetchall()
            result=[]
            for op in ops:
                need=float(op['minutes_per_unit'])*quantity
                shifts=c.execute('SELECT COALESCE(sum(available_minutes),0) m FROM vector_shifts WHERE work_center=%s AND active=TRUE',(op['work_center'],)).fetchone()['m']
                result.append({'operation_no':op['operation_no'],'work_center':op['work_center'],'required_minutes':need,'available_shift_minutes':float(shifts),'feasible':float(shifts)>=need})
            return {'sku':sku,'quantity':quantity,'operations':result,'finite_capacity_feasible':all(r['feasible'] for r in result)}

    @app.post('/v1/shopfloor/events',status_code=201)
    def shopfloor_event(b:ShopfloorEventIn,authorization:str|None=Header(None)):
        auth('vector.shopfloor.write',authorization)
        with conn() as c:return c.execute("INSERT INTO vector_shopfloor_events VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",(str(uuid4()),b.production_order_id,b.operation_no,b.event_type,b.labor_minutes,b.machine_minutes,b.quantity,_now())).fetchone()

    @app.get('/v1/shopfloor/wip')
    def wip(authorization:str|None=Header(None)):
        auth('vector.shopfloor.read',authorization)
        with conn() as c:return c.execute("SELECT * FROM vector_production_orders WHERE status IN ('released','in_progress') ORDER BY created_at").fetchall()

    @app.post('/v1/quality/nonconformance',status_code=201)
    def nc(b:NCIn,authorization:str|None=Header(None)):
        auth('vector.quality.write',authorization)
        with conn() as c:return c.execute("INSERT INTO vector_nonconformance VALUES(%s,%s,%s,%s,%s,'open',NULL,%s,NULL) RETURNING *",(str(uuid4()),b.sku,b.reference,b.defect,b.severity,_now())).fetchone()

    @app.post('/v1/quality/nonconformance/{nc_id}/capa')
    def capa(nc_id:str,action:str,authorization:str|None=Header(None)):
        auth('vector.quality.write',authorization)
        with conn() as c:
            row=c.execute("UPDATE vector_nonconformance SET corrective_action=%s,status='closed',closed_at=%s WHERE id=%s RETURNING *",(action,_now(),nc_id)).fetchone()
            if not row:raise HTTPException(404,'nonconformance_not_found')
            return row

    @app.post('/v1/transport/exceptions',status_code=201)
    def transport_exception(b:TransportExceptionIn,authorization:str|None=Header(None)):
        auth('vector.transport.write',authorization)
        with conn() as c:return c.execute("INSERT INTO vector_transport_exceptions VALUES(%s,%s,%s,%s,'open',%s,NULL) RETURNING *",(str(uuid4()),b.shipment_id,b.exception_type,b.details,_now())).fetchone()

    @app.get('/v1/transport/carrier-performance')
    def carrier_performance(authorization:str|None=Header(None)):
        auth('vector.transport.read',authorization)
        with conn() as c:
            return c.execute("""SELECT c.code,c.name,c.on_time_pct,c.quality_score,
              count(s.id) shipments,COALESCE(sum(s.estimated_cost),0) freight_cost
              FROM vector_carriers c LEFT JOIN vector_shipments s ON s.carrier_code=c.code
              GROUP BY c.code,c.name,c.on_time_pct,c.quality_score ORDER BY c.on_time_pct DESC""").fetchall()

    @app.get('/v1/transport/load-plan')
    def load_plan(max_weight_kg:float,authorization:str|None=Header(None)):
        auth('vector.transport.read',authorization)
        with conn() as c:
            rows=c.execute("SELECT * FROM vector_shipments WHERE status='planned' ORDER BY total_weight_kg DESC").fetchall()
            loads=[]; current=[]; weight=0.0
            for s in rows:
                w=float(s['total_weight_kg'])
                if current and weight+w>max_weight_kg:
                    loads.append({'weight_kg':weight,'shipments':current});current=[];weight=0.0
                current.append(s);weight+=w
            if current:loads.append({'weight_kg':weight,'shipments':current})
            return {'max_weight_kg':max_weight_kg,'loads':loads}

    @app.get('/v1/transport/route-sequence')
    def route_sequence(origin:str,authorization:str|None=Header(None)):
        auth('vector.transport.read',authorization)
        with conn() as c:
            rows=c.execute("SELECT id,reference,destination,total_weight_kg FROM vector_shipments WHERE origin=%s AND status IN ('planned','optimized','tendered') ORDER BY destination,created_at",(origin,)).fetchall()
            return {'origin':origin,'sequence':rows,'method':'deterministic destination grouping'}

    @app.get('/v1/simulation/inventory')
    def simulate_inventory(sku:str,demand:float=0,inbound:float=0,authorization:str|None=Header(None)):
        auth('vector.analytics.read',authorization)
        with conn() as c:
            on_hand=float(c.execute('SELECT COALESCE(sum(quantity),0) q FROM vector_inventory WHERE sku=%s',(sku,)).fetchone()['q'])
            projected=on_hand+inbound-demand
            return {'sku':sku,'on_hand':on_hand,'inbound':inbound,'demand':demand,'projected_on_hand':projected,'stockout':projected<0}

    @app.get('/v1/replenishment/{sku}')
    def replenishment(sku:str,authorization:str|None=Header(None)):
        auth('vector.inventory.read',authorization)
        with conn() as c:
            m=c.execute('SELECT min_level,max_level,reorder_level FROM vector_materials WHERE sku=%s',(sku,)).fetchone()
            if not m:raise HTTPException(404,'material_not_found')
            on_hand=float(c.execute('SELECT COALESCE(sum(quantity),0) q FROM vector_inventory WHERE sku=%s',(sku,)).fetchone()['q'])
            target=float(m['max_level'] or m['reorder_level'] or m['min_level'])
            return {'sku':sku,'on_hand':on_hand,'reorder_level':float(m['reorder_level']),'target_level':target,'recommended_replenishment_qty':max(0,target-on_hand)}

    @app.get('/v1/warehouse/slotting')
    def slotting(authorization:str|None=Header(None)):
        auth('vector.warehouse.read',authorization)
        with conn() as c:
            rows=c.execute('SELECT sku,count(*) movement_count FROM vector_movements GROUP BY sku ORDER BY movement_count DESC').fetchall()
            return [{'sku':r['sku'],'movement_count':r['movement_count'],'slotting_priority':'A' if i<10 else ('B' if i<30 else 'C')} for i,r in enumerate(rows)]

    @app.get('/v1/maintenance/capacity-impact/{work_center}')
    def maintenance_capacity_impact(work_center:str,authorization:str|None=Header(None)):
        auth('vector.scheduling.read',authorization)
        with conn() as c:
            wc=c.execute('SELECT capacity_per_day FROM vector_work_centers WHERE code=%s',(work_center,)).fetchone()
            if not wc:raise HTTPException(404,'work_center_not_found')
            downtime=c.execute("SELECT COALESCE(sum(EXTRACT(EPOCH FROM (ends_at-starts_at))/3600),0) h FROM vector_machine_downtime WHERE work_center=%s AND starts_at::date=CURRENT_DATE",(work_center,)).fetchone()['h']
            available=max(0,float(wc['capacity_per_day'])-float(downtime))
            return {'work_center':work_center,'base_capacity_hours':float(wc['capacity_per_day']),'downtime_hours':round(float(downtime),2),'available_capacity_hours':round(available,2),'asset_system':'TITAN-ready'}

    @app.get('/v1/automation/recommendations')
    def automation_recommendations(authorization:str|None=Header(None)):
        auth('vector.analytics.read',authorization)
        with conn() as c:
            shortages=c.execute('SELECT sku,reorder_level,COALESCE((SELECT sum(i.quantity) FROM vector_inventory i WHERE i.sku=m.sku),0) on_hand FROM vector_materials m WHERE COALESCE((SELECT sum(i.quantity) FROM vector_inventory i WHERE i.sku=m.sku),0) < reorder_level').fetchall()
            overloaded=c.execute("""SELECT r.work_center,sum(r.minutes_per_unit*p.quantity)/60 required_hours,w.capacity_per_day
              FROM vector_routings r JOIN vector_production_orders p ON p.sku=r.sku AND p.status IN ('planned','released','in_progress')
              JOIN vector_work_centers w ON w.code=r.work_center
              GROUP BY r.work_center,w.capacity_per_day HAVING sum(r.minutes_per_unit*p.quantity)/60>w.capacity_per_day""").fetchall()
            return {'inventory_actions':[{'sku':x['sku'],'action':'replenish','qty':max(0,float(x['reorder_level'])-float(x['on_hand']))} for x in shortages],
                    'capacity_actions':[{'work_center':x['work_center'],'action':'reschedule_or_add_capacity','required_hours':float(x['required_hours']),'available_hours':float(x['capacity_per_day'])} for x in overloaded]}
