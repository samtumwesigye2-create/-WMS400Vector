from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/v1/production", tags=["production-control"])

ALLOWED_TRANSITIONS = {
    "CREATED": {"RELEASED", "BLOCKED", "CANCELLED"},
    "RELEASED": {"IN_PROGRESS", "BLOCKED", "CANCELLED"},
    "IN_PROGRESS": {"COMPLETED", "BLOCKED", "CANCELLED"},
    "BLOCKED": {"RELEASED", "IN_PROGRESS", "CANCELLED"},
    "COMPLETED": {"CLOSED"},
    "CLOSED": set(),
    "CANCELLED": set(),
}


class ProductionTransitionError(ValueError):
    pass


def now() -> datetime:
    return datetime.now(timezone.utc)


def validate_transition(current: str, target: str) -> None:
    if target not in ALLOWED_TRANSITIONS.get(current, set()):
        raise ProductionTransitionError("invalid_production_order_transition")


class ProductionOrderIn(BaseModel):
    production_order_id: str = Field(min_length=1, max_length=160)
    transaction_id: str = Field(min_length=1, max_length=160)
    correlation_id: str = Field(min_length=1, max_length=160)
    production_version_code: str = Field(min_length=1, max_length=160)
    material_code: str = Field(min_length=1, max_length=160)
    planned_quantity: float = Field(gt=0)
    plant_code: str = Field(min_length=1, max_length=160)
    storage_location: str = Field(min_length=1, max_length=160)


class ProductionActionIn(BaseModel):
    transaction_id: str = Field(min_length=1, max_length=160)
    correlation_id: str = Field(min_length=1, max_length=160)


def init_production_control(conn) -> None:
    with conn() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS vector_production_orders(
              id UUID PRIMARY KEY,
              production_order_id TEXT UNIQUE NOT NULL,
              production_version_code TEXT NOT NULL,
              material_code TEXT NOT NULL,
              planned_quantity DOUBLE PRECISION NOT NULL CHECK(planned_quantity>0),
              received_quantity DOUBLE PRECISION NOT NULL DEFAULT 0 CHECK(received_quantity>=0),
              plant_code TEXT NOT NULL,
              storage_location TEXT NOT NULL,
              status TEXT NOT NULL,
              correlation_id TEXT NOT NULL,
              created_at TIMESTAMPTZ NOT NULL,
              updated_at TIMESTAMPTZ NOT NULL
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS vector_production_transactions(
              id UUID PRIMARY KEY,
              transaction_id TEXT UNIQUE NOT NULL,
              production_order_id TEXT NOT NULL,
              ucode TEXT NOT NULL,
              correlation_id TEXT NOT NULL,
              result_json JSONB NOT NULL,
              created_at TIMESTAMPTZ NOT NULL
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS vector_production_audit(
              id UUID PRIMARY KEY,
              production_order_id TEXT NOT NULL,
              transaction_id TEXT NOT NULL,
              correlation_id TEXT NOT NULL,
              action TEXT NOT NULL,
              from_status TEXT NULL,
              to_status TEXT NULL,
              payload JSONB NOT NULL,
              created_at TIMESTAMPTZ NOT NULL
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS vector_production_confirmations(
              id UUID PRIMARY KEY,
              production_order_id TEXT NOT NULL,
              operation_code TEXT NOT NULL,
              quantity DOUBLE PRECISION NOT NULL CHECK(quantity>=0),
              transaction_id TEXT UNIQUE NOT NULL,
              correlation_id TEXT NOT NULL,
              created_at TIMESTAMPTZ NOT NULL
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS vector_production_exceptions(
              id UUID PRIMARY KEY,
              production_order_id TEXT NOT NULL,
              exception_type TEXT NOT NULL,
              reason_code TEXT NOT NULL,
              quantity DOUBLE PRECISION NOT NULL CHECK(quantity>=0),
              transaction_id TEXT UNIQUE NOT NULL,
              correlation_id TEXT NOT NULL,
              created_at TIMESTAMPTZ NOT NULL
            )"""
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_vector_prod_audit_order ON vector_production_audit(production_order_id, created_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_vector_prod_audit_corr ON vector_production_audit(correlation_id, created_at)")


def _json_result(row):
    if not row:
        return None
    value = row["result_json"]
    if isinstance(value, str):
        return json.loads(value)
    return value


def _idempotent_result(c, transaction_id: str):
    return _json_result(
        c.execute(
            "SELECT result_json FROM vector_production_transactions WHERE transaction_id=%s",
            (transaction_id,),
        ).fetchone()
    )


def _store_transaction(c, *, transaction_id: str, production_order_id: str, ucode: str, correlation_id: str, result: dict) -> None:
    c.execute(
        """INSERT INTO vector_production_transactions
        (id,transaction_id,production_order_id,ucode,correlation_id,result_json,created_at)
        VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s)""",
        (str(uuid4()), transaction_id, production_order_id, ucode, correlation_id, json.dumps(result), now()),
    )


def _audit(c, *, production_order_id: str, transaction_id: str, correlation_id: str, action: str, from_status: str | None, to_status: str | None, payload: dict) -> None:
    c.execute(
        """INSERT INTO vector_production_audit
        (id,production_order_id,transaction_id,correlation_id,action,from_status,to_status,payload,created_at)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)""",
        (str(uuid4()), production_order_id, transaction_id, correlation_id, action, from_status, to_status, json.dumps(payload), now()),
    )


def _serialize_order(row) -> dict:
    return {
        "production_order_id": row["production_order_id"],
        "production_version_code": row["production_version_code"],
        "material_code": row["material_code"],
        "planned_quantity": float(row["planned_quantity"]),
        "received_quantity": float(row["received_quantity"]),
        "plant_code": row["plant_code"],
        "storage_location": row["storage_location"],
        "status": row["status"],
        "correlation_id": row["correlation_id"],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


def install_production_routes(app, conn, auth) -> None:
    @router.post("/orders", status_code=201)
    def create_order(body: ProductionOrderIn, authorization: str | None = Header(None)):
        auth("vector.production.write", authorization)
        with conn() as c:
            existing = _idempotent_result(c, body.transaction_id)
            if existing is not None:
                return existing
            duplicate = c.execute(
                "SELECT production_order_id FROM vector_production_orders WHERE production_order_id=%s",
                (body.production_order_id,),
            ).fetchone()
            if duplicate:
                raise HTTPException(409, "production_order_already_exists")
            ts = now()
            row = c.execute(
                """INSERT INTO vector_production_orders
                (id,production_order_id,production_version_code,material_code,planned_quantity,received_quantity,plant_code,storage_location,status,correlation_id,created_at,updated_at)
                VALUES(%s,%s,%s,%s,%s,0,%s,%s,'CREATED',%s,%s,%s)
                RETURNING *""",
                (
                    str(uuid4()), body.production_order_id, body.production_version_code,
                    body.material_code, body.planned_quantity, body.plant_code,
                    body.storage_location, body.correlation_id, ts, ts,
                ),
            ).fetchone()
            result = _serialize_order(row)
            _audit(
                c,
                production_order_id=body.production_order_id,
                transaction_id=body.transaction_id,
                correlation_id=body.correlation_id,
                action="U-PP-001",
                from_status=None,
                to_status="CREATED",
                payload=body.model_dump(),
            )
            _store_transaction(
                c,
                transaction_id=body.transaction_id,
                production_order_id=body.production_order_id,
                ucode="U-PP-001",
                correlation_id=body.correlation_id,
                result=result,
            )
            return result

    @router.get("/orders/{production_order_id}")
    def get_order(production_order_id: str, authorization: str | None = Header(None)):
        auth("vector.production.read", authorization)
        with conn() as c:
            row = c.execute(
                "SELECT * FROM vector_production_orders WHERE production_order_id=%s",
                (production_order_id,),
            ).fetchone()
            if not row:
                raise HTTPException(404, "production_order_not_found")
            return _serialize_order(row)

    @router.post("/orders/{production_order_id}/release")
    def release_order(production_order_id: str, body: ProductionActionIn, authorization: str | None = Header(None)):
        auth("vector.production.write", authorization)
        with conn() as c:
            existing = _idempotent_result(c, body.transaction_id)
            if existing is not None:
                return existing
            row = c.execute(
                "SELECT * FROM vector_production_orders WHERE production_order_id=%s FOR UPDATE",
                (production_order_id,),
            ).fetchone()
            if not row:
                raise HTTPException(404, "production_order_not_found")
            try:
                validate_transition(row["status"], "RELEASED")
            except ProductionTransitionError as exc:
                raise HTTPException(409, str(exc))
            updated = c.execute(
                "UPDATE vector_production_orders SET status='RELEASED',correlation_id=%s,updated_at=%s WHERE id=%s RETURNING *",
                (body.correlation_id, now(), row["id"]),
            ).fetchone()
            result = _serialize_order(updated)
            _audit(
                c,
                production_order_id=production_order_id,
                transaction_id=body.transaction_id,
                correlation_id=body.correlation_id,
                action="U-PP-002",
                from_status=row["status"],
                to_status="RELEASED",
                payload=body.model_dump(),
            )
            _store_transaction(
                c,
                transaction_id=body.transaction_id,
                production_order_id=production_order_id,
                ucode="U-PP-002",
                correlation_id=body.correlation_id,
                result=result,
            )
            return result

    @router.get("/orders/{production_order_id}/trace")
    def trace_order(production_order_id: str, authorization: str | None = Header(None)):
        auth("vector.production.read", authorization)
        with conn() as c:
            order = c.execute(
                "SELECT * FROM vector_production_orders WHERE production_order_id=%s",
                (production_order_id,),
            ).fetchone()
            if not order:
                raise HTTPException(404, "production_order_not_found")
            audit = c.execute(
                "SELECT production_order_id,transaction_id,correlation_id,action,from_status,to_status,payload,created_at FROM vector_production_audit WHERE production_order_id=%s ORDER BY created_at,id",
                (production_order_id,),
            ).fetchall()
            return {"order": _serialize_order(order), "audit": audit}

    app.include_router(router)
