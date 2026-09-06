from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from typing import Any
from datetime import datetime, timezone
from uuid import uuid4
from app import auth, conn
import psycopg

router=APIRouter(prefix='/v1/nexus',tags=['NEXUS Integration'])

class Envelope(BaseModel):
    message_id:str|None=None
    source_system:str
    target_system:str
    message_type:str
    payload:dict[str,Any]=Field(default_factory=dict)
    sent_at:str|None=None
    principal_id:str|None=None

def ensure_table():
    with conn() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS vector_integration_events(
            id UUID PRIMARY KEY,message_id TEXT UNIQUE,source_system TEXT,target_system TEXT,
            message_type TEXT,payload JSONB,status TEXT,created_at TIMESTAMPTZ)''')

@router.post('/inbound',status_code=202)
def inbound(b:Envelope,authorization:str|None=Header(None)):
    principal=auth('nexus.messages.write',authorization)
    if b.target_system!='UNG-VECTOR': raise HTTPException(409,'wrong_target_system')
    ensure_table(); mid=b.message_id or str(uuid4()); now=datetime.now(timezone.utc)
    with conn() as c:
        existing=c.execute('SELECT * FROM vector_integration_events WHERE message_id=%s',(mid,)).fetchone()
        if existing:return {'accepted':True,'duplicate':True,'event':existing}
        status='receiving_expected' if b.message_type=='PROCURE.PURCHASE_ORDER.RECEIVING_EXPECTED' else 'accepted'
        event=c.execute('''INSERT INTO vector_integration_events
            (id,message_id,source_system,target_system,message_type,payload,status,created_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
            (str(uuid4()),mid,b.source_system,b.target_system,b.message_type,psycopg.types.json.Jsonb(b.payload),status,now)).fetchone()
    return {'accepted':True,'duplicate':False,'principal_id':principal.get('id'),'event':event}

@router.get('/status')
def status():
    try:
        ensure_table()
        with conn() as c:
            total=c.execute('SELECT COUNT(*) n FROM vector_integration_events').fetchone()['n']
            last=c.execute('SELECT * FROM vector_integration_events ORDER BY created_at DESC LIMIT 1').fetchone()
        return {'status':'ready','service':'UNG-VECTOR','inbound':'/v1/nexus/inbound','events':total,'last_event':last}
    except Exception as e: raise HTTPException(503,f'nexus_bridge_unavailable:{type(e).__name__}')
