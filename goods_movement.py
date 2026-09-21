from datetime import datetime,timezone
from fastapi import Header,HTTPException
from pydantic import BaseModel,Field
from uuid import uuid4,UUID
def now():return datetime.now(timezone.utc)
MOVEMENTS={'101':'goods_receipt_po','102':'reversal_101','201':'goods_issue_cost_center','261':'goods_issue_production','311':'storage_location_transfer','122':'return_supplier'}
def init_goods_movement(conn):
 with conn() as c:
  c.execute("""CREATE TABLE IF NOT EXISTS vector_material_documents(id UUID PRIMARY KEY,document_no TEXT UNIQUE NOT NULL,movement_type TEXT NOT NULL,sku TEXT NOT NULL,quantity NUMERIC NOT NULL,uom TEXT NOT NULL,plant_code TEXT NOT NULL,from_location TEXT NULL,to_location TEXT NULL,po_id UUID NULL,batch_no TEXT NULL,serial_no TEXT NULL,reversal_of TEXT NULL,status TEXT NOT NULL,posted_at TIMESTAMPTZ NOT NULL)""")
  c.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_vector_material_reversal ON vector_material_documents(reversal_of) WHERE reversal_of IS NOT NULL AND status='posted'")
class Move(BaseModel):
 movement_type:str;sku:str;quantity:float=Field(gt=0);uom:str='EA';plant_code:str;from_location:str|None=None;to_location:str|None=None;po_id:str|None=None;batch_no:str|None=None;serial_no:str|None=None;reversal_of:str|None=None
def _inv(c,sku,loc):
 return c.execute('SELECT * FROM vector_inventory WHERE sku=%s AND location_code=%s FOR UPDATE',(sku,loc)).fetchone()
def _add(c,sku,loc,qty):
 r=_inv(c,sku,loc)
 if r:c.execute('UPDATE vector_inventory SET quantity=quantity+%s,updated_at=%s WHERE id=%s',(qty,now(),r['id']))
 else:c.execute("INSERT INTO vector_inventory VALUES(%s,%s,%s,%s,%s,'available',%s)",(str(uuid4()),sku,sku,int(qty),loc,now()))
def _take(c,sku,loc,qty):
 r=_inv(c,sku,loc)
 if not r or float(r['quantity'])<qty:raise HTTPException(409,'insufficient_stock')
 c.execute('UPDATE vector_inventory SET quantity=quantity-%s,updated_at=%s WHERE id=%s',(qty,now(),r['id']))
def _ledger(c,sku,qty,kind,src,dst,ref):
 c.execute('INSERT INTO vector_movements VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',(str(uuid4()),sku,int(qty),kind,src,dst,ref,now()))
def install_goods_movement_routes(app,conn,auth):
 @app.get('/v1/goods-movement/types')
 def types(authorization:str|None=Header(None)):auth('vector.inventory.read',authorization);return MOVEMENTS
 @app.post('/v1/goods-movement/check')
 def check(b:Move,authorization:str|None=Header(None)):
  auth('vector.inventory.write',authorization);e=[]
  if b.movement_type not in MOVEMENTS:e.append('unsupported_movement_type')
  if b.movement_type=='101' and (not b.po_id or not b.to_location):e.append('po_and_destination_required')
  if b.movement_type in ('201','261','122') and not b.from_location:e.append('source_location_required')
  if b.movement_type=='311' and (not b.from_location or not b.to_location):e.append('source_and_destination_required')
  if b.movement_type=='102' and not b.reversal_of:e.append('original_document_required')
  return {'postable':not e,'errors':e}
 @app.post('/v1/goods-movement/post',status_code=201)
 def post(b:Move,authorization:str|None=Header(None)):
  auth('vector.inventory.write',authorization);chk=check(b,authorization)
  if not chk['postable']:raise HTTPException(400,chk)
  doc='VMD-'+now().strftime('%Y%m%d%H%M%S%f')
  with conn() as c:
   original=None
   if b.movement_type=='102':
    original=c.execute("SELECT * FROM vector_material_documents WHERE document_no=%s AND movement_type='101' AND status='posted' FOR UPDATE",(b.reversal_of,)).fetchone()
    if not original:raise HTTPException(404,'reversible_101_document_not_found')
    if c.execute("SELECT 1 FROM vector_material_documents WHERE reversal_of=%s AND status='posted'",(b.reversal_of,)).fetchone():raise HTTPException(409,'document_already_reversed')
    b.sku=original['sku'];b.quantity=float(original['quantity']);b.uom=original['uom'];b.plant_code=original['plant_code'];b.po_id=str(original['po_id']) if original['po_id'] else None;b.to_location=original['to_location']
   po_uuid=UUID(b.po_id) if b.po_id else None
   if b.movement_type=='101':
    po=c.execute('SELECT * FROM vector_purchase_orders WHERE id=%s FOR UPDATE',(po_uuid,)).fetchone()
    if not po:raise HTTPException(404,'purchase_order_not_found')
    if po['status'] in ('pending_approval','rejected'):raise HTTPException(409,'purchase_order_not_released')
    if po['sku']!=b.sku:raise HTTPException(409,'po_sku_mismatch')
    received=float(c.execute("SELECT COALESCE(sum(CASE WHEN movement_type='101' THEN quantity WHEN movement_type='102' THEN -quantity ELSE 0 END),0) q FROM vector_material_documents WHERE po_id=%s AND status='posted'",(po_uuid,)).fetchone()['q'])
    if received+b.quantity>float(po['quantity']):raise HTTPException(409,'receipt_exceeds_po_quantity')
    _add(c,b.sku,b.to_location,b.quantity);_ledger(c,b.sku,b.quantity,'101',None,b.to_location,doc)
    new_received=received+b.quantity
    c.execute("UPDATE vector_purchase_orders SET status=%s WHERE id=%s",('received' if new_received>=float(po['quantity']) else 'partially_received',po_uuid))
   elif b.movement_type=='102':
    _take(c,b.sku,b.to_location,b.quantity);_ledger(c,b.sku,-b.quantity,'102',b.to_location,None,doc)
    if po_uuid:c.execute("UPDATE vector_purchase_orders SET status='open' WHERE id=%s",(po_uuid,))
   elif b.movement_type=='311':
    if b.from_location==b.to_location:raise HTTPException(400,'locations_must_differ')
    _take(c,b.sku,b.from_location,b.quantity);_add(c,b.sku,b.to_location,b.quantity);_ledger(c,b.sku,b.quantity,'311',b.from_location,b.to_location,doc)
   elif b.movement_type in ('201','261','122'):
    _take(c,b.sku,b.from_location,b.quantity);_ledger(c,b.sku,-b.quantity,b.movement_type,b.from_location,None,doc)
   row=c.execute("""INSERT INTO vector_material_documents VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'posted',%s) RETURNING *""",(str(uuid4()),doc,b.movement_type,b.sku,b.quantity,b.uom,b.plant_code,b.from_location,b.to_location,po_uuid,b.batch_no,b.serial_no,b.reversal_of,now())).fetchone()
   return row
 @app.get('/v1/goods-movement/documents')
 def docs(authorization:str|None=Header(None)):
  auth('vector.inventory.read',authorization)
  with conn() as c:return c.execute('SELECT * FROM vector_material_documents ORDER BY posted_at DESC LIMIT 500').fetchall()
