from datetime import datetime, timezone
from uuid import uuid4
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

router=APIRouter(prefix='/v1/inbound',tags=['inbound-receiving'])

def now(): return datetime.now(timezone.utc)

def validate_receipt_disposition(quantity, accepted, quarantined, damaged):
    values=[float(quantity),float(accepted),float(quarantined),float(damaged)]
    if any(v < 0 for v in values):
        raise ValueError('receipt_quantities_must_be_non_negative')
    if abs((values[1]+values[2]+values[3])-values[0]) > 1e-9:
        raise ValueError('receipt_disposition_must_equal_quantity')
    return True

FULFILLMENT_SEQUENCE=('allocate','pick','pack','stage')

def can_complete_task(sequence_no, prior_status):
    return sequence_no <= 1 or prior_status == 'completed'

class ExpectedPOIn(BaseModel):
    procure_order_id:str
    supplier_id:str
    sku:str
    ordered_quantity:float=Field(gt=0)
    expected_date:str|None=None

class ReceiptIn(BaseModel):
    quantity:float=Field(gt=0)
    location_code:str
    accepted_quantity:float|None=None
    quarantined_quantity:float=Field(default=0,ge=0)
    damaged_quantity:float=Field(default=0,ge=0)
    note:str=''

class InspectionDecisionIn(BaseModel):
    quantity:float=Field(gt=0)
    action:str
    note:str=''

class TaskTransitionIn(BaseModel):
    status:str
    target_location:str|None=None

def init_inbound_workflow(conn):
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS vector_expected_receipts(
          id UUID PRIMARY KEY,procure_order_id TEXT UNIQUE NOT NULL,supplier_id TEXT NOT NULL,sku TEXT NOT NULL,
          ordered_quantity NUMERIC NOT NULL CHECK(ordered_quantity>0),received_quantity NUMERIC NOT NULL DEFAULT 0,
          accepted_quantity NUMERIC NOT NULL DEFAULT 0,quarantined_quantity NUMERIC NOT NULL DEFAULT 0,
          damaged_quantity NUMERIC NOT NULL DEFAULT 0,expected_date DATE NULL,status TEXT NOT NULL DEFAULT 'expected',
          created_at TIMESTAMPTZ NOT NULL,updated_at TIMESTAMPTZ NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_receipt_events(
          id UUID PRIMARY KEY,expected_receipt_id UUID NOT NULL REFERENCES vector_expected_receipts(id) ON DELETE CASCADE,
          quantity NUMERIC NOT NULL CHECK(quantity>0),accepted_quantity NUMERIC NOT NULL DEFAULT 0,
          quarantined_quantity NUMERIC NOT NULL DEFAULT 0,damaged_quantity NUMERIC NOT NULL DEFAULT 0,
          location_code TEXT NOT NULL,note TEXT NOT NULL DEFAULT '',created_at TIMESTAMPTZ NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_receipt_discrepancies(
          id UUID PRIMARY KEY,expected_receipt_id UUID NOT NULL REFERENCES vector_expected_receipts(id) ON DELETE CASCADE,
          discrepancy_type TEXT NOT NULL,quantity NUMERIC NOT NULL DEFAULT 0,note TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'open',created_at TIMESTAMPTZ NOT NULL,resolved_at TIMESTAMPTZ NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_fulfillment_tasks(
          id UUID PRIMARY KEY,reference TEXT NOT NULL,task_type TEXT NOT NULL,sku TEXT NOT NULL,
          quantity NUMERIC NOT NULL CHECK(quantity>0),source_location TEXT NULL,target_location TEXT NULL,
          status TEXT NOT NULL DEFAULT 'open',sequence_no INTEGER NOT NULL,created_at TIMESTAMPTZ NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL,UNIQUE(reference,task_type,sku))""")

def _ensure_inventory(c,sku,loc):
    row=c.execute('SELECT * FROM vector_inventory WHERE sku=%s AND location_code=%s FOR UPDATE',(sku,loc)).fetchone()
    if not row:
        c.execute("INSERT INTO vector_inventory VALUES(%s,%s,%s,0,%s,'available',%s)",(str(uuid4()),sku,sku,loc,now()))
        row=c.execute('SELECT * FROM vector_inventory WHERE sku=%s AND location_code=%s FOR UPDATE',(sku,loc)).fetchone()
    return row

def install_inbound_workflow_routes(app,conn,auth,emit=None):
    def publish(target,message_type,payload):
        return emit(target,message_type,payload) if emit else {'status':'disabled'}

    @router.post('/expected',status_code=201)
    def expected(b:ExpectedPOIn,authorization:str|None=Header(None)):
        auth('vector.inventory.write',authorization);t=now()
        with conn() as c:
            row=c.execute("""INSERT INTO vector_expected_receipts
              (id,procure_order_id,supplier_id,sku,ordered_quantity,expected_date,status,created_at,updated_at)
              VALUES(%s,%s,%s,%s,%s,%s,'expected',%s,%s)
              ON CONFLICT(procure_order_id) DO UPDATE SET supplier_id=EXCLUDED.supplier_id,sku=EXCLUDED.sku,
              ordered_quantity=EXCLUDED.ordered_quantity,expected_date=EXCLUDED.expected_date,updated_at=EXCLUDED.updated_at
              RETURNING *""",(str(uuid4()),b.procure_order_id,b.supplier_id,b.sku,b.ordered_quantity,b.expected_date,t,t)).fetchone()
        return row

    @router.post('/{procure_order_id}/receive',status_code=201)
    def receive(procure_order_id:str,b:ReceiptIn,authorization:str|None=Header(None)):
        auth('vector.inventory.write',authorization);t=now()
        accepted=b.accepted_quantity if b.accepted_quantity is not None else b.quantity-b.quarantined_quantity-b.damaged_quantity
        try:
            validate_receipt_disposition(b.quantity,accepted,b.quarantined_quantity,b.damaged_quantity)
        except ValueError as e:
            raise HTTPException(422,str(e))
        with conn() as c:
            exp=c.execute('SELECT * FROM vector_expected_receipts WHERE procure_order_id=%s FOR UPDATE',(procure_order_id,)).fetchone()
            if not exp: raise HTTPException(404,'expected_receipt_not_found')
            if float(exp['received_quantity'])+b.quantity>float(exp['ordered_quantity']):
                raise HTTPException(409,'receipt_exceeds_order_quantity')
            _ensure_inventory(c,exp['sku'],b.location_code)
            c.execute('UPDATE vector_inventory SET quantity=quantity+%s,updated_at=%s WHERE sku=%s AND location_code=%s',
                      (b.quantity,t,exp['sku'],b.location_code))
            c.execute("""INSERT INTO vector_inventory_status(sku,location_code,reserved,quarantine,damaged,updated_at)
              VALUES(%s,%s,0,%s,%s,%s)
              ON CONFLICT(sku,location_code) DO UPDATE SET quarantine=vector_inventory_status.quarantine+EXCLUDED.quarantine,
              damaged=vector_inventory_status.damaged+EXCLUDED.damaged,updated_at=EXCLUDED.updated_at""",
              (exp['sku'],b.location_code,b.quarantined_quantity,b.damaged_quantity,t))
            ev=c.execute("""INSERT INTO vector_receipt_events
              (id,expected_receipt_id,quantity,accepted_quantity,quarantined_quantity,damaged_quantity,location_code,note,created_at)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
              (str(uuid4()),exp['id'],b.quantity,accepted,b.quarantined_quantity,b.damaged_quantity,b.location_code,b.note,t)).fetchone()
            new_received=float(exp['received_quantity'])+b.quantity
            status='received' if new_received>=float(exp['ordered_quantity']) else 'partially_received'
            updated=c.execute("""UPDATE vector_expected_receipts SET received_quantity=received_quantity+%s,
              accepted_quantity=accepted_quantity+%s,quarantined_quantity=quarantined_quantity+%s,
              damaged_quantity=damaged_quantity+%s,status=%s,updated_at=%s WHERE id=%s RETURNING *""",
              (b.quantity,accepted,b.quarantined_quantity,b.damaged_quantity,status,t,exp['id'])).fetchone()
            if b.quarantined_quantity>0:
                c.execute("""INSERT INTO vector_receipt_discrepancies
                  VALUES(%s,%s,'quality_quarantine',%s,%s,'open',%s,NULL)""",
                  (str(uuid4()),exp['id'],b.quarantined_quantity,b.note,t))
            if b.damaged_quantity>0:
                c.execute("""INSERT INTO vector_receipt_discrepancies
                  VALUES(%s,%s,'damaged',%s,%s,'open',%s,NULL)""",
                  (str(uuid4()),exp['id'],b.damaged_quantity,b.note,t))
        delivery=publish('UNG-PROCURE','VECTOR.GOODS.RECEIVED',{'procure_order_id':procure_order_id,'sku':updated['sku'],'received_quantity':float(b.quantity),'cumulative_received':float(updated['received_quantity']),'status':updated['status']})
        return {'receipt':ev,'expected_receipt':updated,'integration_delivery':delivery}

    @router.post('/{procure_order_id}/inspection')
    def inspect(procure_order_id:str,b:InspectionDecisionIn,authorization:str|None=Header(None)):
        auth('vector.quality.write',authorization)
        if b.action not in {'release','reject'}: raise HTTPException(422,'invalid_inspection_action')
        t=now()
        with conn() as c:
            exp=c.execute('SELECT * FROM vector_expected_receipts WHERE procure_order_id=%s FOR UPDATE',(procure_order_id,)).fetchone()
            if not exp: raise HTTPException(404,'expected_receipt_not_found')
            if float(exp['quarantined_quantity'])<b.quantity: raise HTTPException(409,'insufficient_quarantined_quantity')
            ev=c.execute('SELECT location_code FROM vector_receipt_events WHERE expected_receipt_id=%s AND quarantined_quantity>0 ORDER BY created_at DESC LIMIT 1',(exp['id'],)).fetchone()
            if not ev: raise HTTPException(409,'quarantine_location_not_found')
            if b.action=='release':
                c.execute('UPDATE vector_inventory_status SET quarantine=quarantine-%s,updated_at=%s WHERE sku=%s AND location_code=%s',(b.quantity,t,exp['sku'],ev['location_code']))
                c.execute('UPDATE vector_expected_receipts SET quarantined_quantity=quarantined_quantity-%s,accepted_quantity=accepted_quantity+%s,updated_at=%s WHERE id=%s',(b.quantity,b.quantity,t,exp['id']))
            else:
                c.execute('UPDATE vector_inventory_status SET quarantine=quarantine-%s,damaged=damaged+%s,updated_at=%s WHERE sku=%s AND location_code=%s',(b.quantity,b.quantity,t,exp['sku'],ev['location_code']))
                c.execute('UPDATE vector_expected_receipts SET quarantined_quantity=quarantined_quantity-%s,damaged_quantity=damaged_quantity+%s,updated_at=%s WHERE id=%s',(b.quantity,b.quantity,t,exp['id']))
            c.execute("UPDATE vector_receipt_discrepancies SET status='resolved',resolved_at=%s WHERE expected_receipt_id=%s AND discrepancy_type='quality_quarantine' AND status='open'",(t,exp['id']))
            row=c.execute('SELECT * FROM vector_expected_receipts WHERE id=%s',(exp['id'],)).fetchone()
        return {'expected_receipt':row,'action':b.action,'quantity':b.quantity,'note':b.note}

    @router.post('/fulfillment/{reference}/build',status_code=201)
    def build_fulfillment(reference:str,sku:str,quantity:float,location_code:str,authorization:str|None=Header(None)):
        auth('vector.warehouse.write',authorization)
        if quantity<=0: raise HTTPException(422,'quantity_must_be_positive')
        t=now();tasks=[]
        with conn() as c:
            inv=_ensure_inventory(c,sku,location_code)
            stat=c.execute('SELECT COALESCE(reserved,0) reserved,COALESCE(quarantine,0) quarantine,COALESCE(damaged,0) damaged FROM vector_inventory_status WHERE sku=%s AND location_code=%s',(sku,location_code)).fetchone() or {'reserved':0,'quarantine':0,'damaged':0}
            available=float(inv['quantity'])-float(stat['reserved'])-float(stat['quarantine'])-float(stat['damaged'])
            if available<quantity: raise HTTPException(409,'insufficient_available_inventory')
            for seq,kind in enumerate(FULFILLMENT_SEQUENCE,1):
                tasks.append(c.execute("""INSERT INTO vector_fulfillment_tasks
                  (id,reference,task_type,sku,quantity,source_location,target_location,status,sequence_no,created_at,updated_at)
                  VALUES(%s,%s,%s,%s,%s,%s,NULL,'open',%s,%s,%s)
                  ON CONFLICT(reference,task_type,sku) DO UPDATE SET quantity=EXCLUDED.quantity,updated_at=EXCLUDED.updated_at
                  RETURNING *""",(str(uuid4()),reference,kind,sku,quantity,location_code,seq,t,t)).fetchone())
        return {'reference':reference,'tasks':tasks}

    @router.post('/fulfillment/tasks/{task_id}/transition')
    def transition_task(task_id:str,b:TaskTransitionIn,authorization:str|None=Header(None)):
        auth('vector.warehouse.write',authorization)
        if b.status not in {'in_progress','completed','cancelled'}: raise HTTPException(422,'invalid_task_status')
        t=now()
        with conn() as c:
            task=c.execute('SELECT * FROM vector_fulfillment_tasks WHERE id=%s FOR UPDATE',(task_id,)).fetchone()
            if not task: raise HTTPException(404,'task_not_found')
            if task['status']=='completed': raise HTTPException(409,'task_already_completed')
            if b.status=='completed' and task['sequence_no']>1:
                prior=c.execute('SELECT 1 FROM vector_fulfillment_tasks WHERE reference=%s AND sku=%s AND sequence_no=%s AND status=%s',(task['reference'],task['sku'],task['sequence_no']-1,'completed')).fetchone()
                if not can_complete_task(task['sequence_no'],'completed' if prior else None): raise HTTPException(409,'prior_task_not_completed')
            row=c.execute('UPDATE vector_fulfillment_tasks SET status=%s,target_location=COALESCE(%s,target_location),updated_at=%s WHERE id=%s RETURNING *',(b.status,b.target_location,t,task_id)).fetchone()
            if b.status=='completed' and task['task_type']=='allocate':
                c.execute("""INSERT INTO vector_inventory_status(sku,location_code,reserved,quarantine,damaged,updated_at)
                  VALUES(%s,%s,%s,0,0,%s)
                  ON CONFLICT(sku,location_code) DO UPDATE SET reserved=vector_inventory_status.reserved+EXCLUDED.reserved,updated_at=EXCLUDED.updated_at""",
                  (task['sku'],task['source_location'],task['quantity'],t))
            if b.status=='completed' and task['task_type']=='stage':
                inv=_ensure_inventory(c,task['sku'],task['source_location'])
                if float(inv['quantity'])<float(task['quantity']): raise HTTPException(409,'insufficient_inventory')
                c.execute('UPDATE vector_inventory SET quantity=quantity-%s,updated_at=%s WHERE id=%s',(task['quantity'],t,inv['id']))
                c.execute('UPDATE vector_inventory_status SET reserved=GREATEST(0,reserved-%s),updated_at=%s WHERE sku=%s AND location_code=%s',(task['quantity'],t,task['sku'],task['source_location']))
        return row

    @router.get('/fulfillment/{reference}')
    def fulfillment(reference:str,authorization:str|None=Header(None)):
        auth('vector.warehouse.read',authorization)
        with conn() as c:
            return c.execute('SELECT * FROM vector_fulfillment_tasks WHERE reference=%s ORDER BY sequence_no',(reference,)).fetchall()

    @router.get('/{procure_order_id}')
    def receipt_status(procure_order_id:str,authorization:str|None=Header(None)):
        auth('vector.inventory.read',authorization)
        with conn() as c:
            exp=c.execute('SELECT * FROM vector_expected_receipts WHERE procure_order_id=%s',(procure_order_id,)).fetchone()
            if not exp: raise HTTPException(404,'expected_receipt_not_found')
            discrepancies=c.execute('SELECT * FROM vector_receipt_discrepancies WHERE expected_receipt_id=%s ORDER BY created_at',(exp['id'],)).fetchall()
            return {'expected_receipt':exp,'discrepancies':discrepancies}

    app.include_router(router)
