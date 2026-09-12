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
    return TestClient(app)


def headers():
    return {"Authorization": "Bearer test"}


def create_order(client, planned_quantity=10):
    response = client.post(
        "/v1/production/orders",
        json={
            "production_order_id": "PO-2001",
            "transaction_id": "TX-CREATE-2001",
            "correlation_id": "CORR-2001",
            "production_version_code": "PV-BIKE-001",
            "material_code": "FG-BIKE",
            "planned_quantity": planned_quantity,
            "plant_code": "PLANT-01",
            "storage_location": "FG-01",
        },
        headers=headers(),
    )
    assert response.status_code == 201


def release_order(client):
    response = client.post(
        "/v1/production/orders/PO-2001/release",
        json={"transaction_id": "TX-RELEASE-2001", "correlation_id": "CORR-2001"},
        headers=headers(),
    )
    assert response.status_code == 200


def seed_inventory(sku, quantity, location="RM-01"):
    with db_conn() as c:
        c.execute(
            "INSERT INTO vector_inventory VALUES(%s,%s,%s,%s,%s,'available',now())",
            (str(uuid4()), sku, sku, quantity, location),
        )


def test_reservation_scales_bom_quantities_and_is_atomic(client):
    create_order(client, planned_quantity=10)
    release_order(client)
    seed_inventory("RM-A", 20)
    seed_inventory("RM-B", 9)

    response = client.post(
        "/v1/production/orders/PO-2001/reservations",
        json={
            "transaction_id": "TX-RES-2001",
            "correlation_id": "CORR-2001",
            "bom_base_quantity": 2,
            "components": [
                {"material_code": "RM-A", "quantity": 3, "location_code": "RM-01"},
                {"material_code": "RM-B", "quantity": 2, "location_code": "RM-01"},
            ],
        },
        headers=headers(),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "insufficient_available_inventory"
    with db_conn() as c:
        assert c.execute("SELECT count(*) AS n FROM vector_reservations").fetchone()["n"] == 0
        rows = c.execute("SELECT sku,reserved FROM vector_inventory_status ORDER BY sku").fetchall()
        assert all(row["reserved"] == 0 for row in rows)


def test_reservation_uses_scaled_quantities(client):
    create_order(client, planned_quantity=10)
    release_order(client)
    seed_inventory("RM-A", 30)
    seed_inventory("RM-B", 20)

    response = client.post(
        "/v1/production/orders/PO-2001/reservations",
        json={
            "transaction_id": "TX-RES-2002",
            "correlation_id": "CORR-2001",
            "bom_base_quantity": 2,
            "components": [
                {"material_code": "RM-A", "quantity": 3, "location_code": "RM-01"},
                {"material_code": "RM-B", "quantity": 2, "location_code": "RM-01"},
            ],
        },
        headers=headers(),
    )

    assert response.status_code == 201
    reservations = sorted(response.json()["reservations"], key=lambda x: x["sku"])
    assert [(r["sku"], r["quantity"]) for r in reservations] == [("RM-A", 15), ("RM-B", 10)]


def test_material_issue_requires_released_order_and_is_idempotent(client):
    create_order(client, planned_quantity=10)
    seed_inventory("RM-A", 20)

    before_release = client.post(
        "/v1/production/orders/PO-2001/material-issues",
        json={
            "transaction_id": "TX-ISSUE-BEFORE",
            "correlation_id": "CORR-2001",
            "reservation_code": "PROD-PO-2001-001",
        },
        headers=headers(),
    )
    assert before_release.status_code == 409
    assert before_release.json()["detail"] == "production_order_not_released"

    release_order(client)
    reserved = client.post(
        "/v1/production/orders/PO-2001/reservations",
        json={
            "transaction_id": "TX-RES-2003",
            "correlation_id": "CORR-2001",
            "bom_base_quantity": 1,
            "components": [
                {"material_code": "RM-A", "quantity": 5, "location_code": "RM-01"},
            ],
        },
        headers=headers(),
    )
    assert reserved.status_code == 201
    reservation_code = reserved.json()["reservations"][0]["reservation_code"]

    payload = {
        "transaction_id": "TX-ISSUE-2001",
        "correlation_id": "CORR-2001",
        "reservation_code": reservation_code,
    }
    first = client.post("/v1/production/orders/PO-2001/material-issues", json=payload, headers=headers())
    second = client.post("/v1/production/orders/PO-2001/material-issues", json=payload, headers=headers())

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    with db_conn() as c:
        inventory = c.execute("SELECT quantity FROM vector_inventory WHERE sku='RM-A' AND location_code='RM-01'").fetchone()
        assert inventory["quantity"] == 15
        reservation = c.execute("SELECT status FROM vector_reservations WHERE reservation_code=%s", (reservation_code,)).fetchone()
        assert reservation["status"] == "consumed"
        assert c.execute("SELECT count(*) AS n FROM vector_movements WHERE movement_type='production_issue'").fetchone()["n"] == 1
