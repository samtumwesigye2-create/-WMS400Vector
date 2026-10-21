"""UNG-SCM V4 integration contract for VECTOR.
Generated 2026-09-25.
"""
SCM_VERSION="4.0"
SYSTEM="VECTOR"
ROLE="Owns supply-chain sustainability metrics, carbon/resource intensity, waste, route efficiency, multi-stop optimization, load consolidation, carrier history, predictive exceptions and AI-assisted transportation planning."
EVENT_ENDPOINT="/integrations/events"
SCM_CHAIN=["CAD/BOM","DEMAND","INVENTORY","PROCUREMENT","PRODUCTION","QC","FULFILLMENT","SHIPMENT"]
SUPPORTED_EVENTS=["CAD.BOM_RELEASED","CAD.REVISION_RELEASED","INVENTORY.UPDATED","INVENTORY.SHORTAGE","RFQ.CREATED","PO.CREATED","MATERIAL.RECEIVED","MFG.JOB_CREATED","MFG.JOB_STARTED","MFG.JOB_COMPLETED","MFG.QC_PASSED","MFG.QC_FAILED","SHIPMENT.DISPATCHED","SHIPMENT.DELIVERED","RISK.ALERT"]

def scm_event(event_type, entity_ref, payload=None):
    if event_type not in SUPPORTED_EVENTS:
        raise ValueError(f"Unsupported UNG-SCM event: {event_type}")
    return {"type":event_type,"source":f"UNG-{SYSTEM}","entity_ref":entity_ref,"payload":payload or {}}
