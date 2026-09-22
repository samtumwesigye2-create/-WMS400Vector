from datetime import date, datetime, timezone
from uuid import uuid4
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

router=APIRouter(prefix="/v1/workforce-capacity",tags=["Workforce Resource Planning"])

def now(): return datetime.now(timezone.utc)

class SkillIn(BaseModel):
    code:str
    description:str=""

class WorkerIn(BaseModel):
    worker_ref:str
    display_name:str
    skills:list[str]=[]
    active:bool=True

class ShiftIn(BaseModel):
    shift_code:str
    work_date:date
    work_center:str
    start_hour:int=Field(ge=0,le=23)
    end_hour:int=Field(ge=1,le=24)
    required_skill:str|None=None
    assigned_workers:list[str]=[]

def init_workforce_capacity(conn):
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS vector_workforce_skills(
          code TEXT PRIMARY KEY,description TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_workers(
          id UUID PRIMARY KEY,worker_ref TEXT UNIQUE NOT NULL,display_name TEXT NOT NULL,
          active BOOLEAN NOT NULL,created_at TIMESTAMPTZ NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_worker_skills(
          worker_id UUID NOT NULL REFERENCES vector_workers(id) ON DELETE CASCADE,
          skill_code TEXT NOT NULL REFERENCES vector_workforce_skills(code),PRIMARY KEY(worker_id,skill_code))""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_workforce_shifts(
          id UUID PRIMARY KEY,shift_code TEXT NOT NULL,work_date DATE NOT NULL,work_center TEXT NOT NULL,
          start_hour INTEGER NOT NULL,end_hour INTEGER NOT NULL,required_skill TEXT NULL,
          assigned_workers JSONB NOT NULL,status TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL,
          UNIQUE(shift_code,work_date,work_center))""")

def install_workforce_capacity_routes(app,conn,auth):
    @router.post("/skills",status_code=201)
    def create_skill(b:SkillIn,authorization:str|None=Header(None)):
        auth("vector.manufacturing.write",authorization)
        with conn() as c:return c.execute("""INSERT INTO vector_workforce_skills VALUES(%s,%s)
          ON CONFLICT(code) DO UPDATE SET description=EXCLUDED.description RETURNING *""",
          (b.code,b.description)).fetchone()

    @router.post("/workers",status_code=201)
    def create_worker(b:WorkerIn,authorization:str|None=Header(None)):
        auth("vector.manufacturing.write",authorization); t=now()
        with conn() as c:
            row=c.execute("""INSERT INTO vector_workers VALUES(%s,%s,%s,%s,%s)
              ON CONFLICT(worker_ref) DO UPDATE SET display_name=EXCLUDED.display_name,
              active=EXCLUDED.active RETURNING *""",
              (str(uuid4()),b.worker_ref,b.display_name,b.active,t)).fetchone()
            for skill in b.skills:
                if not c.execute("SELECT 1 FROM vector_workforce_skills WHERE code=%s",(skill,)).fetchone():
                    raise HTTPException(422,f"unknown_skill:{skill}")
                c.execute("INSERT INTO vector_worker_skills VALUES(%s,%s) ON CONFLICT DO NOTHING",(row["id"],skill))
        return row

    @router.put("/shifts")
    def upsert_shift(b:ShiftIn,authorization:str|None=Header(None)):
        auth("vector.manufacturing.write",authorization)
        if b.end_hour<=b.start_hour: raise HTTPException(422,"end_hour_must_be_after_start_hour")
        with conn() as c:
            if not c.execute("SELECT 1 FROM vector_work_centers WHERE code=%s",(b.work_center,)).fetchone():
                raise HTTPException(404,"work_center_not_found")
            for ref in b.assigned_workers:
                if not c.execute("SELECT 1 FROM vector_workers WHERE worker_ref=%s AND active=TRUE",(ref,)).fetchone():
                    raise HTTPException(422,f"worker_unavailable:{ref}")
            import json
            return c.execute("""INSERT INTO vector_workforce_shifts VALUES(
              %s,%s,%s,%s,%s,%s,%s,%s::jsonb,'scheduled',%s)
              ON CONFLICT(shift_code,work_date,work_center) DO UPDATE SET start_hour=EXCLUDED.start_hour,
              end_hour=EXCLUDED.end_hour,required_skill=EXCLUDED.required_skill,
              assigned_workers=EXCLUDED.assigned_workers,status='scheduled' RETURNING *""",
              (str(uuid4()),b.shift_code,b.work_date,b.work_center,b.start_hour,b.end_hour,
               b.required_skill,json.dumps(b.assigned_workers),now())).fetchone()

    @router.get("/availability")
    def availability(work_center:str,work_date:date,authorization:str|None=Header(None)):
        auth("vector.manufacturing.read",authorization)
        with conn() as c:
            shifts=c.execute("""SELECT * FROM vector_workforce_shifts WHERE work_center=%s AND work_date=%s
              ORDER BY start_hour""",(work_center,work_date)).fetchall()
        total_hours=0.0
        workers=set()
        for s in shifts:
            assigned=s["assigned_workers"] or []
            hours=max(0,int(s["end_hour"])-int(s["start_hour"]))
            total_hours+=hours*len(assigned)
            for w in assigned: workers.add(w)
        return {"work_center":work_center,"work_date":work_date,"labor_hours":round(total_hours,2),
          "unique_workers":len(workers),"shifts":shifts}

    @router.get("/skill-gaps")
    def skill_gaps(work_center:str,work_date:date,authorization:str|None=Header(None)):
        auth("vector.manufacturing.read",authorization)
        gaps=[]
        with conn() as c:
            shifts=c.execute("""SELECT * FROM vector_workforce_shifts WHERE work_center=%s AND work_date=%s""",
              (work_center,work_date)).fetchall()
            for s in shifts:
                req=s["required_skill"]
                if not req: continue
                assigned=s["assigned_workers"] or []
                qualified=0
                for ref in assigned:
                    row=c.execute("""SELECT 1 FROM vector_workers w JOIN vector_worker_skills ws ON ws.worker_id=w.id
                      WHERE w.worker_ref=%s AND ws.skill_code=%s AND w.active=TRUE""",(ref,req)).fetchone()
                    if row: qualified+=1
                if qualified==0:
                    gaps.append({"shift_code":s["shift_code"],"required_skill":req,"status":"uncovered"})
        return {"work_center":work_center,"work_date":work_date,"skill_gaps":gaps,"gap_count":len(gaps)}

    app.include_router(router)
