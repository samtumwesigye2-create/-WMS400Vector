from datetime import datetime, timezone
from fastapi import Header, HTTPException
from pydantic import BaseModel, Field

def _now(): return datetime.now(timezone.utc)

class CRPRequest(BaseModel):
    sku: str
    quantity: int = Field(gt=0)

def install_crp_routes(app, conn, auth):
    @app.post('/v1/manufacturing/crp/calculate')
    def calculate_crp(b: CRPRequest, authorization: str | None = Header(None)):
        auth('vector.manufacturing.read', authorization)
        with conn() as c:
            ops=c.execute('SELECT operation_no,work_center,description,minutes_per_unit FROM vector_routings WHERE sku=%s ORDER BY operation_no',(b.sku,)).fetchall()
            if not ops: raise HTTPException(404,'routing_not_found')
            rows=[]; total_required_hours=0.0; total_available_hours=0.0
            for op in ops:
                wc=c.execute('SELECT code,name,capacity_per_day FROM vector_work_centers WHERE code=%s',(op['work_center'],)).fetchone()
                required_hours=(float(op['minutes_per_unit'])*b.quantity)/60.0
                available_hours=float(wc['capacity_per_day']) if wc else 0.0
                gap=available_hours-required_hours
                status='surplus' if gap>0 else ('balanced' if gap==0 else 'shortage')
                rows.append({'operation_no':op['operation_no'],'work_center':op['work_center'],'description':op['description'],'required_hours':round(required_hours,2),'available_hours':round(available_hours,2),'gap_hours':round(gap,2),'status':status})
                total_required_hours+=required_hours; total_available_hours+=available_hours
            total_gap=total_available_hours-total_required_hours
            actions=[]
            if total_gap<0: actions=['consider_overtime','reschedule_load','add_capacity','subcontract']
            elif total_gap>0: actions=['capacity_available']
            else: actions=['capacity_balanced']
            return {'sku':b.sku,'quantity':b.quantity,'required_hours':round(total_required_hours,2),'available_hours':round(total_available_hours,2),'gap_hours':round(total_gap,2),'status':'shortage' if total_gap<0 else ('surplus' if total_gap>0 else 'balanced'),'work_centers':rows,'recommended_actions':actions,'generated_at':_now()}
