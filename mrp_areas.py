from datetime import datetime, timezone
from fastapi import Header, HTTPException
from pydantic import BaseModel, Field
from uuid import uuid4
def now():return datetime.now(timezone.utc)
def init_mrp_areas(conn):
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS vector_mrp_areas(
        id UUID PRIMARY KEY,code TEXT UNIQUE NOT NULL,name TEXT NOT NULL,area_type TEXT NOT NULL,
        plant_code TEXT NOT NULL,storage_location TEXT NULL,subcontractor_code TEXT NULL,active BOOLEAN NOT NULL DEFAULT TRUE,created_at TIMESTAMPTZ NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_mrp_area_materials(
        id UUID PRIMARY KEY,area_code TEXT NOT NULL,sku TEXT NOT NULL,safety_stock NUMERIC NOT NULL DEFAULT 0,
        reorder_point NUMERIC NOT NULL DEFAULT 0,lot_size NUMERIC NOT NULL DEFAULT 0,UNIQUE(area_code,sku))""")
class Area(BaseModel):
    code:str;name:str;area_type:str;plant_code:str;storage_location:str|None=None;subcontractor_code:str|None=None
class AreaMaterial(BaseModel):
    sku:str;safety_stock:float=Field(default=0,ge=0);reorder_point:float=Field(default=0,ge=0);lot_size:float=Field(default=0,ge=0)
def install_mrp_area_routes(app,conn,auth):
    @app.post('/v1/planning/mrp-areas',status_code=201)
    def area(b:Area,authorization:str|None=Header(None)):
        auth('vector.planning.write',authorization)
        if b.area_type not in ('plant','storage_location','subcontractor'):raise HTTPException(400,'invalid_mrp_area_type')
        with conn() as c:return c.execute("""INSERT INTO vector_mrp_areas VALUES(%s,%s,%s,%s,%s,%s,%s,true,%s)
        ON CONFLICT(code) DO UPDATE SET name=EXCLUDED.name,area_type=EXCLUDED.area_type,plant_code=EXCLUDED.plant_code,storage_location=EXCLUDED.storage_location,subcontractor_code=EXCLUDED.subcontractor_code RETURNING *""",
        (str(uuid4()),b.code,b.name,b.area_type,b.plant_code,b.storage_location,b.subcontractor_code,now())).fetchone()
    @app.post('/v1/planning/mrp-areas/{code}/materials',status_code=201)
    def material(code:str,b:AreaMaterial,authorization:str|None=Header(None)):
        auth('vector.planning.write',authorization)
        with conn() as c:return c.execute("""INSERT INTO vector_mrp_area_materials VALUES(%s,%s,%s,%s,%s,%s)
        ON CONFLICT(area_code,sku) DO UPDATE SET safety_stock=EXCLUDED.safety_stock,reorder_point=EXCLUDED.reorder_point,lot_size=EXCLUDED.lot_size RETURNING *""",
        (str(uuid4()),code,b.sku,b.safety_stock,b.reorder_point,b.lot_size)).fetchone()
    @app.get('/v1/planning/mrp-areas/{code}/run')
    def run(code:str,authorization:str|None=Header(None)):
        auth('vector.planning.read',authorization)
        with conn() as c:
            a=c.execute('SELECT * FROM vector_mrp_areas WHERE code=%s',(code,)).fetchone()
            if not a:raise HTTPException(404,'mrp_area_not_found')
            ms=c.execute('SELECT * FROM vector_mrp_area_materials WHERE area_code=%s',(code,)).fetchall();out=[]
            for m in ms:
                inv=c.execute('SELECT COALESCE(sum(qty),0) q FROM vector_inventory WHERE sku=%s AND location_code=%s',(m['sku'],a['storage_location'] or a['plant_code'])).fetchone()['q']
                shortage=max(0,float(m['safety_stock'])-float(inv));proposal=max(shortage,float(m['lot_size'])) if shortage else 0
                out.append({'sku':m['sku'],'on_hand':float(inv),'safety_stock':float(m['safety_stock']),'shortage':shortage,'planned_proposal':proposal})
            return {'area':a,'results':out}
