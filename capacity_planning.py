from datetime import date, datetime, timedelta, timezone
from fastapi import Header, HTTPException
from pydantic import BaseModel, Field

def _now(): return datetime.now(timezone.utc)

class CRPRequest(BaseModel):
    sku: str
    quantity: int = Field(gt=0)
    horizon_days: int = Field(default=1, ge=1, le=365)

class CapacityCalendarIn(BaseModel):
    work_center: str
    work_date: date
    available_hours: float = Field(ge=0)
    labor_hours: float = Field(default=0, ge=0)
    machine_hours: float = Field(default=0, ge=0)
    maintenance_hours: float = Field(default=0, ge=0)

class SetupTimeIn(BaseModel):
    sku: str
    operation_no: int = Field(gt=0)
    setup_minutes: float = Field(default=0, ge=0)

def capacity_result(required_hours, available_hours):
    required=float(required_hours); available=float(available_hours)
    gap=available-required
    util=(100.0*required/available) if available>0 else (100.0 if required==0 else 999.0)
    status='surplus' if gap>0 else ('balanced' if abs(gap)<1e-9 else 'shortage')
    return {
        'required_hours':round(required,2),
        'available_hours':round(available,2),
        'gap_hours':round(gap,2),
        'utilization_pct':round(util,2),
        'status':status
    }

def init_capacity_planning(conn):
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS vector_capacity_calendar(
          id BIGSERIAL PRIMARY KEY,work_center TEXT NOT NULL,work_date DATE NOT NULL,
          available_hours NUMERIC NOT NULL DEFAULT 0 CHECK(available_hours>=0),
          labor_hours NUMERIC NOT NULL DEFAULT 0 CHECK(labor_hours>=0),
          machine_hours NUMERIC NOT NULL DEFAULT 0 CHECK(machine_hours>=0),
          maintenance_hours NUMERIC NOT NULL DEFAULT 0 CHECK(maintenance_hours>=0),
          UNIQUE(work_center,work_date))""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_setup_times(
          id BIGSERIAL PRIMARY KEY,sku TEXT NOT NULL,operation_no INTEGER NOT NULL,
          setup_minutes NUMERIC NOT NULL DEFAULT 0 CHECK(setup_minutes>=0),
          UNIQUE(sku,operation_no))""")

def install_crp_routes(app, conn, auth):
    @app.put('/v1/manufacturing/capacity-calendar')
    def upsert_calendar(b:CapacityCalendarIn, authorization:str|None=Header(None)):
        auth('vector.manufacturing.write',authorization)
        with conn() as c:
            if not c.execute('SELECT 1 FROM vector_work_centers WHERE code=%s',(b.work_center,)).fetchone():
                raise HTTPException(404,'work_center_not_found')
            return c.execute("""INSERT INTO vector_capacity_calendar(
              work_center,work_date,available_hours,labor_hours,machine_hours,maintenance_hours)
              VALUES(%s,%s,%s,%s,%s,%s)
              ON CONFLICT(work_center,work_date) DO UPDATE SET
              available_hours=EXCLUDED.available_hours,labor_hours=EXCLUDED.labor_hours,
              machine_hours=EXCLUDED.machine_hours,maintenance_hours=EXCLUDED.maintenance_hours
              RETURNING *""",(b.work_center,b.work_date,b.available_hours,b.labor_hours,b.machine_hours,b.maintenance_hours)).fetchone()

    @app.put('/v1/manufacturing/setup-times')
    def upsert_setup_time(b:SetupTimeIn,authorization:str|None=Header(None)):
        auth('vector.manufacturing.write',authorization)
        with conn() as c:
            if not c.execute('SELECT 1 FROM vector_routings WHERE sku=%s AND operation_no=%s',(b.sku,b.operation_no)).fetchone():
                raise HTTPException(404,'routing_operation_not_found')
            return c.execute("""INSERT INTO vector_setup_times(sku,operation_no,setup_minutes)
              VALUES(%s,%s,%s) ON CONFLICT(sku,operation_no) DO UPDATE SET
              setup_minutes=EXCLUDED.setup_minutes RETURNING *""",(b.sku,b.operation_no,b.setup_minutes)).fetchone()

    @app.post('/v1/manufacturing/crp/calculate')
    def calculate_crp(b: CRPRequest, authorization: str | None = Header(None)):
        auth('vector.manufacturing.read', authorization)
        start=date.today(); end=start+timedelta(days=b.horizon_days-1)
        with conn() as c:
            ops=c.execute('SELECT operation_no,work_center,description,minutes_per_unit FROM vector_routings WHERE sku=%s ORDER BY operation_no',(b.sku,)).fetchall()
            if not ops: raise HTTPException(404,'routing_not_found')
            rows=[]; total_required=0.0; total_available=0.0
            for op in ops:
                wc=c.execute('SELECT code,name,capacity_per_day FROM vector_work_centers WHERE code=%s',(op['work_center'],)).fetchone()
                setup=c.execute('SELECT setup_minutes FROM vector_setup_times WHERE sku=%s AND operation_no=%s',(b.sku,op['operation_no'])).fetchone()
                setup_minutes=float(setup['setup_minutes']) if setup else 0.0
                runtime_hours=(float(op['minutes_per_unit'])*b.quantity)/60.0
                required_hours=runtime_hours+(setup_minutes/60.0)
                cal=c.execute("""SELECT COALESCE(SUM(GREATEST(available_hours-maintenance_hours,0)),0) available,
                  COALESCE(SUM(labor_hours),0) labor,COALESCE(SUM(machine_hours),0) machine,
                  COALESCE(SUM(maintenance_hours),0) maintenance
                  FROM vector_capacity_calendar WHERE work_center=%s AND work_date BETWEEN %s AND %s""",
                  (op['work_center'],start,end)).fetchone()
                configured=float(cal['available'])
                if configured<=0:
                    configured=float(wc['capacity_per_day'])*b.horizon_days if wc else 0.0
                labor=float(cal['labor'])
                if labor<=0:
                    wf=c.execute("""SELECT COALESCE(SUM((end_hour-start_hour)*jsonb_array_length(assigned_workers)),0) labor
                      FROM vector_workforce_shifts WHERE work_center=%s AND work_date BETWEEN %s AND %s
                      AND status='scheduled'""",(op['work_center'],start,end)).fetchone()
                    labor=float(wf['labor']) if wf else 0.0
                machine=float(cal['machine'])
                constraints=[x for x in [configured,labor if labor>0 else configured,machine if machine>0 else configured] if x>=0]
                available=min(constraints) if constraints else 0.0
                r=capacity_result(required_hours,available)
                bottleneck=None
                if r['status']=='shortage':
                    if labor>0 and labor<=available: bottleneck='labor'
                    elif machine>0 and machine<=available: bottleneck='machine'
                    else: bottleneck='calendar_or_work_center'
                rows.append({
                    'operation_no':op['operation_no'],'work_center':op['work_center'],'description':op['description'],
                    'runtime_hours':round(runtime_hours,2),'setup_hours':round(setup_minutes/60.0,2),
                    'labor_hours':round(labor,2),'machine_hours':round(machine,2),
                    'maintenance_hours':round(float(cal['maintenance']),2),'bottleneck':bottleneck,**r
                })
                total_required+=required_hours; total_available+=available
            overall=capacity_result(total_required,total_available)
            shortages=[x for x in rows if x['status']=='shortage']
            actions=[]
            if shortages:
                actions=['reschedule_load','use_alternate_work_center','add_shift_or_overtime','reduce_setup_time','subcontract_if_approved']
            else:
                actions=['release_feasible_plan']
            return {'sku':b.sku,'quantity':b.quantity,'horizon_days':b.horizon_days,
                    'window':{'start':start,'end':end},**overall,
                    'feasible':len(shortages)==0,'bottleneck_count':len(shortages),
                    'work_centers':rows,'recommended_actions':actions,'generated_at':_now()}

    @app.get('/v1/manufacturing/capacity/constraints')
    def capacity_constraints(days:int=7,authorization:str|None=Header(None)):
        auth('vector.manufacturing.read',authorization)
        if days<1 or days>365: raise HTTPException(422,'days_must_be_1_to_365')
        start=date.today(); end=start+timedelta(days=days-1)
        with conn() as c:
            rows=c.execute("""SELECT wc.code,wc.name,wc.capacity_per_day,
              COALESCE(SUM(cal.available_hours),0) calendar_hours,
              COALESCE(SUM(cal.labor_hours),0) labor_hours,
              COALESCE(SUM(cal.machine_hours),0) machine_hours,
              COALESCE(SUM(cal.maintenance_hours),0) maintenance_hours
              FROM vector_work_centers wc LEFT JOIN vector_capacity_calendar cal
              ON cal.work_center=wc.code AND cal.work_date BETWEEN %s AND %s
              GROUP BY wc.code,wc.name,wc.capacity_per_day ORDER BY wc.code""",(start,end)).fetchall()
        out=[]
        for r in rows:
            nominal=float(r['capacity_per_day'])*days
            calendar=max(0.0,float(r['calendar_hours'])-float(r['maintenance_hours'])) if float(r['calendar_hours'])>0 else nominal
            labor=float(r['labor_hours']) if float(r['labor_hours'])>0 else calendar
            machine=float(r['machine_hours']) if float(r['machine_hours'])>0 else calendar
            constrained=min(calendar,labor,machine)
            limiting='calendar'
            if labor<=constrained: limiting='labor'
            if machine<=constrained: limiting='machine'
            out.append({**r,'effective_available_hours':round(constrained,2),'limiting_resource':limiting})
        return {'window':{'start':start,'end':end},'work_centers':out,'generated_at':_now()}
