from datetime import datetime,timezone
from fastapi import Header,HTTPException
from pydantic import BaseModel,Field
from uuid import uuid4
def now():return datetime.now(timezone.utc)
def init_mm_transactions(conn):
 with conn() as c:
  c.execute("""CREATE TABLE IF NOT EXISTS vector_purchase_requisitions(id UUID PRIMARY KEY,pr_no TEXT UNIQUE NOT NULL,sku TEXT NOT NULL,quantity NUMERIC NOT NULL,needed_by DATE NULL,status TEXT NOT NULL,requested_by TEXT NULL,created_at TIMESTAMPTZ NOT NULL)""")
  c.execute("""CREATE TABLE IF NOT EXISTS vector_supplier_invoices(id UUID PRIMARY KEY,invoice_no TEXT UNIQUE NOT NULL,po_id UUID NOT NULL,supplier_code TEXT NOT NULL,amount NUMERIC NOT NULL,status TEXT NOT NULL,match_status TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL)""")
  c.execute("""CREATE TABLE IF NOT EXISTS vector_scheduling_agreements(id UUID PRIMARY KEY,agreement_no TEXT UNIQUE NOT NULL,supplier_code TEXT NOT NULL,sku TEXT NOT NULL,start_date DATE NOT NULL,end_date DATE NOT NULL,quantity NUMERIC NOT NULL,status TEXT NOT NULL)""")
  c.execute("""CREATE TABLE IF NOT EXISTS vector_release_strategies(id UUID PRIMARY KEY,code TEXT UNIQUE NOT NULL,document_type TEXT NOT NULL,min_amount NUMERIC NOT NULL DEFAULT 0,required_approvals INTEGER NOT NULL DEFAULT 1,active BOOLEAN NOT NULL DEFAULT TRUE)""")
class PR(BaseModel):sku:str;quantity:float=Field(gt=0);needed_by:str|None=None
class Invoice(BaseModel):invoice_no:str;po_id:str;supplier_code:str;amount:float=Field(ge=0)
class Agreement(BaseModel):agreement_no:str;supplier_code:str;sku:str;start_date:str;end_date:str;quantity:float=Field(gt=0)
def install_mm_transaction_routes(app,conn,auth):
 @app.post('/v1/procurement/requisitions',status_code=201)
 def pr(b:PR,authorization:str|None=Header(None)):
  u=auth('vector.procurement.write',authorization);n='PR-'+now().strftime('%Y%m%d%H%M%S%f')
  with conn() as c:return c.execute("INSERT INTO vector_purchase_requisitions VALUES(%s,%s,%s,%s,%s,'open',%s,%s) RETURNING *",(str(uuid4()),n,b.sku,b.quantity,b.needed_by,str(u),now())).fetchone()
 @app.post('/v1/procurement/invoices/verify',status_code=201)
 def invoice(b:Invoice,authorization:str|None=Header(None)):
  auth('vector.procurement.write',authorization)
  with conn() as c:
   po=c.execute('SELECT * FROM vector_purchase_orders WHERE id=%s',(b.po_id,)).fetchone()
   if not po:raise HTTPException(404,'purchase_order_not_found')
   rec=c.execute("SELECT COALESCE(sum(quantity),0) q FROM vector_material_documents WHERE po_id=%s AND movement_type='101' AND status='posted'",(b.po_id,)).fetchone()['q']
   match='matched' if float(rec)>0 else 'goods_receipt_missing'
   return c.execute("INSERT INTO vector_supplier_invoices VALUES(%s,%s,%s,%s,%s,'parked',%s,%s) RETURNING *",(str(uuid4()),b.invoice_no,b.po_id,b.supplier_code,b.amount,match,now())).fetchone()
 @app.post('/v1/procurement/scheduling-agreements',status_code=201)
 def agreement(b:Agreement,authorization:str|None=Header(None)):
  auth('vector.procurement.write',authorization)
  with conn() as c:return c.execute("INSERT INTO vector_scheduling_agreements VALUES(%s,%s,%s,%s,%s,%s,%s,'active') RETURNING *",(str(uuid4()),b.agreement_no,b.supplier_code,b.sku,b.start_date,b.end_date,b.quantity)).fetchone()
