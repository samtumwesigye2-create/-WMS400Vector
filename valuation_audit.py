from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix='/v1', tags=['valuation-audit'])

def now(): return datetime.now(timezone.utc)

class ValuationIn(BaseModel):
    sku: str
    unit_cost: Decimal
    currency: str = 'USD'
    valuation_method: str = 'moving_average'
    reference: str | None = None

class AuditIn(BaseModel):
    event_type: str
    sku: str | None = None
    location_code: str | None = None
    quantity: int | None = None
    reference: str | None = None
    source_system: str = 'UNG-VECTOR'
    metadata: dict = {}

class MidasEventIn(BaseModel):
    event_type: str
    sku: str
    quantity: int
    unit_cost: Decimal
    currency: str = 'USD'
    reference: str | None = None


def init_valuation_audit(conn):
    with conn() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS vector_valuations(
          id UUID PRIMARY KEY, sku TEXT NOT NULL, unit_cost NUMERIC(18,4) NOT NULL CHECK(unit_cost>=0),
          currency TEXT NOT NULL, valuation_method TEXT NOT NULL, reference TEXT NULL,
          effective_at TIMESTAMPTZ NOT NULL)''')
        c.execute('CREATE INDEX IF NOT EXISTS ix_vector_valuations_sku_time ON vector_valuations(sku,effective_at DESC)')
        c.execute('''CREATE TABLE IF NOT EXISTS vector_audit_ledger(
          sequence BIGSERIAL PRIMARY KEY, id UUID UNIQUE NOT NULL, event_type TEXT NOT NULL,
          sku TEXT NULL, location_code TEXT NULL, quantity INTEGER NULL, reference TEXT NULL,
          source_system TEXT NOT NULL, metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
          principal_id TEXT NULL, created_at TIMESTAMPTZ NOT NULL)''')
        c.execute('CREATE INDEX IF NOT EXISTS ix_vector_audit_time ON vector_audit_ledger(created_at DESC)')
        c.execute('''CREATE TABLE IF NOT EXISTS vector_midas_outbox(
          id UUID PRIMARY KEY, event_type TEXT NOT NULL, sku TEXT NOT NULL, quantity INTEGER NOT NULL,
          unit_cost NUMERIC(18,4) NOT NULL, currency TEXT NOT NULL, amount NUMERIC(18,4) NOT NULL,
          reference TEXT NULL, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
          created_at TIMESTAMPTZ NOT NULL, published_at TIMESTAMPTZ NULL)''')
        # Database-enforced append-only audit ledger. Even application bugs cannot UPDATE/DELETE history.
        c.execute('''CREATE OR REPLACE FUNCTION vector_audit_immutable() RETURNS trigger AS $$
          BEGIN RAISE EXCEPTION 'vector_audit_ledger_is_append_only'; END; $$ LANGUAGE plpgsql''')
        c.execute('DROP TRIGGER IF EXISTS trg_vector_audit_immutable ON vector_audit_ledger')
        c.execute('''CREATE TRIGGER trg_vector_audit_immutable BEFORE UPDATE OR DELETE ON vector_audit_ledger
          FOR EACH ROW EXECUTE FUNCTION vector_audit_immutable()''')


def install_valuation_audit_routes(app, conn, auth):
    @router.post('/valuations', status_code=201)
    def set_valuation(b: ValuationIn, authorization: str | None = Header(None)):
        p=auth('vector.inventory.write', authorization)
        if b.unit_cost < 0: raise HTTPException(400,'unit_cost_cannot_be_negative')
        if b.valuation_method not in {'moving_average','standard','fifo'}: raise HTTPException(400,'invalid_valuation_method')
        t=now(); pid=str(p.get('id') or p.get('sub') or p.get('user_id') or '') or None
        with conn() as c:
            row=c.execute('INSERT INTO vector_valuations VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *',
              (str(uuid4()),b.sku,b.unit_cost,b.currency.upper(),b.valuation_method,b.reference,t)).fetchone()
            c.execute('''INSERT INTO vector_audit_ledger(id,event_type,sku,reference,source_system,metadata,principal_id,created_at)
              VALUES(%s,'valuation.set',%s,%s,'UNG-VECTOR',jsonb_build_object('unit_cost',%s,'currency',%s,'method',%s),%s,%s)''',
              (str(uuid4()),b.sku,b.reference,str(b.unit_cost),b.currency.upper(),b.valuation_method,pid,t))
            return row

    @router.get('/valuations/{sku}')
    def valuation(sku: str, authorization: str | None = Header(None)):
        auth('vector.inventory.read', authorization)
        with conn() as c:
            row=c.execute('SELECT * FROM vector_valuations WHERE sku=%s ORDER BY effective_at DESC LIMIT 1',(sku,)).fetchone()
            if not row: raise HTTPException(404,'valuation_not_found')
            stock=c.execute('SELECT COALESCE(sum(quantity),0) q FROM vector_inventory WHERE sku=%s',(sku,)).fetchone()['q']
            return {**row,'on_hand':stock,'inventory_value':Decimal(stock)*row['unit_cost']}

    @router.get('/valuation-summary')
    def valuation_summary(authorization: str | None = Header(None)):
        auth('vector.inventory.read', authorization)
        with conn() as c:
            rows=c.execute('''SELECT i.sku,COALESCE(sum(i.quantity),0) on_hand,v.unit_cost,v.currency,v.valuation_method,
              COALESCE(sum(i.quantity),0)*v.unit_cost inventory_value
              FROM vector_inventory i JOIN LATERAL (SELECT unit_cost,currency,valuation_method FROM vector_valuations x
              WHERE x.sku=i.sku ORDER BY effective_at DESC LIMIT 1) v ON true
              GROUP BY i.sku,v.unit_cost,v.currency,v.valuation_method ORDER BY i.sku''').fetchall()
            return {'items':rows,'generated_at':now()}

    @router.post('/audit', status_code=201)
    def append_audit(b: AuditIn, authorization: str | None = Header(None)):
        p=auth('vector.movements.write', authorization); pid=str(p.get('id') or p.get('sub') or p.get('user_id') or '') or None
        with conn() as c:
            return c.execute('''INSERT INTO vector_audit_ledger(id,event_type,sku,location_code,quantity,reference,source_system,metadata,principal_id,created_at)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s) RETURNING *''',
              (str(uuid4()),b.event_type,b.sku,b.location_code,b.quantity,b.reference,b.source_system,__import__('json').dumps(b.metadata),pid,now())).fetchone()

    @router.get('/audit')
    def audit(limit: int=100, authorization: str | None = Header(None)):
        auth('vector.movements.read', authorization); limit=max(1,min(limit,500))
        with conn() as c:return c.execute('SELECT * FROM vector_audit_ledger ORDER BY sequence DESC LIMIT %s',(limit,)).fetchall()

    @router.post('/midas/outbox', status_code=201)
    def queue_midas(b: MidasEventIn, authorization: str | None = Header(None)):
        auth('vector.inventory.write', authorization)
        if b.quantity <= 0 or b.unit_cost < 0: raise HTTPException(400,'invalid_quantity_or_cost')
        amount=Decimal(b.quantity)*b.unit_cost; t=now()
        with conn() as c:
            row=c.execute('''INSERT INTO vector_midas_outbox(id,event_type,sku,quantity,unit_cost,currency,amount,reference,status,attempts,created_at)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'pending',0,%s) RETURNING *''',
              (str(uuid4()),b.event_type,b.sku,b.quantity,b.unit_cost,b.currency.upper(),amount,b.reference,t)).fetchone()
            c.execute('''INSERT INTO vector_audit_ledger(id,event_type,sku,quantity,reference,source_system,metadata,created_at)
              VALUES(%s,'midas.queued',%s,%s,%s,'UNG-VECTOR',jsonb_build_object('outbox_id',%s,'amount',%s,'currency',%s),%s)''',
              (str(uuid4()),b.sku,b.quantity,b.reference,str(row['id']),str(amount),b.currency.upper(),t))
            return row

    @router.get('/midas/outbox')
    def midas_outbox(status: str='pending', authorization: str | None = Header(None)):
        auth('vector.inventory.read', authorization)
        with conn() as c:return c.execute('SELECT * FROM vector_midas_outbox WHERE status=%s ORDER BY created_at',(status,)).fetchall()

    app.include_router(router)
