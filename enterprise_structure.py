from datetime import datetime, timezone
from fastapi import Header, HTTPException
from pydantic import BaseModel
from uuid import uuid4
def now(): return datetime.now(timezone.utc)
LEVELS=['client','company','company_code','plant','storage_location','purchasing_organization','purchasing_group']
def init_enterprise_structure(conn):
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS vector_org_units(
        id UUID PRIMARY KEY,code TEXT UNIQUE NOT NULL,name TEXT NOT NULL,level TEXT NOT NULL,
        parent_code TEXT NULL,country TEXT NULL,currency TEXT NULL,metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        active BOOLEAN NOT NULL DEFAULT TRUE,created_at TIMESTAMPTZ NOT NULL)""")
class OrgUnit(BaseModel):
    code:str;name:str;level:str;parent_code:str|None=None;country:str|None=None;currency:str|None=None;metadata:dict={}
def install_enterprise_structure_routes(app,conn,auth):
    @app.post('/v1/enterprise-structure/units',status_code=201)
    def create_unit(b:OrgUnit,authorization:str|None=Header(None)):
        auth('vector.admin',authorization)
        if b.level not in LEVELS: raise HTTPException(400,'invalid_org_level')
        with conn() as c:
            if b.parent_code and not c.execute('SELECT 1 FROM vector_org_units WHERE code=%s',(b.parent_code,)).fetchone(): raise HTTPException(400,'parent_not_found')
            return c.execute("""INSERT INTO vector_org_units VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb,true,%s)
            ON CONFLICT(code) DO UPDATE SET name=EXCLUDED.name,level=EXCLUDED.level,parent_code=EXCLUDED.parent_code,country=EXCLUDED.country,currency=EXCLUDED.currency,metadata=EXCLUDED.metadata RETURNING *""",
            (str(uuid4()),b.code,b.name,b.level,b.parent_code,b.country,b.currency,__import__('json').dumps(b.metadata),now())).fetchone()
    @app.get('/v1/enterprise-structure')
    def structure(authorization:str|None=Header(None)):
        auth('vector.inventory.read',authorization)
        with conn() as c:return {'levels':LEVELS,'units':c.execute('SELECT * FROM vector_org_units ORDER BY level,code').fetchall()}
