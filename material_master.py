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
                 reorder_level,min_level,max_level,unit_cost,currency,valuation_method,status,created_at,updated_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',%s,%s) RETURNING *''',
                (str(uuid4()),b.sku,b.name,b.description,b.category,b.uom,b.barcode,
                 b.batch_managed,b.serial_managed,b.reorder_level,b.min_level,b.max_level,
                 b.unit_cost,b.currency,b.valuation_method,t,t)).fetchone()
            except Exception as e:
                if 'unique' in str(e).lower(): raise HTTPException(409,'material_already_exists')
                raise

    app.include_router(router)
