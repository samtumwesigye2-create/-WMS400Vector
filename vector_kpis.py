import json, os, urllib.request
from datetime import datetime, timezone
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from app import auth, conn
from warehouse_control import ensure_schema as ensure_warehouse
from inventory_costs import ensure_schema as ensure_costs

router=APIRouter(prefix='/v1/kpis',tags=['Supply Chain KPIs'])
NOVA_BASE_URL=os.getenv('NOVA_BASE_URL','https://ung-nova-production.up.railway.app').rstrip('/')

def now(): return datetime.now(timezone.utc)
def ensure():
 ensure_warehouse();ensure_costs()
 with conn() as c:
  c.execute('''CREATE TABLE IF NOT EXISTS vector_cycle_counts(id UUID PRIMARY KEY,sku TEXT NOT NULL,location_code TEXT NOT NULL,system_quantity INTEGER NOT NULL,physical_quantity INTEGER NOT NULL,counted_at TIMESTAMPTZ NOT NULL,counted_by TEXT)''')
  c.execute('CREATE INDEX IF NOT EXISTS ix_vector_cycle_counts_time ON vector_cycle_counts(counted_at DESC)')

class CycleCountIn(BaseModel):
 sku:str=Field(min_length=1,max_length=120)
 location_code:str=Field(min_length=1,max_length=64)
 physical_quantity:int=Field(ge=0)

@router.post('/cycle-counts',status_code=201)
def cycle_count(b:CycleCountIn,authorization:str|None=Header(None)):
 principal=auth('vector.inventory.write',authorization);ensure()
 with conn() as c:
  row=c.execute('SELECT quantity FROM vector_inventory WHERE sku=%s AND location_code=%s',(b.sku,b.location_code)).fetchone()
  if not row:raise HTTPException(404,'inventory_not_found')
  return c.execute('INSERT INTO vector_cycle_counts VALUES(gen_random_uuid(),%s,%s,%s,%s,%s,%s) RETURNING *',(b.sku,b.location_code,row['quantity'],b.physical_quantity,now(),principal.get('id') or principal.get('display_name'))).fetchone()

def snapshot():
 ensure();out=[]
 with conn() as c:
  cc=c.execute("SELECT COUNT(*) n,COUNT(*) FILTER(WHERE system_quantity=physical_quantity) ok FROM vector_cycle_counts WHERE counted_at>=NOW()-INTERVAL '90 days'").fetchone()
  if cc['n']:out.append(('inventory_accuracy',100*cc['ok']/cc['n']))
  inv=c.execute('SELECT COALESCE(SUM(quantity),0)::double precision q FROM vector_inventory').fetchone()['q']; usage=c.execute("SELECT COALESCE(SUM(quantity),0)::double precision q FROM vector_movements WHERE movement_type='dispatch' AND created_at>=NOW()-INTERVAL '365 days'").fetchone()['q']
  if usage>0:out.append(('inventory_days_of_supply',inv/(usage/365.0)))
  excess=c.execute("""WITH used AS (SELECT DISTINCT sku FROM vector_movements WHERE movement_type='dispatch' AND created_at>=NOW()-INTERVAL '365 days') SELECT COALESCE(SUM(i.quantity*COALESCE(c.unit_price,0)),0)::double precision v FROM vector_inventory i LEFT JOIN vector_item_costs c ON c.sku=i.sku LEFT JOIN used u ON u.sku=i.sku WHERE u.sku IS NULL""").fetchone()['v']
  out.append(('excess_obsolete_inventory',excess))
  cap=c.execute('SELECT COALESCE(SUM(current_units),0)::double precision cur,COALESCE(SUM(max_units),0)::double precision mx FROM vector_capacity').fetchone()
  if cap['mx']>0:out.append(('capacity_utilization',100*cap['cur']/cap['mx']))
 return out

@router.get('/snapshot')
def kpi_snapshot(authorization:str|None=Header(None)):
 auth('vector.inventory.read',authorization);vals=snapshot();return {'source_system':'UNG-VECTOR','observations':[{'kpi_key':k,'value':round(v,4)} for k,v in vals],'generated_at':now()}

@router.post('/publish')
def publish(authorization:str|None=Header(None)):
 auth('vector.inventory.read',authorization);vals=snapshot();body={'observations':[{'kpi_key':k,'value':v,'source_system':'UNG-VECTOR','measured_at':now().isoformat()} for k,v in vals]}
 if not vals:return {'status':'no-source-data','published':0}
 req=urllib.request.Request(NOVA_BASE_URL+'/v1/supply-chain/observations/bulk',data=json.dumps(body).encode(),method='POST',headers={'Content-Type':'application/json','X-UNG-Permissions':'nova.datasets.write','User-Agent':'UNG-VECTOR/0.4'})
 try:
  with urllib.request.urlopen(req,timeout=8) as r:return {'status':'published','published':len(vals),'nova_status':r.status}
 except Exception as e:raise HTTPException(502,f'nova_publish_failed:{type(e).__name__}')
