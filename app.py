from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from datetime import datetime, timezone
from uuid import uuid4
import json, os, psycopg, urllib.error, urllib.request
from psycopg.rows import dict_row
from material_master import init_material_master, install_material_routes
from inventory_control import init_inventory_control, install_inventory_control_routes
from valuation_audit import init_valuation_audit, install_valuation_audit_routes
from traceability import init_traceability, install_traceability_routes
from integration_events import inventory_changed
from manufacturing_planning import init_manufacturing, install_manufacturing_routes
from capacity_planning import install_crp_routes
from transportation_management import init_transportation, install_transportation_routes
from enterprise_suite import init_enterprise_suite, install_enterprise_suite_routes
from advanced_operations import init_advanced_ops, install_advanced_ops_routes
from acceptance_test import install_acceptance_routes
from inventory_ageing import init_inventory_ageing, install_inventory_ageing_routes
from warehouse_layout import init_warehouse_layout, install_warehouse_layout_routes
from enterprise_structure import init_enterprise_structure, install_enterprise_structure_routes
from mrp_areas import init_mrp_areas, install_mrp_area_routes
from supply_chain_kpis import init_supply_chain_kpis, install_supply_chain_kpi_routes
from goods_movement import init_goods_movement, install_goods_movement_routes
from mm_transactions import init_mm_transactions, install_mm_transaction_routes
from demand_classification import install_demand_classification_routes
from release_approvals import init_release_approvals, install_release_approval_routes

app=FastAPI(title='UNG-VECTOR',version='0.23.0')
DB=os.getenv('DATABASE_URL','')
JANUS_BASE_URL=os.getenv('JANUS_BASE_URL','https://ung-iam-production.up.railway.app').rstrip('/')
NEXUS_BASE_URL=os.getenv('NEXUS_BASE_URL','https://ung-nexus-production.up.railway.app').rstrip('/')
VECTOR_SERVICE_TOKEN=os.getenv('UNG_VECTOR_SERVICE_TOKEN','').strip()
def now(): return datetime.now(timezone.utc)
def emit(target,message_type,payload):
 if not NEXUS_BASE_URL:return {'status':'disabled'}
 if not VECTOR_SERVICE_TOKEN:return {'status':'failed','error':'vector_service_token_missing'}
 body=json.dumps({'source_system':'UNG-VECTOR','target_system':target,'message_type':message_type,'payload':payload}).encode()
 req=urllib.request.Request(NEXUS_BASE_URL+'/v1/messages',data=body,method='POST',headers={'Authorization':f'Bearer {VECTOR_SERVICE_TOKEN}','Content-Type':'application/json','User-Agent':'UNG-VECTOR/0.23.0'})
 try:
  with urllib.request.urlopen(req,timeout=8) as r:return {'status':'delivered','response_code':r.status,'response':json.loads(r.read().decode() or '{}')}
 except urllib.error.HTTPError as e:return {'status':'failed','response_code':e.code,'error':f'http_{e.code}'}
 except Exception as e:return {'status':'failed','error':type(e).__name__}
def auth(permission,authorization):
 if not authorization or not authorization.lower().startswith('bearer '): raise HTTPException(401,'JANUS bearer token required')
 req=urllib.request.Request(JANUS_BASE_URL+'/v1/auth/introspect',data=b'',method='POST',headers={'Authorization':authorization})
 try:
  with urllib.request.urlopen(req,timeout=5) as r:data=json.loads(r.read().decode())
 except urllib.error.HTTPError as e:
  if e.code in (401,403): raise HTTPException(401,'JANUS token invalid or expired')
  raise HTTPException(503,'JANUS authorization unavailable')
 except Exception: raise HTTPException(503,'JANUS authorization unavailable')
 principal=data.get('principal') or {};perms=set(principal.get('permissions') or [])
 if permission not in perms and 'ung.admin' not in perms: raise HTTPException(403,f'Missing JANUS permission: {permission}')
 return principal
def conn():
 if not DB: raise HTTPException(503,'database_not_configured')
 return psycopg.connect(DB,row_factory=dict_row)
def init_db():
 if not DB:return
 with conn() as c:
  c.execute('CREATE TABLE IF NOT EXISTS vector_locations(id UUID PRIMARY KEY,code TEXT UNIQUE NOT NULL,name TEXT NOT NULL,location_type TEXT NOT NULL,status TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL)')
  c.execute('CREATE TABLE IF NOT EXISTS vector_inventory(id UUID PRIMARY KEY,sku TEXT NOT NULL,description TEXT NOT NULL,quantity INTEGER NOT NULL CHECK(quantity>=0),location_code TEXT NOT NULL,status TEXT NOT NULL,updated_at TIMESTAMPTZ NOT NULL)')
  c.execute('CREATE TABLE IF NOT EXISTS vector_movements(id UUID PRIMARY KEY,sku TEXT NOT NULL,quantity INTEGER NOT NULL,movement_type TEXT NOT NULL,from_location TEXT NULL,to_location TEXT NULL,reference TEXT NULL,created_at TIMESTAMPTZ NOT NULL)')
  c.execute('CREATE UNIQUE INDEX IF NOT EXISTS uq_vector_inventory_sku_location ON vector_inventory(sku,location_code)')
 init_material_master(conn)
 init_inventory_control(conn)
 init_valuation_audit(conn)
 init_traceability(conn)
 init_manufacturing(conn)
 init_transportation(conn)
 init_enterprise_suite(conn)
 init_advanced_ops(conn)
 init_inventory_ageing(conn)
 init_warehouse_layout(conn)
 init_enterprise_structure(conn)
 init_mrp_areas(conn)
 init_supply_chain_kpis(conn)
 init_goods_movement(conn)
 init_mm_transactions(conn)
 init_release_approvals(conn)
@app.on_event('startup')
def startup():init_db()
class LocationIn(BaseModel):code:str;name:str;location_type:str='warehouse'
class InventoryIn(BaseModel):sku:str;description:str;quantity:int;location_code:str
class MovementIn(BaseModel):sku:str;quantity:int;movement_type:str;from_location:str|None=None;to_location:str|None=None;reference:str|None=None
@app.get('/api/status')
def root():return {'system':'UNG-VECTOR','domain':'warehouse-logistics','status':'online','version':'0.23.0'}
@app.get('/health')
def health():return {'status':'ok','service':'UNG-VECTOR','version':'0.23.0'}
@app.get('/ready')
def ready():
 try:
  with conn() as c:c.execute('SELECT 1')
  return {'status':'ready','database':'connected','janus':JANUS_BASE_URL}
 except Exception:return {'status':'degraded','database':'unavailable','janus':JANUS_BASE_URL}
@app.get('/v1/system')
def system():return {'system_id':'UNG-VECTOR','domain':'warehouse-logistics','capabilities':['material-master','inventory-control','reservations','stock-status','stock-in-transit','inventory-valuation','immutable-audit-ledger','midas-outbox','batch-lot-tracking','serial-tracking','expiration-tracking','traceability','recalls','locations','inventory','receiving','dispatch','transfer','adjustment','transactional-stock','janus-bearer-auth','bom','mps','mrp','work-centers','routings','capacity-planning','production-orders','mto','mts','production-confirmation','scrap-tracking','crp','capacity-gap-analysis','transport-lanes','carrier-management','freight-costing','shipment-planning','carrier-tendering','shipment-execution','tracking-events','transport-kpis','demand-planning','forecast-accuracy','sop-ibp','atp','ctp','multi-level-mrp','mrp-pegging','supplier-management','procure-to-pay-foundation','quality-inspection','warehouse-tasking','abc-classification','returns-reverse-logistics','control-tower','scenario-planning','cost-to-serve','genealogy','sustainability','planning-alerts','finite-capacity-scheduling','shift-calendars','downtime','shopfloor-execution','wip','nonconformance','capa','transport-exceptions','carrier-performance','load-consolidation','route-sequencing','inventory-simulation','replenishment','slotting','maintenance-capacity-impact','automation-recommendations','inventory-ageing','ageing-buckets','fefo','shelf-life-monitoring','slow-moving-stock','warehouse-layout-design','warehouse-zones','warehouse-paths','multi-temperature-layout','cross-dock-layout','asrs-layout','amr-layout']}
@app.get('/v1/locations')
def locations(authorization:str|None=Header(None)):
 auth('vector.locations.read',authorization)
 with conn() as c:return c.execute('SELECT * FROM vector_locations ORDER BY code').fetchall()
@app.post('/v1/locations',status_code=201)
def create_location(b:LocationIn,authorization:str|None=Header(None)):
 auth('vector.locations.write',authorization)
 with conn() as c:return c.execute('INSERT INTO vector_locations VALUES(%s,%s,%s,%s,%s,%s) RETURNING *',(str(uuid4()),b.code,b.name,b.location_type,'active',now())).fetchone()
@app.get('/v1/inventory')
def inventory(authorization:str|None=Header(None)):
 auth('vector.inventory.read',authorization)
 with conn() as c:return c.execute('SELECT * FROM vector_inventory ORDER BY sku,location_code').fetchall()
@app.post('/v1/inventory',status_code=201)
def create_inventory(b:InventoryIn,authorization:str|None=Header(None)):
 auth('vector.inventory.write',authorization)
 if b.quantity<0:raise HTTPException(400,'quantity_cannot_be_negative')
 with conn() as c:
  return c.execute("INSERT INTO vector_inventory VALUES(%s,%s,%s,%s,%s,'available',%s) ON CONFLICT(sku,location_code) DO UPDATE SET description=EXCLUDED.description,quantity=EXCLUDED.quantity,status='available',updated_at=EXCLUDED.updated_at RETURNING *",(str(uuid4()),b.sku,b.description,b.quantity,b.location_code,now())).fetchone()
@app.get('/v1/movements')
def movements(authorization:str|None=Header(None)):
 auth('vector.movements.read',authorization)
 with conn() as c:return c.execute('SELECT * FROM vector_movements ORDER BY created_at DESC').fetchall()
@app.post('/v1/movements',status_code=201)
def move(b:MovementIn,authorization:str|None=Header(None)):
 auth('vector.movements.write',authorization)
 if b.quantity<=0:raise HTTPException(400,'quantity_must_be_positive')
 if b.movement_type not in {'receive','dispatch','transfer','adjust'}:raise HTTPException(400,'invalid_movement_type')
 if b.movement_type=='receive' and not b.to_location:raise HTTPException(400,'to_location_required')
 if b.movement_type in {'dispatch','adjust'} and not b.from_location:raise HTTPException(400,'from_location_required')
 if b.movement_type=='transfer' and (not b.from_location or not b.to_location or b.from_location==b.to_location):raise HTTPException(400,'distinct_from_and_to_locations_required')
 with conn() as c:
  if b.movement_type=='receive':
   c.execute("INSERT INTO vector_inventory VALUES(%s,%s,%s,%s,%s,'available',%s) ON CONFLICT(sku,location_code) DO UPDATE SET quantity=vector_inventory.quantity+EXCLUDED.quantity,updated_at=EXCLUDED.updated_at",(str(uuid4()),b.sku,b.sku,b.quantity,b.to_location,now()))
  elif b.movement_type=='dispatch':
   row=c.execute('SELECT id,quantity FROM vector_inventory WHERE sku=%s AND location_code=%s FOR UPDATE',(b.sku,b.from_location)).fetchone()
   if not row or row['quantity']<b.quantity:raise HTTPException(409,'insufficient_inventory')
   c.execute('UPDATE vector_inventory SET quantity=quantity-%s,updated_at=%s WHERE id=%s',(b.quantity,now(),row['id']))
  elif b.movement_type=='transfer':
   src=c.execute('SELECT id,quantity,description FROM vector_inventory WHERE sku=%s AND location_code=%s FOR UPDATE',(b.sku,b.from_location)).fetchone()
   if not src or src['quantity']<b.quantity:raise HTTPException(409,'insufficient_inventory')
   c.execute('UPDATE vector_inventory SET quantity=quantity-%s,updated_at=%s WHERE id=%s',(b.quantity,now(),src['id']))
   c.execute("INSERT INTO vector_inventory VALUES(%s,%s,%s,%s,%s,'available',%s) ON CONFLICT(sku,location_code) DO UPDATE SET quantity=vector_inventory.quantity+EXCLUDED.quantity,updated_at=EXCLUDED.updated_at",(str(uuid4()),b.sku,src['description'],b.quantity,b.to_location,now()))
  else:
   row=c.execute('SELECT id,quantity FROM vector_inventory WHERE sku=%s AND location_code=%s FOR UPDATE',(b.sku,b.from_location)).fetchone()
   if not row:raise HTTPException(404,'inventory_not_found')
   adjustment_delta=b.quantity-row['quantity']
   c.execute('UPDATE vector_inventory SET quantity=%s,updated_at=%s WHERE id=%s',(b.quantity,now(),row['id']))
  movement=c.execute('INSERT INTO vector_movements VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *',(str(uuid4()),b.sku,b.quantity,b.movement_type,b.from_location,b.to_location,b.reference,now())).fetchone()
 location=b.to_location if b.movement_type=='receive' else b.from_location
 delta=b.quantity if b.movement_type=='receive' else (-b.quantity if b.movement_type=='dispatch' else 0)
 events=[]
 if b.movement_type=='transfer':
  events=[inventory_changed(b.sku,b.from_location,-b.quantity,'transfer_out',b.reference or ''),inventory_changed(b.sku,b.to_location,b.quantity,'transfer_in',b.reference or '')]
 elif b.movement_type=='adjust':
  events=[inventory_changed(b.sku,b.from_location,adjustment_delta,'adjust',b.reference or '')]
 else:
  events=[inventory_changed(b.sku,location,delta,b.movement_type,b.reference or '')]
 deliveries=[emit(e['target_system'],e['message_type'],e['payload']) for e in events]
 return {'movement':movement,'integration_events':events,'integration_delivery':deliveries}
@app.get('/v1/summary')
def summary(authorization:str|None=Header(None)):
 auth('vector.inventory.read',authorization)
 with conn() as c:return {'distinct_skus':c.execute('SELECT count(DISTINCT sku) n FROM vector_inventory').fetchone()['n'],'units_on_hand':c.execute('SELECT COALESCE(sum(quantity),0) n FROM vector_inventory').fetchone()['n'],'movements':c.execute('SELECT count(*) n FROM vector_movements').fetchone()['n'],'generated_at':now()}

install_material_routes(app, conn, auth)
install_inventory_control_routes(app, conn, auth, emit)
install_valuation_audit_routes(app, conn, auth)
install_traceability_routes(app, conn, auth)
install_manufacturing_routes(app, conn, auth)
install_crp_routes(app, conn, auth)
install_transportation_routes(app, conn, auth)
install_enterprise_suite_routes(app, conn, auth)
install_advanced_ops_routes(app, conn, auth)
install_acceptance_routes(app, conn, auth)
install_inventory_ageing_routes(app, conn, auth)
install_warehouse_layout_routes(app, conn, auth)
install_enterprise_structure_routes(app, conn, auth)
install_mrp_area_routes(app, conn, auth)
install_supply_chain_kpi_routes(app, conn, auth)
install_goods_movement_routes(app, conn, auth)
install_mm_transaction_routes(app, conn, auth)
install_release_approval_routes(app, conn, auth)
install_demand_classification_routes(app, conn, auth)

from pathlib import Path
from ui_portal import install_ui
install_ui(app, Path(__file__).with_name('ui') / 'index.html', JANUS_BASE_URL)
