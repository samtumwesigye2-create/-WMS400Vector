from datetime import datetime, timezone
from fastapi import Header
from pydantic import BaseModel
from uuid import uuid4

def now(): return datetime.now(timezone.utc)

def init_inventory_ageing(conn):
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS vector_inventory_lots(
          id UUID PRIMARY KEY, sku TEXT NOT NULL, location_code TEXT NOT NULL,
          lot_code TEXT NOT NULL, quantity NUMERIC NOT NULL CHECK(quantity>=0),
          receipt_date DATE NOT NULL, manufacture_date DATE NULL, expiry_date DATE NULL,
          unit_cost NUMERIC NOT NULL DEFAULT 0, stock_status TEXT NOT NULL DEFAULT 'available',
          created_at TIMESTAMPTZ NOT NULL, UNIQUE(sku,location_code,lot_code))""")

class LotReceipt(BaseModel):
    sku:str; location_code:str; lot_code:str; quantity:float
    receipt_date:str; manufacture_date:str|None=None; expiry_date:str|None=None
    unit_cost:float=0; stock_status:str='available'

def bucket(days):
    if days<=30:return '0-30'
    if days<=60:return '31-60'
    if days<=90:return '61-90'
    if days<=180:return '91-180'
    if days<=365:return '181-365'
    return '365+'

def install_inventory_ageing_routes(app,conn,auth):
    @app.post('/v1/inventory-ageing/lots',status_code=201)
    def receive_lot(b:LotReceipt,authorization:str|None=Header(None)):
        auth('vector.inventory.write',authorization)
        with conn() as c:
            return c.execute("""INSERT INTO vector_inventory_lots VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
              ON CONFLICT(sku,location_code,lot_code) DO UPDATE SET quantity=EXCLUDED.quantity,
              receipt_date=EXCLUDED.receipt_date,manufacture_date=EXCLUDED.manufacture_date,
              expiry_date=EXCLUDED.expiry_date,unit_cost=EXCLUDED.unit_cost,stock_status=EXCLUDED.stock_status
              RETURNING *""",(str(uuid4()),b.sku,b.location_code,b.lot_code,b.quantity,b.receipt_date,b.manufacture_date,b.expiry_date,b.unit_cost,b.stock_status,now())).fetchone()

    @app.get('/v1/inventory-ageing/report')
    def ageing_report(sku:str|None=None,location_code:str|None=None,authorization:str|None=Header(None)):
        auth('vector.inventory.read',authorization)
        q="SELECT *,CURRENT_DATE-receipt_date AS age_days,(quantity*unit_cost) AS inventory_value,CASE WHEN expiry_date IS NULL THEN NULL ELSE expiry_date-CURRENT_DATE END AS shelf_life_days FROM vector_inventory_lots WHERE quantity>0"
        p=[]
        if sku:q+=" AND sku=%s";p.append(sku)
        if location_code:q+=" AND location_code=%s";p.append(location_code)
        q+=" ORDER BY receipt_date"
        with conn() as c: rows=c.execute(q,tuple(p)).fetchall()
        out=[]; totals={}
        total_value=sum(float(r['inventory_value']) for r in rows)
        for r in rows:
            d=dict(r);d['ageing_bucket']=bucket(int(r['age_days']));v=float(r['inventory_value'])
            d['ageing_pct_of_value']=round(v*100/total_value,2) if total_value else 0
            d['slow_moving']=int(r['age_days'])>90;d['expired']=r['shelf_life_days'] is not None and int(r['shelf_life_days'])<0
            d['near_expiry']=r['shelf_life_days'] is not None and 0<=int(r['shelf_life_days'])<=30
            totals[d['ageing_bucket']]=totals.get(d['ageing_bucket'],0)+v;out.append(d)
        return {'reporting_date':str(now().date()),'total_inventory_value':total_value,'bucket_values':totals,'lots':out}

    @app.get('/v1/inventory-ageing/fefo')
    def fefo(sku:str,authorization:str|None=Header(None)):
        auth('vector.inventory.read',authorization)
        with conn() as c:return c.execute("""SELECT *,expiry_date-CURRENT_DATE shelf_life_days FROM vector_inventory_lots
          WHERE sku=%s AND quantity>0 AND stock_status='available'
          ORDER BY expiry_date NULLS LAST,receipt_date""",(sku,)).fetchall()

    @app.get('/v1/inventory-ageing/exceptions')
    def ageing_exceptions(authorization:str|None=Header(None)):
        auth('vector.inventory.read',authorization)
        with conn() as c:return c.execute("""SELECT *,CURRENT_DATE-receipt_date age_days,
          CASE WHEN expiry_date IS NULL THEN NULL ELSE expiry_date-CURRENT_DATE END shelf_life_days
          FROM vector_inventory_lots WHERE quantity>0 AND
          (CURRENT_DATE-receipt_date>90 OR stock_status IN ('blocked','rejected','quarantine','damaged')
           OR (expiry_date IS NOT NULL AND expiry_date-CURRENT_DATE<=30))
          ORDER BY expiry_date NULLS LAST,receipt_date""").fetchall()
