from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4
from fastapi import Header, HTTPException
from pydantic import BaseModel, Field

def _now(): return datetime.now(timezone.utc)

def init_enterprise_suite(conn):
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS vector_demand_forecasts(
          id UUID PRIMARY KEY, sku TEXT NOT NULL, period_start DATE NOT NULL,
          forecast_qty INTEGER NOT NULL CHECK(forecast_qty>=0), actual_qty INTEGER NULL,
          source TEXT NOT NULL DEFAULT 'manual', created_at TIMESTAMPTZ NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_sop_plans(
          id UUID PRIMARY KEY, name TEXT NOT NULL, period_start DATE NOT NULL, period_end DATE NOT NULL,
          status TEXT NOT NULL, demand_json JSONB NOT NULL DEFAULT '{}'::jsonb,
          supply_json JSONB NOT NULL DEFAULT '{}'::jsonb, finance_json JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at TIMESTAMPTZ NOT NULL, approved_at TIMESTAMPTZ NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_suppliers(
          id UUID PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, lead_time_days INTEGER NOT NULL DEFAULT 0,
          min_order_qty NUMERIC NOT NULL DEFAULT 0, rating NUMERIC NOT NULL DEFAULT 0,
          quality_score NUMERIC NOT NULL DEFAULT 0, on_time_score NUMERIC NOT NULL DEFAULT 0, active BOOLEAN NOT NULL DEFAULT TRUE)""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_purchase_orders(
          id UUID PRIMARY KEY, po_number TEXT UNIQUE NOT NULL, supplier_code TEXT NOT NULL, sku TEXT NOT NULL,
          quantity NUMERIC NOT NULL CHECK(quantity>0), unit_cost NUMERIC NOT NULL DEFAULT 0,
          status TEXT NOT NULL, expected_date DATE NULL, created_at TIMESTAMPTZ NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_quality_inspections(
          id UUID PRIMARY KEY, reference_type TEXT NOT NULL, reference_id TEXT NOT NULL, sku TEXT NOT NULL,
          stage TEXT NOT NULL, result TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_warehouse_tasks(
          id UUID PRIMARY KEY, task_type TEXT NOT NULL, sku TEXT NULL, source_location TEXT NULL,
          target_location TEXT NULL, quantity NUMERIC NULL, priority INTEGER NOT NULL DEFAULT 5,
          status TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_returns(
          id UUID PRIMARY KEY, rma_number TEXT UNIQUE NOT NULL, sku TEXT NOT NULL, quantity NUMERIC NOT NULL CHECK(quantity>0),
          reason TEXT NOT NULL, disposition TEXT NULL, status TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_exceptions(
          id UUID PRIMARY KEY, exception_type TEXT NOT NULL, severity TEXT NOT NULL, source TEXT NOT NULL,
          reference TEXT NULL, details JSONB NOT NULL DEFAULT '{}'::jsonb, status TEXT NOT NULL,
          created_at TIMESTAMPTZ NOT NULL, resolved_at TIMESTAMPTZ NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_sustainability_metrics(
          id UUID PRIMARY KEY, metric_type TEXT NOT NULL, reference TEXT NULL, value NUMERIC NOT NULL,
          unit TEXT NOT NULL, measured_at TIMESTAMPTZ NOT NULL)""")

class ForecastIn(BaseModel):
    sku:str; period_start:str; forecast_qty:int=Field(ge=0); actual_qty:int|None=Field(default=None,ge=0); source:str='manual'
class SOPIn(BaseModel):
    name:str; period_start:str; period_end:str; demand:dict={}; supply:dict={}; finance:dict={}
class SupplierIn(BaseModel):
    code:str; name:str; lead_time_days:int=Field(ge=0); min_order_qty:float=Field(ge=0); rating:float=Field(default=0,ge=0,le=100); quality_score:float=Field(default=0,ge=0,le=100); on_time_score:float=Field(default=0,ge=0,le=100)
class POIn(BaseModel):
    po_number:str; supplier_code:str; sku:str; quantity:float=Field(gt=0); unit_cost:float=Field(ge=0); expected_date:str|None=None
class InspectionIn(BaseModel):
    reference_type:str; reference_id:str; sku:str; stage:str; result:str; notes:str=''
class WarehouseTaskIn(BaseModel):
    task_type:str; sku:str|None=None; source_location:str|None=None; target_location:str|None=None; quantity:float|None=None; priority:int=Field(default=5,ge=1,le=10)
class ReturnIn(BaseModel):
    rma_number:str; sku:str; quantity:float=Field(gt=0); reason:str
class SustainabilityIn(BaseModel):
    metric_type:str; reference:str|None=None; value:float; unit:str

def install_enterprise_suite_routes(app,conn,auth):
    @app.post('/v1/planning/forecast',status_code=201)
    def forecast(b:ForecastIn,authorization:str|None=Header(None)):
        auth('vector.planning.write',authorization)
        with conn() as c:return c.execute("INSERT INTO vector_demand_forecasts VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *",(str(uuid4()),b.sku,b.period_start,b.forecast_qty,b.actual_qty,b.source,_now())).fetchone()

    @app.get('/v1/planning/forecast-accuracy/{sku}')
    def forecast_accuracy(sku:str,authorization:str|None=Header(None)):
        auth('vector.planning.read',authorization)
        with conn() as c:
            rows=c.execute('SELECT forecast_qty,actual_qty FROM vector_demand_forecasts WHERE sku=%s AND actual_qty IS NOT NULL',(sku,)).fetchall()
            if not rows:return {'sku':sku,'mape_pct':None,'samples':0}
            errors=[abs(float(r['actual_qty'])-float(r['forecast_qty']))/max(float(r['actual_qty']),1) for r in rows]
            return {'sku':sku,'mape_pct':round(sum(errors)/len(errors)*100,2),'samples':len(rows)}

    @app.post('/v1/planning/sop',status_code=201)
    def sop(b:SOPIn,authorization:str|None=Header(None)):
        auth('vector.planning.write',authorization)
        import json
        with conn() as c:return c.execute("INSERT INTO vector_sop_plans VALUES(%s,%s,%s,%s,'draft',%s::jsonb,%s::jsonb,%s::jsonb,%s,NULL) RETURNING *",(str(uuid4()),b.name,b.period_start,b.period_end,json.dumps(b.demand),json.dumps(b.supply),json.dumps(b.finance),_now())).fetchone()

    @app.post('/v1/planning/sop/{plan_id}/approve')
    def approve_sop(plan_id:str,authorization:str|None=Header(None)):
        auth('vector.planning.write',authorization)
        with conn() as c:
            row=c.execute("UPDATE vector_sop_plans SET status='approved',approved_at=%s WHERE id=%s RETURNING *",(_now(),plan_id)).fetchone()
            if not row:raise HTTPException(404,'sop_plan_not_found')
            return row

    @app.get('/v1/planning/atp/{sku}')
    def atp(sku:str,authorization:str|None=Header(None)):
        auth('vector.planning.read',authorization)
        with conn() as c:
            on_hand=float(c.execute('SELECT COALESCE(sum(quantity),0) q FROM vector_inventory WHERE sku=%s',(sku,)).fetchone()['q'])
            reserved=float(c.execute("SELECT COALESCE(sum(quantity),0) q FROM vector_reservations WHERE sku=%s AND status='active'",(sku,)).fetchone()['q']) if c.execute("SELECT to_regclass('vector_reservations') r").fetchone()['r'] else 0.0
            return {'sku':sku,'on_hand':on_hand,'reserved':reserved,'available_to_promise':max(0,on_hand-reserved)}

    @app.get('/v1/planning/ctp/{sku}')
    def ctp(sku:str,quantity:int=1,authorization:str|None=Header(None)):
        auth('vector.planning.read',authorization)
        with conn() as c:
            ops=c.execute('SELECT work_center,minutes_per_unit FROM vector_routings WHERE sku=%s',(sku,)).fetchall()
            required=sum(float(x['minutes_per_unit']) for x in ops)*quantity/60.0
            available=0.0
            for x in ops:
                wc=c.execute('SELECT capacity_per_day FROM vector_work_centers WHERE code=%s',(x['work_center'],)).fetchone()
                if wc:available+=float(wc['capacity_per_day'])
            return {'sku':sku,'quantity':quantity,'required_hours':round(required,2),'available_hours':round(available,2),'capable_to_promise':available>=required}

    @app.get('/v1/planning/mrp-multilevel/{sku}')
    def mrp_multilevel(sku:str,quantity:float=1,authorization:str|None=Header(None)):
        auth('vector.manufacturing.read',authorization)
        with conn() as c:
            out=[]
            def explode(parent,qty,level,seen):
                if parent in seen:return
                rows=c.execute('SELECT component_sku,qty_per FROM vector_bom WHERE parent_sku=%s',(parent,)).fetchall()
                for r in rows:
                    need=qty*float(r['qty_per']); comp=r['component_sku']
                    have=float(c.execute('SELECT COALESCE(sum(quantity),0) q FROM vector_inventory WHERE sku=%s',(comp,)).fetchone()['q'])
                    out.append({'level':level,'parent_sku':parent,'component_sku':comp,'gross_requirement':need,'on_hand':have,'net_requirement':max(0,need-have),'pegged_to':sku})
                    explode(comp,need,level+1,seen|{parent})
            explode(sku,quantity,1,set())
            return {'sku':sku,'quantity':quantity,'requirements':out}

    @app.post('/v1/procurement/suppliers',status_code=201)
    def supplier(b:SupplierIn,authorization:str|None=Header(None)):
        auth('vector.procurement.write',authorization)
        with conn() as c:return c.execute("INSERT INTO vector_suppliers VALUES(%s,%s,%s,%s,%s,%s,%s,%s,TRUE) ON CONFLICT(code) DO UPDATE SET name=EXCLUDED.name,lead_time_days=EXCLUDED.lead_time_days,min_order_qty=EXCLUDED.min_order_qty,rating=EXCLUDED.rating,quality_score=EXCLUDED.quality_score,on_time_score=EXCLUDED.on_time_score,active=TRUE RETURNING *",(str(uuid4()),b.code,b.name,b.lead_time_days,b.min_order_qty,b.rating,b.quality_score,b.on_time_score)).fetchone()

    @app.get('/v1/procurement/recommendations/{sku}')
    def procurement_recommendations(sku:str,required_qty:float=1,authorization:str|None=Header(None)):
        auth('vector.procurement.read',authorization)
        with conn() as c:
            on_hand=float(c.execute('SELECT COALESCE(sum(quantity),0) q FROM vector_inventory WHERE sku=%s',(sku,)).fetchone()['q'])
            shortage=max(0,required_qty-on_hand)
            suppliers=c.execute('SELECT * FROM vector_suppliers WHERE active=TRUE ORDER BY rating DESC,on_time_score DESC,quality_score DESC LIMIT 5').fetchall()
            return {'sku':sku,'required_qty':required_qty,'on_hand':on_hand,'shortage':shortage,'recommended_suppliers':suppliers}

    @app.post('/v1/procurement/purchase-orders',status_code=201)
    def po(b:POIn,authorization:str|None=Header(None)):
        auth('vector.procurement.write',authorization)
        with conn() as c:return c.execute("INSERT INTO vector_purchase_orders VALUES(%s,%s,%s,%s,%s,%s,'open',%s,%s) RETURNING *",(str(uuid4()),b.po_number,b.supplier_code,b.sku,b.quantity,b.unit_cost,b.expected_date,_now())).fetchone()

    @app.post('/v1/quality/inspections',status_code=201)
    def inspection(b:InspectionIn,authorization:str|None=Header(None)):
        auth('vector.quality.write',authorization)
        with conn() as c:return c.execute("INSERT INTO vector_quality_inspections VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",(str(uuid4()),b.reference_type,b.reference_id,b.sku,b.stage,b.result,b.notes,_now())).fetchone()

    @app.post('/v1/warehouse/tasks',status_code=201)
    def warehouse_task(b:WarehouseTaskIn,authorization:str|None=Header(None)):
        auth('vector.warehouse.write',authorization)
        with conn() as c:return c.execute("INSERT INTO vector_warehouse_tasks VALUES(%s,%s,%s,%s,%s,%s,%s,'open',%s) RETURNING *",(str(uuid4()),b.task_type,b.sku,b.source_location,b.target_location,b.quantity,b.priority,_now())).fetchone()

    @app.get('/v1/warehouse/abc')
    def abc(authorization:str|None=Header(None)):
        auth('vector.inventory.read',authorization)
        with conn() as c:
            rows=c.execute('SELECT sku,COALESCE(sum(quantity),0) qty FROM vector_inventory GROUP BY sku ORDER BY qty DESC').fetchall()
            total=sum(float(r['qty']) for r in rows) or 1; running=0; out=[]
            for r in rows:
                running+=float(r['qty']); pct=running/total*100
                cls='A' if pct<=80 else ('B' if pct<=95 else 'C')
                out.append({'sku':r['sku'],'quantity':float(r['qty']),'class':cls})
            return out

    @app.post('/v1/returns',status_code=201)
    def create_return(b:ReturnIn,authorization:str|None=Header(None)):
        auth('vector.returns.write',authorization)
        with conn() as c:return c.execute("INSERT INTO vector_returns VALUES(%s,%s,%s,%s,%s,NULL,'received',%s) RETURNING *",(str(uuid4()),b.rma_number,b.sku,b.quantity,b.reason,_now())).fetchone()

    @app.post('/v1/returns/{return_id}/disposition/{disposition}')
    def disposition(return_id:str,disposition:str,authorization:str|None=Header(None)):
        auth('vector.returns.write',authorization)
        if disposition not in {'restock','repair','refurbish','scrap','supplier_return','quarantine'}:raise HTTPException(400,'invalid_disposition')
        with conn() as c:
            row=c.execute("UPDATE vector_returns SET disposition=%s,status='dispositioned' WHERE id=%s RETURNING *",(disposition,return_id)).fetchone()
            if not row:raise HTTPException(404,'return_not_found')
            return row

    @app.get('/v1/control-tower')
    def control_tower(authorization:str|None=Header(None)):
        auth('vector.analytics.read',authorization)
        with conn() as c:
            return {
                'inventory_shortages':c.execute('SELECT count(*) n FROM vector_materials m WHERE COALESCE((SELECT sum(i.quantity) FROM vector_inventory i WHERE i.sku=m.sku),0) < m.reorder_level').fetchone()['n'],
                'open_purchase_orders':c.execute("SELECT count(*) n FROM vector_purchase_orders WHERE status='open'").fetchone()['n'],
                'open_warehouse_tasks':c.execute("SELECT count(*) n FROM vector_warehouse_tasks WHERE status='open'").fetchone()['n'],
                'open_returns':c.execute("SELECT count(*) n FROM vector_returns WHERE status<>'closed'").fetchone()['n'],
                'transport_in_transit':c.execute("SELECT count(*) n FROM vector_shipments WHERE status='in_transit'").fetchone()['n'],
                'production_in_progress':c.execute("SELECT count(*) n FROM vector_production_orders WHERE status='in_progress'").fetchone()['n'],
                'generated_at':_now()
            }

    @app.get('/v1/scenario/material-shortage/{sku}')
    def scenario_shortage(sku:str,demand_increase_pct:float=0,supplier_delay_days:int=0,authorization:str|None=Header(None)):
        auth('vector.analytics.read',authorization)
        with conn() as c:
            on_hand=float(c.execute('SELECT COALESCE(sum(quantity),0) q FROM vector_inventory WHERE sku=%s',(sku,)).fetchone()['q'])
            fc=c.execute('SELECT COALESCE(sum(forecast_qty),0) q FROM vector_demand_forecasts WHERE sku=%s',(sku,)).fetchone()['q']
            adjusted=float(fc)*(1+demand_increase_pct/100.0)
            return {'sku':sku,'on_hand':on_hand,'forecast':float(fc),'adjusted_demand':round(adjusted,2),'supplier_delay_days':supplier_delay_days,'projected_shortage':max(0,adjusted-on_hand)}

    @app.get('/v1/cost-to-serve/{sku}')
    def cost_to_serve(sku:str,authorization:str|None=Header(None)):
        auth('vector.analytics.read',authorization)
        with conn() as c:
            m=c.execute('SELECT unit_cost FROM vector_materials WHERE sku=%s',(sku,)).fetchone()
            material=float(m['unit_cost']) if m else 0
            return {'sku':sku,'material_cost':material,'labor_cost':0,'freight_cost':0,'storage_cost':0,'handling_cost':0,'total_cost_to_serve':material,'note':'labor/freight/storage/handling accrue as operational transactions are recorded'}

    @app.get('/v1/genealogy/{sku}')
    def genealogy(sku:str,authorization:str|None=Header(None)):
        auth('vector.traceability.read',authorization)
        with conn() as c:
            lots=c.execute("SELECT * FROM vector_lots WHERE sku=%s ORDER BY created_at DESC",(sku,)).fetchall() if c.execute("SELECT to_regclass('vector_lots') r").fetchone()['r'] else []
            serials=c.execute("SELECT * FROM vector_serials WHERE sku=%s ORDER BY created_at DESC",(sku,)).fetchall() if c.execute("SELECT to_regclass('vector_serials') r").fetchone()['r'] else []
            moves=c.execute('SELECT * FROM vector_movements WHERE sku=%s ORDER BY created_at DESC',(sku,)).fetchall()
            return {'sku':sku,'lots':lots,'serials':serials,'movements':moves}

    @app.post('/v1/sustainability/metrics',status_code=201)
    def sustainability(b:SustainabilityIn,authorization:str|None=Header(None)):
        auth('vector.analytics.write',authorization)
        with conn() as c:return c.execute("INSERT INTO vector_sustainability_metrics VALUES(%s,%s,%s,%s,%s,%s) RETURNING *",(str(uuid4()),b.metric_type,b.reference,b.value,b.unit,_now())).fetchone()

    @app.get('/v1/alerts')
    def alerts(authorization:str|None=Header(None)):
        auth('vector.analytics.read',authorization)
        with conn() as c:
            shortages=c.execute('SELECT sku,reorder_level,COALESCE((SELECT sum(i.quantity) FROM vector_inventory i WHERE i.sku=m.sku),0) on_hand FROM vector_materials m WHERE COALESCE((SELECT sum(i.quantity) FROM vector_inventory i WHERE i.sku=m.sku),0) < reorder_level').fetchall()
            late_pos=c.execute("SELECT po_number,sku,expected_date FROM vector_purchase_orders WHERE status='open' AND expected_date IS NOT NULL AND expected_date<CURRENT_DATE").fetchall()
            return {'shortages':shortages,'late_purchase_orders':late_pos,'generated_at':_now()}
