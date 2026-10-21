from datetime import date, datetime, timezone
from fastapi import APIRouter, Header, Query
from pydantic import BaseModel, Field
from app import auth, conn

router=APIRouter(prefix='/v1/analytics',tags=['Warehouse Analytics'])
def now(): return datetime.now(timezone.utc)

def ensure_schema():
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS vector_reorder_policy(
          sku TEXT NOT NULL, location_code TEXT NOT NULL, reorder_point INTEGER NOT NULL CHECK(reorder_point>=0),
          reorder_quantity INTEGER NOT NULL CHECK(reorder_quantity>0), safety_stock INTEGER NOT NULL DEFAULT 0 CHECK(safety_stock>=0),
          updated_at TIMESTAMPTZ NOT NULL, PRIMARY KEY(sku,location_code))""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_supplier_deliveries(
          id UUID PRIMARY KEY DEFAULT gen_random_uuid(), supplier TEXT NOT NULL, reference TEXT NOT NULL,
          sku TEXT, quantity INTEGER CHECK(quantity IS NULL OR quantity>0), expected_at TIMESTAMPTZ NOT NULL,
          received_at TIMESTAMPTZ, location_code TEXT, created_at TIMESTAMPTZ NOT NULL)""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_vector_supplier_delivery_time ON vector_supplier_deliveries(expected_at DESC)")

class ReorderPolicyIn(BaseModel):
    sku:str=Field(min_length=1,max_length=120); location_code:str=Field(min_length=1,max_length=64)
    reorder_point:int=Field(ge=0); reorder_quantity:int=Field(gt=0); safety_stock:int=Field(ge=0,default=0)

class SupplierDeliveryIn(BaseModel):
    supplier:str=Field(min_length=1,max_length=160); reference:str=Field(min_length=1,max_length=160)
    expected_at:datetime; received_at:datetime|None=None; sku:str|None=None; quantity:int|None=Field(default=None,gt=0)
    location_code:str|None=None

@router.put('/reorder-policy')
def set_reorder_policy(b:ReorderPolicyIn,authorization:str|None=Header(None)):
    auth('vector.inventory.write',authorization);ensure_schema()
    with conn() as c:
        return c.execute("""INSERT INTO vector_reorder_policy VALUES(%s,%s,%s,%s,%s,%s)
          ON CONFLICT(sku,location_code) DO UPDATE SET reorder_point=EXCLUDED.reorder_point,
          reorder_quantity=EXCLUDED.reorder_quantity,safety_stock=EXCLUDED.safety_stock,updated_at=EXCLUDED.updated_at RETURNING *""",
          (b.sku,b.location_code,b.reorder_point,b.reorder_quantity,b.safety_stock,now())).fetchone()

@router.get('/reorder-alerts')
def reorder_alerts(authorization:str|None=Header(None)):
    auth('vector.inventory.read',authorization);ensure_schema()
    with conn() as c:
        return c.execute("""SELECT p.sku,p.location_code,COALESCE(i.quantity,0) current_stock,p.reorder_point,p.safety_stock,
          p.reorder_quantity,(p.reorder_point-COALESCE(i.quantity,0)) shortage,
          CASE WHEN COALESCE(i.quantity,0)<=p.safety_stock THEN 'critical' ELSE 'reorder' END status
          FROM vector_reorder_policy p LEFT JOIN vector_inventory i ON i.sku=p.sku AND i.location_code=p.location_code
          WHERE COALESCE(i.quantity,0)<=p.reorder_point ORDER BY status,current_stock""").fetchall()

@router.post('/supplier-deliveries',status_code=201)
def record_supplier_delivery(b:SupplierDeliveryIn,authorization:str|None=Header(None)):
    auth('vector.inventory.write',authorization);ensure_schema()
    with conn() as c:
        return c.execute("""INSERT INTO vector_supplier_deliveries
          (supplier,reference,sku,quantity,expected_at,received_at,location_code,created_at)
          VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
          (b.supplier,b.reference,b.sku,b.quantity,b.expected_at,b.received_at,b.location_code,now())).fetchone()

@router.get('/supplier-performance')
def supplier_performance(start:date|None=None,end:date|None=None,authorization:str|None=Header(None)):
    auth('vector.inventory.read',authorization);ensure_schema()
    start=start or date(2000,1,1);end=end or date.today()
    with conn() as c:
        return c.execute("""SELECT supplier,COUNT(*) total_orders,
          COUNT(*) FILTER(WHERE received_at IS NOT NULL) received_orders,
          COUNT(*) FILTER(WHERE received_at IS NOT NULL AND received_at<=expected_at) on_time_orders,
          ROUND(100.0*COUNT(*) FILTER(WHERE received_at IS NOT NULL AND received_at<=expected_at)/NULLIF(COUNT(*) FILTER(WHERE received_at IS NOT NULL),0),2) on_time_pct
          FROM vector_supplier_deliveries WHERE expected_at::date BETWEEN %s AND %s
          GROUP BY supplier ORDER BY on_time_pct DESC NULLS LAST,supplier""",(start,end)).fetchall()

@router.get('/dashboard')
def dashboard(start:date|None=None,end:date|None=None,location_code:str|None=None,authorization:str|None=Header(None)):
    auth('vector.inventory.read',authorization);ensure_schema()
    end=end or date.today();start=start or date(end.year,1,1)
    with conn() as c:
        params=[start,end]
        loc_sql='';inv_params=[]
        if location_code: loc_sql=' WHERE i.location_code=%s';inv_params=[location_code]
        summary=c.execute(f"""SELECT COALESCE(SUM(i.quantity),0) current_stock,
          COALESCE(SUM(i.quantity*COALESCE(p.unit_price,0)),0)::double precision inventory_value,
          COUNT(DISTINCT i.sku) distinct_skus FROM vector_inventory i
          LEFT JOIN vector_item_costs p ON p.sku=i.sku{loc_sql}""",inv_params).fetchone()
        move_loc=" AND (from_location=%s OR to_location=%s)" if location_code else ""
        mp=params+([location_code,location_code] if location_code else [])
        movement=c.execute(f"""SELECT date_trunc('month',created_at)::date month,
          COALESCE(SUM(quantity) FILTER(WHERE movement_type='receive'),0) stock_in,
          COALESCE(SUM(quantity) FILTER(WHERE movement_type='dispatch'),0) stock_out
          FROM vector_movements WHERE created_at::date BETWEEN %s AND %s{move_loc}
          GROUP BY 1 ORDER BY 1""",mp).fetchall()
        top=c.execute(f"""SELECT i.sku,MAX(i.description) description,SUM(i.quantity) quantity,
          MAX(COALESCE(p.unit_price,0)) unit_price,
          SUM(i.quantity*COALESCE(p.unit_price,0))::double precision inventory_value
          FROM vector_inventory i LEFT JOIN vector_item_costs p ON p.sku=i.sku{loc_sql}
          GROUP BY i.sku ORDER BY inventory_value DESC LIMIT 10""",inv_params).fetchall()
        by_wh=c.execute("""SELECT location_code,SUM(quantity) quantity FROM vector_inventory GROUP BY location_code ORDER BY quantity DESC""").fetchall()
        status=c.execute("""SELECT CASE WHEN i.quantity=0 THEN 'out_of_stock'
          WHEN p.reorder_point IS NOT NULL AND i.quantity<=p.reorder_point THEN 'low_stock' ELSE 'active' END status,
          COUNT(*) items,SUM(i.quantity) units FROM vector_inventory i LEFT JOIN vector_reorder_policy p
          ON p.sku=i.sku AND p.location_code=i.location_code GROUP BY 1 ORDER BY 1""").fetchall()
        categories=c.execute("""SELECT COALESCE(NULLIF(split_part(i.description,' ',1),''),'Uncategorized') category,
          SUM(i.quantity*COALESCE(p.unit_price,0))::double precision inventory_value
          FROM vector_inventory i LEFT JOIN vector_item_costs p ON p.sku=i.sku GROUP BY 1 ORDER BY inventory_value DESC""").fetchall()
        alerts=c.execute("""SELECT COUNT(*) n FROM vector_reorder_policy p LEFT JOIN vector_inventory i
          ON i.sku=p.sku AND i.location_code=p.location_code WHERE COALESCE(i.quantity,0)<=p.reorder_point""").fetchone()['n']
        shipments=c.execute("""SELECT id,sku,quantity,movement_type,from_location,to_location,reference,created_at
          FROM vector_movements ORDER BY created_at DESC LIMIT 10""").fetchall()
    return {'period':{'start':start,'end':end},'location_code':location_code,'summary':{**summary,'low_stock_items':alerts},
      'monthly_stock_movement':movement,'inventory_value_by_category':categories,'stock_by_warehouse':by_wh,
      'top_products_by_inventory_value':top,'inventory_status_breakdown':status,'recent_shipments':shipments,'generated_at':now()}
