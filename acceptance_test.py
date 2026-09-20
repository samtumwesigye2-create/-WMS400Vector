from datetime import datetime, timezone
from uuid import uuid4
from fastapi import Header, HTTPException

TAG='VECTOR-ACCEPTANCE'

def _now(): return datetime.now(timezone.utc)

def install_acceptance_routes(app,conn,auth):
    @app.post('/v1/acceptance/run')
    def run_acceptance(authorization:str|None=Header(None)):
        auth('vector.admin',authorization)
        run_id=str(uuid4()); sku='VEC-TEST-'+run_id[:8].upper()
        checks=[]
        with conn() as c:
            try:
                # Material + opening stock
                c.execute("""INSERT INTO vector_materials
                  (sku,name,description,category,uom,batch_managed,serial_managed,reorder_level,min_level,max_level,unit_cost,currency,valuation_method,status,material_type,industry_sector)
                  VALUES(%s,%s,%s,'acceptance','EA',FALSE,FALSE,10,5,100,25,'USD','standard','active','FERT','test')""",
                  (sku,TAG,TAG))
                checks.append({'stage':'material','status':'PASS'})
                # Validate core/new enterprise schemas without inventing business records that bypass their APIs.
                tables=['vector_inventory','vector_bom','vector_work_centers','vector_routings',
                  'vector_production_orders','vector_demand_forecasts','vector_sop_plans','vector_suppliers',
                  'vector_purchase_orders','vector_quality_inspections','vector_warehouse_tasks',
                  'vector_shipments','vector_returns','vector_sustainability_metrics','vector_shopfloor_events',
                  'vector_nonconformance','vector_transport_exceptions']
                for t in tables:
                    exists=c.execute('SELECT to_regclass(%s) r',(t,)).fetchone()['r']
                    checks.append({'stage':t,'status':'PASS' if exists else 'FAIL'})
                status='PASS' if all(x['status']=='PASS' for x in checks) else 'FAIL'
                return {'run_id':run_id,'tag':TAG,'test_sku':sku,'status':status,'checks':checks,'generated_at':_now()}
            except Exception as e:
                raise HTTPException(500,{'run_id':run_id,'status':'FAIL','error':str(e)})

    @app.delete('/v1/acceptance/purge')
    def purge_acceptance(authorization:str|None=Header(None)):
        auth('vector.admin',authorization)
        deleted={}
        with conn() as c:
            # Only records uniquely tagged by this acceptance harness.
            for table in ['vector_materials']:
                try:
                    n=c.execute(f"DELETE FROM {table} WHERE name=%s AND description=%s",(TAG,TAG)).rowcount
                    deleted[table]=n
                except Exception:
                    deleted[table]=0
        return {'status':'purged','tag':TAG,'deleted':deleted}
