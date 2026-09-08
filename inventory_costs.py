from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app import auth, conn, now

router = APIRouter(prefix="/v1", tags=["Inventory Intelligence"])


def ensure_schema():
    with conn() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS vector_item_costs(
              sku TEXT PRIMARY KEY,
              unit_price DOUBLE PRECISION NOT NULL CHECK(unit_price >= 0),
              currency TEXT NOT NULL DEFAULT 'USD',
              source TEXT NOT NULL DEFAULT 'manual',
              updated_at TIMESTAMPTZ NOT NULL,
              updated_by TEXT
            )
            """
        )


class ItemCostIn(BaseModel):
    unit_price: float = Field(ge=0)
    currency: str = "USD"
    source: str = "manual"


@router.get("/item-costs")
def list_item_costs(authorization: str | None = Header(None)):
    auth("vector.inventory.read", authorization)
    ensure_schema()
    with conn() as c:
        return c.execute("SELECT * FROM vector_item_costs ORDER BY sku").fetchall()


@router.put("/item-costs/{sku}")
def set_item_cost(sku: str, body: ItemCostIn, authorization: str | None = Header(None)):
    principal = auth("vector.inventory.write", authorization)
    sku = sku.strip()
    if not sku:
        raise HTTPException(422, "sku_required")
    ensure_schema()
    with conn() as c:
        return c.execute(
            """
            INSERT INTO vector_item_costs(sku,unit_price,currency,source,updated_at,updated_by)
            VALUES(%s,%s,%s,%s,%s,%s)
            ON CONFLICT(sku) DO UPDATE SET
              unit_price=EXCLUDED.unit_price,
              currency=EXCLUDED.currency,
              source=EXCLUDED.source,
              updated_at=EXCLUDED.updated_at,
              updated_by=EXCLUDED.updated_by
            RETURNING *
            """,
            (sku, body.unit_price, body.currency.upper(), body.source, now(), principal.get("id") or principal.get("display_name")),
        ).fetchone()


@router.get("/oracle/abc-input")
def oracle_abc_input(authorization: str | None = Header(None)):
    auth("vector.inventory.read", authorization)
    ensure_schema()
    with conn() as c:
        rows = c.execute(
            """
            WITH inventory AS (
              SELECT sku,
                     MAX(description) AS description,
                     SUM(quantity)::DOUBLE PRECISION AS quantity_on_hand
              FROM vector_inventory
              GROUP BY sku
            ), usage AS (
              SELECT sku,
                     SUM(quantity)::DOUBLE PRECISION AS annual_usage_qty
              FROM vector_movements
              WHERE movement_type='dispatch'
                AND created_at >= NOW() - INTERVAL '365 days'
              GROUP BY sku
            )
            SELECT i.sku AS item_code,
                   i.description,
                   i.quantity_on_hand,
                   COALESCE(u.annual_usage_qty,0) AS annual_usage_qty,
                   p.unit_price,
                   p.currency,
                   p.source AS price_source,
                   p.updated_at AS price_updated_at
            FROM inventory i
            LEFT JOIN usage u ON u.sku=i.sku
            LEFT JOIN vector_item_costs p ON p.sku=i.sku
            ORDER BY i.sku
            """
        ).fetchall()
        priced = sum(1 for r in rows if r.get("unit_price") is not None)
        active_usage = sum(1 for r in rows if float(r.get("annual_usage_qty") or 0) > 0)
        return {
            "source": "UNG-VECTOR",
            "generated_at": now(),
            "inventory_records": len(rows),
            "priced_records": priced,
            "records_with_annual_usage": active_usage,
            "missing_price": [r["item_code"] for r in rows if r.get("unit_price") is None],
            "items": rows,
        }
