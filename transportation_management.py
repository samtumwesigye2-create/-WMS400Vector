from datetime import datetime, timezone
from uuid import uuid4
from fastapi import Header, HTTPException
from pydantic import BaseModel, Field

def _now(): return datetime.now(timezone.utc)

def init_transportation(conn):
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS vector_transport_lanes(id UUID PRIMARY KEY,code TEXT UNIQUE NOT NULL,origin TEXT NOT NULL,destination TEXT NOT NULL,distance_km NUMERIC NOT NULL DEFAULT 0,active BOOLEAN NOT NULL DEFAULT TRUE)""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_carriers(id UUID PRIMARY KEY,code TEXT UNIQUE NOT NULL,name TEXT NOT NULL,rate_per_km NUMERIC NOT NULL DEFAULT 0,capacity_kg NUMERIC NOT NULL DEFAULT 0,on_time_pct NUMERIC NOT NULL DEFAULT 0,active BOOLEAN NOT NULL DEFAULT TRUE)""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_shipments(id UUID PRIMARY KEY,reference TEXT UNIQUE NOT NULL,origin TEXT NOT NULL,destination TEXT NOT NULL,total_weight_kg NUMERIC NOT NULL DEFAULT 0,status TEXT NOT NULL,lane_code TEXT NULL,carrier_code TEXT NULL,estimated_cost NUMERIC NOT NULL DEFAULT 0,tracking_event TEXT NULL,created_at TIMESTAMPTZ NOT NULL,updated_at TIMESTAMPTZ NOT NULL)""")

class LaneIn(BaseModel):
    code:str; origin:str; destination:str; distance_km:float=Field(ge=0)
class CarrierIn(BaseModel):
    code:str; name:str; rate_per_km:float=Field(ge=0); capacity_kg:float=Field(ge=0); on_time_pct:float=Field(default=0,ge=0,le=100)
class ShipmentIn(BaseModel):
    reference:str; origin:str; destination:str; total_weight_kg:float=Field(ge=0)
class TrackIn(BaseModel):
    event:str

def install_transportation_routes(app,conn,auth):
    @app.post('/v1/transport/lanes',status_code=201)
    def lane(b:LaneIn,authorization:str|None=Header(None)):
        auth('vector.transport.write',authorization)
        with conn() as c:return c.execute("INSERT INTO vector_transport_lanes VALUES(%s,%s,%s,%s,%s,TRUE) ON CONFLICT(code) DO UPDATE SET origin=EXCLUDED.origin,destination=EXCLUDED.destination,distance_km=EXCLUDED.distance_km,active=TRUE RETURNING *",(str(uuid4()),b.code,b.origin,b.destination,b.distance_km)).fetchone()

    @app.post('/v1/transport/carriers',status_code=201)
    def carrier(b:CarrierIn,authorization:str|None=Header(None)):
        auth('vector.transport.write',authorization)
        with conn() as c:return c.execute("INSERT INTO vector_carriers VALUES(%s,%s,%s,%s,%s,%s,TRUE) ON CONFLICT(code) DO UPDATE SET name=EXCLUDED.name,rate_per_km=EXCLUDED.rate_per_km,capacity_kg=EXCLUDED.capacity_kg,on_time_pct=EXCLUDED.on_time_pct,active=TRUE RETURNING *",(str(uuid4()),b.code,b.name,b.rate_per_km,b.capacity_kg,b.on_time_pct)).fetchone()

    @app.post('/v1/transport/shipments',status_code=201)
    def shipment(b:ShipmentIn,authorization:str|None=Header(None)):
        auth('vector.transport.write',authorization)
        t=_now()
        with conn() as c:return c.execute("INSERT INTO vector_shipments VALUES(%s,%s,%s,%s,%s,'planned',NULL,NULL,0,NULL,%s,%s) RETURNING *",(str(uuid4()),b.reference,b.origin,b.destination,b.total_weight_kg,t,t)).fetchone()

    @app.post('/v1/transport/shipments/{shipment_id}/optimize')
    def optimize(shipment_id:str,authorization:str|None=Header(None)):
        auth('vector.transport.write',authorization)
        with conn() as c:
            s=c.execute('SELECT * FROM vector_shipments WHERE id=%s',(shipment_id,)).fetchone()
            if not s: raise HTTPException(404,'shipment_not_found')
            lane=c.execute('SELECT * FROM vector_transport_lanes WHERE origin=%s AND destination=%s AND active=TRUE ORDER BY distance_km ASC LIMIT 1',(s['origin'],s['destination'])).fetchone()
            if not lane: raise HTTPException(409,'transport_lane_not_found')
            carriers=c.execute('SELECT * FROM vector_carriers WHERE active=TRUE AND capacity_kg>=%s ORDER BY rate_per_km ASC,on_time_pct DESC',(s['total_weight_kg'],)).fetchall()
            if not carriers: raise HTTPException(409,'carrier_capacity_unavailable')
            carrier=carriers[0]
            cost=float(lane['distance_km'])*float(carrier['rate_per_km'])
            return c.execute("UPDATE vector_shipments SET lane_code=%s,carrier_code=%s,estimated_cost=%s,status='optimized',updated_at=%s WHERE id=%s RETURNING *",(lane['code'],carrier['code'],cost,_now(),shipment_id)).fetchone()

    @app.post('/v1/transport/shipments/{shipment_id}/tender')
    def tender(shipment_id:str,authorization:str|None=Header(None)):
        auth('vector.transport.write',authorization)
        with conn() as c:
            row=c.execute("UPDATE vector_shipments SET status='tendered',updated_at=%s WHERE id=%s AND status='optimized' RETURNING *",(_now(),shipment_id)).fetchone()
            if not row: raise HTTPException(409,'shipment_not_optimized')
            return row

    @app.post('/v1/transport/shipments/{shipment_id}/dispatch')
    def dispatch(shipment_id:str,authorization:str|None=Header(None)):
        auth('vector.transport.write',authorization)
        with conn() as c:
            row=c.execute("UPDATE vector_shipments SET status='in_transit',tracking_event='dispatched',updated_at=%s WHERE id=%s AND status IN ('optimized','tendered') RETURNING *",(_now(),shipment_id)).fetchone()
            if not row: raise HTTPException(409,'shipment_not_ready')
            return row

    @app.post('/v1/transport/shipments/{shipment_id}/track')
    def track(shipment_id:str,b:TrackIn,authorization:str|None=Header(None)):
        auth('vector.transport.write',authorization)
        status='delivered' if b.event.lower()=='delivered' else 'in_transit'
        with conn() as c:
            row=c.execute("UPDATE vector_shipments SET tracking_event=%s,status=%s,updated_at=%s WHERE id=%s RETURNING *",(b.event,status,_now(),shipment_id)).fetchone()
            if not row: raise HTTPException(404,'shipment_not_found')
            return row

    @app.get('/v1/transport/shipments')
    def shipments(authorization:str|None=Header(None)):
        auth('vector.transport.read',authorization)
        with conn() as c:return c.execute('SELECT * FROM vector_shipments ORDER BY created_at DESC').fetchall()

    @app.get('/v1/transport/kpis')
    def kpis(authorization:str|None=Header(None)):
        auth('vector.transport.read',authorization)
        with conn() as c:
            total=c.execute('SELECT count(*) n FROM vector_shipments').fetchone()['n']
            delivered=c.execute("SELECT count(*) n FROM vector_shipments WHERE status='delivered'").fetchone()['n']
            cost=c.execute('SELECT COALESCE(sum(estimated_cost),0) n FROM vector_shipments').fetchone()['n']
            return {'shipments':total,'delivered':delivered,'delivery_rate_pct':round((delivered/total*100),2) if total else 0,'estimated_freight_cost':float(cost)}
