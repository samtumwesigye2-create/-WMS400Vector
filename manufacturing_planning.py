from datetime import datetime, timezone
from collections import defaultdict
from uuid import uuid4
from fastapi import Header, HTTPException
from pydantic import BaseModel, Field

def _now(): return datetime.now(timezone.utc)

def validate_bom_graph(rows):
    graph=defaultdict(list)
    for row in rows:
        parent=row['parent_sku']; component=row['component_sku']
        qty=float(row['qty_per'])
        if parent == component:
            raise ValueError('bom_self_reference')
        if qty <= 0:
            raise ValueError('bom_quantity_must_be_positive')
        graph[parent].append(component)
    visiting=set(); visited=set()
    def walk(node):
        if node in visiting: raise ValueError('circular_bom')
        if node in visited: return
        visiting.add(node)
        for child in graph.get(node,[]): walk(child)
        visiting.remove(node); visited.add(node)
    for node in list(graph): walk(node)
    return True

def explode_bom(rows, parent_sku, required_qty=1.0):
    if required_qty <= 0: raise ValueError('required_qty_must_be_positive')
    validate_bom_graph(rows)
    children=defaultdict(list)
    for row in rows:
        children[row['parent_sku']].append((row['component_sku'],float(row['qty_per'])))
    flattened=defaultdict(float)
    genealogy=[]
    def walk(parent, qty, level, path):
        for component,qty_per in children.get(parent,[]):
            required=qty*qty_per
            flattened[component]+=required
            genealogy.append({
                'parent_sku':parent,'component_sku':component,'level':level,
                'qty_per':qty_per,'required_qty':required,'path':path+[component]
            })
            walk(component,required,level+1,path+[component])
    walk(parent_sku,float(required_qty),1,[parent_sku])
    return {
        'parent_sku':parent_sku,
        'required_qty':float(required_qty),
        'requirements':[{'component_sku':sku,'gross_requirement':qty} for sku,qty in sorted(flattened.items())],
        'genealogy':genealogy
    }

def init_manufacturing(conn):
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS vector_bom(id UUID PRIMARY KEY,parent_sku TEXT NOT NULL,component_sku TEXT NOT NULL,qty_per NUMERIC NOT NULL CHECK(qty_per>0),UNIQUE(parent_sku,component_sku))""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_work_centers(id UUID PRIMARY KEY,code TEXT UNIQUE NOT NULL,name TEXT NOT NULL,capacity_per_day NUMERIC NOT NULL DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_routings(id UUID PRIMARY KEY,sku TEXT NOT NULL,operation_no INTEGER NOT NULL,work_center TEXT NOT NULL,description TEXT NOT NULL,minutes_per_unit NUMERIC NOT NULL DEFAULT 0,UNIQUE(sku,operation_no))""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_production_orders(id UUID PRIMARY KEY,sku TEXT NOT NULL,quantity INTEGER NOT NULL CHECK(quantity>0),strategy TEXT NOT NULL,status TEXT NOT NULL,due_date DATE NULL,created_at TIMESTAMPTZ NOT NULL,completed_qty INTEGER NOT NULL DEFAULT 0,scrap_qty INTEGER NOT NULL DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_mps(id UUID PRIMARY KEY,sku TEXT NOT NULL,quantity INTEGER NOT NULL CHECK(quantity>0),period_start DATE NOT NULL,due_date DATE NOT NULL,status TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_bom_revisions(
          id UUID PRIMARY KEY,parent_sku TEXT NOT NULL,revision TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'draft',
          effective_from DATE NULL,effective_to DATE NULL,created_at TIMESTAMPTZ NOT NULL,released_at TIMESTAMPTZ NULL,
          UNIQUE(parent_sku,revision))""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_bom_revision_lines(
          id UUID PRIMARY KEY,bom_revision_id UUID NOT NULL REFERENCES vector_bom_revisions(id) ON DELETE CASCADE,
          component_sku TEXT NOT NULL,qty_per NUMERIC NOT NULL CHECK(qty_per>0),scrap_factor NUMERIC NOT NULL DEFAULT 0 CHECK(scrap_factor>=0),
          alternate_group TEXT NULL,created_at TIMESTAMPTZ NOT NULL,UNIQUE(bom_revision_id,component_sku))""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_vector_bom_revision_parent_status ON vector_bom_revisions(parent_sku,status)")

class BOMIn(BaseModel): parent_sku:str; component_sku:str; qty_per:float=Field(gt=0)
class BOMRevisionIn(BaseModel):
    parent_sku:str
    revision:str
    effective_from:str|None=None
    effective_to:str|None=None
class BOMRevisionLineIn(BaseModel):
    component_sku:str
    qty_per:float=Field(gt=0)
    scrap_factor:float=Field(ge=0,default=0)
    alternate_group:str|None=None
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

    @app.get('/v1/manufacturing/bom/{sku}/explode')
    def bom_explode(sku:str,required_qty:float=1,authorization:str|None=Header(None)):
        auth('vector.manufacturing.read',authorization)
        with conn() as c:
            rows=c.execute('SELECT parent_sku,component_sku,qty_per FROM vector_bom').fetchall()
        try:return explode_bom(rows,sku,required_qty)
        except ValueError as e:raise HTTPException(409,str(e))

    @app.post('/v1/manufacturing/bom-revisions',status_code=201)
    def create_bom_revision(b:BOMRevisionIn,authorization:str|None=Header(None)):
        auth('vector.manufacturing.write',authorization)
        if b.effective_from and b.effective_to and b.effective_to < b.effective_from:
            raise HTTPException(400,'effective_to_before_effective_from')
        with conn() as c:
            if not c.execute('SELECT 1 FROM vector_materials WHERE sku=%s',(b.parent_sku,)).fetchone():
                raise HTTPException(404,'parent_material_not_found')
            try:
                return c.execute("""INSERT INTO vector_bom_revisions
                (id,parent_sku,revision,status,effective_from,effective_to,created_at)
                VALUES(%s,%s,%s,'draft',%s,%s,%s) RETURNING *""",
                (str(uuid4()),b.parent_sku,b.revision,b.effective_from,b.effective_to,_now())).fetchone()
            except Exception as e:
                if 'unique' in str(e).lower(): raise HTTPException(409,'bom_revision_exists')
                raise

    @app.post('/v1/manufacturing/bom-revisions/{revision_id}/lines',status_code=201)
    def add_bom_revision_line(revision_id:str,b:BOMRevisionLineIn,authorization:str|None=Header(None)):
        auth('vector.manufacturing.write',authorization)
        with conn() as c:
            header=c.execute('SELECT * FROM vector_bom_revisions WHERE id=%s',(revision_id,)).fetchone()
            if not header: raise HTTPException(404,'bom_revision_not_found')
            if header['status']!='draft': raise HTTPException(409,'bom_revision_not_draft')
            if not c.execute('SELECT 1 FROM vector_materials WHERE sku=%s',(b.component_sku,)).fetchone():
                raise HTTPException(404,'component_material_not_found')
            if b.component_sku==header['parent_sku']: raise HTTPException(400,'bom_self_reference')
            return c.execute("""INSERT INTO vector_bom_revision_lines
              (id,bom_revision_id,component_sku,qty_per,scrap_factor,alternate_group,created_at)
              VALUES(%s,%s,%s,%s,%s,%s,%s)
              ON CONFLICT(bom_revision_id,component_sku) DO UPDATE SET qty_per=EXCLUDED.qty_per,
              scrap_factor=EXCLUDED.scrap_factor,alternate_group=EXCLUDED.alternate_group RETURNING *""",
              (str(uuid4()),revision_id,b.component_sku,b.qty_per,b.scrap_factor,b.alternate_group,_now())).fetchone()

    @app.post('/v1/manufacturing/bom-revisions/{revision_id}/release')
    def release_bom_revision(revision_id:str,authorization:str|None=Header(None)):
        auth('vector.manufacturing.write',authorization)
        with conn() as c:
            header=c.execute('SELECT * FROM vector_bom_revisions WHERE id=%s FOR UPDATE',(revision_id,)).fetchone()
            if not header: raise HTTPException(404,'bom_revision_not_found')
            lines=c.execute("""SELECT r.parent_sku,l.component_sku,(l.qty_per*(1+l.scrap_factor)) qty_per
              FROM vector_bom_revisions r JOIN vector_bom_revision_lines l ON l.bom_revision_id=r.id
              WHERE r.status IN ('released','draft')""").fetchall()
            target=[x for x in lines if x['parent_sku']==header['parent_sku']]
            if not target: raise HTTPException(409,'bom_revision_has_no_lines')
            try: validate_bom_graph(lines)
            except ValueError as e: raise HTTPException(409,str(e))
            c.execute("UPDATE vector_bom_revisions SET status='superseded' WHERE parent_sku=%s AND status='released' AND id<>%s",(header['parent_sku'],revision_id))
            return c.execute("UPDATE vector_bom_revisions SET status='released',released_at=%s WHERE id=%s RETURNING *",(_now(),revision_id)).fetchone()

    @app.get('/v1/manufacturing/bom-revisions/{revision_id}/explode')
    def explode_bom_revision(revision_id:str,required_qty:float=1,authorization:str|None=Header(None)):
        auth('vector.manufacturing.read',authorization)
        with conn() as c:
            header=c.execute('SELECT * FROM vector_bom_revisions WHERE id=%s',(revision_id,)).fetchone()
            if not header: raise HTTPException(404,'bom_revision_not_found')
            rows=c.execute("""SELECT r.parent_sku,l.component_sku,(l.qty_per*(1+l.scrap_factor)) qty_per
              FROM vector_bom_revisions r JOIN vector_bom_revision_lines l ON l.bom_revision_id=r.id
              WHERE r.status='released' OR r.id=%s""",(revision_id,)).fetchall()
        try:
            result=explode_bom(rows,header['parent_sku'],required_qty)
            result['revision']=header['revision']; result['status']=header['status']
            return result
        except ValueError as e: raise HTTPException(409,str(e))

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
