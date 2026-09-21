from datetime import datetime,timezone
from uuid import uuid4
from fastapi import Header,HTTPException
from pydantic import BaseModel,Field

def now():return datetime.now(timezone.utc)

def init_release_approvals(conn):
 with conn() as c:
  c.execute("""CREATE TABLE IF NOT EXISTS vector_release_strategies(
   id UUID PRIMARY KEY,code TEXT UNIQUE NOT NULL,document_type TEXT NOT NULL,
   min_amount NUMERIC NOT NULL DEFAULT 0,required_approvals INTEGER NOT NULL DEFAULT 1,
   active BOOLEAN NOT NULL DEFAULT TRUE)""")
  c.execute("""CREATE TABLE IF NOT EXISTS vector_approval_requests(
   id UUID PRIMARY KEY,document_type TEXT NOT NULL,document_id UUID NOT NULL,
   strategy_code TEXT NOT NULL,amount NUMERIC NOT NULL DEFAULT 0,
   required_approvals INTEGER NOT NULL,status TEXT NOT NULL,
   requested_by TEXT NULL,created_at TIMESTAMPTZ NOT NULL,resolved_at TIMESTAMPTZ NULL,
   UNIQUE(document_type,document_id))""")
  c.execute("""CREATE TABLE IF NOT EXISTS vector_approval_decisions(
   id UUID PRIMARY KEY,request_id UUID NOT NULL,approver TEXT NOT NULL,
   decision TEXT NOT NULL,comment TEXT NOT NULL DEFAULT '',decided_at TIMESTAMPTZ NOT NULL,
   UNIQUE(request_id,approver))""")

def matching_strategy(c,document_type,amount):
 return c.execute("""SELECT * FROM vector_release_strategies
  WHERE active=TRUE AND document_type=%s AND min_amount<=%s
  ORDER BY min_amount DESC LIMIT 1""",(document_type,float(amount))).fetchone()

def create_approval_request(c,document_type,document_id,amount,requested_by):
 s=matching_strategy(c,document_type,amount)
 if not s:return None
 return c.execute("""INSERT INTO vector_approval_requests
  VALUES(%s,%s,%s,%s,%s,%s,'pending',%s,%s,NULL)
  ON CONFLICT(document_type,document_id) DO UPDATE SET strategy_code=EXCLUDED.strategy_code,
  amount=EXCLUDED.amount,required_approvals=EXCLUDED.required_approvals
  RETURNING *""",(str(uuid4()),document_type,document_id,s['code'],amount,s['required_approvals'],requested_by,now())).fetchone()

class StrategyIn(BaseModel):
 code:str;document_type:str;min_amount:float=Field(default=0,ge=0);required_approvals:int=Field(default=1,ge=1,le=20);active:bool=True
class DecisionIn(BaseModel):
 decision:str;comment:str=''

def _release_document(c,r):
 if r['document_type']=='PO':
  c.execute("UPDATE vector_purchase_orders SET status='open' WHERE id=%s",(r['document_id'],))
 elif r['document_type']=='PR':
  c.execute("UPDATE vector_purchase_requisitions SET status='open' WHERE id=%s",(r['document_id'],))
 elif r['document_type']=='EXCEPTION':
  c.execute("UPDATE vector_exceptions SET status='approved' WHERE id=%s",(r['document_id'],))

def _reject_document(c,r):
 if r['document_type']=='PO':
  c.execute("UPDATE vector_purchase_orders SET status='rejected' WHERE id=%s",(r['document_id'],))
 elif r['document_type']=='PR':
  c.execute("UPDATE vector_purchase_requisitions SET status='rejected' WHERE id=%s",(r['document_id'],))
 elif r['document_type']=='EXCEPTION':
  c.execute("UPDATE vector_exceptions SET status='rejected' WHERE id=%s",(r['document_id'],))

def install_release_approval_routes(app,conn,auth):
 @app.post('/v1/approvals/strategies',status_code=201)
 def strategy(b:StrategyIn,authorization:str|None=Header(None)):
  auth('vector.approvals.admin',authorization)
  dt=b.document_type.upper()
  if dt not in {'PR','PO','EXCEPTION'}:raise HTTPException(400,'unsupported_document_type')
  with conn() as c:
   return c.execute("""INSERT INTO vector_release_strategies VALUES(%s,%s,%s,%s,%s,%s)
    ON CONFLICT(code) DO UPDATE SET document_type=EXCLUDED.document_type,min_amount=EXCLUDED.min_amount,
    required_approvals=EXCLUDED.required_approvals,active=EXCLUDED.active RETURNING *""",
    (str(uuid4()),b.code,dt,b.min_amount,b.required_approvals,b.active)).fetchone()
 @app.get('/v1/approvals/strategies')
 def strategies(authorization:str|None=Header(None)):
  auth('vector.approvals.read',authorization)
  with conn() as c:return c.execute('SELECT * FROM vector_release_strategies ORDER BY document_type,min_amount').fetchall()
 @app.get('/v1/approvals/requests')
 def requests(status:str|None=None,authorization:str|None=Header(None)):
  auth('vector.approvals.read',authorization)
  with conn() as c:
   if status:return c.execute('SELECT * FROM vector_approval_requests WHERE status=%s ORDER BY created_at DESC',(status,)).fetchall()
   return c.execute('SELECT * FROM vector_approval_requests ORDER BY created_at DESC LIMIT 500').fetchall()
 @app.post('/v1/approvals/requests/{request_id}/decision')
 def decide(request_id:str,b:DecisionIn,authorization:str|None=Header(None)):
  principal=auth('vector.approvals.decide',authorization);decision=b.decision.lower()
  if decision not in {'approve','reject'}:raise HTTPException(400,'decision_must_be_approve_or_reject')
  approver=str(principal.get('sub') or principal.get('id') or principal.get('username') or principal)
  with conn() as c:
   r=c.execute("SELECT * FROM vector_approval_requests WHERE id=%s FOR UPDATE",(request_id,)).fetchone()
   if not r:raise HTTPException(404,'approval_request_not_found')
   if r['status']!='pending':raise HTTPException(409,'approval_request_already_resolved')
   c.execute("""INSERT INTO vector_approval_decisions VALUES(%s,%s,%s,%s,%s,%s)
    ON CONFLICT(request_id,approver) DO UPDATE SET decision=EXCLUDED.decision,comment=EXCLUDED.comment,decided_at=EXCLUDED.decided_at""",
    (str(uuid4()),request_id,approver,decision,b.comment,now()))
   if decision=='reject':
    c.execute("UPDATE vector_approval_requests SET status='rejected',resolved_at=%s WHERE id=%s",(now(),request_id));_reject_document(c,r)
   else:
    n=c.execute("SELECT count(*) n FROM vector_approval_decisions WHERE request_id=%s AND decision='approve'",(request_id,)).fetchone()['n']
    if n>=r['required_approvals']:
     c.execute("UPDATE vector_approval_requests SET status='approved',resolved_at=%s WHERE id=%s",(now(),request_id));_release_document(c,r)
   return c.execute('SELECT * FROM vector_approval_requests WHERE id=%s',(request_id,)).fetchone()
 @app.get('/v1/approvals/requests/{request_id}/decisions')
 def decisions(request_id:str,authorization:str|None=Header(None)):
  auth('vector.approvals.read',authorization)
  with conn() as c:return c.execute('SELECT * FROM vector_approval_decisions WHERE request_id=%s ORDER BY decided_at',(request_id,)).fetchall()
