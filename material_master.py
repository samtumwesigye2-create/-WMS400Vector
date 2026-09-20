from datetime import datetime, timezone
from uuid import uuid4
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix='/v1/materials', tags=['materials'])

class MaterialIn(BaseModel):
    sku: str
    name: str
    description: str = ''
    category: str = 'general'
    uom: str = 'EA'
    barcode: str | None = None
    batch_managed: bool = False
    serial_managed: bool = False
    reorder_level: int = 0
    min_level: int = 0
    max_level: int | None = None
    unit_cost: float = 0
    currency: str = 'USD'
    valuation_method: str = 'moving_average'
    material_type: str = 'ROH'
    industry_sector: str = 'general'

def init_material_master(conn):
    with conn() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS vector_materials(
          id UUID PRIMARY KEY, sku TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
          description TEXT NOT NULL DEFAULT '', category TEXT NOT NULL,
          uom TEXT NOT NULL, barcode TEXT UNIQUE NULL,
          batch_managed BOOLEAN NOT NULL DEFAULT FALSE,
          serial_managed BOOLEAN NOT NULL DEFAULT FALSE,
          reorder_level INTEGER NOT NULL DEFAULT 0 CHECK(reorder_level>=0),
          min_level INTEGER NOT NULL DEFAULT 0 CHECK(min_level>=0),
          max_level INTEGER NULL CHECK(max_level IS NULL OR max_level>=0),
          unit_cost NUMERIC(18,4) NOT NULL DEFAULT 0 CHECK(unit_cost>=0),
          currency TEXT NOT NULL DEFAULT 'USD', valuation_method TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'active', created_at TIMESTAMPTZ NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL)''')
        c.execute("ALTER TABLE vector_materials ADD COLUMN IF NOT EXISTS material_type TEXT NOT NULL DEFAULT 'ROH'")
        c.execute("ALTER TABLE vector_materials ADD COLUMN IF NOT EXISTS industry_sector TEXT NOT NULL DEFAULT 'general'")
        c.execute('''CREATE TABLE IF NOT EXISTS vector_material_extensions(
          id UUID PRIMARY KEY, sku TEXT NOT NULL, plant_code TEXT NOT NULL,
          storage_location TEXT NULL, view_name TEXT NOT NULL,
          purchasing_data JSONB NOT NULL DEFAULT '{}'::jsonb,
          mrp_data JSONB NOT NULL DEFAULT '{}'::jsonb,
          accounting_data JSONB NOT NULL DEFAULT '{}'::jsonb,
          storage_data JSONB NOT NULL DEFAULT '{}'::jsonb,
          sales_data JSONB NOT NULL DEFAULT '{}'::jsonb,
          work_scheduling_data JSONB NOT NULL DEFAULT '{}'::jsonb,
          quality_data JSONB NOT NULL DEFAULT '{}'::jsonb,
          forecasting_data JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
          UNIQUE(sku,plant_code,storage_location,view_name))''')

def install_material_routes(app, conn, auth):
    @router.get('')
    def list_materials(authorization: str | None = Header(None)):
        auth('vector.inventory.read', authorization)
        with conn() as c:
            return c.execute('SELECT * FROM vector_materials ORDER BY sku').fetchall()

    @router.get('/{sku}')
    def get_material(sku: str, authorization: str | None = Header(None)):
        auth('vector.inventory.read', authorization)
        with conn() as c:
            row=c.execute('SELECT * FROM vector_materials WHERE sku=%s',(sku,)).fetchone()
            if not row: raise HTTPException(404,'material_not_found')
            return row

    @router.post('', status_code=201)
    def create_material(b: MaterialIn, authorization: str | None = Header(None)):
        auth('vector.inventory.write', authorization)
        if b.max_level is not None and b.max_level < b.min_level:
            raise HTTPException(400,'max_level_below_min_level')
        t=datetime.now(timezone.utc)
        with conn() as c:
            try:
                return c.execute('''INSERT INTO vector_materials
                (id,sku,name,description,category,uom,barcode,batch_managed,serial_managed,
                 reorder_level,min_level,max_level,unit_cost,currency,valuation_method,status,created_at,updated_at,material_type,industry_sector)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',%s,%s,%s,%s) RETURNING *''',
                (str(uuid4()),b.sku,b.name,b.description,b.category,b.uom,b.barcode,
                 b.batch_managed,b.serial_managed,b.reorder_level,b.min_level,b.max_level,
                 b.unit_cost,b.currency,b.valuation_method,t,t,b.material_type,b.industry_sector)).fetchone()
            except Exception as e:
                if 'unique' in str(e).lower(): raise HTTPException(409,'material_already_exists')
                raise


    class MaterialExtensionIn(BaseModel):
        plant_code: str
        storage_location: str | None = None
        views: list[str] = ['basic']
        purchasing_data: dict = {}
        mrp_data: dict = {}
        accounting_data: dict = {}
        storage_data: dict = {}
        sales_data: dict = {}
        work_scheduling_data: dict = {}
        quality_data: dict = {}
        forecasting_data: dict = {}

    @router.post('/{sku}/extend', status_code=201)
    def extend_material(sku: str, b: MaterialExtensionIn, authorization: str | None = Header(None)):
        auth('vector.inventory.write', authorization)
        allowed={'basic','purchasing','mrp','accounting','storage','sales','work_scheduling','quality'}
        bad=set(b.views)-allowed
        if bad: raise HTTPException(400, f'invalid_views:{sorted(bad)}')
        t=datetime.now(timezone.utc); rows=[]
        with conn() as c:
            if not c.execute('SELECT 1 FROM vector_materials WHERE sku=%s',(sku,)).fetchone(): raise HTTPException(404,'material_not_found')
            for view in b.views:
                row=c.execute("""INSERT INTO vector_material_extensions
                (id,sku,plant_code,storage_location,view_name,purchasing_data,mrp_data,accounting_data,storage_data,sales_data,work_scheduling_data,quality_data,forecasting_data,created_at,updated_at)
                VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s)
                ON CONFLICT(sku,plant_code,storage_location,view_name) DO UPDATE SET
                purchasing_data=EXCLUDED.purchasing_data,mrp_data=EXCLUDED.mrp_data,accounting_data=EXCLUDED.accounting_data,
                storage_data=EXCLUDED.storage_data,sales_data=EXCLUDED.sales_data,work_scheduling_data=EXCLUDED.work_scheduling_data,
                quality_data=EXCLUDED.quality_data,forecasting_data=EXCLUDED.forecasting_data,updated_at=EXCLUDED.updated_at RETURNING *""",
                (str(uuid4()),sku,b.plant_code,b.storage_location,view,
                 __import__('json').dumps(b.purchasing_data),__import__('json').dumps(b.mrp_data),__import__('json').dumps(b.accounting_data),
                 __import__('json').dumps(b.storage_data),__import__('json').dumps(b.sales_data),__import__('json').dumps(b.work_scheduling_data),
                 __import__('json').dumps(b.quality_data),__import__('json').dumps(b.forecasting_data),t,t)).fetchone()
                rows.append(row)
        return {'sku':sku,'plant_code':b.plant_code,'storage_location':b.storage_location,'extensions':rows}

    @router.get('/{sku}/extensions')
    def material_extensions(sku: str, authorization: str | None = Header(None)):
        auth('vector.inventory.read', authorization)
        with conn() as c:return c.execute('SELECT * FROM vector_material_extensions WHERE sku=%s ORDER BY plant_code,storage_location,view_name',(sku,)).fetchall()

    app.include_router(router)
