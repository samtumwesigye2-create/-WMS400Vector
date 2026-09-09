from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from app import auth, conn

router=APIRouter(prefix='/v1/material-documents',tags=['Material Documents'])
MOVEMENT_CODES={'receive':'101','dispatch':'601','transfer':'311','adjust':'701'}
REVERSAL_CODES={'101':'102','601':'602','311':'312','701':'702','702':'701','261':'262','262':'261','122':'123'}

def now(): return datetime.now(timezone.utc)
def ensure_schema(c):
 c.execute('CREATE SEQUENCE IF NOT EXISTS vector_material_doc_seq START 5000000001')
 c.execute('''CREATE TABLE IF NOT EXISTS vector_material_documents(
 id UUID PRIMARY KEY, document_number TEXT UNIQUE NOT NULL, document_date DATE NOT NULL,
 posting_date DATE NOT NULL, movement_code TEXT NOT NULL, movement_type TEXT NOT NULL,
 sku TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', quantity NUMERIC NOT NULL,
 uom TEXT NOT NULL DEFAULT 'EA', unit_price NUMERIC NULL, amount NUMERIC NULL,
 from_location TEXT NULL, to_location TEXT NULL, reference_document TEXT NULL,
 movement_id UUID NULL, reversal_of TEXT NULL, reversed_by TEXT NULL,
 actor_id TEXT NULL, created_at TIMESTAMPTZ NOT NULL)''')
 c.execute('CREATE INDEX IF NOT EXISTS ix_vector_material_docs_sku ON vector_material_documents(sku,posting_date DESC)')
 c.execute('CREATE INDEX IF NOT EXISTS ix_vector_material_docs_ref ON vector_material_documents(reference_document)')

def next_no(c): return str(c.execute("SELECT nextval('vector_material_doc_seq') n").fetchone()['n'])
def create_for_movement(c,movement,actor_id=None,movement_code=None,uom='EA',unit_price=None):
 ensure_schema(c); code=movement_code or MOVEMENT_CODES.get(movement['movement_type'],movement['movement_type'])
 docno=next_no(c); price=Decimal(str(unit_price)) if unit_price is not None else None
 amount=(Decimal(str(movement['quantity']))*price) if price is not None else None
 desc=c.execute('SELECT description FROM vector_inventory WHERE sku=%s ORDER BY updated_at DESC LIMIT 1',(movement['sku'],)).fetchone(); desc=(desc or {}).get('description') or movement['sku']
 return c.execute('''INSERT INTO vector_material_documents(id,document_number,document_date,posting_date,movement_code,movement_type,sku,description,quantity,uom,unit_price,amount,from_location,to_location,reference_document,movement_id,actor_id,created_at)
 VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',(str(uuid4()),docno,now().date(),now().date(),code,movement['movement_type'],movement['sku'],desc,movement['quantity'],uom,price,amount,movement.get('from_location'),movement.get('to_location'),movement.get('reference'),movement['id'],actor_id,now())).fetchone()

class ReverseIn(BaseModel): reason:str=Field(min_length=1); posting_date:str|None=None
@router.get('')
def list_docs(sku:str|None=None,movement_code:str|None=None,reference_document:str|None=None,limit:int=200,authorization:str|None=Header(None)):
 auth('vector.movements.read',authorization); limit=max(1,min(limit,1000))
 with conn() as c:
  ensure_schema(c); where=[]; vals=[]
  if sku: where.append('sku=%s'); vals.append(sku)
  if movement_code: where.append('movement_code=%s'); vals.append(movement_code)
  if reference_document: where.append('reference_document=%s'); vals.append(reference_document)
  q='SELECT * FROM vector_material_documents'+((' WHERE '+' AND '.join(where)) if where else '')+' ORDER BY created_at DESC LIMIT %s'; vals.append(limit)
  return c.execute(q,tuple(vals)).fetchall()
@router.get('/{document_number}')
def get_doc(document_number:str,authorization:str|None=Header(None)):
 auth('vector.movements.read',authorization)
 with conn() as c:
  ensure_schema(c); row=c.execute('SELECT * FROM vector_material_documents WHERE document_number=%s',(document_number,)).fetchone()
  if not row: raise HTTPException(404,'material_document_not_found')
  return row
@router.post('/{document_number}/reverse',status_code=201)
def reverse_doc(document_number:str,b:ReverseIn,authorization:str|None=Header(None)):
 principal=auth('vector.movements.write',authorization)
 with conn() as c:
  ensure_schema(c); src=c.execute('SELECT * FROM vector_material_documents WHERE document_number=%s FOR UPDATE',(document_number,)).fetchone()
  if not src: raise HTTPException(404,'material_document_not_found')
  if src['reversal_of'] or src['reversed_by']: raise HTTPException(409,'document_already_reversed_or_is_reversal')
  code=REVERSAL_CODES.get(src['movement_code'],'REV-'+src['movement_code']); newno=next_no(c); amount=-src['amount'] if src['amount'] is not None else None
  row=c.execute('''INSERT INTO vector_material_documents(id,document_number,document_date,posting_date,movement_code,movement_type,sku,description,quantity,uom,unit_price,amount,from_location,to_location,reference_document,reversal_of,actor_id,created_at)
 VALUES(%s,%s,%s,%s,%s,'reversal',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',(str(uuid4()),newno,now().date(),b.posting_date or now().date(),code,src['sku'],src['description'],-src['quantity'],src['uom'],src['unit_price'],amount,src['to_location'],src['from_location'],'REV:'+document_number,document_number,principal.get('id') or principal.get('sub'),now())).fetchone()
  c.execute('UPDATE vector_material_documents SET reversed_by=%s WHERE document_number=%s',(newno,document_number)); return row
