from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from datetime import datetime, timezone
from uuid import uuid4
import os, psycopg
from psycopg.rows import dict_row

app = FastAPI(title='UNG-VECTOR', version='0.1.0')
DB = os.getenv('DATABASE_URL','')

def now(): return datetime.now(timezone.utc).isoformat()
def auth(permission, header):
    perms={x.strip() for x in (header or '').split(',') if x.strip()}
    if permission not in perms and 'ung.admin' not in perms:
        raise HTTPException(403,'UNG-JANUS permission required')
def conn():
    if not DB: raise HTTPException(503,'database_not_configured')
    return psycopg.connect(DB,row_factory=dict_row)
def init_db():
    if not DB: return
    with conn() as c:
        c.execute('CREATE TABLE IF NOT EXISTS vector_locations (id UUID PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, location_type TEXT NOT NULL, status TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS vector_inventory (id UUID PRIMARY KEY, sku TEXT NOT NULL, description TEXT NOT NULL, quantity INTEGER NOT NULL, location_code TEXT NOT NULL, status TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS vector_movements (id UUID PRIMARY KEY, sku TEXT NOT NULL, quantity INTEGER NOT NULL, movement_type TEXT NOT NULL, from_location TEXT NULL, to_location TEXT NULL, reference TEXT NULL, created_at TIMESTAMPTZ NOT NULL)')

@app.on_event('startup')
def startup(): init_db()

class LocationIn(BaseModel):
    code:str; name:str; location_type:str='warehouse'
class InventoryIn(BaseModel):
    sku:str; description:str; quantity:int; location_code:str
class MovementIn(BaseModel):
    sku:str; quantity:int; movement_type:str; from_location:str|None=None; to_location:str|None=None; reference:str|None=None

@app.get('/')
def root(): return {'system':'UNG-VECTOR','domain':'warehouse-logistics','status':'online','version':'0.1.0'}
@app.get('/health')
def health(): return {'status':'ok','service':'UNG-VECTOR','version':'0.1.0'}
@app.get('/ready')
def ready():
    try:
        with conn() as c: c.execute('SELECT 1')
        return {'status':'ready','service':'UNG-VECTOR','database':'connected'}
    except Exception:
        return {'status':'degraded','service':'UNG-VECTOR','database':'unavailable'}
@app.get('/v1/system')
def system(): return {'system_id':'UNG-VECTOR','domain':'warehouse-logistics','capabilities':['locations','inventory','movements','receiving','dispatch','postgresql']}
@app.get('/v1/locations')
def locations(x_ung_permissions:str|None=Header(None)):
    auth('vector.locations.read',x_ung_permissions)
    with conn() as c: return c.execute('SELECT * FROM vector_locations ORDER BY code').fetchall()
@app.post('/v1/locations',status_code=201)
def create_location(body:LocationIn,x_ung_permissions:str|None=Header(None)):
    auth('vector.locations.write',x_ung_permissions); rid=str(uuid4())
    with conn() as c: return c.execute('INSERT INTO vector_locations(id,code,name,location_type,status,created_at) VALUES(%s,%s,%s,%s,%s,%s) RETURNING *',(rid,body.code,body.name,body.location_type,'active',now())).fetchone()
@app.get('/v1/inventory')
def inventory(x_ung_permissions:str|None=Header(None)):
    auth('vector.inventory.read',x_ung_permissions)
    with conn() as c: return c.execute('SELECT * FROM vector_inventory ORDER BY sku').fetchall()
@app.post('/v1/inventory',status_code=201)
def create_inventory(body:InventoryIn,x_ung_permissions:str|None=Header(None)):
    auth('vector.inventory.write',x_ung_permissions); rid=str(uuid4())
    with conn() as c: return c.execute('INSERT INTO vector_inventory(id,sku,description,quantity,location_code,status,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *',(rid,body.sku,body.description,body.quantity,body.location_code,'available',now())).fetchone()
@app.get('/v1/movements')
def movements(x_ung_permissions:str|None=Header(None)):
    auth('vector.movements.read',x_ung_permissions)
    with conn() as c: return c.execute('SELECT * FROM vector_movements ORDER BY created_at DESC').fetchall()
@app.post('/v1/movements',status_code=201)
def create_movement(body:MovementIn,x_ung_permissions:str|None=Header(None)):
    auth('vector.movements.write',x_ung_permissions)
    if body.quantity <= 0: raise HTTPException(400,'quantity_must_be_positive')
    if body.movement_type not in {'receive','dispatch','transfer','adjust'}: raise HTTPException(400,'invalid_movement_type')
    rid=str(uuid4())
    with conn() as c:
        rec=c.execute('INSERT INTO vector_movements(id,sku,quantity,movement_type,from_location,to_location,reference,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *',(rid,body.sku,body.quantity,body.movement_type,body.from_location,body.to_location,body.reference,now())).fetchone()
        if body.movement_type=='receive' and body.to_location:
            item=c.execute('SELECT * FROM vector_inventory WHERE sku=%s AND location_code=%s ORDER BY updated_at DESC LIMIT 1',(body.sku,body.to_location)).fetchone()
            if item: c.execute('UPDATE vector_inventory SET quantity=quantity+%s, updated_at=%s WHERE id=%s',(body.quantity,now(),item['id']))
        elif body.movement_type=='dispatch' and body.from_location:
            item=c.execute('SELECT * FROM vector_inventory WHERE sku=%s AND location_code=%s ORDER BY updated_at DESC LIMIT 1',(body.sku,body.from_location)).fetchone()
            if not item or item['quantity'] < body.quantity: raise HTTPException(409,'insufficient_inventory')
            c.execute('UPDATE vector_inventory SET quantity=quantity-%s, updated_at=%s WHERE id=%s',(body.quantity,now(),item['id']))
        return rec
@app.get('/v1/summary')
def summary(x_ung_permissions:str|None=Header(None)):
    auth('vector.inventory.read',x_ung_permissions)
    with conn() as c:
        sku=c.execute('SELECT count(DISTINCT sku) n FROM vector_inventory').fetchone()['n']
        qty=c.execute('SELECT COALESCE(sum(quantity),0) n FROM vector_inventory').fetchone()['n']
        moves=c.execute('SELECT count(*) n FROM vector_movements').fetchone()['n']
    return {'distinct_skus':sku,'units_on_hand':qty,'movements':moves,'generated_at':now()}
