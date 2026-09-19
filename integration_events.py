"""Canonical VECTOR inventory events for UNG-NEXUS and analytics."""
from datetime import datetime, timezone
from uuid import uuid4

SCHEMA_VERSION = "1.0"

def _event(message_type: str, payload: dict, target_system: str = "UNG-NOVA") -> dict:
    return {
        "source_system": "UNG-VECTOR",
        "target_system": target_system,
        "message_type": message_type,
        "message_id": str(uuid4()),
        "schema_version": SCHEMA_VERSION,
        "priority": 50,
        "classification": "internal",
        "payload": {**payload, "event_time": datetime.now(timezone.utc).isoformat()},
    }

def inventory_changed(sku: str, location_code: str, quantity: float, reason: str, reference: str = "") -> dict:
    return _event("inventory.stock.changed", {
        "sku": sku, "location_code": location_code, "quantity": float(quantity),
        "reason": reason, "reference": reference,
    })

def reservation_changed(row: dict) -> dict:
    return _event("inventory.reservation.changed", {
        "reservation_code": row["reservation_code"], "sku": row["sku"],
        "location_code": row["location_code"], "quantity": float(row["quantity"]),
        "status": row["status"], "reference": row.get("reference"),
    })

def transit_changed(row: dict) -> dict:
    return _event("inventory.transit.changed", {
        "transit_code": row["transit_code"], "sku": row["sku"],
        "quantity": float(row["quantity"]), "from_location": row["from_location"],
        "to_location": row["to_location"], "status": row["status"],
        "reference": row.get("reference"),
    })
