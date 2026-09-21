from datetime import datetime,timezone
from fastapi import Header,HTTPException
from pydantic import BaseModel,Field
from uuid import uuid4,UUID
from release_approvals import create_approval_request

def now():return datetime.now(timezone.utc)

def init_mm_transactions(conn):
 with conn() as c:
  c.execute("""CREATE TABLE IF NOT EXISTS vector_purchase_requisitions(id UUID PRIMARY KEY,pr_no TEXT UNIQUE NOT NULL,sku TEXT NOT NULL,quantity NUMERIC NOT NULL,needed_by DATE NULL,status TEXT NOT NULL,requested_by TEXT NULL,created_at TIMESTAMPTZ NOT NULL)""")
  c.execute("""CREATE TABLE IF NOT EXISTS vector_supplier_invoices(id UUID PRIMARY KEY,invoice_no TEXT UNIQUE NOT NULL,po_id UUID NOT NULL,supplier_code TEXT NOT NULL,amount NUMERIC NOT NULL,status TEXT NOT NULL,match_status TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL)""")
  c.execute("ALTER TABLE vector_supplier_invoices ADD COLUMN IF NOT EXISTS invoice_quantity NUMERIC NULL")
  c.execute("ALTER TABLE vector_supplier_invoices ADD COLUMN IF NOT EXISTS payment_status TEXT NOT NULL DEFAULT 'not_payable'")
  c.execute("""CREATE TABLE IF NOT EXISTS vector_supplier_payments(
   id UUID PRIMARY KEY,payment_no TEXT UNIQUE NOT NULL,invoice_id UUID NOT NULL,
   po_id UUID NOT NULL,supplier_code TEXT NOT NULL,amount NUMERIC NOT NULL CHECK(amount>0),
   reference TEXT NULL,status TEXT NOT NULL,created_by TEXT NULL,paid_at TIMESTAMPTZ NOT NULL)""")
  c.execute("CREATE INDEX IF NOT EXISTS idx_vector_supplier_payments_invoice ON vector_supplier_payments(invoice_id)")
  c.execute("CREATE INDEX IF NOT EXISTS idx_vector_supplier_payments_po ON vector_supplier_payments(po_id)")
  c.execute("""CREATE TABLE IF NOT EXISTS vector_scheduling_agreements(id UUID PRIMARY KEY,agreement_no TEXT UNIQUE NOT NULL,supplier_code TEXT NOT NULL,sku TEXT NOT NULL,start_date DATE NOT NULL,end_date DATE NOT NULL,quantity NUMERIC NOT NULL,status TEXT NOT NULL)""")
  c.execute("""CREATE TABLE IF NOT EXISTS vector_release_strategies(id UUID PRIMARY KEY,code TEXT UNIQUE NOT NULL,document_type TEXT NOT NULL,min_amount NUMERIC NOT NULL DEFAULT 0,required_approvals INTEGER NOT NULL DEFAULT 1,active BOOLEAN NOT NULL DEFAULT TRUE)""")

class PR(BaseModel):sku:str;quantity:float=Field(gt=0);needed_by:str|None=None
class Invoice(BaseModel):invoice_no:str;po_id:str;supplier_code:str;amount:float=Field(ge=0);quantity:float|None=Field(default=None,gt=0)
class Agreement(BaseModel):agreement_no:str;supplier_code:str;sku:str;start_date:str;end_date:str;quantity:float=Field(gt=0)
class PaymentIn(BaseModel):amount:float=Field(gt=0);reference:str|None=None

def _po_totals(c,po_id):
 po=c.execute('SELECT * FROM vector_purchase_orders WHERE id=%s FOR UPDATE',(po_id,)).fetchone()
 if not po:raise HTTPException(404,'purchase_order_not_found')
 received=float(c.execute("""SELECT COALESCE(sum(CASE WHEN movement_type='101' THEN quantity WHEN movement_type='102' THEN -quantity ELSE 0 END),0) q
  FROM vector_material_documents WHERE po_id=%s AND status='posted'""",(po_id,)).fetchone()['q'])
 inv=c.execute("""SELECT COALESCE(sum(invoice_quantity),0) qty,COALESCE(sum(amount),0) value
  FROM vector_supplier_invoices WHERE po_id=%s AND status='verified'""",(po_id,)).fetchone()
 return po,received,float(inv['qty']),float(inv['value'])

def _maybe_complete(c,po_id):
 po,received,invoiced_qty,invoiced_value=_po_totals(c,po_id)
 po_qty=float(po['quantity']);po_value=po_qty*float(po['unit_cost'])
 complete=received>=po_qty and invoiced_qty>=po_qty and invoiced_value+0.01>=po_value
 if complete:c.execute("UPDATE vector_purchase_orders SET status='complete' WHERE id=%s",(po_id,))
 return complete,received,invoiced_qty,invoiced_value,po_qty,po_value

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
   po_id=UUID(b.po_id);po,rec,prior_qty,prior_value=_po_totals(c,po_id)
   if po['status'] in ('pending_approval','rejected','cancelled','complete'):raise HTTPException(409,'purchase_order_not_invoiceable')
   if po['supplier_code']!=b.supplier_code:raise HTTPException(409,'supplier_po_mismatch')
   po_qty=float(po['quantity']);unit_cost=float(po['unit_cost']);po_value=po_qty*unit_cost
   invoice_qty=float(b.quantity) if b.quantity is not None else max(0,rec-prior_qty)
   if invoice_qty<=0:raise HTTPException(409,'no_uninvoiced_received_quantity')
   if prior_qty+invoice_qty>po_qty+1e-9:raise HTTPException(409,'cumulative_invoice_quantity_exceeds_po')
   if prior_value+float(b.amount)>po_value+0.01:raise HTTPException(409,'cumulative_invoice_value_exceeds_po')
   reasons=[]
   if rec<=0:reasons.append('goods_receipt_missing')
   if prior_qty+invoice_qty>rec+1e-9:reasons.append('invoice_quantity_exceeds_received')
   expected=invoice_qty*unit_cost
   if abs(float(b.amount)-expected)>0.01:reasons.append('invoice_value_mismatch')
   match='matched' if not reasons else '+'.join(reasons);status='verified' if not reasons else 'parked'
   row=c.execute("""INSERT INTO vector_supplier_invoices
    (id,invoice_no,po_id,supplier_code,amount,status,match_status,created_at,invoice_quantity)
    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
    (str(uuid4()),b.invoice_no,po_id,b.supplier_code,b.amount,status,match,now(),invoice_qty)).fetchone()
   if status=='verified':
    row=c.execute("UPDATE vector_supplier_invoices SET payment_status='awaiting_release' WHERE id=%s RETURNING *",(row['id'],)).fetchone()
   complete=False
   if status=='verified':complete,rec,total_qty,total_value,po_qty,po_value=_maybe_complete(c,po_id)
   else:total_qty,total_value=prior_qty,prior_value
   return {**row,'po_quantity':po_qty,'received_quantity':rec,'verified_invoice_quantity':total_qty,
    'verified_invoice_value':total_value,'po_value':po_value,'expected_invoice_value':expected,
    'match_reasons':reasons,'po_complete':complete}

 @app.post('/v1/procurement/invoices/{invoice_id}/make-payable')
 def make_payable(invoice_id:str,authorization:str|None=Header(None)):
  auth('vector.payables.write',authorization)
  with conn() as c:
   iid=UUID(invoice_id);inv=c.execute('SELECT * FROM vector_supplier_invoices WHERE id=%s FOR UPDATE',(iid,)).fetchone()
   if not inv:raise HTTPException(404,'supplier_invoice_not_found')
   if inv['status']!='verified' or inv['match_status']!='matched':raise HTTPException(409,'invoice_not_verified_and_matched')
   if inv['payment_status'] in ('paid','partially_paid','payable'):return inv
   return c.execute("UPDATE vector_supplier_invoices SET payment_status='payable' WHERE id=%s RETURNING *",(iid,)).fetchone()

 @app.post('/v1/procurement/invoices/{invoice_id}/payments',status_code=201)
 def pay_invoice(invoice_id:str,b:PaymentIn,authorization:str|None=Header(None)):
  principal=auth('vector.payables.write',authorization)
  with conn() as c:
   iid=UUID(invoice_id);inv=c.execute('SELECT * FROM vector_supplier_invoices WHERE id=%s FOR UPDATE',(iid,)).fetchone()
   if not inv:raise HTTPException(404,'supplier_invoice_not_found')
   if inv['status']!='verified' or inv['match_status']!='matched':raise HTTPException(409,'invoice_not_verified_and_matched')
   if inv['payment_status'] not in ('payable','partially_paid'):raise HTTPException(409,'invoice_not_payable')
   paid=float(c.execute("SELECT COALESCE(sum(amount),0) total FROM vector_supplier_payments WHERE invoice_id=%s AND status='posted'",(iid,)).fetchone()['total'])
   invoice_amount=float(inv['amount']);remaining=max(0,invoice_amount-paid)
   if b.amount>remaining+0.01:raise HTTPException(409,{'error':'payment_exceeds_invoice_balance','remaining_balance':remaining})
   pno='PAY-'+now().strftime('%Y%m%d%H%M%S%f')
   payment=c.execute("""INSERT INTO vector_supplier_payments
    VALUES(%s,%s,%s,%s,%s,%s,%s,'posted',%s,%s) RETURNING *""",
    (str(uuid4()),pno,iid,inv['po_id'],inv['supplier_code'],b.amount,b.reference,str(principal),now())).fetchone()
   total_paid=paid+b.amount;balance=max(0,invoice_amount-total_paid)
   pstatus='paid' if balance<=0.01 else 'partially_paid'
   invoice=c.execute("UPDATE vector_supplier_invoices SET payment_status=%s WHERE id=%s RETURNING *",(pstatus,iid)).fetchone()
   if pstatus=='paid':
    unpaid=c.execute("""SELECT count(*) n FROM vector_supplier_invoices
     WHERE po_id=%s AND status='verified' AND payment_status<>'paid'""",(inv['po_id'],)).fetchone()['n']
    po=c.execute('SELECT status FROM vector_purchase_orders WHERE id=%s',(inv['po_id'],)).fetchone()
    if unpaid==0 and po and po['status']=='complete':c.execute("UPDATE vector_purchase_orders SET status='settled' WHERE id=%s",(inv['po_id'],))
   return {'payment':payment,'invoice':invoice,'paid_total':total_paid,'remaining_balance':balance}

 @app.get('/v1/procurement/invoices/{invoice_id}/payment-status')
 def payment_status(invoice_id:str,authorization:str|None=Header(None)):
  auth('vector.payables.read',authorization)
  with conn() as c:
   iid=UUID(invoice_id);inv=c.execute('SELECT * FROM vector_supplier_invoices WHERE id=%s',(iid,)).fetchone()
   if not inv:raise HTTPException(404,'supplier_invoice_not_found')
   paid=float(c.execute("SELECT COALESCE(sum(amount),0) total FROM vector_supplier_payments WHERE invoice_id=%s AND status='posted'",(iid,)).fetchone()['total'])
   payments=c.execute("SELECT * FROM vector_supplier_payments WHERE invoice_id=%s ORDER BY paid_at",(iid,)).fetchall()
   return {'invoice':inv,'invoice_amount':float(inv['amount']),'paid_total':paid,'remaining_balance':max(0,float(inv['amount'])-paid),'payments':payments}

 @app.get('/v1/procurement/purchase-orders/{po_id}/lifecycle')
 def lifecycle(po_id:str,authorization:str|None=Header(None)):
  auth('vector.procurement.read',authorization)
  with conn() as c:
   po,received,invoiced_qty,invoiced_value=_po_totals(c,UUID(po_id))
   po_qty=float(po['quantity']);po_value=po_qty*float(po['unit_cost'])
   paid=float(c.execute("SELECT COALESCE(sum(p.amount),0) total FROM vector_supplier_payments p WHERE p.po_id=%s AND p.status='posted'",(UUID(po_id),)).fetchone()['total'])
   return {'purchase_order':po,'ordered_quantity':po_qty,'received_quantity':received,
    'remaining_to_receive':max(0,po_qty-received),'verified_invoice_quantity':invoiced_qty,
    'remaining_to_invoice':max(0,po_qty-invoiced_qty),'po_value':po_value,
    'verified_invoice_value':invoiced_value,'remaining_invoice_value':max(0,po_value-invoiced_value),
    'paid_value':paid,'remaining_to_pay':max(0,invoiced_value-paid),'settlement_status':('settled' if po['status']=='settled' else ('paid' if invoiced_value>0 and paid+0.01>=invoiced_value else ('partially_paid' if paid>0 else 'unpaid')))}

 @app.post('/v1/procurement/purchase-orders/{po_id}/cancel')
 def cancel_po(po_id:str,authorization:str|None=Header(None)):
  auth('vector.procurement.write',authorization)
  with conn() as c:
   pid=UUID(po_id);po,received,invoiced_qty,invoiced_value=_po_totals(c,pid)
   if po['status'] in ('complete','cancelled'):raise HTTPException(409,'purchase_order_not_cancellable')
   if received>0 or invoiced_qty>0 or invoiced_value>0:raise HTTPException(409,'purchase_order_has_posted_activity')
   row=c.execute("UPDATE vector_purchase_orders SET status='cancelled' WHERE id=%s RETURNING *",(pid,)).fetchone()
   c.execute("UPDATE vector_approval_requests SET status='rejected',resolved_at=%s WHERE document_type='PO' AND document_id=%s AND status='pending'",(now(),pid))
   return row

 @app.post('/v1/procurement/purchase-orders/{po_id}/close')
 def close_po(po_id:str,authorization:str|None=Header(None)):
  auth('vector.procurement.write',authorization)
  with conn() as c:
   pid=UUID(po_id);complete,received,invoiced_qty,invoiced_value,po_qty,po_value=_maybe_complete(c,pid)
   if not complete:raise HTTPException(409,{'error':'purchase_order_not_fully_received_and_invoiced','received':received,'ordered':po_qty,'invoiced_quantity':invoiced_qty,'invoiced_value':invoiced_value,'po_value':po_value})
   return c.execute('SELECT * FROM vector_purchase_orders WHERE id=%s',(pid,)).fetchone()

 @app.post('/v1/procurement/scheduling-agreements',status_code=201)
 def agreement(b:Agreement,authorization:str|None=Header(None)):
  auth('vector.procurement.write',authorization)
  with conn() as c:return c.execute("INSERT INTO vector_scheduling_agreements VALUES(%s,%s,%s,%s,%s,%s,%s,'active') RETURNING *",(str(uuid4()),b.agreement_no,b.supplier_code,b.sku,b.start_date,b.end_date,b.quantity)).fetchone()
