from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException

from production_control import _serialize_order

router = APIRouter(prefix="/v1/production", tags=["production-reporting"])


def install_production_reporting_routes(app, conn, auth) -> None:
    @router.get("/orders/{production_order_id}/status")
    def production_status(production_order_id: str, authorization: str | None = Header(None)):
        auth("vector.production.read", authorization)
        with conn() as c:
            order = c.execute(
                "SELECT * FROM vector_production_orders WHERE production_order_id=%s",
                (production_order_id,),
            ).fetchone()
            if not order:
                raise HTTPException(404, "production_order_not_found")
            latest = c.execute(
                """SELECT transaction_id,ucode,correlation_id,created_at
                   FROM vector_production_transactions
                   WHERE production_order_id=%s
                   ORDER BY created_at DESC,id DESC LIMIT 1""",
                (production_order_id,),
            ).fetchone()
            return {
                "ucode": "U-PP-009",
                "production_order_id": order["production_order_id"],
                "status": order["status"],
                "planned_quantity": float(order["planned_quantity"]),
                "received_quantity": float(order["received_quantity"]),
                "material_code": order["material_code"],
                "plant_code": order["plant_code"],
                "storage_location": order["storage_location"],
                "correlation_id": order["correlation_id"],
                "latest_transaction_id": latest["transaction_id"] if latest else None,
                "latest_action": latest["ucode"] if latest else None,
                "updated_at": order["updated_at"],
            }

    @router.get("/orders/{production_order_id}/trace")
    def production_trace(production_order_id: str, authorization: str | None = Header(None)):
        auth("vector.production.read", authorization)
        with conn() as c:
            order = c.execute(
                "SELECT * FROM vector_production_orders WHERE production_order_id=%s",
                (production_order_id,),
            ).fetchone()
            if not order:
                raise HTTPException(404, "production_order_not_found")

            transactions = c.execute(
                """SELECT transaction_id,ucode,correlation_id,created_at
                   FROM vector_production_transactions
                   WHERE production_order_id=%s
                   ORDER BY created_at,id""",
                (production_order_id,),
            ).fetchall()
            audit = c.execute(
                """SELECT transaction_id,correlation_id,action,from_status,to_status,payload,created_at
                   FROM vector_production_audit
                   WHERE production_order_id=%s
                   ORDER BY created_at,id""",
                (production_order_id,),
            ).fetchall()
            reservations = c.execute(
                """SELECT reservation_code,sku,location_code,quantity,status,reference,created_at,updated_at
                   FROM vector_reservations WHERE reference=%s ORDER BY created_at,id""",
                (production_order_id,),
            ).fetchall()
            confirmations = c.execute(
                """SELECT operation_code,quantity,transaction_id,correlation_id,created_at
                   FROM vector_production_confirmations
                   WHERE production_order_id=%s ORDER BY created_at,id""",
                (production_order_id,),
            ).fetchall()
            exceptions = c.execute(
                """SELECT exception_type,reason_code,quantity,transaction_id,correlation_id,created_at
                   FROM vector_production_exceptions
                   WHERE production_order_id=%s ORDER BY created_at,id""",
                (production_order_id,),
            ).fetchall()
            movements = c.execute(
                """SELECT sku,quantity,movement_type,from_location,to_location,reference,created_at
                   FROM vector_movements WHERE reference=%s ORDER BY created_at,id""",
                (production_order_id,),
            ).fetchall()
            return {
                "ucode": "U-PP-010",
                "production_order_id": production_order_id,
                "correlation_id": order["correlation_id"],
                "order": _serialize_order(order),
                "transactions": transactions,
                "audit": audit,
                "reservations": reservations,
                "confirmations": confirmations,
                "exceptions": exceptions,
                "movements": movements,
            }

    app.include_router(router)
