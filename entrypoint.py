from app import app
from nexus_bridge import router as nexus_router
from warehouse_control import router as warehouse_control_router

app.include_router(nexus_router)
app.include_router(warehouse_control_router)
