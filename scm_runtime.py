from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Any
from uuid import uuid4

router = APIRouter()
SCM_EVENTS = {
"CAD.BOM_RELEASED","CAD.REVISION_RELEASED","INVENTORY.UPDATED","INVENTORY.SHORTAGE",
"RFQ.CREATED","PO.CREATED","MATERIAL.RECEIVED","MFG.JOB_CREATED","MFG.JOB_STARTED",
"MFG.JOB_COMPLETED","MFG.QC_PASSED","MFG.QC_FAILED","SHIPMENT.DISPATCHED",
"SHIPMENT.DELIVERED","RISK.ALERT"
}
_recent=[]

class SCMEvent(BaseModel):
    type: str
    source: str
    entity_ref: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

@router.post("/integrations/events", status_code=202)
def receive_scm_event(event: SCMEvent):
    if event.type not in SCM_EVENTS:
        raise HTTPException(400, f"unsupported_scm_event:{event.type}")
    row={"event_id":str(uuid4()),"accepted_at":datetime.now(timezone.utc).isoformat(),**event.model_dump()}
    _recent.append(row)
    if len(_recent)>100: del _recent[:-100]
    return {"status":"accepted","event_id":row["event_id"],"type":event.type}

@router.get("/integrations/events")
def recent_scm_events(limit:int=20):
    limit=max(1,min(limit,100))
    return {"count":min(limit,len(_recent)),"events":_recent[-limit:]}

@router.get("/integration-contract")
def scm_contract():
    return {"version":"4.0","endpoint":"/integrations/events","supported_events":sorted(SCM_EVENTS)}
