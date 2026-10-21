import os

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from production_control import (
    ProductionTransitionError,
    init_production_control,
    install_production_routes,
    validate_transition,
)


def db_conn():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)


def allow_auth(permission, authorization):
    return {"id": "test-user", "permissions": [permission]}


@pytest.fixture()
def client():
    init_production_control(db_conn)
    with db_conn() as c:
        c.execute("TRUNCATE vector_production_audit, vector_production_transactions, vector_production_exceptions, vector_production_confirmations, vector_production_orders RESTART IDENTITY CASCADE")
    app = FastAPI()
    install_production_routes(app, db_conn, allow_auth)
    return TestClient(app)


def headers():
    return {"Authorization": "Bearer test"}


def order_payload(transaction_id="TX-1001"):
    return {
        "production_order_id": "PO-1001",
        "transaction_id": transaction_id,
        "correlation_id": "CORR-1001",
        "production_version_code": "PV-BIKE-001",
        "material_code": "FG-BIKE",
        "planned_quantity": 10,
        "plant_code": "PLANT-01",
        "storage_location": "FG-01",
    }


def test_new_order_starts_created(client):
    response = client.post("/v1/production/orders", json=order_payload(), headers=headers())
    assert response.status_code == 201
    body = response.json()
    assert body["production_order_id"] == "PO-1001"
    assert body["status"] == "CREATED"
    assert body["correlation_id"] == "CORR-1001"


def test_create_is_idempotent_by_transaction_id(client):
    first = client.post("/v1/production/orders", json=order_payload(), headers=headers())
    second = client.post("/v1/production/orders", json=order_payload(), headers=headers())
    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json() == first.json()
    with db_conn() as c:
        assert c.execute("SELECT count(*) AS n FROM vector_production_orders").fetchone()["n"] == 1
        assert c.execute("SELECT count(*) AS n FROM vector_production_transactions").fetchone()["n"] == 1


def test_release_moves_created_to_released_and_audits(client):
    client.post("/v1/production/orders", json=order_payload(), headers=headers())
    response = client.post(
        "/v1/production/orders/PO-1001/release",
        json={"transaction_id": "TX-REL-1", "correlation_id": "CORR-1001"},
        headers=headers(),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "RELEASED"
    with db_conn() as c:
        audit = c.execute("SELECT action, from_status, to_status FROM vector_production_audit WHERE transaction_id=%s", ("TX-REL-1",)).fetchone()
        assert audit == {"action": "U-PP-002", "from_status": "CREATED", "to_status": "RELEASED"}


def test_invalid_transition_is_rejected():
    with pytest.raises(ProductionTransitionError, match="invalid_production_order_transition"):
        validate_transition("CREATED", "COMPLETED")


def test_closed_is_terminal():
    with pytest.raises(ProductionTransitionError, match="invalid_production_order_transition"):
        validate_transition("CLOSED", "RELEASED")
