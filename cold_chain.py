from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app import auth, conn

router = APIRouter(prefix='/v1/cold-chain', tags=['Cold Chain'])
STAGES = ['insulated_shipment','transport_unit','custody_check','delivery']


def now():
    return datetime.now(timezone.utc)


def ensure_schema():
    with conn() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS vector_cold_shipments(
            id UUID PRIMARY KEY,
            reference TEXT UNIQUE NOT NULL,
            sku TEXT,
            min_temp_c DOUBLE PRECISION NOT NULL DEFAULT 2,
            max_temp_c DOUBLE PRECISION NOT NULL DEFAULT 8,
            stage INTEGER NOT NULL DEFAULT 0,
            custody_ok BOOLEAN NOT NULL DEFAULT TRUE,
            status TEXT NOT NULL DEFAULT 'active',
            breach_count INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS vector_cold_readings(
            id UUID PRIMARY KEY,
            shipment_id UUID NOT NULL,
            source TEXT NOT NULL,
            temperature_c DOUBLE PRECISION NOT NULL,
            recorded_at TIMESTAMPTZ NOT NULL,
            in_range BOOLEAN NOT NULL,
            created_at TIMESTAMPTZ NOT NULL
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS vector_cold_events(
            id UUID PRIMARY KEY,
            shipment_id UUID NOT NULL,
            event_type TEXT NOT NULL,
            note TEXT,
            actor TEXT,
            created_at TIMESTAMPTZ NOT NULL
        )''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_vector_cold_readings_ship_time ON vector_cold_readings(shipment_id,recorded_at DESC)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_vector_cold_events_ship_time ON vector_cold_events(shipment_id,created_at DESC)')


class ShipmentIn(BaseModel):
    reference: str = Field(min_length=3, max_length=120)
    sku: str = Field(default='', max_length=120)
    min_temp_c: float = 2.0
    max_temp_c: float = 8.0


class ReadingIn(BaseModel):
    temperature_c: float = Field(ge=-120, le=200)
    source: str = Field(default='sensor', min_length=2, max_length=120)
    recorded_at: datetime | None = None


class ExceptionClearIn(BaseModel):
    note: str = Field(min_length=3, max_length=1000)


def _shipment(c, shipment_id: str, lock=False):
    sql = 'SELECT * FROM vector_cold_shipments WHERE id=%s' + (' FOR UPDATE' if lock else '')
    row = c.execute(sql, (shipment_id,)).fetchone()
    if not row:
        raise HTTPException(404, 'cold_chain_shipment_not_found')
    return row


@router.post('/shipments', status_code=201)
def create_shipment(body: ShipmentIn, authorization: str | None = Header(None)):
    principal = auth('vector.movements.write', authorization)
    ensure_schema()
    if body.max_temp_c <= body.min_temp_c:
        raise HTTPException(422, 'max_temp_must_exceed_min_temp')
    ts = now()
    try:
        with conn() as c:
            row = c.execute('''INSERT INTO vector_cold_shipments(id,reference,sku,min_temp_c,max_temp_c,stage,custody_ok,status,breach_count,created_at,updated_at)
                VALUES(%s,%s,%s,%s,%s,0,TRUE,'active',0,%s,%s) RETURNING *''',
                (str(uuid4()), body.reference.strip(), body.sku.strip(), body.min_temp_c, body.max_temp_c, ts, ts)).fetchone()
            c.execute('INSERT INTO vector_cold_events VALUES(%s,%s,%s,%s,%s,%s)',
                (str(uuid4()), str(row['id']), 'shipment_created', 'cold-chain shipment opened', str(principal.get('id') or principal.get('sub') or ''), ts))
            return row
    except Exception as exc:
        if 'unique' in str(exc).lower():
            raise HTTPException(409, 'cold_chain_reference_exists')
        raise


@router.get('/shipments')
def list_shipments(status: str | None = None, authorization: str | None = Header(None)):
    auth('vector.inventory.read', authorization)
    ensure_schema()
    with conn() as c:
        if status:
            return c.execute('SELECT * FROM vector_cold_shipments WHERE status=%s ORDER BY updated_at DESC', (status,)).fetchall()
        return c.execute('SELECT * FROM vector_cold_shipments ORDER BY updated_at DESC LIMIT 250').fetchall()


@router.get('/shipments/{shipment_id}')
def get_shipment(shipment_id: str, authorization: str | None = Header(None)):
    auth('vector.inventory.read', authorization)
    ensure_schema()
    with conn() as c:
        shipment = _shipment(c, shipment_id)
        readings = c.execute('SELECT * FROM vector_cold_readings WHERE shipment_id=%s ORDER BY recorded_at DESC LIMIT 12', (shipment_id,)).fetchall()
        events = c.execute('SELECT * FROM vector_cold_events WHERE shipment_id=%s ORDER BY created_at DESC LIMIT 100', (shipment_id,)).fetchall()
        return {'shipment': shipment, 'stage_name': STAGES[shipment['stage']], 'readings': readings, 'events': events}


@router.post('/shipments/{shipment_id}/readings', status_code=201)
def add_reading(shipment_id: str, body: ReadingIn, authorization: str | None = Header(None)):
    principal = auth('vector.movements.write', authorization)
    ensure_schema()
    recorded = body.recorded_at or now()
    with conn() as c:
        shipment = _shipment(c, shipment_id, lock=True)
        if shipment['status'] != 'active':
            raise HTTPException(409, 'cold_chain_shipment_not_active')
        in_range = shipment['min_temp_c'] <= body.temperature_c <= shipment['max_temp_c']
        reading = c.execute('''INSERT INTO vector_cold_readings(id,shipment_id,source,temperature_c,recorded_at,in_range,created_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
            (str(uuid4()), shipment_id, body.source.strip(), body.temperature_c, recorded, in_range, now())).fetchone()
        if not in_range:
            c.execute('UPDATE vector_cold_shipments SET custody_ok=FALSE,breach_count=breach_count+1,updated_at=%s WHERE id=%s', (now(), shipment_id))
            c.execute('INSERT INTO vector_cold_events VALUES(%s,%s,%s,%s,%s,%s)',
                (str(uuid4()), shipment_id, 'temperature_breach', f'{body.temperature_c}C outside {shipment["min_temp_c"]}-{shipment["max_temp_c"]}C', str(principal.get('id') or principal.get('sub') or ''), now()))
        return {'reading': reading, 'custody_ok': in_range and shipment['custody_ok'], 'breach': not in_range}


@router.post('/shipments/{shipment_id}/advance')
def advance_shipment(shipment_id: str, authorization: str | None = Header(None)):
    principal = auth('vector.movements.write', authorization)
    ensure_schema()
    with conn() as c:
        shipment = _shipment(c, shipment_id, lock=True)
        stage = int(shipment['stage'])
        if shipment['status'] != 'active':
            raise HTTPException(409, 'cold_chain_shipment_not_active')
        if stage >= len(STAGES) - 1:
            return {'shipment': shipment, 'stage_name': STAGES[stage]}
        if stage == 2 and not shipment['custody_ok']:
            raise HTTPException(409, 'custody_breach_must_be_documented_before_delivery')
        new_stage = stage + 1
        status = 'delivered' if new_stage == len(STAGES) - 1 else 'active'
        row = c.execute('UPDATE vector_cold_shipments SET stage=%s,status=%s,updated_at=%s WHERE id=%s RETURNING *', (new_stage,status,now(),shipment_id)).fetchone()
        c.execute('INSERT INTO vector_cold_events VALUES(%s,%s,%s,%s,%s,%s)',
            (str(uuid4()), shipment_id, 'stage_advanced', STAGES[new_stage], str(principal.get('id') or principal.get('sub') or ''), now()))
        return {'shipment': row, 'stage_name': STAGES[new_stage]}


@router.post('/shipments/{shipment_id}/document-exception')
def document_exception(shipment_id: str, body: ExceptionClearIn, authorization: str | None = Header(None)):
    principal = auth('vector.movements.write', authorization)
    ensure_schema()
    with conn() as c:
        shipment = _shipment(c, shipment_id, lock=True)
        if shipment['custody_ok']:
            raise HTTPException(409, 'no_open_custody_breach')
        row = c.execute('UPDATE vector_cold_shipments SET custody_ok=TRUE,updated_at=%s WHERE id=%s RETURNING *', (now(),shipment_id)).fetchone()
        c.execute('INSERT INTO vector_cold_events VALUES(%s,%s,%s,%s,%s,%s)',
            (str(uuid4()), shipment_id, 'custody_exception_documented', body.note.strip(), str(principal.get('id') or principal.get('sub') or ''), now()))
        return row


@router.get('/summary')
def summary(authorization: str | None = Header(None)):
    auth('vector.inventory.read', authorization)
    ensure_schema()
    with conn() as c:
        return {
            'active_shipments': c.execute("SELECT COUNT(*) n FROM vector_cold_shipments WHERE status='active'").fetchone()['n'],
            'breached_shipments': c.execute('SELECT COUNT(*) n FROM vector_cold_shipments WHERE custody_ok=FALSE').fetchone()['n'],
            'delivered_shipments': c.execute("SELECT COUNT(*) n FROM vector_cold_shipments WHERE status='delivered'").fetchone()['n'],
            'temperature_breaches': c.execute("SELECT COUNT(*) n FROM vector_cold_events WHERE event_type='temperature_breach'").fetchone()['n'],
            'generated_at': now(),
        }
