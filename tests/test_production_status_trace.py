from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from inventory_control import init_inventory_control
from production_control import init_production_control, install_production_routes
from production_execution import install_production_execution_routes
from production_reporting import install_production_reporting_routes


def db_conn():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)


def allow_auth(permission, authorization):
    return {"id": "test-user", "permissions": [permission]}


@pytest.fixture()
def client():
    with db_conn() as c:
        c.execute("DROP TABLE IF EXISTS vector_production_audit, vector_production_transactions, vector_production_exceptions, vector_production_confirmations, vector_production_orders, vector_reservations, vector_inventory_status, vector_stock_transit, vector_inventory, vector_movements CASCADE")
        c.execute("CREATE TABLE vector_inventory(id UUID PRIMARY KEY,sku TEXT NOT NULL,description TEXT NOT NULL,quantity INTEGER NOT NULL CHECK(quantity>=0),location_code TEXT NOT NULL,status TEXT NOT NULL,updated_at TIMESTAMPTZ NOT NULL)")
        c.execute("CREATE UNIQUE INDEX uq_vector_inventory_sku_location ON vector_inventory(sku,location_code)")
        c.execute("CREATE TABLE vector_movements(id UUID PRIMARY KEY,sku TEXT NOT NULL,quantity INTEGER NOT NULL,movement_type TEXT NOT NULL,from_location TEXT NULL,to_location TEXT NULL,reference TEXT NULL,created_at TIMESTAMPTZ NOT NULL)")
    init_inventory_control(db_conn)
    init_production_control(db_conn)
    app = FastAPI()
    install_production_reporting_routes(app, db_conn, allow_auth)
    install_production_routes(app, db_conn, allow_auth)
    install_production_execution_routes(app, db_conn, allow_auth)
    return TestClient(app)


def headers():
    return {"Authorization": "Bearer test"}


def seed_inventory(sku, quantity, location):
    with db_conn() as c:
        c.execute(
            "INSERT INTO vector_inventory VALUES(%s,%s,%s,%s,%s,'available',now())",
            (str(uuid4()), sku, sku, quantity, location),
        )


def build_complete_chain(client):
    created = client.post("/v1/production/orders", json={
        "production_order_id": "PO-4001",
        "transaction_id": "TX-CREATE-4001",
        "correlation_id": "CORR-4001",
        "production_version_code": "PV-4001",
        "material_code": "FG-4001",
        "planned_quantity": 4,
        "plant_code": "PLANT-01",
        "storage_location": "FG-01",
    }, headers=headers())
    assert created.status_code == 201
    released = client.post("/v1/production/orders/PO-4001/release", json={
        "transaction_id": "TX-REL-4001", "correlation_id": "CORR-4001"
    }, headers=headers())
    assert released.status_code == 200
    seed_inventory("RM-4001", 20, "RM-01")
    reserved = client.post("/v1/production/orders/PO-4001/reservations", json={
        "transaction_id": "TX-RES-4001",
        "correlation_id": "CORR-4001",
        "bom_base_quantity": 1,
        "components": [{"material_code": "RM-4001", "quantity": 2, "location_code": "RM-01"}],
    }, headers=headers())
    assert reserved.status_code == 201
    reservation_code = reserved.json()["reservations"][0]["reservation_code"]
    issued = client.post("/v1/production/orders/PO-4001/material-issues", json={
        "transaction_id": "TX-ISSUE-4001",
        "correlation_id": "CORR-4001",
        "reservation_code": reservation_code,
    }, headers=headers())
    assert issued.status_code == 200
    confirmed = client.post("/v1/production/orders/PO-4001/confirmations", json={
        "transaction_id": "TX-CONF-4001",
        "correlation_id": "CORR-4001",
        "operation_code": "OP-010",
        "quantity": 4,
    }, headers=headers())
    assert confirmed.status_code == 200
    exception = client.post("/v1/production/orders/PO-4001/exceptions", json={
        "transaction_id": "TX-EXC-4001",
        "correlation_id": "CORR-4001",
        "exception_type": "REWORK",
        "reason_code": "RW-01",
        "quantity": 1,
    }, headers=headers())
    assert exception.status_code == 201
    receipt = client.post("/v1/production/orders/PO-4001/finished-goods", json={
        "transaction_id": "TX-GR-4001",
        "correlation_id": "CORR-4001",
        "quantity": 4,
    }, headers=headers())
    assert receipt.status_code == 200
    closed = client.post("/v1/production/orders/PO-4001/close", json={
        "transaction_id": "TX-CLOSE-4001", "correlation_id": "CORR-4001"
    }, headers=headers())
    assert closed.status_code == 200


def test_u_pp_009_status_view_exposes_current_order_state(client):
    build_complete_chain(client)
    response = client.get("/v1/production/orders/PO-4001/status", headers=headers())
    assert response.status_code == 200
    body = response.json()
    assert body["ucode"] == "U-PP-009"
    assert body["production_order_id"] == "PO-4001"
    assert body["status"] == "CLOSED"
    assert body["planned_quantity"] == 4.0
    assert body["received_quantity"] == 4.0
    assert body["correlation_id"] == "CORR-4001"
    assert body["latest_transaction_id"] == "TX-CLOSE-4001"


def test_u_pp_010_trace_returns_full_correlated_chain(client):
    build_complete_chain(client)
    response = client.get("/v1/production/orders/PO-4001/trace", headers=headers())
    assert response.status_code == 200
    body = response.json()
    assert body["ucode"] == "U-PP-010"
    assert body["production_order_id"] == "PO-4001"
    assert body["correlation_id"] == "CORR-4001"
    assert body["order"]["status"] == "CLOSED"
    assert [row["ucode"] for row in body["transactions"]] == [
        "U-PP-001", "U-PP-002", "U-PP-003", "U-PP-004",
        "U-PP-005", "U-PP-006", "U-PP-007", "U-PP-008",
    ]
    assert len(body["reservations"]) == 1
    assert body["reservations"][0]["status"] == "consumed"
    assert len(body["confirmations"]) == 1
    assert len(body["exceptions"]) == 1
    assert body["exceptions"][0]["exception_type"] == "REWORK"
    movement_types = {row["movement_type"] for row in body["movements"]}
    assert {"production_issue", "production_receipt"}.issubset(movement_types)
    assert all(row["correlation_id"] == "CORR-4001" for row in body["transactions"])
