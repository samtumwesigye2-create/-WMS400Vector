from datetime import date, datetime, timedelta, timezone
from uuid import uuid4
import json
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix='/v1/traceability', tags=['traceability'])

def now():
    return datetime.now(timezone.utc)

def validate_trace_unit(batch_managed, serial_managed, lot_code, serial_numbers, quantity, manufacture_date, expiration_date):
    if quantity <= 0:
        raise ValueError('quantity_must_be_positive')
    if batch_managed and not lot_code:
        raise ValueError('lot_code_required')
    serial_numbers = serial_numbers or []
    if serial_managed and len(serial_numbers) != quantity:
        raise ValueError('serial_count_must_equal_quantity')
    if len(serial_numbers) != len(set(serial_numbers)):
        raise ValueError('duplicate_serial_number')
    if manufacture_date and expiration_date and expiration_date < manufacture_date:
        raise ValueError('expiration_before_manufacture')
    return True

def recall_scope_matches(row, sku=None, lot_code=None, serial_number=None):
    if sku is not None and row.get('sku') != sku:
        return False
    if lot_code is not None and row.get('lot_code') != lot_code:
        return False
    if serial_number is not None and row.get('serial_number') != serial_number:
        return False
    return any(v is not None for v in (sku, lot_code, serial_number))

class LotIn(BaseModel):
    sku: str
    lot_code: str
    manufacture_date: date | None = None
    expiration_date: date | None = None
    supplier_lot: str | None = None

class SerialIn(BaseModel):
    sku: str
    serial_number: str
    lot_code: str | None = None
    location_code: str | None = None
    expiration_date: date | None = None
    reference: str | None = None

class TraceEventIn(BaseModel):
    event_type: str
    sku: str
    lot_code: str | None = None
    serial_number: str | None = None
    location_code: str | None = None
    reference: str | None = None
    metadata: dict = {}

class RecallIn(BaseModel):
    recall_code: str
    reason: str
    sku: str | None = None
    lot_code: str | None = None
    serial_number: str | None = None

class RecallAction(BaseModel):
    action: str


def init_traceability(conn):
    with conn() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS vector_lots(
          id UUID PRIMARY KEY, sku TEXT NOT NULL, lot_code TEXT NOT NULL,
          manufacture_date DATE NULL, expiration_date DATE NULL, supplier_lot TEXT NULL,
          status TEXT NOT NULL DEFAULT 'active', created_at TIMESTAMPTZ NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL, UNIQUE(sku,lot_code))''')
        c.execute('CREATE INDEX IF NOT EXISTS ix_vector_lots_expiration ON vector_lots(expiration_date)')
        c.execute('''CREATE TABLE IF NOT EXISTS vector_serials(
          id UUID PRIMARY KEY, sku TEXT NOT NULL, serial_number TEXT UNIQUE NOT NULL,
          lot_code TEXT NULL, location_code TEXT NULL, expiration_date DATE NULL,
          status TEXT NOT NULL DEFAULT 'available', reference TEXT NULL,
          created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL)''')
        c.execute('CREATE INDEX IF NOT EXISTS ix_vector_serials_lot ON vector_serials(sku,lot_code)')
        c.execute('''CREATE TABLE IF NOT EXISTS vector_trace_events(
          sequence BIGSERIAL PRIMARY KEY, id UUID UNIQUE NOT NULL, event_type TEXT NOT NULL,
          sku TEXT NOT NULL, lot_code TEXT NULL, serial_number TEXT NULL,
          location_code TEXT NULL, reference TEXT NULL, metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at TIMESTAMPTZ NOT NULL)''')
        c.execute('CREATE INDEX IF NOT EXISTS ix_vector_trace_lookup ON vector_trace_events(sku,lot_code,serial_number,created_at DESC)')
        c.execute('''CREATE TABLE IF NOT EXISTS vector_recalls(
          id UUID PRIMARY KEY, recall_code TEXT UNIQUE NOT NULL, reason TEXT NOT NULL,
          sku TEXT NULL, lot_code TEXT NULL, serial_number TEXT NULL,
          status TEXT NOT NULL DEFAULT 'active', created_at TIMESTAMPTZ NOT NULL,
          closed_at TIMESTAMPTZ NULL)''')
        c.execute('''CREATE OR REPLACE FUNCTION vector_trace_immutable() RETURNS trigger AS $$
          BEGIN RAISE EXCEPTION 'vector_trace_events_are_append_only'; END; $$ LANGUAGE plpgsql''')
        c.execute('DROP TRIGGER IF EXISTS trg_vector_trace_immutable ON vector_trace_events')
        c.execute('''CREATE TRIGGER trg_vector_trace_immutable BEFORE UPDATE OR DELETE ON vector_trace_events
          FOR EACH ROW EXECUTE FUNCTION vector_trace_immutable()''')

def _trace(c, event_type, sku, lot_code=None, serial_number=None, location_code=None, reference=None, metadata=None):
    return c.execute('''INSERT INTO vector_trace_events
      (id,event_type,sku,lot_code,serial_number,location_code,reference,metadata,created_at)
      VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s) RETURNING *''',
      (str(uuid4()),event_type,sku,lot_code,serial_number,location_code,reference,json.dumps(metadata or {}),now())).fetchone()

def install_traceability_routes(app, conn, auth):
    @router.get('/lots')
    def lots(sku: str | None=None, authorization: str | None=Header(None)):
        auth('vector.inventory.read', authorization)
        with conn() as c:
            if sku: return c.execute('SELECT * FROM vector_lots WHERE sku=%s ORDER BY created_at DESC',(sku,)).fetchall()
            return c.execute('SELECT * FROM vector_lots ORDER BY created_at DESC').fetchall()

    @router.post('/lots', status_code=201)
    def create_lot(b: LotIn, authorization: str | None=Header(None)):
        auth('vector.inventory.write', authorization)
        try: validate_trace_unit(True,False,b.lot_code,[],1,b.manufacture_date,b.expiration_date)
        except ValueError as e: raise HTTPException(400,str(e))
        t=now()
        with conn() as c:
            try:
                row=c.execute('''INSERT INTO vector_lots(id,sku,lot_code,manufacture_date,expiration_date,supplier_lot,status,created_at,updated_at)
                  VALUES(%s,%s,%s,%s,%s,%s,'active',%s,%s) RETURNING *''',
                  (str(uuid4()),b.sku,b.lot_code,b.manufacture_date,b.expiration_date,b.supplier_lot,t,t)).fetchone()
            except Exception as e:
                if 'unique' in str(e).lower(): raise HTTPException(409,'lot_already_exists')
                raise
            _trace(c,'lot.created',b.sku,b.lot_code,reference=b.supplier_lot,metadata={'expiration_date':str(b.expiration_date) if b.expiration_date else None})
            return row

    @router.get('/serials')
    def serials(sku: str | None=None, lot_code: str | None=None, authorization: str | None=Header(None)):
        auth('vector.inventory.read', authorization)
        with conn() as c:
            if sku and lot_code: return c.execute('SELECT * FROM vector_serials WHERE sku=%s AND lot_code=%s ORDER BY created_at DESC',(sku,lot_code)).fetchall()
            if sku: return c.execute('SELECT * FROM vector_serials WHERE sku=%s ORDER BY created_at DESC',(sku,)).fetchall()
            return c.execute('SELECT * FROM vector_serials ORDER BY created_at DESC').fetchall()

    @router.post('/serials', status_code=201)
    def create_serial(b: SerialIn, authorization: str | None=Header(None)):
        auth('vector.inventory.write', authorization); t=now()
        with conn() as c:
            if b.lot_code:
                lot=c.execute('SELECT * FROM vector_lots WHERE sku=%s AND lot_code=%s',(b.sku,b.lot_code)).fetchone()
                if not lot: raise HTTPException(404,'lot_not_found')
                if lot['status']=='recalled': raise HTTPException(409,'lot_recalled')
            try:
                row=c.execute('''INSERT INTO vector_serials(id,sku,serial_number,lot_code,location_code,expiration_date,status,reference,created_at,updated_at)
                  VALUES(%s,%s,%s,%s,%s,%s,'available',%s,%s,%s) RETURNING *''',
                  (str(uuid4()),b.sku,b.serial_number,b.lot_code,b.location_code,b.expiration_date,b.reference,t,t)).fetchone()
            except Exception as e:
                if 'unique' in str(e).lower(): raise HTTPException(409,'serial_already_exists')
                raise
            _trace(c,'serial.created',b.sku,b.lot_code,b.serial_number,b.location_code,b.reference)
            return row

    @router.post('/events', status_code=201)
    def append_event(b: TraceEventIn, authorization: str | None=Header(None)):
        auth('vector.movements.write', authorization)
        with conn() as c: return _trace(c,b.event_type,b.sku,b.lot_code,b.serial_number,b.location_code,b.reference,b.metadata)

    @router.get('/trace')
    def trace(sku: str | None=None, lot_code: str | None=None, serial_number: str | None=None, authorization: str | None=Header(None)):
        auth('vector.movements.read', authorization)
        if not any((sku,lot_code,serial_number)): raise HTTPException(400,'trace_scope_required')
        clauses=[]; params=[]
        for col,val in [('sku',sku),('lot_code',lot_code),('serial_number',serial_number)]:
            if val is not None: clauses.append(f'{col}=%s'); params.append(val)
        with conn() as c:
            return c.execute('SELECT * FROM vector_trace_events WHERE '+' AND '.join(clauses)+' ORDER BY sequence',tuple(params)).fetchall()

    @router.get('/expiring')
    def expiring(days: int=30, authorization: str | None=Header(None)):
        auth('vector.inventory.read', authorization)
        days=max(0,min(days,3650)); cutoff=date.today()+timedelta(days=days)
        with conn() as c:
            return {
              'lots':c.execute("SELECT * FROM vector_lots WHERE expiration_date IS NOT NULL AND expiration_date<=%s AND status NOT IN ('recalled','expired') ORDER BY expiration_date",(cutoff,)).fetchall(),
              'serials':c.execute("SELECT * FROM vector_serials WHERE expiration_date IS NOT NULL AND expiration_date<=%s AND status NOT IN ('recalled','expired') ORDER BY expiration_date",(cutoff,)).fetchall(),
              'cutoff':cutoff}

    @router.get('/recalls')
    def recalls(authorization: str | None=Header(None)):
        auth('vector.inventory.read', authorization)
        with conn() as c:return c.execute('SELECT * FROM vector_recalls ORDER BY created_at DESC').fetchall()

    @router.post('/recalls', status_code=201)
    def create_recall(b: RecallIn, authorization: str | None=Header(None)):
        auth('vector.inventory.write', authorization)
        if not any((b.sku,b.lot_code,b.serial_number)): raise HTTPException(400,'recall_scope_required')
        t=now()
        with conn() as c:
            try:
                row=c.execute('''INSERT INTO vector_recalls(id,recall_code,reason,sku,lot_code,serial_number,status,created_at)
                  VALUES(%s,%s,%s,%s,%s,%s,'active',%s) RETURNING *''',
                  (str(uuid4()),b.recall_code,b.reason,b.sku,b.lot_code,b.serial_number,t)).fetchone()
            except Exception as e:
                if 'unique' in str(e).lower(): raise HTTPException(409,'recall_already_exists')
                raise
            if b.lot_code:
                params=[t,b.lot_code]; sql="UPDATE vector_lots SET status='recalled',updated_at=%s WHERE lot_code=%s"
                if b.sku: sql+=' AND sku=%s'; params.append(b.sku)
                c.execute(sql,tuple(params))
            if b.serial_number:
                params=[t,b.serial_number]; sql="UPDATE vector_serials SET status='recalled',updated_at=%s WHERE serial_number=%s"
                if b.sku: sql+=' AND sku=%s'; params.append(b.sku)
                c.execute(sql,tuple(params))
            elif b.sku:
                c.execute("UPDATE vector_serials SET status='recalled',updated_at=%s WHERE sku=%s",(t,b.sku))
                if not b.lot_code: c.execute("UPDATE vector_lots SET status='recalled',updated_at=%s WHERE sku=%s",(t,b.sku))
            _trace(c,'recall.opened',b.sku or '*',b.lot_code,b.serial_number,reference=b.recall_code,metadata={'reason':b.reason})
            return row

    @router.post('/recalls/{recall_code}/action')
    def recall_action(recall_code: str, b: RecallAction, authorization: str | None=Header(None)):
        auth('vector.inventory.write', authorization)
        if b.action!='close': raise HTTPException(400,'invalid_action')
        with conn() as c:
            r=c.execute('SELECT * FROM vector_recalls WHERE recall_code=%s FOR UPDATE',(recall_code,)).fetchone()
            if not r: raise HTTPException(404,'recall_not_found')
            if r['status']!='active': raise HTTPException(409,'recall_not_active')
            t=now(); row=c.execute("UPDATE vector_recalls SET status='closed',closed_at=%s WHERE id=%s RETURNING *",(t,r['id'])).fetchone()
            _trace(c,'recall.closed',r['sku'] or '*',r['lot_code'],r['serial_number'],reference=recall_code)
            return row

    app.include_router(router)
