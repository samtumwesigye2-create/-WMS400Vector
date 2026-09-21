from datetime import datetime,timezone,date,timedelta
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
  c.execute("ALTER TABLE vector_supplier_invoices ADD COLUMN IF NOT EXISTS invoice_date DATE NULL")
  c.execute("ALTER TABLE vector_supplier_invoices ADD COLUMN IF NOT EXISTS due_date DATE NULL")
  c.execute("ALTER TABLE vector_supplier_invoices ADD COLUMN IF NOT EXISTS terms_days INTEGER NOT NULL DEFAULT 30")
  c.execute("""CREATE TABLE IF NOT EXISTS vector_supplier_payment_terms(
   supplier_code TEXT PRIMARY KEY,terms_days INTEGER NOT NULL CHECK(terms_days>=0),
   description TEXT NOT NULL DEFAULT '',updated_at TIMESTAMPTZ NOT NULL)""")
  c.execute("""CREATE TABLE IF NOT EXISTS vector_payment_schedules(
   id UUID PRIMARY KEY,invoice_id UUID NOT NULL UNIQUE,supplier_code TEXT NOT NULL,
   scheduled_date DATE NOT NULL,scheduled_amount NUMERIC NOT NULL CHECK(scheduled_amount>0),
   priority INTEGER NOT NULL DEFAULT 5 CHECK(priority BETWEEN 1 AND 10),
   status TEXT NOT NULL,created_by TEXT NULL,created_at TIMESTAMPTZ NOT NULL,updated_at TIMESTAMPTZ NOT NULL)""")
  c.execute("""CREATE TABLE IF NOT EXISTS vector_payment_runs(
   id UUID PRIMARY KEY,run_no TEXT UNIQUE NOT NULL,run_date DATE NOT NULL,total_amount NUMERIC NOT NULL DEFAULT 0,
   item_count INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL,created_by TEXT NULL,created_at TIMESTAMPTZ NOT NULL,
   approved_at TIMESTAMPTZ NULL,ready_at TIMESTAMPTZ NULL)""")
  c.execute("""CREATE TABLE IF NOT EXISTS vector_payment_run_items(
   id UUID PRIMARY KEY,run_id UUID NOT NULL,invoice_id UUID NOT NULL,supplier_code TEXT NOT NULL,
   amount NUMERIC NOT NULL CHECK(amount>0),scheduled_date DATE NOT NULL,status TEXT NOT NULL,
   created_at TIMESTAMPTZ NOT NULL,UNIQUE(run_id,invoice_id))""")
  c.execute("""CREATE TABLE IF NOT EXISTS vector_bank_execution_batches(
   id UUID PRIMARY KEY,execution_ref TEXT UNIQUE NOT NULL,run_id UUID NOT NULL UNIQUE,
   total_amount NUMERIC NOT NULL,item_count INTEGER NOT NULL,status TEXT NOT NULL,
   created_by TEXT NULL,created_at TIMESTAMPTZ NOT NULL,acknowledged_at TIMESTAMPTZ NULL,
   bank_reference TEXT NULL,bank_message TEXT NULL)""")
  c.execute("""CREATE TABLE IF NOT EXISTS vector_bank_execution_items(
   id UUID PRIMARY KEY,batch_id UUID NOT NULL,run_item_id UUID NOT NULL UNIQUE,invoice_id UUID NOT NULL,
   supplier_code TEXT NOT NULL,amount NUMERIC NOT NULL,status TEXT NOT NULL,
   bank_reference TEXT NULL,bank_message TEXT NULL,updated_at TIMESTAMPTZ NOT NULL)""")
  c.execute("""CREATE TABLE IF NOT EXISTS vector_supplier_payments(
   id UUID PRIMARY KEY,payment_no TEXT UNIQUE NOT NULL,invoice_id UUID NOT NULL,
   po_id UUID NOT NULL,supplier_code TEXT NOT NULL,amount NUMERIC NOT NULL CHECK(amount>0),
   reference TEXT NULL,status TEXT NOT NULL,created_by TEXT NULL,paid_at TIMESTAMPTZ NOT NULL)""")
  c.execute("CREATE INDEX IF NOT EXISTS idx_vector_supplier_payments_invoice ON vector_supplier_payments(invoice_id)")
  c.execute("CREATE INDEX IF NOT EXISTS idx_vector_supplier_payments_po ON vector_supplier_payments(po_id)")
  c.execute("""CREATE TABLE IF NOT EXISTS vector_supplier_adjustments(
   id UUID PRIMARY KEY,adjustment_no TEXT UNIQUE NOT NULL,invoice_id UUID NOT NULL,po_id UUID NOT NULL,
   supplier_code TEXT NOT NULL,adjustment_type TEXT NOT NULL,amount NUMERIC NOT NULL CHECK(amount>0),
   reason TEXT NOT NULL DEFAULT '',status TEXT NOT NULL,created_by TEXT NULL,created_at TIMESTAMPTZ NOT NULL)""")
  c.execute("""CREATE TABLE IF NOT EXISTS vector_payment_reversals(
   id UUID PRIMARY KEY,reversal_no TEXT UNIQUE NOT NULL,payment_id UUID NOT NULL UNIQUE,
   amount NUMERIC NOT NULL CHECK(amount>0),reason TEXT NOT NULL,status TEXT NOT NULL,
   created_by TEXT NULL,reversed_at TIMESTAMPTZ NOT NULL)""")
  c.execute("""CREATE TABLE IF NOT EXISTS vector_scheduling_agreements(id UUID PRIMARY KEY,agreement_no TEXT UNIQUE NOT NULL,supplier_code TEXT NOT NULL,sku TEXT NOT NULL,start_date DATE NOT NULL,end_date DATE NOT NULL,quantity NUMERIC NOT NULL,status TEXT NOT NULL)""")
  c.execute("""CREATE TABLE IF NOT EXISTS vector_release_strategies(id UUID PRIMARY KEY,code TEXT UNIQUE NOT NULL,document_type TEXT NOT NULL,min_amount NUMERIC NOT NULL DEFAULT 0,required_approvals INTEGER NOT NULL DEFAULT 1,active BOOLEAN NOT NULL DEFAULT TRUE)""")

class PR(BaseModel):sku:str;quantity:float=Field(gt=0);needed_by:str|None=None
class Invoice(BaseModel):invoice_no:str;po_id:str;supplier_code:str;amount:float=Field(ge=0);quantity:float|None=Field(default=None,gt=0);invoice_date:date|None=None;due_date:date|None=None
class Agreement(BaseModel):agreement_no:str;supplier_code:str;sku:str;start_date:str;end_date:str;quantity:float=Field(gt=0)
class PaymentIn(BaseModel):amount:float=Field(gt=0);reference:str|None=None
class AdjustmentIn(BaseModel):adjustment_type:str;amount:float=Field(gt=0);reason:str=''
class ReversalIn(BaseModel):reason:str
class StatementIn(BaseModel):supplier_code:str;statement_balance:float
class PaymentTermsIn(BaseModel):terms_days:int=Field(ge=0,le=3650);description:str=''
class PaymentScheduleIn(BaseModel):scheduled_date:date;scheduled_amount:float=Field(gt=0);priority:int=Field(default=5,ge=1,le=10)
class PaymentRunIn(BaseModel):run_date:date|None=None
class BankAckIn(BaseModel):status:str;bank_reference:str|None=None;message:str=''
class BankItemAckIn(BaseModel):run_item_id:str;status:str;bank_reference:str|None=None;message:str=''

def _po_totals(c,po_id):
 po=c.execute('SELECT * FROM vector_purchase_orders WHERE id=%s FOR UPDATE',(po_id,)).fetchone()
 if not po:raise HTTPException(404,'purchase_order_not_found')
 received=float(c.execute("""SELECT COALESCE(sum(CASE WHEN movement_type='101' THEN quantity WHEN movement_type='102' THEN -quantity ELSE 0 END),0) q
  FROM vector_material_documents WHERE po_id=%s AND status='posted'""",(po_id,)).fetchone()['q'])
 inv=c.execute("""SELECT COALESCE(sum(invoice_quantity),0) qty,COALESCE(sum(amount),0) value
  FROM vector_supplier_invoices WHERE po_id=%s AND status='verified'""",(po_id,)).fetchone()
 return po,received,float(inv['qty']),float(inv['value'])

def _invoice_net_amount(c,invoice_id,base_amount):
 a=c.execute("""SELECT COALESCE(sum(CASE WHEN adjustment_type='credit_memo' AND status='posted' THEN amount
 WHEN adjustment_type='debit_adjustment' AND status='posted' THEN -amount ELSE 0 END),0) net_credit
 FROM vector_supplier_adjustments WHERE invoice_id=%s""",(invoice_id,)).fetchone()['net_credit']
 return max(0,float(base_amount)-float(a))

def _payment_net(c,invoice_id):
 p=float(c.execute("""SELECT COALESCE(sum(amount),0) total FROM vector_supplier_payments
 WHERE invoice_id=%s AND status='posted'""",(invoice_id,)).fetchone()['total'])
 r=float(c.execute("""SELECT COALESCE(sum(pr.amount),0) total FROM vector_payment_reversals pr
 JOIN vector_supplier_payments p ON p.id=pr.payment_id
 WHERE p.invoice_id=%s AND pr.status='posted'""",(invoice_id,)).fetchone()['total'])
 return max(0,p-r)

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
   inv_date=b.invoice_date or date.today()
   term=c.execute('SELECT terms_days FROM vector_supplier_payment_terms WHERE supplier_code=%s',(b.supplier_code,)).fetchone()
   terms_days=int(term['terms_days']) if term else 30
   due=b.due_date or (inv_date+timedelta(days=terms_days))
   if due<inv_date:raise HTTPException(400,'due_date_before_invoice_date')
   row=c.execute("""INSERT INTO vector_supplier_invoices
    (id,invoice_no,po_id,supplier_code,amount,status,match_status,created_at,invoice_quantity,payment_status,invoice_date,due_date,terms_days)
    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'not_payable',%s,%s,%s) RETURNING *""",
    (str(uuid4()),b.invoice_no,po_id,b.supplier_code,b.amount,status,match,now(),invoice_qty,inv_date,due,terms_days)).fetchone()
   if status=='verified':
    row=c.execute("UPDATE vector_supplier_invoices SET payment_status='awaiting_release' WHERE id=%s RETURNING *",(row['id'],)).fetchone()
   complete=False
   if status=='verified':complete,rec,total_qty,total_value,po_qty,po_value=_maybe_complete(c,po_id)
   else:total_qty,total_value=prior_qty,prior_value
   return {**row,'po_quantity':po_qty,'received_quantity':rec,'verified_invoice_quantity':total_qty,
    'verified_invoice_value':total_value,'po_value':po_value,'expected_invoice_value':expected,
    'match_reasons':reasons,'po_complete':complete}

 @app.put('/v1/procurement/suppliers/{supplier_code}/payment-terms')
 def set_payment_terms(supplier_code:str,b:PaymentTermsIn,authorization:str|None=Header(None)):
  auth('vector.payables.write',authorization)
  with conn() as c:
   if not c.execute('SELECT 1 FROM vector_suppliers WHERE code=%s',(supplier_code,)).fetchone():raise HTTPException(404,'supplier_not_found')
   return c.execute("""INSERT INTO vector_supplier_payment_terms VALUES(%s,%s,%s,%s)
    ON CONFLICT(supplier_code) DO UPDATE SET terms_days=EXCLUDED.terms_days,description=EXCLUDED.description,updated_at=EXCLUDED.updated_at
    RETURNING *""",(supplier_code,b.terms_days,b.description,now())).fetchone()

 @app.get('/v1/procurement/suppliers/{supplier_code}/payment-terms')
 def get_payment_terms(supplier_code:str,authorization:str|None=Header(None)):
  auth('vector.payables.read',authorization)
  with conn() as c:
   row=c.execute('SELECT * FROM vector_supplier_payment_terms WHERE supplier_code=%s',(supplier_code,)).fetchone()
   return row or {'supplier_code':supplier_code,'terms_days':30,'description':'default NET 30'}

 @app.get('/v1/procurement/ap-aging')
 def ap_aging(supplier_code:str|None=None,as_of:date|None=None,authorization:str|None=Header(None)):
  auth('vector.payables.read',authorization);cutoff=as_of or date.today()
  with conn() as c:
   if supplier_code:
    rows=c.execute("""SELECT * FROM vector_supplier_invoices WHERE status='verified' AND supplier_code=%s AND payment_status<>'paid' ORDER BY due_date""",(supplier_code,)).fetchall()
   else:
    rows=c.execute("""SELECT * FROM vector_supplier_invoices WHERE status='verified' AND payment_status<>'paid' ORDER BY supplier_code,due_date""").fetchall()
   buckets={'current':0.0,'1_30':0.0,'31_60':0.0,'61_90':0.0,'91_plus':0.0};items=[];total=0.0
   for inv in rows:
    net=_invoice_net_amount(c,inv['id'],inv['amount']);paid=_payment_net(c,inv['id']);open_balance=max(0,net-paid)
    if open_balance<=0.01:continue
    due=inv['due_date'] or (inv['invoice_date'] or inv['created_at'].date())+timedelta(days=int(inv['terms_days'] or 30))
    days=(cutoff-due).days
    bucket='current' if days<=0 else ('1_30' if days<=30 else ('31_60' if days<=60 else ('61_90' if days<=90 else '91_plus')))
    buckets[bucket]+=open_balance;total+=open_balance
    items.append({'invoice_id':inv['id'],'invoice_no':inv['invoice_no'],'supplier_code':inv['supplier_code'],
     'invoice_date':inv['invoice_date'],'due_date':due,'days_overdue':max(0,days),'bucket':bucket,'open_balance':round(open_balance,2)})
   return {'as_of':cutoff,'supplier_code':supplier_code,'total_open_ap':round(total,2),
    'buckets':{k:round(v,2) for k,v in buckets.items()},'invoice_count':len(items),'items':items}

 @app.get('/v1/procurement/ap-overdue')
 def ap_overdue(supplier_code:str|None=None,authorization:str|None=Header(None)):
  auth('vector.payables.read',authorization);today=date.today()
  with conn() as c:
   q="""SELECT * FROM vector_supplier_invoices WHERE status='verified' AND payment_status<>'paid' AND due_date<CURRENT_DATE"""
   params=()
   if supplier_code:q+=" AND supplier_code=%s";params=(supplier_code,)
   rows=c.execute(q+" ORDER BY due_date",params).fetchall();out=[]
   for inv in rows:
    net=_invoice_net_amount(c,inv['id'],inv['amount']);paid=_payment_net(c,inv['id']);balance=max(0,net-paid)
    if balance>0.01:out.append({'invoice_id':inv['id'],'invoice_no':inv['invoice_no'],'supplier_code':inv['supplier_code'],
     'due_date':inv['due_date'],'days_overdue':(today-inv['due_date']).days,'open_balance':round(balance,2)})
   return {'as_of':today,'overdue_count':len(out),'overdue_total':round(sum(x['open_balance'] for x in out),2),'items':out}

 @app.get('/v1/procurement/cash-requirements')
 def cash_requirements(days:int=30,group_by:str='day',authorization:str|None=Header(None)):
  auth('vector.payables.read',authorization)
  if days<1 or days>3650:raise HTTPException(400,'days_out_of_range')
  if group_by not in ('day','week'):raise HTTPException(400,'group_by_must_be_day_or_week')
  start=date.today();end=start+timedelta(days=days)
  with conn() as c:
   rows=c.execute("""SELECT * FROM vector_supplier_invoices
    WHERE status='verified' AND payment_status<>'paid' AND due_date IS NOT NULL AND due_date<=%s
    ORDER BY due_date,supplier_code""",(end,)).fetchall()
   periods={};items=[];total=0.0;overdue=0.0
   for inv in rows:
    net=_invoice_net_amount(c,inv['id'],inv['amount']);paid=_payment_net(c,inv['id']);balance=max(0,net-paid)
    if balance<=0.01:continue
    due=inv['due_date'];key=due.isoformat() if group_by=='day' else (due-timedelta(days=due.weekday())).isoformat()
    periods[key]=periods.get(key,0.0)+balance;total+=balance
    if due<start:overdue+=balance
    items.append({'invoice_id':inv['id'],'invoice_no':inv['invoice_no'],'supplier_code':inv['supplier_code'],
     'due_date':due,'open_balance':round(balance,2),'overdue':due<start})
   return {'as_of':start,'horizon_end':end,'group_by':group_by,'total_cash_required':round(total,2),
    'overdue_cash_required':round(overdue,2),'periods':{k:round(v,2) for k,v in sorted(periods.items())},'items':items}

 @app.put('/v1/procurement/invoices/{invoice_id}/payment-schedule')
 def schedule_payment(invoice_id:str,b:PaymentScheduleIn,authorization:str|None=Header(None)):
  principal=auth('vector.payables.write',authorization)
  with conn() as c:
   iid=UUID(invoice_id);inv=c.execute('SELECT * FROM vector_supplier_invoices WHERE id=%s FOR UPDATE',(iid,)).fetchone()
   if not inv:raise HTTPException(404,'supplier_invoice_not_found')
   if inv['status']!='verified' or inv['match_status']!='matched':raise HTTPException(409,'invoice_not_verified_and_matched')
   net=_invoice_net_amount(c,iid,inv['amount']);paid=_payment_net(c,iid);balance=max(0,net-paid)
   if balance<=0.01:raise HTTPException(409,'invoice_has_no_open_balance')
   if b.scheduled_amount>balance+0.01:raise HTTPException(409,{'error':'scheduled_amount_exceeds_open_balance','open_balance':balance})
   if b.scheduled_date<date.today():raise HTTPException(400,'scheduled_date_in_past')
   row=c.execute("""INSERT INTO vector_payment_schedules
    VALUES(%s,%s,%s,%s,%s,%s,'scheduled',%s,%s,%s)
    ON CONFLICT(invoice_id) DO UPDATE SET scheduled_date=EXCLUDED.scheduled_date,
    scheduled_amount=EXCLUDED.scheduled_amount,priority=EXCLUDED.priority,status='scheduled',
    updated_at=EXCLUDED.updated_at RETURNING *""",
    (str(uuid4()),iid,inv['supplier_code'],b.scheduled_date,b.scheduled_amount,b.priority,str(principal),now(),now())).fetchone()
   return {'schedule':row,'open_balance':round(balance,2),'note':'schedule only; no funds are transmitted'}

 @app.get('/v1/procurement/payment-schedule')
 def payment_schedule(start:date|None=None,end:date|None=None,authorization:str|None=Header(None)):
  auth('vector.payables.read',authorization);s=start or date.today();e=end or (s+timedelta(days=30))
  if e<s:raise HTTPException(400,'end_before_start')
  with conn() as c:
   rows=c.execute("""SELECT ps.*,si.invoice_no,si.due_date,si.payment_status FROM vector_payment_schedules ps
    JOIN vector_supplier_invoices si ON si.id=ps.invoice_id
    WHERE ps.status='scheduled' AND ps.scheduled_date BETWEEN %s AND %s
    ORDER BY ps.scheduled_date,ps.priority ASC,si.due_date""",(s,e)).fetchall()
   return {'start':s,'end':e,'scheduled_total':round(sum(float(r['scheduled_amount']) for r in rows),2),'items':rows}

 @app.post('/v1/procurement/payment-schedule/auto-prioritize')
 def auto_prioritize(days:int=30,budget:float|None=None,authorization:str|None=Header(None)):
  auth('vector.payables.write',authorization)
  if days<1 or days>3650:raise HTTPException(400,'days_out_of_range')
  today=date.today();end=today+timedelta(days=days)
  with conn() as c:
   rows=c.execute("""SELECT * FROM vector_supplier_invoices
    WHERE status='verified' AND match_status='matched' AND payment_status IN ('payable','partially_paid','awaiting_release')
    AND due_date IS NOT NULL AND due_date<=%s ORDER BY due_date,supplier_code""",(end,)).fetchall()
   remaining=float(budget) if budget is not None else None;plan=[];scheduled_total=0.0
   for inv in rows:
    net=_invoice_net_amount(c,inv['id'],inv['amount']);paid=_payment_net(c,inv['id']);balance=max(0,net-paid)
    if balance<=0.01:continue
    amount=balance if remaining is None else min(balance,max(0,remaining))
    if amount<=0:break
    days_overdue=max(0,(today-inv['due_date']).days)
    priority=1 if days_overdue>0 else (2 if inv['due_date']<=today+timedelta(days=7) else 5)
    plan.append({'invoice_id':inv['id'],'invoice_no':inv['invoice_no'],'supplier_code':inv['supplier_code'],
     'due_date':inv['due_date'],'open_balance':round(balance,2),'proposed_amount':round(amount,2),'priority':priority})
    scheduled_total+=amount
    if remaining is not None:remaining-=amount
   return {'as_of':today,'horizon_end':end,'budget':budget,'proposed_total':round(scheduled_total,2),
    'remaining_budget':None if remaining is None else round(max(0,remaining),2),'plan':plan,
    'note':'advisory prioritization only; no payment is created or transmitted'}

 @app.post('/v1/procurement/payment-runs',status_code=201)
 def create_payment_run(b:PaymentRunIn,authorization:str|None=Header(None)):
  principal=auth('vector.payables.write',authorization);rdate=b.run_date or date.today()
  with conn() as c:
   schedules=c.execute("""SELECT ps.*,si.payment_status,si.status invoice_status,si.match_status
    FROM vector_payment_schedules ps JOIN vector_supplier_invoices si ON si.id=ps.invoice_id
    WHERE ps.status='scheduled' AND ps.scheduled_date<=%s
    ORDER BY ps.scheduled_date,ps.priority,si.due_date""",(rdate,)).fetchall()
   if not schedules:raise HTTPException(409,'no_eligible_scheduled_payments')
   run_id=str(uuid4());run_no='RUN-'+now().strftime('%Y%m%d%H%M%S%f');total=0.0;count=0
   c.execute("""INSERT INTO vector_payment_runs(id,run_no,run_date,total_amount,item_count,status,created_by,created_at)
    VALUES(%s,%s,%s,0,0,'draft',%s,%s)""",(run_id,run_no,rdate,str(principal),now()))
   for s in schedules:
    if s['invoice_status']!='verified' or s['match_status']!='matched' or s['payment_status'] not in ('payable','partially_paid'):continue
    inv=c.execute('SELECT * FROM vector_supplier_invoices WHERE id=%s FOR UPDATE',(s['invoice_id'],)).fetchone()
    net=_invoice_net_amount(c,inv['id'],inv['amount']);paid=_payment_net(c,inv['id']);balance=max(0,net-paid)
    amount=min(float(s['scheduled_amount']),balance)
    if amount<=0.01:continue
    c.execute("""INSERT INTO vector_payment_run_items VALUES(%s,%s,%s,%s,%s,%s,'draft',%s)""",
     (str(uuid4()),run_id,inv['id'],inv['supplier_code'],amount,s['scheduled_date'],now()))
    total+=amount;count+=1
   if count==0:
    c.execute('DELETE FROM vector_payment_runs WHERE id=%s',(run_id,));raise HTTPException(409,'no_payable_items_for_run')
   c.execute("UPDATE vector_payment_runs SET total_amount=%s,item_count=%s WHERE id=%s",(total,count,run_id))
   approval=create_approval_request(c,'PAYMENT_RUN',run_id,total,str(principal))
   status='pending_approval' if approval else 'approved'
   approved_at=None if approval else now()
   run=c.execute("UPDATE vector_payment_runs SET status=%s,approved_at=%s WHERE id=%s RETURNING *",(status,approved_at,run_id)).fetchone()
   c.execute("UPDATE vector_payment_run_items SET status=%s WHERE run_id=%s",('pending_approval' if approval else 'approved',run_id))
   return {'payment_run':run,'approval_request':approval,'note':'batch created only; no funds transmitted'}

 @app.get('/v1/procurement/payment-runs')
 def list_payment_runs(status:str|None=None,authorization:str|None=Header(None)):
  auth('vector.payables.read',authorization)
  with conn() as c:
   if status:return c.execute('SELECT * FROM vector_payment_runs WHERE status=%s ORDER BY created_at DESC',(status,)).fetchall()
   return c.execute('SELECT * FROM vector_payment_runs ORDER BY created_at DESC LIMIT 200').fetchall()

 @app.get('/v1/procurement/payment-runs/{run_id}')
 def get_payment_run(run_id:str,authorization:str|None=Header(None)):
  auth('vector.payables.read',authorization)
  with conn() as c:
   rid=UUID(run_id);run=c.execute('SELECT * FROM vector_payment_runs WHERE id=%s',(rid,)).fetchone()
   if not run:raise HTTPException(404,'payment_run_not_found')
   items=c.execute("""SELECT pri.*,si.invoice_no,si.due_date,si.payment_status FROM vector_payment_run_items pri
    JOIN vector_supplier_invoices si ON si.id=pri.invoice_id WHERE pri.run_id=%s ORDER BY pri.scheduled_date,si.due_date""",(rid,)).fetchall()
   return {'payment_run':run,'items':items}

 @app.post('/v1/procurement/payment-runs/{run_id}/ready')
 def mark_payment_run_ready(run_id:str,authorization:str|None=Header(None)):
  auth('vector.payables.write',authorization)
  with conn() as c:
   rid=UUID(run_id);run=c.execute('SELECT * FROM vector_payment_runs WHERE id=%s FOR UPDATE',(rid,)).fetchone()
   if not run:raise HTTPException(404,'payment_run_not_found')
   if run['status']!='approved':raise HTTPException(409,'payment_run_not_approved')
   rows=c.execute('SELECT * FROM vector_payment_run_items WHERE run_id=%s FOR UPDATE',(rid,)).fetchall()
   if not rows:raise HTTPException(409,'payment_run_has_no_items')
   for item in rows:
    inv=c.execute('SELECT * FROM vector_supplier_invoices WHERE id=%s',(item['invoice_id'],)).fetchone()
    if not inv or inv['payment_status'] not in ('payable','partially_paid'):raise HTTPException(409,'payment_run_contains_nonpayable_invoice')
    balance=max(0,_invoice_net_amount(c,inv['id'],inv['amount'])-_payment_net(c,inv['id']))
    if float(item['amount'])>balance+0.01:raise HTTPException(409,'payment_run_item_exceeds_current_balance')
   c.execute("UPDATE vector_payment_run_items SET status='ready' WHERE run_id=%s",(rid,))
   run=c.execute("UPDATE vector_payment_runs SET status='ready_for_bank',ready_at=%s WHERE id=%s RETURNING *",(now(),rid)).fetchone()
   return {'payment_run':run,'note':'ready for external bank execution; VECTOR has not moved funds'}

 @app.post('/v1/procurement/payment-runs/{run_id}/bank-handoff',status_code=201)
 def bank_handoff(run_id:str,authorization:str|None=Header(None)):
  principal=auth('vector.payables.write',authorization)
  with conn() as c:
   rid=UUID(run_id);run=c.execute('SELECT * FROM vector_payment_runs WHERE id=%s FOR UPDATE',(rid,)).fetchone()
   if not run:raise HTTPException(404,'payment_run_not_found')
   existing=c.execute('SELECT * FROM vector_bank_execution_batches WHERE run_id=%s',(rid,)).fetchone()
   if existing:return {'batch':existing,'instructions':c.execute('SELECT * FROM vector_bank_execution_items WHERE batch_id=%s ORDER BY id',(existing['id'],)).fetchall(),'note':'existing immutable handoff returned; no funds transmitted'}
   if run['status']!='ready_for_bank':raise HTTPException(409,'payment_run_not_ready_for_bank')
   items=c.execute("SELECT * FROM vector_payment_run_items WHERE run_id=%s AND status='ready' ORDER BY id FOR UPDATE",(rid,)).fetchall()
   if not items:raise HTTPException(409,'payment_run_has_no_ready_items')
   bid=str(uuid4());eref='VECBANK-'+now().strftime('%Y%m%d%H%M%S%f')
   batch=c.execute("""INSERT INTO vector_bank_execution_batches VALUES(%s,%s,%s,%s,%s,'generated',%s,%s,NULL,NULL,NULL) RETURNING *""",
    (bid,eref,rid,run['total_amount'],run['item_count'],str(principal),now())).fetchone()
   instructions=[]
   for item in items:
    inv=c.execute('SELECT * FROM vector_supplier_invoices WHERE id=%s FOR UPDATE',(item['invoice_id'],)).fetchone()
    balance=max(0,_invoice_net_amount(c,inv['id'],inv['amount'])-_payment_net(c,inv['id']))
    if float(item['amount'])>balance+0.01:raise HTTPException(409,'bank_handoff_item_exceeds_current_balance')
    instructions.append(c.execute("""INSERT INTO vector_bank_execution_items VALUES(%s,%s,%s,%s,%s,%s,'generated',NULL,NULL,%s) RETURNING *""",
     (str(uuid4()),bid,item['id'],item['invoice_id'],item['supplier_code'],item['amount'],now())).fetchone())
   c.execute("UPDATE vector_payment_run_items SET status='handed_off' WHERE run_id=%s",(rid,))
   c.execute("UPDATE vector_payment_runs SET status='handed_off' WHERE id=%s",(rid,))
   return {'batch':batch,'instructions':instructions,'note':'immutable execution instructions generated; VECTOR has not transmitted funds'}

 @app.post('/v1/procurement/bank-executions/{execution_ref}/acknowledge')
 def acknowledge_bank(execution_ref:str,b:BankAckIn,authorization:str|None=Header(None)):
  auth('vector.payables.write',authorization)
  if b.status not in ('accepted','rejected'):raise HTTPException(400,'bank_status_must_be_accepted_or_rejected')
  with conn() as c:
   batch=c.execute('SELECT * FROM vector_bank_execution_batches WHERE execution_ref=%s FOR UPDATE',(execution_ref,)).fetchone()
   if not batch:raise HTTPException(404,'bank_execution_not_found')
   if batch['status'] not in ('generated','accepted'):raise HTTPException(409,'bank_execution_already_finalized')
   batch=c.execute("""UPDATE vector_bank_execution_batches SET status=%s,acknowledged_at=%s,bank_reference=%s,bank_message=%s
    WHERE id=%s RETURNING *""",(b.status,now(),b.bank_reference,b.message,batch['id'])).fetchone()
   if b.status=='rejected':
    c.execute("UPDATE vector_bank_execution_items SET status='rejected',bank_message=%s,updated_at=%s WHERE batch_id=%s",(b.message,now(),batch['id']))
    c.execute("UPDATE vector_payment_run_items SET status='ready' WHERE run_id=%s",(batch['run_id'],))
    c.execute("UPDATE vector_payment_runs SET status='ready_for_bank' WHERE id=%s",(batch['run_id'],))
   else:
    c.execute("UPDATE vector_bank_execution_items SET status='accepted',updated_at=%s WHERE batch_id=%s",(now(),batch['id']))
   return batch

 @app.post('/v1/procurement/bank-executions/{execution_ref}/reconcile')
 def reconcile_bank_execution(execution_ref:str,items:list[BankItemAckIn],authorization:str|None=Header(None)):
  principal=auth('vector.payables.write',authorization)
  with conn() as c:
   batch=c.execute('SELECT * FROM vector_bank_execution_batches WHERE execution_ref=%s FOR UPDATE',(execution_ref,)).fetchone()
   if not batch:raise HTTPException(404,'bank_execution_not_found')
   if batch['status']!='accepted':raise HTTPException(409,'bank_execution_not_accepted')
   for ack in items:
    if ack.status not in ('settled','rejected'):raise HTTPException(400,'item_status_must_be_settled_or_rejected')
    riid=UUID(ack.run_item_id)
    bei=c.execute('SELECT * FROM vector_bank_execution_items WHERE batch_id=%s AND run_item_id=%s FOR UPDATE',(batch['id'],riid)).fetchone()
    if not bei:raise HTTPException(404,'bank_execution_item_not_found')
    if bei['status'] in ('settled','rejected'):continue
    if ack.status=='settled':
     inv=c.execute('SELECT * FROM vector_supplier_invoices WHERE id=%s FOR UPDATE',(bei['invoice_id'],)).fetchone()
     balance=max(0,_invoice_net_amount(c,inv['id'],inv['amount'])-_payment_net(c,inv['id']))
     amount=min(float(bei['amount']),balance)
     if amount<=0.01:raise HTTPException(409,'invoice_has_no_reconcilable_balance')
     pno='PAY-'+now().strftime('%Y%m%d%H%M%S%f')
     c.execute("""INSERT INTO vector_supplier_payments VALUES(%s,%s,%s,%s,%s,%s,%s,'posted',%s,%s)""",
      (str(uuid4()),pno,inv['id'],inv['po_id'],inv['supplier_code'],amount,ack.bank_reference or execution_ref,str(principal),now()))
     paid=_payment_net(c,inv['id']);net=_invoice_net_amount(c,inv['id'],inv['amount']);pstatus='paid' if net-paid<=0.01 else 'partially_paid'
     c.execute('UPDATE vector_supplier_invoices SET payment_status=%s WHERE id=%s',(pstatus,inv['id']))
     c.execute("UPDATE vector_payment_run_items SET status='settled' WHERE id=%s",(riid,))
    else:c.execute("UPDATE vector_payment_run_items SET status='rejected' WHERE id=%s",(riid,))
    c.execute("""UPDATE vector_bank_execution_items SET status=%s,bank_reference=%s,bank_message=%s,updated_at=%s WHERE id=%s""",
     (ack.status,ack.bank_reference,ack.message,now(),bei['id']))
   states=c.execute('SELECT status,count(*) n FROM vector_bank_execution_items WHERE batch_id=%s GROUP BY status',(batch['id'],)).fetchall()
   pending=sum(int(x['n']) for x in states if x['status'] not in ('settled','rejected'))
   if pending==0:
    settled=sum(int(x['n']) for x in states if x['status']=='settled');rejected=sum(int(x['n']) for x in states if x['status']=='rejected')
    final='settled' if rejected==0 else ('rejected' if settled==0 else 'partially_settled')
    c.execute('UPDATE vector_bank_execution_batches SET status=%s WHERE id=%s',(final,batch['id']))
    c.execute('UPDATE vector_payment_runs SET status=%s WHERE id=%s',(final,batch['run_id']))
   return {'execution_ref':execution_ref,'states':states,'pending_items':pending,'note':'reconciliation records externally reported bank results'}

 @app.get('/v1/procurement/bank-executions/{execution_ref}')
 def bank_execution_status(execution_ref:str,authorization:str|None=Header(None)):
  auth('vector.payables.read',authorization)
  with conn() as c:
   batch=c.execute('SELECT * FROM vector_bank_execution_batches WHERE execution_ref=%s',(execution_ref,)).fetchone()
   if not batch:raise HTTPException(404,'bank_execution_not_found')
   items=c.execute('SELECT * FROM vector_bank_execution_items WHERE batch_id=%s ORDER BY id',(batch['id'],)).fetchall()
   return {'batch':batch,'items':items}

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
   paid=_payment_net(c,iid)
   invoice_amount=_invoice_net_amount(c,iid,inv['amount']);remaining=max(0,invoice_amount-paid)
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
   paid=_payment_net(c,iid);net_amount=_invoice_net_amount(c,iid,inv['amount'])
   payments=c.execute("SELECT * FROM vector_supplier_payments WHERE invoice_id=%s ORDER BY paid_at",(iid,)).fetchall()
   adjustments=c.execute("SELECT * FROM vector_supplier_adjustments WHERE invoice_id=%s ORDER BY created_at",(iid,)).fetchall()
   reversals=c.execute("""SELECT pr.* FROM vector_payment_reversals pr JOIN vector_supplier_payments p ON p.id=pr.payment_id
    WHERE p.invoice_id=%s ORDER BY pr.reversed_at""",(iid,)).fetchall()
   return {'invoice':inv,'invoice_amount':float(inv['amount']),'net_invoice_amount':net_amount,'paid_total':paid,
    'remaining_balance':max(0,net_amount-paid),'payments':payments,'adjustments':adjustments,'payment_reversals':reversals}

 @app.post('/v1/procurement/invoices/{invoice_id}/adjustments',status_code=201)
 def adjust_invoice(invoice_id:str,b:AdjustmentIn,authorization:str|None=Header(None)):
  principal=auth('vector.payables.write',authorization)
  if b.adjustment_type not in ('credit_memo','debit_adjustment'):raise HTTPException(400,'invalid_adjustment_type')
  with conn() as c:
   iid=UUID(invoice_id);inv=c.execute('SELECT * FROM vector_supplier_invoices WHERE id=%s FOR UPDATE',(iid,)).fetchone()
   if not inv:raise HTTPException(404,'supplier_invoice_not_found')
   if inv['status']!='verified':raise HTTPException(409,'invoice_not_verified')
   current_net=_invoice_net_amount(c,iid,inv['amount']);paid=_payment_net(c,iid)
   if b.adjustment_type=='credit_memo' and b.amount>current_net-paid+0.01:raise HTTPException(409,'credit_memo_exceeds_open_balance')
   ano=('CM-' if b.adjustment_type=='credit_memo' else 'DA-')+now().strftime('%Y%m%d%H%M%S%f')
   row=c.execute("""INSERT INTO vector_supplier_adjustments VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'posted',%s,%s) RETURNING *""",
    (str(uuid4()),ano,iid,inv['po_id'],inv['supplier_code'],b.adjustment_type,b.amount,b.reason,str(principal),now())).fetchone()
   net_amount=_invoice_net_amount(c,iid,inv['amount']);paid=_payment_net(c,iid);balance=max(0,net_amount-paid)
   pstatus='paid' if balance<=0.01 else ('partially_paid' if paid>0 else inv['payment_status'])
   invoice=c.execute("UPDATE vector_supplier_invoices SET payment_status=%s WHERE id=%s RETURNING *",(pstatus,iid)).fetchone()
   return {'adjustment':row,'invoice':invoice,'net_invoice_amount':net_amount,'paid_total':paid,'remaining_balance':balance}

 @app.post('/v1/procurement/payments/{payment_id}/reverse',status_code=201)
 def reverse_payment(payment_id:str,b:ReversalIn,authorization:str|None=Header(None)):
  principal=auth('vector.payables.write',authorization)
  with conn() as c:
   pid=UUID(payment_id);p=c.execute('SELECT * FROM vector_supplier_payments WHERE id=%s FOR UPDATE',(pid,)).fetchone()
   if not p:raise HTTPException(404,'payment_not_found')
   if p['status']!='posted':raise HTTPException(409,'payment_not_posted')
   if c.execute('SELECT 1 FROM vector_payment_reversals WHERE payment_id=%s',(pid,)).fetchone():raise HTTPException(409,'payment_already_reversed')
   rno='REV-'+now().strftime('%Y%m%d%H%M%S%f')
   rev=c.execute("""INSERT INTO vector_payment_reversals VALUES(%s,%s,%s,%s,%s,'posted',%s,%s) RETURNING *""",
    (str(uuid4()),rno,pid,p['amount'],b.reason,str(principal),now())).fetchone()
   inv=c.execute('SELECT * FROM vector_supplier_invoices WHERE id=%s FOR UPDATE',(p['invoice_id'],)).fetchone()
   paid=_payment_net(c,p['invoice_id']);net_amount=_invoice_net_amount(c,p['invoice_id'],inv['amount']);balance=max(0,net_amount-paid)
   pstatus='payable' if paid<=0 else 'partially_paid'
   invoice=c.execute("UPDATE vector_supplier_invoices SET payment_status=%s WHERE id=%s RETURNING *",(pstatus,p['invoice_id'])).fetchone()
   po=c.execute('SELECT status FROM vector_purchase_orders WHERE id=%s',(p['po_id'],)).fetchone()
   if po and po['status']=='settled':c.execute("UPDATE vector_purchase_orders SET status='complete' WHERE id=%s",(p['po_id'],))
   return {'reversal':rev,'invoice':invoice,'paid_total':paid,'remaining_balance':balance}

 @app.post('/v1/procurement/suppliers/statement-reconciliation')
 def reconcile_statement(b:StatementIn,authorization:str|None=Header(None)):
  auth('vector.payables.read',authorization)
  with conn() as c:
   rows=c.execute("""SELECT id,amount FROM vector_supplier_invoices
    WHERE supplier_code=%s AND status='verified'""",(b.supplier_code,)).fetchall()
   ledger=0.0
   for inv in rows:
    net=_invoice_net_amount(c,inv['id'],inv['amount']);paid=_payment_net(c,inv['id']);ledger+=max(0,net-paid)
   variance=round(float(b.statement_balance)-ledger,2)
   return {'supplier_code':b.supplier_code,'supplier_statement_balance':float(b.statement_balance),
    'vector_open_balance':round(ledger,2),'variance':variance,'reconciled':abs(variance)<=0.01,'open_invoice_count':len(rows)}

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
