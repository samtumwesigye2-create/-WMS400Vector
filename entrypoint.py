from app import app
from nexus_bridge import router as nexus_router
app.include_router(nexus_router)
