from datetime import date, datetime, timezone
from uuid import uuid4
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from app import auth, conn, now

router=APIRouter(prefix="/v1/inventory-methods",tags=["Inventory Methods"])

def ensure():
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS vector_stock_lots(
          id UUID PRIMARY KEY, lot_code TEXT UNIQUE NOT NULL, sku TEXT NOT NULL,
          description TEXT NOT NULL, location_code TEXT NOT NULL,
          received_at TIMESTAMPTZ NOT NULL, expiry_date DATE NULL,
          quantity_received DOUBLE PRECISION NOT NULL, quantity_available DOUBLE PRECISION NOT NULL,
          unit_cost DOUBLE PRECISION NOT NULL DEFAULT 0, currency TEXT NOT NULL DEFAULT 'USD',
          created_at TIMESTAMPTZ NOT NULL)""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_vector_lots_sku_loc ON vector_stock_lots(sku,location_code)")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_lot_issues(
          id UUID PRIMARY KEY, issue_number TEXT UNIQUE NOT NULL, sku TEXT NOT NULL,
          location_code TEXT NOT NULL, strategy TEXT NOT NULL, quantity DOUBLE PRECISION NOT NULL,
          total_cost DOUBLE PRECISION NOT NULL, currency TEXT NOT NULL,
          allocations JSONB NOT NULL, reference TEXT NULL, created_at TIMESTAMPTZ NOT NULL)""")

class LotReceipt(BaseModel):
    lot_code:str
    sku:str
    description:str=""
    location_code:str
    quantity:float=Field(gt=0)
    unit_cost:float=Field(default=0,ge=0)
    currency:str="USD"
    received_at:datetime|None=None
    expiry_date:date|None=None

class IssueIn(BaseModel):
    sku:str
    location_code:str
    quantity:float=Field(gt=0)
    strategy:str="FIFO"
    reference:str|None=None

def _ordered_lots(c,sku,location,strategy):
    strategy=strategy.upper()
    order={
      "FIFO":"received_at ASC, created_at ASC",
      "LIFO":"received_at DESC, created_at DESC",
      "FEFO":"expiry_date ASC NULLS LAST, received_at ASC",
      "HIFO":"unit_cost DESC, received_at ASC",
      "MOFIFO":"quantity_available DESC, received_at ASC",
    }.get(strategy)
    if not order: raise HTTPException(422,"strategy_must_be_FIFO_LIFO_FEFO_HIFO_or_MOFIFO")
    return c.execute(f"""SELECT * FROM vector_stock_lots
      WHERE sku=%s AND location_code=%s AND quantity_available>0
      ORDER BY {order} FOR UPDATE""",(sku,location)).fetchall()

@router.post("/lots",status_code=201)
def receive_lot(b:LotReceipt,authorization:str|None=Header(None)):
    auth("vector.inventory.write",authorization);ensure();t=b.received_at or now()
    with conn() as c:
        lot=c.execute("""INSERT INTO vector_stock_lots
          (id,lot_code,sku,description,location_code,received_at,expiry_date,quantity_received,quantity_available,unit_cost,currency,created_at)
          VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
          (str(uuid4()),b.lot_code,b.sku,b.description or b.sku,b.location_code,t,b.expiry_date,b.quantity,b.quantity,b.unit_cost,b.currency.upper(),now())).fetchone()
        c.execute("""INSERT INTO vector_inventory(id,sku,description,quantity,location_code,status,updated_at)
          VALUES(%s,%s,%s,%s,%s,'available',%s)
          ON CONFLICT(sku,location_code) DO UPDATE SET quantity=vector_inventory.quantity+EXCLUDED.quantity,updated_at=EXCLUDED.updated_at""",
          (str(uuid4()),b.sku,b.description or b.sku,int(round(b.quantity)),b.location_code,now()))
    return lot

@router.get("/lots/{sku}")
def lots(sku:str,location_code:str|None=None,authorization:str|None=Header(None)):
    auth("vector.inventory.read",authorization);ensure()
    with conn() as c:
        if location_code:return c.execute("SELECT * FROM vector_stock_lots WHERE sku=%s AND location_code=%s ORDER BY received_at",(sku,location_code)).fetchall()
        return c.execute("SELECT * FROM vector_stock_lots WHERE sku=%s ORDER BY location_code,received_at",(sku,)).fetchall()

@router.post("/issue",status_code=201)
def issue(b:IssueIn,authorization:str|None=Header(None)):
    auth("vector.inventory.write",authorization);ensure()
    with conn() as c:
        rows=_ordered_lots(c,b.sku,b.location_code,b.strategy)
        available=sum(float(x["quantity_available"]) for x in rows)
        if available+1e-9<b.quantity: raise HTTPException(409,"insufficient_lot_inventory")
        remaining=float(b.quantity); allocations=[]; total=0.0; currency="USD"
        for lot in rows:
            if remaining<=1e-9:break
            take=min(remaining,float(lot["quantity_available"]))
            c.execute("UPDATE vector_stock_lots SET quantity_available=quantity_available-%s WHERE id=%s",(take,lot["id"]))
            cost=take*float(lot["unit_cost"]); total+=cost; currency=lot["currency"]
            allocations.append({"lot_code":lot["lot_code"],"quantity":take,"unit_cost":float(lot["unit_cost"]),"cost":cost,"expiry_date":lot["expiry_date"]})
            remaining-=take
        inv=c.execute("SELECT id,quantity FROM vector_inventory WHERE sku=%s AND location_code=%s FOR UPDATE",(b.sku,b.location_code)).fetchone()
        if not inv or float(inv["quantity"])+1e-9<b.quantity: raise HTTPException(409,"aggregate_inventory_mismatch")
        c.execute("UPDATE vector_inventory SET quantity=GREATEST(0,quantity-%s),updated_at=%s WHERE id=%s",(int(round(b.quantity)),now(),inv["id"]))
        issue_no=f"GIN-{now().strftime('%Y%m%d%H%M%S')}-{str(uuid4())[:6].upper()}"
        row=c.execute("""INSERT INTO vector_lot_issues(id,issue_number,sku,location_code,strategy,quantity,total_cost,currency,allocations,reference,created_at)
          VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s) RETURNING *""",
          (str(uuid4()),issue_no,b.sku,b.location_code,b.strategy.upper(),b.quantity,total,currency,__import__("json").dumps(allocations,default=str),b.reference,now())).fetchone()
    return {"issue":row,"allocations":allocations,"average_issue_cost":round(total/b.quantity,6)}

@router.get("/valuation/{sku}")
def valuation(sku:str,authorization:str|None=Header(None)):
    auth("vector.inventory.read",authorization);ensure()
    with conn() as c:
        r=c.execute("""SELECT COALESCE(SUM(quantity_available),0) qty,
          COALESCE(SUM(quantity_available*unit_cost),0) value,
          MIN(currency) currency FROM vector_stock_lots WHERE sku=%s AND quantity_available>0""",(sku,)).fetchone()
    qty=float(r["qty"] or 0);value=float(r["value"] or 0)
    return {"sku":sku,"quantity":qty,"inventory_value":value,"avco_unit_cost":round(value/qty,6) if qty else 0,"currency":r["currency"] or "USD"}

@router.get("/abc")
def abc(authorization:str|None=Header(None)):
    auth("vector.inventory.read",authorization);ensure()
    with conn() as c:
        rows=c.execute("""WITH usage AS (
          SELECT sku,COALESCE(SUM(quantity),0)::double precision annual_usage
          FROM vector_movements WHERE movement_type='dispatch' AND created_at>=NOW()-INTERVAL '365 days' GROUP BY sku
        ), cost AS (
          SELECT sku,CASE WHEN SUM(quantity_available)>0 THEN SUM(quantity_available*unit_cost)/SUM(quantity_available) ELSE 0 END unit_cost
          FROM vector_stock_lots GROUP BY sku
        ), vals AS (
          SELECT i.sku,COALESCE(u.annual_usage,0) annual_usage,COALESCE(cost.unit_cost,0) unit_cost,
                 COALESCE(u.annual_usage,0)*COALESCE(cost.unit_cost,0) annual_value
          FROM (SELECT DISTINCT sku FROM vector_inventory) i
          LEFT JOIN usage u ON u.sku=i.sku LEFT JOIN cost ON cost.sku=i.sku
        ), ranked AS (
          SELECT *,SUM(annual_value) OVER(ORDER BY annual_value DESC) cumulative,
            NULLIF(SUM(annual_value) OVER(),0) total_value FROM vals
        )
        SELECT sku,annual_usage,unit_cost,annual_value,
          CASE WHEN total_value IS NULL THEN 'C'
               WHEN cumulative/total_value<=0.80 THEN 'A'
               WHEN cumulative/total_value<=0.95 THEN 'B' ELSE 'C' END abc_class
        FROM ranked ORDER BY annual_value DESC""").fetchall()
    return {"method":"ABC annual consumption value","items":rows,"generated_at":now()}
