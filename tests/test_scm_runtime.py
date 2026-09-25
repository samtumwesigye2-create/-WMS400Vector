from fastapi.testclient import TestClient

def validate_scm_runtime(app):
    client=TestClient(app)
    event={"type":"INVENTORY.SHORTAGE","source":"UNG-SCM-ACCEPTANCE","entity_ref":"SCM-ACCEPT-001","payload":{"sku":"TEST-SKU","required":10,"available":2}}
    posted=client.post("/integrations/events",json=event)
    assert posted.status_code==202, posted.text
    body=posted.json()
    assert body["status"]=="accepted"
    fetched=client.get("/integrations/events?limit=100")
    assert fetched.status_code==200, fetched.text
    rows=fetched.json()["events"]
    assert any(x["event_id"]==body["event_id"] and x["type"]=="INVENTORY.SHORTAGE" for x in rows)
    contract=client.get("/integration-contract")
    assert contract.status_code==200
    assert "INVENTORY.SHORTAGE" in contract.json()["supported_events"]
    return {"status":"PASS","event_id":body["event_id"],"event":"INVENTORY.SHORTAGE"}
