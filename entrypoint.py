from app import app
from nexus_bridge import router as nexus_router
from warehouse_control import router as warehouse_control_router
from cold_chain import router as cold_chain_router
from ugatu_returns import router as ugatu_returns_router
from inventory_costs import router as inventory_costs_router
from material_documents import router as material_documents_router
from vector_kpis import router as vector_kpis_router
from ugatu_fulfillment import router as ugatu_fulfillment_router

app.include_router(nexus_router)
app.include_router(warehouse_control_router)
app.include_router(cold_chain_router)
app.include_router(ugatu_returns_router)
app.include_router(inventory_costs_router)
app.include_router(material_documents_router)
app.include_router(vector_kpis_router)
app.include_router(ugatu_fulfillment_router)
