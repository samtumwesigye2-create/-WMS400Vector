from fastapi import APIRouter

router = APIRouter(prefix="/v1/digital-twin", tags=["digital-twin"])

@router.get("/product/{sku}")
def product_twin(sku: str):
    """Cross-links a product to its operational records for interactive twin UIs."""
    return {
        "sku": sku,
        "layers": ["bom", "inventory", "lots", "warehouse", "production", "supplier", "traceability"],
        "interaction": {"select_object": True, "highlight_related_data": True, "select_data": True, "highlight_object": True},
        "links": {
            "bom": f"/v1/manufacturing/bom/{sku}",
            "demand_classification": f"/v1/planning/demand-classification/{sku}",
            "genealogy": f"/v1/enterprise/genealogy/{sku}",
        },
    }

def install_digital_twin_routes(app):
    app.include_router(router)
