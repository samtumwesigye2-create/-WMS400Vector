import json, os, urllib.request
from datetime import datetime, timezone
from uuid import uuid4
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from app import auth, conn

router=APIRouter(prefix="/v1/warehouse-performance",tags=["Warehouse Performance"])
NOVA_BASE_URL=os.getenv("NOVA_BASE_URL","https://ung-nova-production.up.railway.app").rstrip("/")

def now(): return datetime.now(timezone.utc)

def ensure():
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS vector_pick_events(
          id UUID PRIMARY KEY,order_ref TEXT NOT NULL,sku TEXT NOT NULL,location_code TEXT NOT NULL,
          expected_qty DOUBLE PRECISION NOT NULL,picked_qty DOUBLE PRECISION NOT NULL,
          started_at TIMESTAMPTZ NOT NULL,completed_at TIMESTAMPTZ NOT NULL,
          error BOOLEAN NOT NULL,created_at TIMESTAMPTZ NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_throughput_events(
          id UUID PRIMARY KEY,location_code TEXT NOT NULL,stage TEXT NOT NULL,
          units DOUBLE PRECISION NOT NULL,duration_minutes DOUBLE PRECISION NOT NULL,
          occurred_at TIMESTAMPTZ NOT NULL)""")

class PickEvent(BaseModel):
    order_ref:str
    sku:str
    location_code:str
    expected_qty:float=Field(gt=0)
    picked_qty:float=Field(ge=0)
    started_at:datetime
    completed_at:datetime

class ThroughputEvent(BaseModel):
    location_code:str
    stage:str
    units:float=Field(ge=0)
    duration_minutes:float=Field(gt=0)
    occurred_at:datetime|None=None

@router.post("/pick-events",status_code=201)
def record_pick(b:PickEvent,authorization:str|None=Header(None)):
    auth("vector.movements.write",authorization);ensure()
    if b.completed_at<b.started_at: raise HTTPException(422,"completed_before_started")
    err=abs(b.expected_qty-b.picked_qty)>1e-9
    with conn() as c:return c.execute("""INSERT INTO vector_pick_events
      VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
      (str(uuid4()),b.order_ref,b.sku,b.location_code,b.expected_qty,b.picked_qty,b.started_at,b.completed_at,err,now())).fetchone()

@router.post("/throughput-events",status_code=201)
def record_throughput(b:ThroughputEvent,authorization:str|None=Header(None)):
    auth("vector.movements.write",authorization);ensure()
    with conn() as c:return c.execute("""INSERT INTO vector_throughput_events
      VALUES(%s,%s,%s,%s,%s,%s) RETURNING *""",
      (str(uuid4()),b.location_code,b.stage,b.units,b.duration_minutes,b.occurred_at or now())).fetchone()

def snapshot():
    ensure();out=[]
    with conn() as c:
        p=c.execute("""SELECT COUNT(*) n,COUNT(*) FILTER(WHERE error=FALSE) ok
          FROM vector_pick_events WHERE completed_at>=NOW()-INTERVAL '90 days'""").fetchone()
        if p["n"]: out.append(("warehouse_picking_accuracy",100*p["ok"]/p["n"]))
        t=c.execute("""SELECT COALESCE(SUM(units),0)::double precision units,
          COALESCE(SUM(duration_minutes),0)::double precision mins
          FROM vector_throughput_events WHERE occurred_at>=NOW()-INTERVAL '30 days'""").fetchone()
        if t["mins"]>0: out.append(("warehouse_throughput_rate",t["units"]/(t["mins"]/60.0)))
        inv=c.execute("SELECT COALESCE(SUM(quantity),0)::double precision q FROM vector_inventory").fetchone()["q"]
        usage=c.execute("""SELECT COALESCE(SUM(quantity),0)::double precision q FROM vector_movements
          WHERE movement_type='dispatch' AND created_at>=NOW()-INTERVAL '365 days'""").fetchone()["q"]
        avg_inv=max(float(inv),1.0)
        if usage>0: out.append(("inventory_turnover_ratio",float(usage)/avg_inv))
        rr=c.execute("""SELECT COUNT(*) n,AVG(EXTRACT(EPOCH FROM(updated_at-created_at))/60.0) mins
          FROM vector_returns WHERE stage='routed'""").fetchone()
        if rr["n"] and rr["mins"] is not None: out.append(("return_processing_time",float(rr["mins"])))
        returns=c.execute("SELECT COUNT(*) n FROM vector_returns").fetchone()["n"]
        dispatches=c.execute("SELECT COUNT(*) n FROM vector_movements WHERE movement_type='dispatch'").fetchone()["n"]
        if dispatches: out.append(("return_rate",100*returns/dispatches))
    return out

@router.get("/snapshot")
def kpis(authorization:str|None=Header(None)):
    auth("vector.inventory.read",authorization)
    return {"source_system":"UNG-VECTOR","observations":[{"kpi_key":k,"value":round(v,4)} for k,v in snapshot()],"generated_at":now()}

@router.post("/publish")
def publish(authorization:str|None=Header(None)):
    auth("vector.inventory.read",authorization);vals=snapshot()
    body={"observations":[{"kpi_key":k,"value":v,"source_system":"UNG-VECTOR","measured_at":now().isoformat()} for k,v in vals]}
    if not vals:return {"status":"no-source-data","published":0}
    req=urllib.request.Request(NOVA_BASE_URL+"/v1/supply-chain/observations/bulk",data=json.dumps(body).encode(),method="POST",headers={"Content-Type":"application/json","X-UNG-Permissions":"nova.datasets.write","User-Agent":"UNG-VECTOR/0.7"})
    try:
        with urllib.request.urlopen(req,timeout=8) as r:return {"status":"published","published":len(vals),"nova_status":r.status}
    except Exception as e:raise HTTPException(502,f"nova_publish_failed:{type(e).__name__}")
