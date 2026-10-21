from datetime import date, datetime, timezone
from uuid import uuid4
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

router=APIRouter(prefix="/v1/manufacturing/bom-governance",tags=["BOM Governance"])

def now(): return datetime.now(timezone.utc)
BOM_TYPES={"EBOM","MBOM","SBOM"}

class BOMHeaderIn(BaseModel):
    parent_sku:str
    bom_type:str
    revision:str
    description:str=""
    effective_from:date|None=None
    effective_to:date|None=None
    owner:str=""

class BOMLineIn(BaseModel):
    component_sku:str
    quantity:float=Field(gt=0)
    uom:str="EA"
    specification:str=""
    supplier_id:str|None=None
    unit_cost:float=Field(default=0,ge=0)
    sequence_no:int=Field(default=10,gt=0)

class BOMConvertIn(BaseModel):
    target_type:str
    new_revision:str
    owner:str=""

class BOMAuditIn(BaseModel):
    note:str=""
    physical_match:bool=True
    specification_match:bool=True
    supplier_source_match:bool=True
    costing_match:bool=True

def init_bom_governance(conn):
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS vector_bom_headers(
          id UUID PRIMARY KEY,parent_sku TEXT NOT NULL,bom_type TEXT NOT NULL,revision TEXT NOT NULL,
          description TEXT NOT NULL,effective_from DATE NULL,effective_to DATE NULL,owner TEXT NOT NULL,
          status TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL,released_at TIMESTAMPTZ NULL,
          UNIQUE(parent_sku,bom_type,revision))""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_bom_lines_v2(
          id UUID PRIMARY KEY,bom_id UUID NOT NULL REFERENCES vector_bom_headers(id) ON DELETE CASCADE,
          component_sku TEXT NOT NULL,quantity NUMERIC NOT NULL,uom TEXT NOT NULL,specification TEXT NOT NULL,
          supplier_id TEXT NULL,unit_cost NUMERIC NOT NULL,sequence_no INTEGER NOT NULL,
          created_at TIMESTAMPTZ NOT NULL,UNIQUE(bom_id,component_sku))""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_bom_conversions(
          id UUID PRIMARY KEY,source_bom_id UUID NOT NULL,target_bom_id UUID NOT NULL,
          source_type TEXT NOT NULL,target_type TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS vector_bom_audits(
          id UUID PRIMARY KEY,bom_id UUID NOT NULL REFERENCES vector_bom_headers(id) ON DELETE CASCADE,
          physical_match BOOLEAN NOT NULL,specification_match BOOLEAN NOT NULL,
          supplier_source_match BOOLEAN NOT NULL,costing_match BOOLEAN NOT NULL,
          result TEXT NOT NULL,note TEXT NOT NULL,audited_at TIMESTAMPTZ NOT NULL)""")

def install_bom_governance_routes(app,conn,auth):
    @router.post("",status_code=201)
    def create_bom(b:BOMHeaderIn,authorization:str|None=Header(None)):
        auth("vector.manufacturing.write",authorization)
        typ=b.bom_type.upper()
        if typ not in BOM_TYPES: raise HTTPException(422,"bom_type_must_be_EBOM_MBOM_or_SBOM")
        if b.effective_from and b.effective_to and b.effective_to<b.effective_from:
            raise HTTPException(422,"effective_to_before_effective_from")
        with conn() as c:
            return c.execute("""INSERT INTO vector_bom_headers VALUES(
              %s,%s,%s,%s,%s,%s,%s,%s,'draft',%s,NULL) RETURNING *""",
              (str(uuid4()),b.parent_sku,typ,b.revision,b.description,b.effective_from,b.effective_to,b.owner,now())).fetchone()

    @router.post("/{bom_id}/lines",status_code=201)
    def add_line(bom_id:str,b:BOMLineIn,authorization:str|None=Header(None)):
        auth("vector.manufacturing.write",authorization)
        with conn() as c:
            header=c.execute("SELECT * FROM vector_bom_headers WHERE id=%s",(bom_id,)).fetchone()
            if not header: raise HTTPException(404,"bom_not_found")
            if header["status"]!="draft": raise HTTPException(409,"bom_not_draft")
            if b.component_sku==header["parent_sku"]: raise HTTPException(409,"bom_self_reference")
            return c.execute("""INSERT INTO vector_bom_lines_v2 VALUES(
              %s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
              (str(uuid4()),bom_id,b.component_sku,b.quantity,b.uom,b.specification,b.supplier_id,
               b.unit_cost,b.sequence_no,now())).fetchone()

    @router.post("/{bom_id}/release")
    def release_bom(bom_id:str,authorization:str|None=Header(None)):
        auth("vector.manufacturing.write",authorization)
        with conn() as c:
            header=c.execute("SELECT * FROM vector_bom_headers WHERE id=%s FOR UPDATE",(bom_id,)).fetchone()
            if not header: raise HTTPException(404,"bom_not_found")
            lines=c.execute("SELECT * FROM vector_bom_lines_v2 WHERE bom_id=%s",(bom_id,)).fetchall()
            if not lines: raise HTTPException(409,"bom_has_no_lines")
            c.execute("""UPDATE vector_bom_headers SET status='superseded'
              WHERE parent_sku=%s AND bom_type=%s AND status='released' AND id<>%s""",
              (header["parent_sku"],header["bom_type"],bom_id))
            return c.execute("""UPDATE vector_bom_headers SET status='released',released_at=%s
              WHERE id=%s RETURNING *""",(now(),bom_id)).fetchone()

    @router.post("/{bom_id}/convert",status_code=201)
    def convert_bom(bom_id:str,b:BOMConvertIn,authorization:str|None=Header(None)):
        auth("vector.manufacturing.write",authorization)
        target=b.target_type.upper()
        if target not in BOM_TYPES: raise HTTPException(422,"invalid_target_bom_type")
        with conn() as c:
            src=c.execute("SELECT * FROM vector_bom_headers WHERE id=%s",(bom_id,)).fetchone()
            if not src: raise HTTPException(404,"bom_not_found")
            lines=c.execute("SELECT * FROM vector_bom_lines_v2 WHERE bom_id=%s ORDER BY sequence_no",(bom_id,)).fetchall()
            tid=str(uuid4()); t=now()
            target_row=c.execute("""INSERT INTO vector_bom_headers VALUES(
              %s,%s,%s,%s,%s,%s,%s,%s,'draft',%s,NULL) RETURNING *""",
              (tid,src["parent_sku"],target,b.new_revision,src["description"],src["effective_from"],src["effective_to"],b.owner,t)).fetchone()
            for line in lines:
                c.execute("""INSERT INTO vector_bom_lines_v2 VALUES(
                  %s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                  (str(uuid4()),tid,line["component_sku"],line["quantity"],line["uom"],line["specification"],
                   line["supplier_id"],line["unit_cost"],line["sequence_no"],t))
            c.execute("INSERT INTO vector_bom_conversions VALUES(%s,%s,%s,%s,%s,%s)",
              (str(uuid4()),bom_id,tid,src["bom_type"],target,t))
        return {"source_bom_id":bom_id,"target":target_row,"copied_lines":len(lines)}

    @router.post("/{bom_id}/audit")
    def audit_bom(bom_id:str,b:BOMAuditIn,authorization:str|None=Header(None)):
        auth("vector.manufacturing.write",authorization)
        passed=all([b.physical_match,b.specification_match,b.supplier_source_match,b.costing_match])
        result="PASS" if passed else "FAIL"
        with conn() as c:
            if not c.execute("SELECT 1 FROM vector_bom_headers WHERE id=%s",(bom_id,)).fetchone():
                raise HTTPException(404,"bom_not_found")
            return c.execute("""INSERT INTO vector_bom_audits VALUES(
              %s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
              (str(uuid4()),bom_id,b.physical_match,b.specification_match,b.supplier_source_match,
               b.costing_match,result,b.note,now())).fetchone()

    @router.get("/{bom_id}")
    def get_bom(bom_id:str,authorization:str|None=Header(None)):
        auth("vector.manufacturing.read",authorization)
        with conn() as c:
            h=c.execute("SELECT * FROM vector_bom_headers WHERE id=%s",(bom_id,)).fetchone()
            if not h: raise HTTPException(404,"bom_not_found")
            lines=c.execute("SELECT * FROM vector_bom_lines_v2 WHERE bom_id=%s ORDER BY sequence_no",(bom_id,)).fetchall()
            audits=c.execute("SELECT * FROM vector_bom_audits WHERE bom_id=%s ORDER BY audited_at DESC",(bom_id,)).fetchall()
        return {"header":h,"lines":lines,"audits":audits}

    app.include_router(router)
