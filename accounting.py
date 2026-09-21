from datetime import datetime,timezone
from decimal import Decimal
from uuid import uuid4,UUID
from fastapi import Header,HTTPException
from pydantic import BaseModel,Field

def now(): return datetime.now(timezone.utc)

def init_accounting(conn):
 with conn() as c:
  c.execute("""CREATE TABLE IF NOT EXISTS vector_gl_accounts(
   id UUID PRIMARY KEY,code TEXT UNIQUE NOT NULL,name TEXT NOT NULL,account_type TEXT NOT NULL,
   active BOOLEAN NOT NULL DEFAULT TRUE,created_at TIMESTAMPTZ NOT NULL)""")
  c.execute("""CREATE TABLE IF NOT EXISTS vector_journal_entries(
   id UUID PRIMARY KEY,journal_no TEXT UNIQUE NOT NULL,entry_date DATE NOT NULL,description TEXT NOT NULL,
   source_type TEXT NULL,source_id TEXT NULL,status TEXT NOT NULL,created_by TEXT NULL,created_at TIMESTAMPTZ NOT NULL,
   posted_at TIMESTAMPTZ NULL,reversal_of UUID NULL)""")
  c.execute("""CREATE TABLE IF NOT EXISTS vector_journal_lines(
   id UUID PRIMARY KEY,journal_id UUID NOT NULL,account_code TEXT NOT NULL,debit NUMERIC NOT NULL DEFAULT 0,
   credit NUMERIC NOT NULL DEFAULT 0,memo TEXT NOT NULL DEFAULT '',po_id UUID NULL,invoice_id UUID NULL,payment_id UUID NULL,
   CHECK(debit>=0),CHECK(credit>=0),CHECK(NOT(debit>0 AND credit>0)))""")
  c.execute("CREATE INDEX IF NOT EXISTS idx_vector_journal_source ON vector_journal_entries(source_type,source_id)")
  c.execute("CREATE INDEX IF NOT EXISTS idx_vector_journal_lines_po ON vector_journal_lines(po_id)")
  c.execute("CREATE INDEX IF NOT EXISTS idx_vector_journal_lines_invoice ON vector_journal_lines(invoice_id)")
  for code,name,typ in [('1000','Cash','asset'),('1200','Inventory','asset'),('2000','Accounts Payable','liability'),('5000','Purchases / Expense','expense')]:
   c.execute("""INSERT INTO vector_gl_accounts(id,code,name,account_type,active,created_at) VALUES(%s,%s,%s,%s,TRUE,%s)
    ON CONFLICT(code) DO NOTHING""",(str(uuid4()),code,name,typ,now()))


def _period_open(c,entry_date):
 if entry_date is None:return
 row=c.execute("SELECT status FROM vector_accounting_periods WHERE %s::date BETWEEN start_date AND end_date ORDER BY start_date DESC LIMIT 1",(entry_date,)).fetchone()
 if row and row['status']=='closed':raise HTTPException(409,'accounting_period_closed')

def auto_post(c,source_type,source_id,description,lines,actor='system',entry_date=None):
 _period_open(c,entry_date)
 existing=c.execute("SELECT * FROM vector_journal_entries WHERE source_type=%s AND source_id=%s AND status='posted'",(source_type,str(source_id))).fetchone()
 if existing:return existing
 debit=sum((Decimal(str(x.get('debit',0))) for x in lines),Decimal('0'))
 credit=sum((Decimal(str(x.get('credit',0))) for x in lines),Decimal('0'))
 if debit<=0 or debit!=credit:raise HTTPException(409,'automatic_journal_not_balanced')
 codes=[x['account_code'] for x in lines]
 found={r['code'] for r in c.execute("SELECT code FROM vector_gl_accounts WHERE active=TRUE AND code=ANY(%s)",(codes,)).fetchall()}
 if len(found)!=len(set(codes)):raise HTTPException(409,'automatic_gl_account_missing')
 jid=str(uuid4());jno='JRN-'+now().strftime('%Y%m%d%H%M%S%f')
 row=c.execute("""INSERT INTO vector_journal_entries VALUES(%s,%s,COALESCE(%s,CURRENT_DATE),%s,%s,%s,'posted',%s,%s,%s,NULL) RETURNING *""",
  (jid,jno,entry_date,description,source_type,str(source_id),str(actor),now(),now())).fetchone()
 for x in lines:
  c.execute("""INSERT INTO vector_journal_lines VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
   (str(uuid4()),jid,x['account_code'],Decimal(str(x.get('debit',0))),Decimal(str(x.get('credit',0))),x.get('memo',''),
    x.get('po_id'),x.get('invoice_id'),x.get('payment_id')))
 return row

def post_supplier_invoice(c,invoice,actor='system'):
 amount=Decimal(str(invoice['amount']))
 return auto_post(c,'SUPPLIER_INVOICE',invoice['id'],'Supplier invoice '+invoice['invoice_no'],[
  {'account_code':'5000','debit':amount,'invoice_id':invoice['id'],'po_id':invoice['po_id']},
  {'account_code':'2000','credit':amount,'invoice_id':invoice['id'],'po_id':invoice['po_id']}],actor,invoice.get('invoice_date'))

def post_supplier_payment(c,payment,actor='system'):
 amount=Decimal(str(payment['amount']))
 return auto_post(c,'SUPPLIER_PAYMENT',payment['id'],'Supplier payment '+payment['payment_no'],[
  {'account_code':'2000','debit':amount,'invoice_id':payment['invoice_id'],'po_id':payment['po_id'],'payment_id':payment['id']},
  {'account_code':'1000','credit':amount,'invoice_id':payment['invoice_id'],'po_id':payment['po_id'],'payment_id':payment['id']}],actor)

def post_supplier_adjustment(c,adjustment,actor='system'):
 amount=Decimal(str(adjustment['amount']))
 if adjustment['adjustment_type']=='credit_memo':
  lines=[{'account_code':'2000','debit':amount,'invoice_id':adjustment['invoice_id'],'po_id':adjustment['po_id']},
         {'account_code':'5000','credit':amount,'invoice_id':adjustment['invoice_id'],'po_id':adjustment['po_id']}]
 else:
  lines=[{'account_code':'5000','debit':amount,'invoice_id':adjustment['invoice_id'],'po_id':adjustment['po_id']},
         {'account_code':'2000','credit':amount,'invoice_id':adjustment['invoice_id'],'po_id':adjustment['po_id']}]
 return auto_post(c,'SUPPLIER_ADJUSTMENT',adjustment['id'],'Supplier '+adjustment['adjustment_type']+' '+adjustment['adjustment_no'],lines,actor)

def post_payment_reversal(c,reversal,payment,actor='system'):
 amount=Decimal(str(reversal['amount']))
 return auto_post(c,'PAYMENT_REVERSAL',reversal['id'],'Payment reversal '+reversal['reversal_no'],[
  {'account_code':'1000','debit':amount,'invoice_id':payment['invoice_id'],'po_id':payment['po_id'],'payment_id':payment['id']},
  {'account_code':'2000','credit':amount,'invoice_id':payment['invoice_id'],'po_id':payment['po_id'],'payment_id':payment['id']}],actor)

class AccountIn(BaseModel):
 code:str;name:str;account_type:str
class LineIn(BaseModel):
 account_code:str;debit:Decimal=Decimal('0');credit:Decimal=Decimal('0');memo:str='';po_id:str|None=None;invoice_id:str|None=None;payment_id:str|None=None
class JournalIn(BaseModel):
 entry_date:str;description:str;source_type:str|None=None;source_id:str|None=None;lines:list[LineIn]=Field(min_length=2)

def install_accounting_routes(app,conn,auth):
 @app.post('/v1/accounting/accounts',status_code=201)
 def create_account(b:AccountIn,authorization:str|None=Header(None)):
  auth('vector.accounting.write',authorization)
  if b.account_type not in ('asset','liability','equity','revenue','expense'):raise HTTPException(400,'invalid_account_type')
  with conn() as c:
   return c.execute("INSERT INTO vector_gl_accounts VALUES(%s,%s,%s,%s,TRUE,%s) RETURNING *",(str(uuid4()),b.code,b.name,b.account_type,now())).fetchone()

 @app.get('/v1/accounting/accounts')
 def accounts(authorization:str|None=Header(None)):
  auth('vector.accounting.read',authorization)
  with conn() as c:return c.execute("SELECT * FROM vector_gl_accounts ORDER BY code").fetchall()

 @app.post('/v1/accounting/journals',status_code=201)
 def journal(b:JournalIn,authorization:str|None=Header(None)):
  principal=auth('vector.accounting.write',authorization)
  _period_open(c if False else None,b.entry_date) if False else None
  debit=sum((x.debit for x in b.lines),Decimal('0'));credit=sum((x.credit for x in b.lines),Decimal('0'))
  if debit<=0 or debit!=credit:raise HTTPException(409,{'error':'journal_not_balanced','debit':str(debit),'credit':str(credit)})
  with conn() as c:
   codes=[x.account_code for x in b.lines]
   found={r['code'] for r in c.execute("SELECT code FROM vector_gl_accounts WHERE active=TRUE AND code=ANY(%s)",(codes,)).fetchall()}
   missing=[x for x in codes if x not in found]
   if missing:raise HTTPException(409,{'error':'gl_account_not_found','accounts':missing})
   jid=str(uuid4());jno='JRN-'+now().strftime('%Y%m%d%H%M%S%f')
   row=c.execute("""INSERT INTO vector_journal_entries VALUES(%s,%s,%s,%s,%s,%s,'posted',%s,%s,%s,NULL) RETURNING *""",
    (jid,jno,b.entry_date,b.description,b.source_type,b.source_id,str(principal),now(),now())).fetchone()
   for x in b.lines:
    c.execute("""INSERT INTO vector_journal_lines VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
     (str(uuid4()),jid,x.account_code,x.debit,x.credit,x.memo,UUID(x.po_id) if x.po_id else None,UUID(x.invoice_id) if x.invoice_id else None,UUID(x.payment_id) if x.payment_id else None))
   return {'journal':row,'debit':debit,'credit':credit}

 @app.get('/v1/accounting/journals/{journal_id}')
 def get_journal(journal_id:str,authorization:str|None=Header(None)):
  auth('vector.accounting.read',authorization)
  try:jid=UUID(journal_id)
  except ValueError:raise HTTPException(400,'invalid_journal_id')
  with conn() as c:
   j=c.execute("SELECT * FROM vector_journal_entries WHERE id=%s",(jid,)).fetchone()
   if not j:raise HTTPException(404,'journal_not_found')
   return {'journal':j,'lines':c.execute("SELECT * FROM vector_journal_lines WHERE journal_id=%s ORDER BY id",(jid,)).fetchall()}

 @app.post('/v1/accounting/journals/{journal_id}/reverse',status_code=201)
 def reverse(journal_id:str,authorization:str|None=Header(None)):
  principal=auth('vector.accounting.write',authorization)
  try:jid=UUID(journal_id)
  except ValueError:raise HTTPException(400,'invalid_journal_id')
  with conn() as c:
   original=c.execute("SELECT * FROM vector_journal_entries WHERE id=%s FOR UPDATE",(jid,)).fetchone()
   if not original:raise HTTPException(404,'journal_not_found')
   if original['status']!='posted':raise HTTPException(409,'journal_not_posted')
   if c.execute("SELECT 1 FROM vector_journal_entries WHERE reversal_of=%s",(jid,)).fetchone():raise HTTPException(409,'journal_already_reversed')
   lines=c.execute("SELECT * FROM vector_journal_lines WHERE journal_id=%s",(jid,)).fetchall()
   rid=str(uuid4());jno='JRN-'+now().strftime('%Y%m%d%H%M%S%f')
   rev=c.execute("""INSERT INTO vector_journal_entries VALUES(%s,%s,CURRENT_DATE,%s,'REVERSAL',%s,'posted',%s,%s,%s,%s) RETURNING *""",
    (rid,jno,'Reversal of '+original['journal_no'],str(jid),str(principal),now(),now(),jid)).fetchone()
   for x in lines:c.execute("""INSERT INTO vector_journal_lines VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
    (str(uuid4()),rid,x['account_code'],x['credit'],x['debit'],'Reversal: '+x['memo'],x['po_id'],x['invoice_id'],x['payment_id']))
   c.execute("UPDATE vector_journal_entries SET status='reversed' WHERE id=%s",(jid,))
   return {'journal':rev,'reversed_journal_id':jid}


 @app.get('/v1/accounting/trial-balance')
 def trial_balance(as_of:str|None=None,authorization:str|None=Header(None)):
  auth('vector.accounting.read',authorization)
  with conn() as c:
   return c.execute("""SELECT a.code,a.name,a.account_type,COALESCE(SUM(l.debit),0) debit,COALESCE(SUM(l.credit),0) credit,
    COALESCE(SUM(l.debit-l.credit),0) balance FROM vector_gl_accounts a LEFT JOIN vector_journal_lines l ON l.account_code=a.code
    LEFT JOIN vector_journal_entries j ON j.id=l.journal_id AND j.status IN ('posted','reversed')
    WHERE (%s IS NULL OR j.entry_date<=%s::date) GROUP BY a.code,a.name,a.account_type ORDER BY a.code""",(as_of,as_of)).fetchall()

 @app.get('/v1/accounting/journal-register')
 def journal_register(start_date:str|None=None,end_date:str|None=None,authorization:str|None=Header(None)):
  auth('vector.accounting.read',authorization)
  with conn() as c:return c.execute("""SELECT j.*,COALESCE(SUM(l.debit),0) amount FROM vector_journal_entries j
   LEFT JOIN vector_journal_lines l ON l.journal_id=j.id WHERE (%s IS NULL OR j.entry_date>=%s::date) AND (%s IS NULL OR j.entry_date<=%s::date)
   GROUP BY j.id ORDER BY j.entry_date,j.created_at""",(start_date,start_date,end_date,end_date)).fetchall()

 @app.get('/v1/accounting/income-statement')
 def income_statement(start_date:str|None=None,end_date:str|None=None,authorization:str|None=Header(None)):
  auth('vector.accounting.read',authorization)
  with conn() as c:
   rows=c.execute("""SELECT a.code,a.name,a.account_type,COALESCE(SUM(l.credit-l.debit),0) amount FROM vector_gl_accounts a
    LEFT JOIN vector_journal_lines l ON l.account_code=a.code LEFT JOIN vector_journal_entries j ON j.id=l.journal_id
    WHERE a.account_type IN ('revenue','expense') AND (%s IS NULL OR j.entry_date>=%s::date) AND (%s IS NULL OR j.entry_date<=%s::date)
    GROUP BY a.code,a.name,a.account_type ORDER BY a.code""",(start_date,start_date,end_date,end_date)).fetchall()
   revenue=sum((Decimal(str(x['amount'])) for x in rows if x['account_type']=='revenue'),Decimal('0'))
   expenses=-sum((Decimal(str(x['amount'])) for x in rows if x['account_type']=='expense'),Decimal('0'))
   return {'accounts':rows,'revenue':revenue,'expenses':expenses,'net_income':revenue-expenses}

 @app.get('/v1/accounting/balance-sheet')
 def balance_sheet(as_of:str|None=None,authorization:str|None=Header(None)):
  auth('vector.accounting.read',authorization)
  with conn() as c:return c.execute("""SELECT a.code,a.name,a.account_type,
   COALESCE(SUM(CASE WHEN a.account_type='asset' THEN l.debit-l.credit ELSE l.credit-l.debit END),0) balance
   FROM vector_gl_accounts a LEFT JOIN vector_journal_lines l ON l.account_code=a.code LEFT JOIN vector_journal_entries j ON j.id=l.journal_id
   WHERE a.account_type IN ('asset','liability','equity') AND (%s IS NULL OR j.entry_date<=%s::date)
   GROUP BY a.code,a.name,a.account_type ORDER BY a.code""",(as_of,as_of)).fetchall()

 @app.get('/v1/accounting/ap-reconciliation')
 def ap_reconciliation(authorization:str|None=Header(None)):
  auth('vector.accounting.read',authorization)
  with conn() as c:
   gl=c.execute("SELECT COALESCE(SUM(credit-debit),0) v FROM vector_journal_lines WHERE account_code='2000'").fetchone()['v']
   ap=c.execute("""SELECT COALESCE(SUM(i.amount),0) v FROM vector_supplier_invoices i WHERE i.status='verified'""").fetchone()['v']
   paid=c.execute("SELECT COALESCE(SUM(amount),0) v FROM vector_supplier_payments WHERE status='posted'").fetchone()['v']
   expected=Decimal(str(ap))-Decimal(str(paid))
   return {'gl_ap':gl,'operational_ap_before_adjustments':expected,'difference_before_adjustments':Decimal(str(gl))-expected}

 @app.post('/v1/accounting/periods',status_code=201)
 def create_period(period_code:str,start_date:str,end_date:str,authorization:str|None=Header(None)):
  auth('vector.accounting.write',authorization)
  with conn() as c:return c.execute("INSERT INTO vector_accounting_periods VALUES(%s,%s,%s,%s,'open',%s) RETURNING *",(str(uuid4()),period_code,start_date,end_date,now())).fetchone()

 @app.post('/v1/accounting/periods/{period_code}/close')
 def close_period(period_code:str,authorization:str|None=Header(None)):
  auth('vector.accounting.write',authorization)
  with conn() as c:
   r=c.execute("UPDATE vector_accounting_periods SET status='closed' WHERE period_code=%s AND status='open' RETURNING *",(period_code,)).fetchone()
   if not r:raise HTTPException(404,'open_period_not_found')
   return r

 @app.get('/v1/accounting/source/{source_type}/{source_id}')
 def source_journals(source_type:str,source_id:str,authorization:str|None=Header(None)):
  auth('vector.accounting.read',authorization)
  with conn() as c:return c.execute("SELECT * FROM vector_journal_entries WHERE source_type=%s AND source_id=%s ORDER BY created_at",(source_type,source_id)).fetchall()
