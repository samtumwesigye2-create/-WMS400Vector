from datetime import datetime,timezone
from fastapi import Header,HTTPException
from pydantic import BaseModel,Field
from uuid import uuid4
def now():return datetime.now(timezone.utc)
MOVEMENTS={'101':'goods_receipt_po','102':'reversal_101','201':'goods_issue_cost_center','261':'goods_issue_production','311':'storage_location_transfer','122':'return_supplier'}
def init_goods_movement(conn):
 with conn() as c:
  c.execute("""CREATE TABLE IF NOT EXISTS vector_material_documents(id UUID PRIMARY KEY,document_no TEXT UNIQUE NOT NULL,movement_type TEXT NOT NULL,sku TEXT NOT NULL,quantity NUMERIC NOT NULL,uom TEXT NOT NULL,plant_code TEXT NOT NULL,from_location TEXT NULL,to_location TEXT NULL,po_id UUID NULL,batch_no TEXT NULL,serial_no TEXT NULL,reversal_of TEXT NULL,status TEXT NOT NULL,posted_at TIMESTAMPTZ NOT NULL)""")
class Move(BaseModel):
 movement_type:str;sku:str;quantity:float=Field(gt=0);uom:str='EA';plant_code:str;from_location:str|None=None;to_location:str|None=None;po_id:str|None=None;batch_no:str|None=None;serial_no:str|None=None;reversal_of:str|None=None
def install_goods_movement_routes(app,conn,auth):
 @app.get('/v1/goods-movement/types')
 def types(authorization:str|None=Header(None)):auth('vector.inventory.read',authorization);return MOVEMENTS
 @app.post('/v1/goods-movement/check')
 def check(b:Move,authorization:str|None=Header(None)):
  auth('vector.inventory.write',authorization);e=[]
  if b.movement_type not in MOVEMENTS:e.append('unsupported_movement_type')
  if b.movement_type=='101' and not b.po_id:e.append('po_required')
  if b.movement_type in ('201','261') and not b.from_location:e.append('source_location_required')
  if b.movement_type=='311' and (not b.from_location or not b.to_location):e.append('source_and_destination_required')
  if b.movement_type=='102' and not b.reversal_of:e.append('original_document_required')
  return {'postable':not e,'errors':e}
 @app.post('/v1/goods-movement/post',status_code=201)
 def post(b:Move,authorization:str|None=Header(None)):
  auth('vector.inventory.write',authorization)
  chk=check(b,authorization)
  if not chk['postable']:raise HTTPException(400,chk)
  doc='VMD-'+now().strftime('%Y%m%d%H%M%S%f')
  with conn() as c:
   if b.reversal_of and not c.execute('SELECT 1 FROM vector_material_documents WHERE document_no=%s',(b.reversal_of,)).fetchone():raise HTTPException(404,'original_document_not_found')
   return c.execute("""INSERT INTO vector_material_documents VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'posted',%s) RETURNING *""",(str(uuid4()),doc,b.movement_type,b.sku,b.quantity,b.uom,b.plant_code,b.from_location,b.to_location,b.po_id,b.batch_no,b.serial_no,b.reversal_of,now())).fetchone()
 @app.get('/v1/goods-movement/documents')
 def docs(authorization:str|None=Header(None)):
  auth('vector.inventory.read',authorization)
  with conn() as c:return c.execute('SELECT * FROM vector_material_documents ORDER BY posted_at DESC LIMIT 500').fetchall()
