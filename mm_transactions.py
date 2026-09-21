from datetime import datetime,timezone
from fastapi import Header,HTTPException
from pydantic import BaseModel,Field
from uuid import uuid4
from release_approvals import create_approval_request
def now():return datetime.now(timezone.utc)
def init_mm_transactions(conn):
 with conn() as c:
  c.execute("""CREATE TABLE IF NOT EXISTS vector_purchase_requisitions(id UUID PRIMARY KEY,pr_no TEXT UNIQUE NOT NULL,sku TEXT NOT NULL,quantity NUMERIC NOT NULL,needed_by DATE NULL,status TEXT NOT NULL,requested_by TEXT NULL,created_at TIMESTAMPTZ NOT NULL)""")
  c.execute("""CREATE TABLE IF NOT EXISTS vector_supplier_invoices(id UUID PRIMARY KEY,invoice_no TEXT UNIQUE NOT NULL,po_id UUID NOT NULL,supplier_code TEXT NOT NULL,amount NUMERIC NOT NULL,status TEXT NOT NULL,match_status TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL)""")
  c.execute("""CREATE TABLE IF NOT EXISTS vector_scheduling_agreements(id UUID PRIMARY KEY,agreement_no TEXT UNIQUE NOT NULL,supplier_code TEXT NOT NULL,sku TEXT NOT NULL,start_date DATE NOT NULL,end_date DATE NOT NULL,quantity NUMERIC NOT NULL,status TEXT NOT NULL)""")
  c.execute("""CREATE TABLE IF NOT EXISTS vector_release_strategies(id UUID PRIMARY KEY,code TEXT UNIQUE NOT NULL,document_type TEXT NOT NULL,min_amount NUMERIC NOT NULL DEFAULT 0,required_approvals INTEGER NOT NULL DEFAULT 1,active BOOLEAN NOT NULL DEFAULT TRUE)""")
class PR(BaseModel):sku:str;quantity:float=Field(gt=0);needed_by:str|None=None
class Invoice(BaseModel):invoice_no:str;po_id:str;supplier_code:str;amount:float=Field(ge=0);quantity:float|None=Field(default=None,gt=0)
class Agreement(BaseModel):agreement_no:str;supplier_code:str;sku:str;start_date:str;end_date:str;quantity:float=Field(gt=0)
def install_mm_transaction_routes(app,conn,auth):
 @app.post('/v1/procurement/requisitions',status_code=201)
 def pr(b:PR,authorization:str|None=Header(None)):
  u=auth('vector.procurement.write',authorization);n='PR-'+now().strftime('%Y%m%d%H%M%S%f')
  with conn() as c:
   pr_id=str(uuid4());strategy=c.execute("SELECT * FROM vector_release_strategies WHERE active=TRUE AND document_type='PR' AND min_amount<=0 ORDER BY min_amount DESC LIMIT 1").fetchone()
   status='pending_approval' if strategy else 'open'
   row=c.execute("INSERT INTO vector_purchase_requisitions VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",(pr_id,n,b.sku,b.quantity,b.needed_by,status,str(u),now())).fetchone()
   approval=create_approval_request(c,'PR',pr_id,0,str(u)) if strategy else None
   return {'purchase_requisition':row,'approval_request':approval}
 @app.post('/v1/procurement/invoices/verify',status_code=201)
 def invoice(b:Invoice,authorization:str|None=Header(None)):
  auth('vector.procurement.write',authorization)
  with conn() as c:
   from uuid import UUID
   po_id=UUID(b.po_id)
   po=c.execute('SELECT * FROM vector_purchase_orders WHERE id=%s',(po_id,)).fetchone()
   if not po:raise HTTPException(404,'purchase_order_not_found')
   if po['status'] in ('pending_approval','rejected'):raise HTTPException(409,'purchase_order_not_released')
   if po['supplier_code']!=b.supplier_code:raise HTTPException(409,'supplier_po_mismatch')
   rec=float(c.execute("SELECT COALESCE(sum(CASE WHEN movement_type='101' THEN quantity WHEN movement_type='102' THEN -quantity ELSE 0 END),0) q FROM vector_material_documents WHERE po_id=%s AND status='posted'",(po_id,)).fetchone()['q'])
   po_qty=float(po['quantity']);po_value=po_qty*float(po['unit_cost']);invoice_qty=float(b.quantity) if b.quantity is not None else po_qty
   reasons=[]
   if rec<=0:reasons.append('goods_receipt_missing')
   if invoice_qty>rec:reasons.append('invoice_quantity_exceeds_received')
   if invoice_qty>po_qty:reasons.append('invoice_quantity_exceeds_po')
   expected=invoice_qty*float(po['unit_cost'])
   if abs(float(b.amount)-expected)>0.01:reasons.append('invoice_value_mismatch')
   match='matched' if not reasons else '+'.join(reasons)
   status='verified' if not reasons else 'parked'
   row=c.execute("INSERT INTO vector_supplier_invoices VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",(str(uuid4()),b.invoice_no,po_id,b.supplier_code,b.amount,status,match,now())).fetchone()
   return {**row,'po_quantity':po_qty,'received_quantity':rec,'invoice_quantity':invoice_qty,'po_value':po_value,'expected_invoice_value':expected,'match_reasons':reasons}
 @app.post('/v1/procurement/scheduling-agreements',status_code=201)
 def agreement(b:Agreement,authorization:str|None=Header(None)):
  auth('vector.procurement.write',authorization)
  with conn() as c:return c.execute("INSERT INTO vector_scheduling_agreements VALUES(%s,%s,%s,%s,%s,%s,%s,'active') RETURNING *",(str(uuid4()),b.agreement_no,b.supplier_code,b.sku,b.start_date,b.end_date,b.quantity)).fetchone()
