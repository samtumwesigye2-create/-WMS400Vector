from datetime import datetime, timezone
from uuid import uuid4
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix='/v1/inventory-control', tags=['inventory-control'])

def now():
    return datetime.now(timezone.utc)

class ReservationIn(BaseModel):
    reservation_code: str
    sku: str
    location_code: str
    quantity: int
    reference: str | None = None

class ReservationAction(BaseModel):
    action: str

class StockStatusChange(BaseModel):
    sku: str
    location_code: str
    quantity: int
    target_status: str
    reference: str | None = None

class TransitIn(BaseModel):
    transit_code: str
    sku: str
    quantity: int
    from_location: str
    to_location: str
    reference: str | None = None

class TransitAction(BaseModel):
    action: str


def init_inventory_control(conn):
    with conn() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS vector_inventory_status(
          sku TEXT NOT NULL, location_code TEXT NOT NULL,
          reserved INTEGER NOT NULL DEFAULT 0 CHECK(reserved>=0),
          quarantine INTEGER NOT NULL DEFAULT 0 CHECK(quarantine>=0),
          damaged INTEGER NOT NULL DEFAULT 0 CHECK(damaged>=0),
          updated_at TIMESTAMPTZ NOT NULL,
          PRIMARY KEY(sku, location_code))''')
        c.execute('''CREATE TABLE IF NOT EXISTS vector_reservations(
          id UUID PRIMARY KEY, reservation_code TEXT UNIQUE NOT NULL,
          sku TEXT NOT NULL, location_code TEXT NOT NULL,
          quantity INTEGER NOT NULL CHECK(quantity>0), status TEXT NOT NULL,
          reference TEXT NULL, created_at TIMESTAMPTZ NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS vector_stock_transit(
          id UUID PRIMARY KEY, transit_code TEXT UNIQUE NOT NULL,
          sku TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(quantity>0),
          from_location TEXT NOT NULL, to_location TEXT NOT NULL,
          status TEXT NOT NULL, reference TEXT NULL,
          created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL)''')


def _inventory_row(c, sku, location_code):
    return c.execute('SELECT id,quantity,description FROM vector_inventory WHERE sku=%s AND location_code=%s FOR UPDATE',
                     (sku, location_code)).fetchone()


def _status_row(c, sku, location_code):
    row=c.execute('SELECT * FROM vector_inventory_status WHERE sku=%s AND location_code=%s FOR UPDATE',
                  (sku, location_code)).fetchone()
    if not row:
        c.execute('INSERT INTO vector_inventory_status VALUES(%s,%s,0,0,0,%s)',(sku,location_code,now()))
        row=c.execute('SELECT * FROM vector_inventory_status WHERE sku=%s AND location_code=%s FOR UPDATE',
                      (sku, location_code)).fetchone()
    return row


def _available(inv, stat):
    return inv['quantity'] - stat['reserved'] - stat['quarantine'] - stat['damaged']


def install_inventory_control_routes(app, conn, auth):
    @router.get('/balances')
    def balances(authorization: str | None = Header(None)):
        auth('vector.inventory.read', authorization)
        with conn() as c:
            rows=c.execute('''SELECT i.sku,i.description,i.location_code,i.quantity AS on_hand,
              COALESCE(s.reserved,0) reserved,COALESCE(s.quarantine,0) quarantine,
              COALESCE(s.damaged,0) damaged,
              i.quantity-COALESCE(s.reserved,0)-COALESCE(s.quarantine,0)-COALESCE(s.damaged,0) available
              FROM vector_inventory i LEFT JOIN vector_inventory_status s
              ON s.sku=i.sku AND s.location_code=i.location_code
              ORDER BY i.sku,i.location_code''').fetchall()
            return rows

    @router.get('/reservations')
    def reservations(authorization: str | None = Header(None)):
        auth('vector.inventory.read', authorization)
        with conn() as c:
            return c.execute('SELECT * FROM vector_reservations ORDER BY created_at DESC').fetchall()

    @router.post('/reservations', status_code=201)
    def create_reservation(b: ReservationIn, authorization: str | None = Header(None)):
        auth('vector.inventory.write', authorization)
        if b.quantity <= 0: raise HTTPException(400,'quantity_must_be_positive')
        with conn() as c:
            inv=_inventory_row(c,b.sku,b.location_code)
            if not inv: raise HTTPException(404,'inventory_not_found')
            stat=_status_row(c,b.sku,b.location_code)
            if _available(inv,stat) < b.quantity: raise HTTPException(409,'insufficient_available_inventory')
            t=now()
            c.execute('UPDATE vector_inventory_status SET reserved=reserved+%s,updated_at=%s WHERE sku=%s AND location_code=%s',
                      (b.quantity,t,b.sku,b.location_code))
            try:
                return c.execute('''INSERT INTO vector_reservations
                  VALUES(%s,%s,%s,%s,%s,'active',%s,%s,%s) RETURNING *''',
                  (str(uuid4()),b.reservation_code,b.sku,b.location_code,b.quantity,b.reference,t,t)).fetchone()
            except Exception as e:
                if 'unique' in str(e).lower(): raise HTTPException(409,'reservation_already_exists')
                raise

    @router.post('/reservations/{reservation_code}/action')
    def reservation_action(reservation_code: str, b: ReservationAction, authorization: str | None = Header(None)):
        auth('vector.inventory.write', authorization)
        if b.action not in {'release','consume'}: raise HTTPException(400,'invalid_action')
        with conn() as c:
            r=c.execute('SELECT * FROM vector_reservations WHERE reservation_code=%s FOR UPDATE',(reservation_code,)).fetchone()
            if not r: raise HTTPException(404,'reservation_not_found')
            if r['status']!='active': raise HTTPException(409,'reservation_not_active')
            inv=_inventory_row(c,r['sku'],r['location_code'])
            if not inv: raise HTTPException(404,'inventory_not_found')
            _status_row(c,r['sku'],r['location_code'])
            t=now()
            c.execute('UPDATE vector_inventory_status SET reserved=reserved-%s,updated_at=%s WHERE sku=%s AND location_code=%s',
                      (r['quantity'],t,r['sku'],r['location_code']))
            if b.action=='consume':
                if inv['quantity'] < r['quantity']: raise HTTPException(409,'insufficient_inventory')
                c.execute('UPDATE vector_inventory SET quantity=quantity-%s,updated_at=%s WHERE id=%s',
                          (r['quantity'],t,inv['id']))
            return c.execute('UPDATE vector_reservations SET status=%s,updated_at=%s WHERE id=%s RETURNING *',
                             ('consumed' if b.action=='consume' else 'released',t,r['id'])).fetchone()

    @router.post('/stock-status', status_code=201)
    def change_stock_status(b: StockStatusChange, authorization: str | None = Header(None)):
        auth('vector.inventory.write', authorization)
        if b.quantity <= 0: raise HTTPException(400,'quantity_must_be_positive')
        if b.target_status not in {'quarantine','damaged','available'}: raise HTTPException(400,'invalid_target_status')
        with conn() as c:
            inv=_inventory_row(c,b.sku,b.location_code)
            if not inv: raise HTTPException(404,'inventory_not_found')
            stat=_status_row(c,b.sku,b.location_code)
            t=now()
            if b.target_status in {'quarantine','damaged'}:
                if _available(inv,stat) < b.quantity: raise HTTPException(409,'insufficient_available_inventory')
                c.execute(f'UPDATE vector_inventory_status SET {b.target_status}={b.target_status}+%s,updated_at=%s WHERE sku=%s AND location_code=%s',
                          (b.quantity,t,b.sku,b.location_code))
            else:
                source='quarantine' if stat['quarantine'] >= b.quantity else 'damaged'
                if stat[source] < b.quantity: raise HTTPException(409,'insufficient_restricted_inventory')
                c.execute(f'UPDATE vector_inventory_status SET {source}={source}-%s,updated_at=%s WHERE sku=%s AND location_code=%s',
                          (b.quantity,t,b.sku,b.location_code))
            row=c.execute('SELECT * FROM vector_inventory_status WHERE sku=%s AND location_code=%s',(b.sku,b.location_code)).fetchone()
            return {'sku':b.sku,'location_code':b.location_code,'status':row,'reference':b.reference}

    @router.get('/transit')
    def transit_list(authorization: str | None = Header(None)):
        auth('vector.inventory.read', authorization)
        with conn() as c:
            return c.execute('SELECT * FROM vector_stock_transit ORDER BY created_at DESC').fetchall()

    @router.post('/transit', status_code=201)
    def create_transit(b: TransitIn, authorization: str | None = Header(None)):
        auth('vector.inventory.write', authorization)
        if b.quantity <= 0: raise HTTPException(400,'quantity_must_be_positive')
        if b.from_location == b.to_location: raise HTTPException(400,'distinct_locations_required')
        with conn() as c:
            inv=_inventory_row(c,b.sku,b.from_location)
            if not inv: raise HTTPException(404,'inventory_not_found')
            stat=_status_row(c,b.sku,b.from_location)
            if _available(inv,stat) < b.quantity: raise HTTPException(409,'insufficient_available_inventory')
            t=now()
            c.execute('UPDATE vector_inventory SET quantity=quantity-%s,updated_at=%s WHERE id=%s',(b.quantity,t,inv['id']))
            try:
                return c.execute('''INSERT INTO vector_stock_transit
                  VALUES(%s,%s,%s,%s,%s,%s,'in_transit',%s,%s,%s) RETURNING *''',
                  (str(uuid4()),b.transit_code,b.sku,b.quantity,b.from_location,b.to_location,b.reference,t,t)).fetchone()
            except Exception as e:
                if 'unique' in str(e).lower(): raise HTTPException(409,'transit_already_exists')
                raise

    @router.post('/transit/{transit_code}/action')
    def transit_action(transit_code: str, b: TransitAction, authorization: str | None = Header(None)):
        auth('vector.inventory.write', authorization)
        if b.action not in {'receive','cancel'}: raise HTTPException(400,'invalid_action')
        with conn() as c:
            tr=c.execute('SELECT * FROM vector_stock_transit WHERE transit_code=%s FOR UPDATE',(transit_code,)).fetchone()
            if not tr: raise HTTPException(404,'transit_not_found')
            if tr['status']!='in_transit': raise HTTPException(409,'transit_not_active')
            t=now()
            target=tr['to_location'] if b.action=='receive' else tr['from_location']
            src=c.execute('SELECT description FROM vector_inventory WHERE sku=%s ORDER BY updated_at DESC LIMIT 1',(tr['sku'],)).fetchone()
            desc=src['description'] if src else tr['sku']
            c.execute("""INSERT INTO vector_inventory VALUES(%s,%s,%s,%s,%s,'available',%s)
              ON CONFLICT(sku,location_code) DO UPDATE SET quantity=vector_inventory.quantity+EXCLUDED.quantity,updated_at=EXCLUDED.updated_at""",
              (str(uuid4()),tr['sku'],desc,tr['quantity'],target,t))
            return c.execute('UPDATE vector_stock_transit SET status=%s,updated_at=%s WHERE id=%s RETURNING *',
                             ('received' if b.action=='receive' else 'cancelled',t,tr['id'])).fetchone()

    app.include_router(router)
