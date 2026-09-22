from fastapi import APIRouter, Header, HTTPException

router=APIRouter(prefix="/v1/simple",tags=["Simple Warehouse Operations"])

def install_simple_ops_routes(app,conn,auth):
    @router.get("/today")
    def today(authorization:str|None=Header(None)):
        auth("vector.inventory.read",authorization)
        with conn() as c:
            inbound=c.execute("""SELECT procure_order_id reference,sku,ordered_quantity,received_quantity,
              expected_date,status FROM vector_expected_receipts
              WHERE status IN ('expected','partially_received')
              ORDER BY expected_date NULLS LAST,created_at LIMIT 100""").fetchall()
            tasks=c.execute("""SELECT reference,task_type,sku,quantity,source_location,target_location,status,sequence_no
              FROM vector_fulfillment_tasks WHERE status IN ('open','in_progress')
              ORDER BY created_at,sequence_no LIMIT 200""").fetchall()
            shipments=c.execute("""SELECT id,reference,origin,destination,status,tracking_event,updated_at
              FROM vector_shipments WHERE status<>'delivered'
              ORDER BY updated_at DESC LIMIT 100""").fetchall()
            exceptions=c.execute("""SELECT id,shipment_id,exception_type,details,status,created_at
              FROM vector_transport_exceptions WHERE status='open'
              ORDER BY created_at DESC LIMIT 100""").fetchall()
            low_stock=c.execute("""SELECT m.sku,m.reorder_level,
              COALESCE((SELECT SUM(i.quantity) FROM vector_inventory i WHERE i.sku=m.sku),0) on_hand
              FROM vector_materials m
              WHERE m.reorder_level>0
              AND COALESCE((SELECT SUM(i.quantity) FROM vector_inventory i WHERE i.sku=m.sku),0)<m.reorder_level
              ORDER BY (m.reorder_level-COALESCE((SELECT SUM(i.quantity) FROM vector_inventory i WHERE i.sku=m.sku),0)) DESC
              LIMIT 100""").fetchall()
        return {
          "receipts_due":inbound,
          "fulfillment_tasks":tasks,
          "shipments_open":shipments,
          "transport_exceptions":exceptions,
          "low_stock":low_stock,
          "counts":{
            "receipts_due":len(inbound),
            "fulfillment_tasks":len(tasks),
            "shipments_open":len(shipments),
            "transport_exceptions":len(exceptions),
            "low_stock":len(low_stock)
          }
        }

    @router.get("/next-task")
    def next_task(authorization:str|None=Header(None)):
        auth("vector.inventory.read",authorization)
        with conn() as c:
            ex=c.execute("""SELECT id,shipment_id,exception_type,details,status,created_at
              FROM vector_transport_exceptions WHERE status='open'
              ORDER BY created_at LIMIT 1""").fetchone()
            if ex:
                return {"kind":"transport_exception","priority":"urgent","title":"Resolve shipping exception",
                        "summary":ex["exception_type"],"reference":str(ex["shipment_id"]),
                        "record":ex,"suggested_action":"Open the shipment and resolve the exception before continuing."}
            task=c.execute("""SELECT reference,task_type,sku,quantity,source_location,target_location,status,sequence_no
              FROM vector_fulfillment_tasks WHERE status IN ('open','in_progress')
              ORDER BY created_at,sequence_no LIMIT 1""").fetchone()
            if task:
                return {"kind":"fulfillment","priority":"high","title":"Continue fulfillment",
                        "summary":f'{task["task_type"].replace("_"," ").title()} {task["quantity"]} × {task["sku"]}',
                        "reference":task["reference"],"record":task,
                        "suggested_action":"Use Complete Next Fulfillment Step for this reference."}
            receipt=c.execute("""SELECT procure_order_id reference,sku,ordered_quantity,received_quantity,expected_date,status
              FROM vector_expected_receipts WHERE status IN ('expected','partially_received')
              ORDER BY expected_date NULLS LAST,created_at LIMIT 1""").fetchone()
            if receipt:
                remaining=float(receipt["ordered_quantity"])-float(receipt["received_quantity"])
                return {"kind":"receipt","priority":"normal","title":"Receive incoming stock",
                        "summary":f'{remaining:g} × {receipt["sku"]} remaining',
                        "reference":receipt["reference"],"record":receipt,
                        "suggested_action":"Receive the remaining quantity into its warehouse location."}
            low=c.execute("""SELECT m.sku,m.reorder_level,
              COALESCE((SELECT SUM(i.quantity) FROM vector_inventory i WHERE i.sku=m.sku),0) on_hand
              FROM vector_materials m WHERE m.reorder_level>0
              AND COALESCE((SELECT SUM(i.quantity) FROM vector_inventory i WHERE i.sku=m.sku),0)<m.reorder_level
              ORDER BY (m.reorder_level-COALESCE((SELECT SUM(i.quantity) FROM vector_inventory i WHERE i.sku=m.sku),0)) DESC
              LIMIT 1""").fetchone()
            if low:
                return {"kind":"low_stock","priority":"normal","title":"Replenish low stock",
                        "summary":f'{low["sku"]}: {low["on_hand"]} on hand / reorder at {low["reorder_level"]}',
                        "reference":low["sku"],"record":low,
                        "suggested_action":"Review replenishment for this SKU."}
            ship=c.execute("""SELECT id,reference,origin,destination,status,tracking_event,updated_at
              FROM vector_shipments WHERE status<>'delivered'
              ORDER BY updated_at ASC LIMIT 1""").fetchone()
            if ship:
                return {"kind":"shipment","priority":"normal","title":"Review open shipment",
                        "summary":f'{ship["reference"]}: {ship["origin"]} → {ship["destination"]}',
                        "reference":ship["reference"],"record":ship,
                        "suggested_action":"Update tracking or complete the next shipping action."}
        return {"kind":"none","priority":"none","title":"All clear","summary":"No warehouse work is waiting.",
                "reference":None,"record":None,"suggested_action":"No action needed right now."}

    @router.get("/find")
    def quick_find(q:str,authorization:str|None=Header(None)):
        auth("vector.inventory.read",authorization)
        q=(q or "").strip()
        if not q: raise HTTPException(422,"search_value_required")
        like=f"%{q}%"
        with conn() as c:
            inventory=c.execute("""SELECT sku,description,quantity,location_code,status
              FROM vector_inventory WHERE sku ILIKE %s OR description ILIKE %s OR location_code ILIKE %s
              ORDER BY sku,location_code LIMIT 50""",(like,like,like)).fetchall()
            shipments=c.execute("""SELECT id,reference,origin,destination,status,tracking_event,updated_at
              FROM vector_shipments WHERE reference ILIKE %s OR origin ILIKE %s OR destination ILIKE %s
              ORDER BY updated_at DESC LIMIT 50""",(like,like,like)).fetchall()
            fulfillment=c.execute("""SELECT reference,task_type,sku,quantity,source_location,target_location,status,sequence_no
              FROM vector_fulfillment_tasks WHERE reference ILIKE %s OR sku ILIKE %s
              ORDER BY reference,sequence_no LIMIT 50""",(like,like)).fetchall()
            receipts=c.execute("""SELECT procure_order_id reference,sku,ordered_quantity,received_quantity,expected_date,status
              FROM vector_expected_receipts WHERE procure_order_id ILIKE %s OR sku ILIKE %s
              ORDER BY updated_at DESC LIMIT 50""",(like,like)).fetchall()
        return {"query":q,"inventory":inventory,"shipments":shipments,"fulfillment":fulfillment,"receipts":receipts}

    app.include_router(router)
