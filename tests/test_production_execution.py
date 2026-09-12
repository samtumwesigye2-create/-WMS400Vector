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
    install_production_routes(app, db_conn, allow_auth)
    install_production_execution_routes(app, db_conn, allow_auth)
    return TestClient(app)


def headers():
    return {"Authorization": "Bearer test"}


def create_and_release(client):
    created = client.post("/v1/production/orders", json={
        "production_order_id": "PO-3001",
        "transaction_id": "TX-CREATE-3001",
        "correlation_id": "CORR-3001",
        "production_version_code": "PV-BIKE-001",
        "material_code": "FG-BIKE",
        "planned_quantity": 10,
        "plant_code": "PLANT-01",
        "storage_location": "FG-01",
    }, headers=headers())
    assert created.status_code == 201
    released = client.post("/v1/production/orders/PO-3001/release", json={
        "transaction_id": "TX-REL-3001", "correlation_id": "CORR-3001"
    }, headers=headers())
    assert released.status_code == 200


def confirm(client, tx="TX-CONF-3001", quantity=10):
    return client.post("/v1/production/orders/PO-3001/confirmations", json={
        "transaction_id": tx,
        "correlation_id": "CORR-3001",
        "operation_code": "OP-010",
        "quantity": quantity,
    }, headers=headers())


def test_confirmation_starts_execution_and_is_idempotent(client):
    create_and_release(client)
    first = confirm(client)
    second = confirm(client)
    assert first.status_code == 200
    assert first.json()["status"] == "IN_PROGRESS"
    assert second.status_code == 200
    assert second.json() == first.json()
    with db_conn() as c:
        assert c.execute("SELECT count(*) AS n FROM vector_production_confirmations").fetchone()["n"] == 1


def test_scrap_and_rework_are_recorded_and_idempotent(client):
    create_and_release(client)
    assert confirm(client).status_code == 200
    payload = {
        "transaction_id": "TX-EXC-3001",
        "correlation_id": "CORR-3001",
        "exception_type": "SCRAP",
        "reason_code": "QC-DAMAGE",
        "quantity": 2,
    }
    first = client.post("/v1/production/orders/PO-3001/exceptions", json=payload, headers=headers())
    second = client.post("/v1/production/orders/PO-3001/exceptions", json=payload, headers=headers())
    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json() == first.json()
    with db_conn() as c:
        row = c.execute("SELECT exception_type,reason_code,quantity FROM vector_production_exceptions").fetchone()
        assert row == {"exception_type": "SCRAP", "reason_code": "QC-DAMAGE", "quantity": 2.0}


def test_finished_goods_receipt_requires_confirmation_and_completes_order(client):
    create_and_release(client)
    early = client.post("/v1/production/orders/PO-3001/finished-goods", json={
        "transaction_id": "TX-GR-EARLY",
        "correlation_id": "CORR-3001",
        "quantity": 10,
    }, headers=headers())
    assert early.status_code == 409
    assert early.json()["detail"] == "required_confirmations_not_satisfied"

    assert confirm(client).status_code == 200
    receipt = client.post("/v1/production/orders/PO-3001/finished-goods", json={
        "transaction_id": "TX-GR-3001",
        "correlation_id": "CORR-3001",
        "quantity": 10,
    }, headers=headers())
    assert receipt.status_code == 200
    assert receipt.json()["status"] == "COMPLETED"
    assert receipt.json()["received_quantity"] == 10.0
    with db_conn() as c:
        stock = c.execute("SELECT quantity FROM vector_inventory WHERE sku='FG-BIKE' AND location_code='FG-01'").fetchone()
        assert stock["quantity"] == 10
        assert c.execute("SELECT count(*) AS n FROM vector_movements WHERE movement_type='production_receipt'").fetchone()["n"] == 1


def test_close_requires_completed_and_is_terminal(client):
    create_and_release(client)
    too_early = client.post("/v1/production/orders/PO-3001/close", json={
        "transaction_id": "TX-CLOSE-EARLY", "correlation_id": "CORR-3001"
    }, headers=headers())
    assert too_early.status_code == 409

    assert confirm(client).status_code == 200
    receipt = client.post("/v1/production/orders/PO-3001/finished-goods", json={
        "transaction_id": "TX-GR-3002",
        "correlation_id": "CORR-3001",
        "quantity": 10,
    }, headers=headers())
    assert receipt.status_code == 200

    closed = client.post("/v1/production/orders/PO-3001/close", json={
        "transaction_id": "TX-CLOSE-3001", "correlation_id": "CORR-3001"
    }, headers=headers())
    assert closed.status_code == 200
    assert closed.json()["status"] == "CLOSED"

    repeat_release = client.post("/v1/production/orders/PO-3001/release", json={
        "transaction_id": "TX-REL-AFTER-CLOSE", "correlation_id": "CORR-3001"
    }, headers=headers())
    assert repeat_release.status_code == 409
