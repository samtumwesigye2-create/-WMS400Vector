from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from production_control import (
    ProductionTransitionError,
    _audit,
    _idempotent_result,
    _serialize_order,
    _store_transaction,
    now,
    validate_transition,
)

router = APIRouter(prefix="/v1/production", tags=["production-execution"])


class OperationConfirmationIn(BaseModel):
    transaction_id: str = Field(min_length=1, max_length=160)
    correlation_id: str = Field(min_length=1, max_length=160)
    operation_code: str = Field(min_length=1, max_length=160)
    quantity: float = Field(gt=0)


class ProductionExceptionIn(BaseModel):
    transaction_id: str = Field(min_length=1, max_length=160)
    correlation_id: str = Field(min_length=1, max_length=160)
    exception_type: str = Field(min_length=1, max_length=32)
    reason_code: str = Field(min_length=1, max_length=160)
    quantity: float = Field(gt=0)


class FinishedGoodsReceiptIn(BaseModel):
    transaction_id: str = Field(min_length=1, max_length=160)
    correlation_id: str = Field(min_length=1, max_length=160)
    quantity: float = Field(gt=0)


class ProductionCloseIn(BaseModel):
    transaction_id: str = Field(min_length=1, max_length=160)
    correlation_id: str = Field(min_length=1, max_length=160)


def install_production_execution_routes(app, conn, auth) -> None:
    @router.post("/orders/{production_order_id}/confirmations")
    def confirm_operation(production_order_id: str, body: OperationConfirmationIn, authorization: str | None = Header(None)):
        auth("vector.production.write", authorization)
        with conn() as c:
            existing = _idempotent_result(c, body.transaction_id)
            if existing is not None:
                return existing
            order = c.execute(
                "SELECT * FROM vector_production_orders WHERE production_order_id=%s FOR UPDATE",
                (production_order_id,),
            ).fetchone()
            if not order:
                raise HTTPException(404, "production_order_not_found")
            if order["status"] not in {"RELEASED", "IN_PROGRESS"}:
                raise HTTPException(409, "production_order_not_executable")

            c.execute(
                """INSERT INTO vector_production_confirmations
                (id,production_order_id,operation_code,quantity,transaction_id,correlation_id,created_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                (
                    str(uuid4()), production_order_id, body.operation_code,
                    body.quantity, body.transaction_id, body.correlation_id, now(),
                ),
            )
            from_status = order["status"]
            if from_status == "RELEASED":
                try:
                    validate_transition(from_status, "IN_PROGRESS")
                except ProductionTransitionError as exc:
                    raise HTTPException(409, str(exc))
                order = c.execute(
                    "UPDATE vector_production_orders SET status='IN_PROGRESS',correlation_id=%s,updated_at=%s WHERE id=%s RETURNING *",
                    (body.correlation_id, now(), order["id"]),
                ).fetchone()

            result = _serialize_order(order)
            result.update({
                "operation_code": body.operation_code,
                "confirmed_quantity": float(body.quantity),
            })
            _audit(
                c,
                production_order_id=production_order_id,
                transaction_id=body.transaction_id,
                correlation_id=body.correlation_id,
                action="U-PP-005",
                from_status=from_status,
                to_status=order["status"],
                payload=body.model_dump(),
            )
            _store_transaction(
                c,
                transaction_id=body.transaction_id,
                production_order_id=production_order_id,
                ucode="U-PP-005",
                correlation_id=body.correlation_id,
                result=result,
            )
            return result

    @router.post("/orders/{production_order_id}/exceptions", status_code=201)
    def record_exception(production_order_id: str, body: ProductionExceptionIn, authorization: str | None = Header(None)):
        auth("vector.production.write", authorization)
        exception_type = body.exception_type.upper()
        if exception_type not in {"SCRAP", "REWORK"}:
            raise HTTPException(400, "invalid_production_exception_type")
        with conn() as c:
            existing = _idempotent_result(c, body.transaction_id)
            if existing is not None:
                return existing
            order = c.execute(
                "SELECT * FROM vector_production_orders WHERE production_order_id=%s FOR UPDATE",
                (production_order_id,),
            ).fetchone()
            if not order:
                raise HTTPException(404, "production_order_not_found")
            if order["status"] != "IN_PROGRESS":
                raise HTTPException(409, "production_order_not_in_progress")
            row = c.execute(
                """INSERT INTO vector_production_exceptions
                (id,production_order_id,exception_type,reason_code,quantity,transaction_id,correlation_id,created_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (
                    str(uuid4()), production_order_id, exception_type, body.reason_code,
                    body.quantity, body.transaction_id, body.correlation_id, now(),
                ),
            ).fetchone()
            result = {
                "production_order_id": production_order_id,
                "exception_type": row["exception_type"],
                "reason_code": row["reason_code"],
                "quantity": float(row["quantity"]),
                "status": order["status"],
                "correlation_id": body.correlation_id,
            }
            _audit(
                c,
                production_order_id=production_order_id,
                transaction_id=body.transaction_id,
                correlation_id=body.correlation_id,
                action="U-PP-006",
                from_status=order["status"],
                to_status=order["status"],
                payload=body.model_dump(),
            )
            _store_transaction(
                c,
                transaction_id=body.transaction_id,
                production_order_id=production_order_id,
                ucode="U-PP-006",
                correlation_id=body.correlation_id,
                result=result,
            )
            return result

    @router.post("/orders/{production_order_id}/finished-goods")
    def receive_finished_goods(production_order_id: str, body: FinishedGoodsReceiptIn, authorization: str | None = Header(None)):
        auth("vector.production.write", authorization)
        with conn() as c:
            existing = _idempotent_result(c, body.transaction_id)
            if existing is not None:
                return existing
            order = c.execute(
                "SELECT * FROM vector_production_orders WHERE production_order_id=%s FOR UPDATE",
                (production_order_id,),
            ).fetchone()
            if not order:
                raise HTTPException(404, "production_order_not_found")
            if order["status"] not in {"RELEASED", "IN_PROGRESS", "COMPLETED"}:
                raise HTTPException(409, "production_order_not_in_progress")

            new_received = float(order["received_quantity"]) + float(body.quantity)
            if new_received > float(order["planned_quantity"]) + 1e-9:
                raise HTTPException(409, "receipt_exceeds_planned_quantity")
            confirmed = c.execute(
                "SELECT COALESCE(sum(quantity),0) AS quantity FROM vector_production_confirmations WHERE production_order_id=%s",
                (production_order_id,),
            ).fetchone()["quantity"]
            if float(confirmed) + 1e-9 < new_received:
                raise HTTPException(409, "required_confirmations_not_satisfied")

            ts = now()
            c.execute(
                """INSERT INTO vector_inventory(id,sku,description,quantity,location_code,status,updated_at)
                VALUES(%s,%s,%s,%s,%s,'available',%s)
                ON CONFLICT(sku,location_code) DO UPDATE SET
                quantity=vector_inventory.quantity+EXCLUDED.quantity,
                updated_at=EXCLUDED.updated_at""",
                (
                    str(uuid4()), order["material_code"], order["material_code"],
                    int(body.quantity), order["storage_location"], ts,
                ),
            )
            c.execute(
                """INSERT INTO vector_movements
                (id,sku,quantity,movement_type,from_location,to_location,reference,created_at)
                VALUES(%s,%s,%s,'production_receipt',NULL,%s,%s,%s)""",
                (
                    str(uuid4()), order["material_code"], int(body.quantity),
                    order["storage_location"], production_order_id, ts,
                ),
            )

            from_status = order["status"]
            to_status = from_status
            if abs(new_received - float(order["planned_quantity"])) <= 1e-9:
                if from_status != "COMPLETED":
                    try:
                        validate_transition(from_status, "COMPLETED")
                    except ProductionTransitionError as exc:
                        raise HTTPException(409, str(exc))
                to_status = "COMPLETED"
            updated = c.execute(
                "UPDATE vector_production_orders SET received_quantity=%s,status=%s,correlation_id=%s,updated_at=%s WHERE id=%s RETURNING *",
                (new_received, to_status, body.correlation_id, ts, order["id"]),
            ).fetchone()
            result = _serialize_order(updated)
            _audit(
                c,
                production_order_id=production_order_id,
                transaction_id=body.transaction_id,
                correlation_id=body.correlation_id,
                action="U-PP-007",
                from_status=from_status,
                to_status=to_status,
                payload=body.model_dump(),
            )
            _store_transaction(
                c,
                transaction_id=body.transaction_id,
                production_order_id=production_order_id,
                ucode="U-PP-007",
                correlation_id=body.correlation_id,
                result=result,
            )
            return result

    @router.post("/orders/{production_order_id}/close")
    def close_order(production_order_id: str, body: ProductionCloseIn, authorization: str | None = Header(None)):
        auth("vector.production.write", authorization)
        with conn() as c:
            existing = _idempotent_result(c, body.transaction_id)
            if existing is not None:
                return existing
            order = c.execute(
                "SELECT * FROM vector_production_orders WHERE production_order_id=%s FOR UPDATE",
                (production_order_id,),
            ).fetchone()
            if not order:
                raise HTTPException(404, "production_order_not_found")
            try:
                validate_transition(order["status"], "CLOSED")
            except ProductionTransitionError as exc:
                raise HTTPException(409, str(exc))
            updated = c.execute(
                "UPDATE vector_production_orders SET status='CLOSED',correlation_id=%s,updated_at=%s WHERE id=%s RETURNING *",
                (body.correlation_id, now(), order["id"]),
            ).fetchone()
            result = _serialize_order(updated)
            _audit(
                c,
                production_order_id=production_order_id,
                transaction_id=body.transaction_id,
                correlation_id=body.correlation_id,
                action="U-PP-008",
                from_status=order["status"],
                to_status="CLOSED",
                payload=body.model_dump(),
            )
            _store_transaction(
                c,
                transaction_id=body.transaction_id,
                production_order_id=production_order_id,
                ucode="U-PP-008",
                correlation_id=body.correlation_id,
                result=result,
            )
            return result

    app.include_router(router)
