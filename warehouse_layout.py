from datetime import datetime, timezone
from fastapi import Header, HTTPException
from pydantic import BaseModel, Field
from uuid import uuid4

def now(): return datetime.now(timezone.utc)
LAYOUT_TYPES={'through-flow','u-flow','cross-dock','velocity-zoned','forward-pick-reserve','goods-to-person-asrs','amr-flexible','high-bay-dense','multi-temperature','omnichannel-hybrid'}

def init_warehouse_layout(conn):
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS vector_warehouse_layouts(
          id UUID PRIMARY KEY,code TEXT UNIQUE NOT NULL,name TEXT NOT NULL,layout_type TEXT NOT NULL,
          description TEXT NULL,status TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL,updated_at TIMESTAMPTZ NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_warehouse_zones(
          id UUID PRIMARY KEY,layout_code TEXT NOT NULL,zone_code TEXT NOT NULL,zone_type TEXT NOT NULL,
          temperature_min_c NUMERIC NULL,temperature_max_c NUMERIC NULL,velocity_class TEXT NULL,
          capacity_units NUMERIC NOT NULL DEFAULT 0,pick_sequence INTEGER NOT NULL DEFAULT 0,
          UNIQUE(layout_code,zone_code))""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_warehouse_paths(
          id UUID PRIMARY KEY,layout_code TEXT NOT NULL,from_zone TEXT NOT NULL,to_zone TEXT NOT NULL,
          path_type TEXT NOT NULL,distance_m NUMERIC NOT NULL DEFAULT 0,one_way BOOLEAN NOT NULL DEFAULT FALSE)""")

class LayoutIn(BaseModel):
    code:str; name:str; layout_type:str; description:str|None=None
class ZoneIn(BaseModel):
    zone_code:str; zone_type:str; temperature_min_c:float|None=None; temperature_max_c:float|None=None
    velocity_class:str|None=None; capacity_units:float=Field(default=0,ge=0); pick_sequence:int=0
class PathIn(BaseModel):
    from_zone:str; to_zone:str; path_type:str='material'; distance_m:float=Field(default=0,ge=0); one_way:bool=False

def install_warehouse_layout_routes(app,conn,auth):
    @app.post('/v1/warehouse/layouts',status_code=201)
    def create_layout(b:LayoutIn,authorization:str|None=Header(None)):
        auth('vector.warehouse.write',authorization)
        if b.layout_type not in LAYOUT_TYPES:raise HTTPException(400,'invalid_layout_type')
        with conn() as c:return c.execute("""INSERT INTO vector_warehouse_layouts VALUES(%s,%s,%s,%s,%s,'active',%s,%s)
          ON CONFLICT(code) DO UPDATE SET name=EXCLUDED.name,layout_type=EXCLUDED.layout_type,description=EXCLUDED.description,updated_at=EXCLUDED.updated_at RETURNING *""",
          (str(uuid4()),b.code,b.name,b.layout_type,b.description,now(),now())).fetchone()

    @app.get('/v1/warehouse/layouts')
    def layouts(authorization:str|None=Header(None)):
        auth('vector.warehouse.read',authorization)
        with conn() as c:return c.execute('SELECT * FROM vector_warehouse_layouts ORDER BY code').fetchall()

    @app.post('/v1/warehouse/layouts/{code}/zones',status_code=201)
    def add_zone(code:str,b:ZoneIn,authorization:str|None=Header(None)):
        auth('vector.warehouse.write',authorization)
        with conn() as c:return c.execute("""INSERT INTO vector_warehouse_zones VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
          ON CONFLICT(layout_code,zone_code) DO UPDATE SET zone_type=EXCLUDED.zone_type,temperature_min_c=EXCLUDED.temperature_min_c,
          temperature_max_c=EXCLUDED.temperature_max_c,velocity_class=EXCLUDED.velocity_class,capacity_units=EXCLUDED.capacity_units,pick_sequence=EXCLUDED.pick_sequence RETURNING *""",
          (str(uuid4()),code,b.zone_code,b.zone_type,b.temperature_min_c,b.temperature_max_c,b.velocity_class,b.capacity_units,b.pick_sequence)).fetchone()

    @app.post('/v1/warehouse/layouts/{code}/paths',status_code=201)
    def add_path(code:str,b:PathIn,authorization:str|None=Header(None)):
        auth('vector.warehouse.write',authorization)
        with conn() as c:return c.execute('INSERT INTO vector_warehouse_paths VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *',
          (str(uuid4()),code,b.from_zone,b.to_zone,b.path_type,b.distance_m,b.one_way)).fetchone()

    @app.get('/v1/warehouse/layouts/{code}/model')
    def model(code:str,authorization:str|None=Header(None)):
        auth('vector.warehouse.read',authorization)
        with conn() as c:
            layout=c.execute('SELECT * FROM vector_warehouse_layouts WHERE code=%s',(code,)).fetchone()
            if not layout:raise HTTPException(404,'layout_not_found')
            zones=c.execute('SELECT * FROM vector_warehouse_zones WHERE layout_code=%s ORDER BY pick_sequence,zone_code',(code,)).fetchall()
            paths=c.execute('SELECT * FROM vector_warehouse_paths WHERE layout_code=%s ORDER BY from_zone,to_zone',(code,)).fetchall()
            return {'layout':layout,'zones':zones,'paths':paths}

    @app.get('/v1/warehouse/layouts/{code}/analysis')
    def analysis(code:str,authorization:str|None=Header(None)):
        auth('vector.warehouse.read',authorization)
        with conn() as c:
            z=c.execute('SELECT count(*) zones,COALESCE(sum(capacity_units),0) capacity FROM vector_warehouse_zones WHERE layout_code=%s',(code,)).fetchone()
            p=c.execute('SELECT count(*) paths,COALESCE(sum(distance_m),0) distance FROM vector_warehouse_paths WHERE layout_code=%s',(code,)).fetchone()
            velocities=c.execute("SELECT velocity_class,count(*) n FROM vector_warehouse_zones WHERE layout_code=%s AND velocity_class IS NOT NULL GROUP BY velocity_class",(code,)).fetchall()
            return {'layout_code':code,'zone_count':z['zones'],'configured_capacity_units':float(z['capacity']),'path_count':p['paths'],'configured_path_distance_m':float(p['distance']),'velocity_zones':velocities}
