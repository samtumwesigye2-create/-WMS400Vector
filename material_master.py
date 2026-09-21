from datetime import datetime, timezone
from uuid import uuid4
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix='/v1/materials', tags=['materials'])

class MaterialDimensionsIn(BaseModel):
    length: float | None = None
    width: float | None = None
    height: float | None = None
    weight: float | None = None
    dimension_uom: str = 'mm'
    weight_uom: str = 'kg'

class MaterialPlanningIn(BaseModel):
    mrp_policy: str = 'mrp'
    safety_stock: int = 0
    reorder_point: int = 0
    min_order_qty: int = 0
    max_order_qty: int | None = None
    order_multiple: int = 1
    lead_time_days: int = 0
    make_buy: str = 'buy'

class MaterialCostingIn(BaseModel):
    valuation_method: str = 'moving_average'
    standard_cost: float = 0
    moving_average_cost: float = 0
    currency: str = 'USD'

class MaterialSourceIn(BaseModel):
    supplier_id: str
    supplier_sku: str | None = None
    manufacturer: str | None = None
    manufacturer_part_number: str | None = None
    min_order_qty: int = 0
    lead_time_days: int = 0
    unit_price: float | None = None
    currency: str = 'USD'
    approved: bool = True

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

        c.execute('''CREATE TABLE IF NOT EXISTS vector_material_dimensions(
          sku TEXT PRIMARY KEY REFERENCES vector_materials(sku) ON DELETE CASCADE,
          length NUMERIC(18,4) NULL CHECK(length IS NULL OR length>=0),
          width NUMERIC(18,4) NULL CHECK(width IS NULL OR width>=0),
          height NUMERIC(18,4) NULL CHECK(height IS NULL OR height>=0),
          weight NUMERIC(18,4) NULL CHECK(weight IS NULL OR weight>=0),
          dimension_uom TEXT NOT NULL DEFAULT 'mm',
          weight_uom TEXT NOT NULL DEFAULT 'kg',
          updated_at TIMESTAMPTZ NOT NULL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS vector_material_planning(
          sku TEXT PRIMARY KEY REFERENCES vector_materials(sku) ON DELETE CASCADE,
          mrp_policy TEXT NOT NULL DEFAULT 'mrp',
          safety_stock INTEGER NOT NULL DEFAULT 0 CHECK(safety_stock>=0),
          reorder_point INTEGER NOT NULL DEFAULT 0 CHECK(reorder_point>=0),
          min_order_qty INTEGER NOT NULL DEFAULT 0 CHECK(min_order_qty>=0),
          max_order_qty INTEGER NULL CHECK(max_order_qty IS NULL OR max_order_qty>=0),
          order_multiple INTEGER NOT NULL DEFAULT 1 CHECK(order_multiple>0),
          lead_time_days INTEGER NOT NULL DEFAULT 0 CHECK(lead_time_days>=0),
          make_buy TEXT NOT NULL DEFAULT 'buy' CHECK(make_buy IN ('make','buy')),
          updated_at TIMESTAMPTZ NOT NULL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS vector_material_costing(
          sku TEXT PRIMARY KEY REFERENCES vector_materials(sku) ON DELETE CASCADE,
          valuation_method TEXT NOT NULL DEFAULT 'moving_average',
          standard_cost NUMERIC(18,4) NOT NULL DEFAULT 0 CHECK(standard_cost>=0),
          moving_average_cost NUMERIC(18,4) NOT NULL DEFAULT 0 CHECK(moving_average_cost>=0),
          currency TEXT NOT NULL DEFAULT 'USD',
          effective_at TIMESTAMPTZ NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS vector_material_sources(
          id UUID PRIMARY KEY,
          sku TEXT NOT NULL REFERENCES vector_materials(sku) ON DELETE CASCADE,
          supplier_id TEXT NOT NULL,
          supplier_sku TEXT NULL,
          manufacturer TEXT NULL,
          manufacturer_part_number TEXT NULL,
          min_order_qty INTEGER NOT NULL DEFAULT 0 CHECK(min_order_qty>=0),
          lead_time_days INTEGER NOT NULL DEFAULT 0 CHECK(lead_time_days>=0),
          unit_price NUMERIC(18,4) NULL CHECK(unit_price IS NULL OR unit_price>=0),
          currency TEXT NOT NULL DEFAULT 'USD',
          approved BOOLEAN NOT NULL DEFAULT TRUE,
          created_at TIMESTAMPTZ NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL,
          UNIQUE(sku,supplier_id))''')
        c.execute('''CREATE TABLE IF NOT EXISTS vector_material_changes(
          id UUID PRIMARY KEY,
          sku TEXT NOT NULL,
          change_type TEXT NOT NULL,
          old_value JSONB NULL,
          new_value JSONB NULL,
          changed_at TIMESTAMPTZ NOT NULL)''')
        c.execute('CREATE INDEX IF NOT EXISTS ix_vector_material_changes_sku_changed_at ON vector_material_changes(sku,changed_at DESC)')

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

    def _require_material(c, sku: str):
        if not c.execute('SELECT 1 FROM vector_materials WHERE sku=%s',(sku,)).fetchone():
            raise HTTPException(404,'material_not_found')

    def _audit(c, sku: str, change_type: str, old_value, new_value):
        import json
        c.execute('''INSERT INTO vector_material_changes(id,sku,change_type,old_value,new_value,changed_at)
                     VALUES(%s,%s,%s,%s::jsonb,%s::jsonb,%s)''',
                  (str(uuid4()),sku,change_type,
                   json.dumps(old_value) if old_value is not None else None,
                   json.dumps(new_value) if new_value is not None else None,
                   datetime.now(timezone.utc)))

    @router.get('/{sku}/master')
    def material_master_record(sku: str, authorization: str | None = Header(None)):
        auth('vector.inventory.read', authorization)
        with conn() as c:
            _require_material(c, sku)
            return {
                'material': c.execute('SELECT * FROM vector_materials WHERE sku=%s',(sku,)).fetchone(),
                'dimensions': c.execute('SELECT * FROM vector_material_dimensions WHERE sku=%s',(sku,)).fetchone(),
                'planning': c.execute('SELECT * FROM vector_material_planning WHERE sku=%s',(sku,)).fetchone(),
                'costing': c.execute('SELECT * FROM vector_material_costing WHERE sku=%s',(sku,)).fetchone(),
                'sources': c.execute('SELECT * FROM vector_material_sources WHERE sku=%s ORDER BY approved DESC,supplier_id',(sku,)).fetchall(),
                'extensions': c.execute('SELECT * FROM vector_material_extensions WHERE sku=%s ORDER BY plant_code,storage_location,view_name',(sku,)).fetchall(),
            }

    @router.put('/{sku}/dimensions')
    def set_dimensions(sku: str, b: MaterialDimensionsIn, authorization: str | None = Header(None)):
        auth('vector.inventory.write', authorization)
        vals=b.model_dump()
        for k in ('length','width','height','weight'):
            if vals[k] is not None and vals[k] < 0: raise HTTPException(400,f'{k}_cannot_be_negative')
        t=datetime.now(timezone.utc)
        with conn() as c:
            _require_material(c, sku)
            old=c.execute('SELECT * FROM vector_material_dimensions WHERE sku=%s',(sku,)).fetchone()
            row=c.execute('''INSERT INTO vector_material_dimensions(sku,length,width,height,weight,dimension_uom,weight_uom,updated_at)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
              ON CONFLICT(sku) DO UPDATE SET length=EXCLUDED.length,width=EXCLUDED.width,height=EXCLUDED.height,
              weight=EXCLUDED.weight,dimension_uom=EXCLUDED.dimension_uom,weight_uom=EXCLUDED.weight_uom,updated_at=EXCLUDED.updated_at
              RETURNING *''',(sku,b.length,b.width,b.height,b.weight,b.dimension_uom,b.weight_uom,t)).fetchone()
            _audit(c,sku,'dimensions.updated',old,row)
            return row

    @router.put('/{sku}/planning')
    def set_planning(sku: str, b: MaterialPlanningIn, authorization: str | None = Header(None)):
        auth('vector.inventory.write', authorization)
        if b.max_order_qty is not None and b.max_order_qty < b.min_order_qty:
            raise HTTPException(400,'max_order_qty_below_min_order_qty')
        if b.make_buy not in {'make','buy'}: raise HTTPException(400,'invalid_make_buy')
        t=datetime.now(timezone.utc)
        with conn() as c:
            _require_material(c, sku)
            old=c.execute('SELECT * FROM vector_material_planning WHERE sku=%s',(sku,)).fetchone()
            row=c.execute('''INSERT INTO vector_material_planning
              (sku,mrp_policy,safety_stock,reorder_point,min_order_qty,max_order_qty,order_multiple,lead_time_days,make_buy,updated_at)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
              ON CONFLICT(sku) DO UPDATE SET mrp_policy=EXCLUDED.mrp_policy,safety_stock=EXCLUDED.safety_stock,
              reorder_point=EXCLUDED.reorder_point,min_order_qty=EXCLUDED.min_order_qty,max_order_qty=EXCLUDED.max_order_qty,
              order_multiple=EXCLUDED.order_multiple,lead_time_days=EXCLUDED.lead_time_days,make_buy=EXCLUDED.make_buy,updated_at=EXCLUDED.updated_at
              RETURNING *''',(sku,b.mrp_policy,b.safety_stock,b.reorder_point,b.min_order_qty,b.max_order_qty,b.order_multiple,b.lead_time_days,b.make_buy,t)).fetchone()
            _audit(c,sku,'planning.updated',old,row)
            return row

    @router.put('/{sku}/costing')
    def set_costing(sku: str, b: MaterialCostingIn, authorization: str | None = Header(None)):
        auth('vector.inventory.write', authorization)
        if b.standard_cost < 0 or b.moving_average_cost < 0: raise HTTPException(400,'cost_cannot_be_negative')
        t=datetime.now(timezone.utc)
        with conn() as c:
            _require_material(c, sku)
            old=c.execute('SELECT * FROM vector_material_costing WHERE sku=%s',(sku,)).fetchone()
            row=c.execute('''INSERT INTO vector_material_costing
              (sku,valuation_method,standard_cost,moving_average_cost,currency,effective_at,updated_at)
              VALUES(%s,%s,%s,%s,%s,%s,%s)
              ON CONFLICT(sku) DO UPDATE SET valuation_method=EXCLUDED.valuation_method,standard_cost=EXCLUDED.standard_cost,
              moving_average_cost=EXCLUDED.moving_average_cost,currency=EXCLUDED.currency,effective_at=EXCLUDED.effective_at,
              updated_at=EXCLUDED.updated_at RETURNING *''',
              (sku,b.valuation_method,b.standard_cost,b.moving_average_cost,b.currency,t,t)).fetchone()
            _audit(c,sku,'costing.updated',old,row)
            return row

    @router.post('/{sku}/sources', status_code=201)
    def upsert_source(sku: str, b: MaterialSourceIn, authorization: str | None = Header(None)):
        auth('vector.inventory.write', authorization)
        if b.min_order_qty < 0 or b.lead_time_days < 0 or (b.unit_price is not None and b.unit_price < 0):
            raise HTTPException(400,'invalid_source_values')
        t=datetime.now(timezone.utc)
        with conn() as c:
            _require_material(c, sku)
            old=c.execute('SELECT * FROM vector_material_sources WHERE sku=%s AND supplier_id=%s',(sku,b.supplier_id)).fetchone()
            row=c.execute('''INSERT INTO vector_material_sources
              (id,sku,supplier_id,supplier_sku,manufacturer,manufacturer_part_number,min_order_qty,lead_time_days,unit_price,currency,approved,created_at,updated_at)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
              ON CONFLICT(sku,supplier_id) DO UPDATE SET supplier_sku=EXCLUDED.supplier_sku,manufacturer=EXCLUDED.manufacturer,
              manufacturer_part_number=EXCLUDED.manufacturer_part_number,min_order_qty=EXCLUDED.min_order_qty,
              lead_time_days=EXCLUDED.lead_time_days,unit_price=EXCLUDED.unit_price,currency=EXCLUDED.currency,
              approved=EXCLUDED.approved,updated_at=EXCLUDED.updated_at RETURNING *''',
              (str(uuid4()),sku,b.supplier_id,b.supplier_sku,b.manufacturer,b.manufacturer_part_number,b.min_order_qty,
               b.lead_time_days,b.unit_price,b.currency,b.approved,t,t)).fetchone()
            _audit(c,sku,'source.updated',old,row)
            return row

    @router.get('/{sku}/changes')
    def material_changes(sku: str, authorization: str | None = Header(None)):
        auth('vector.inventory.read', authorization)
        with conn() as c:
            _require_material(c, sku)
            return c.execute('SELECT * FROM vector_material_changes WHERE sku=%s ORDER BY changed_at DESC',(sku,)).fetchall()

    app.include_router(router)
