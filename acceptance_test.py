from datetime import datetime, timezone
from uuid import uuid4
import base64, hashlib, json, os, secrets, urllib.request
from urllib.parse import urlencode
from fastapi import Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

TAG='VECTOR-ACCEPTANCE'
JANUS_BASE_URL=os.getenv('JANUS_BASE_URL','https://ung-iam-production.up.railway.app').rstrip('/')
VECTOR_CALLBACK='https://ung-vector-production.up.railway.app/v1/acceptance/callback'
VECTOR_SSO_COOKIE='vector_janus_token'

def _now(): return datetime.now(timezone.utc)

def install_acceptance_routes(app,conn,auth):
    @app.get('/v1/acceptance/login')
    def acceptance_login():
        verifier=secrets.token_urlsafe(48)
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        state=secrets.token_urlsafe(24)
        q=urlencode({'client_id':'UNG-VECTOR','redirect_uri':VECTOR_CALLBACK,'code_challenge':challenge,'state':state})
        r=RedirectResponse(JANUS_BASE_URL+'/sso/launch?'+q,status_code=302)
        r.set_cookie('vector_sso_verifier',verifier,max_age=300,httponly=True,secure=True,samesite='lax')
        r.set_cookie('vector_sso_state',state,max_age=300,httponly=True,secure=True,samesite='lax')
        return r

    @app.get('/v1/acceptance/callback')
    def acceptance_callback(code:str,state:str,request:Request):
        expected=request.cookies.get('vector_sso_state','')
        verifier=request.cookies.get('vector_sso_verifier','')
        if not expected or not secrets.compare_digest(state,expected) or not verifier:
            raise HTTPException(400,'invalid_or_expired_sso_state')
        body=json.dumps({'client_id':'UNG-VECTOR','redirect_uri':VECTOR_CALLBACK,'code':code,'code_verifier':verifier}).encode()
        req=urllib.request.Request(JANUS_BASE_URL+'/v1/sso/token',data=body,method='POST',headers={'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(req,timeout=8) as resp:
                token=json.loads(resp.read().decode()).get('access_token','')
        except Exception:
            raise HTTPException(502,'JANUS_token_exchange_failed')
        if not token: raise HTTPException(502,'JANUS_token_missing')
        r=RedirectResponse('/v1/acceptance',status_code=302)
        r.set_cookie(VECTOR_SSO_COOKIE,token,max_age=28800,httponly=True,secure=True,samesite='lax')
        r.delete_cookie('vector_sso_verifier');r.delete_cookie('vector_sso_state')
        return r

    @app.post('/v1/acceptance/run')
    def run_acceptance(request:Request,authorization:str|None=Header(None)):
        token=authorization or request.cookies.get(VECTOR_SSO_COOKIE,'')
        if token and not token.lower().startswith('bearer '): token='Bearer '+token
        auth('vector.admin',token)
        run_id=str(uuid4()); sku='VEC-TEST-'+run_id[:8].upper()
        checks=[]
        with conn() as c:
            try:
                # Material + opening stock
                c.execute("""INSERT INTO vector_materials
                  (sku,name,description,category,uom,batch_managed,serial_managed,reorder_level,min_level,max_level,unit_cost,currency,valuation_method,status,material_type,industry_sector)
                  VALUES(%s,%s,%s,'acceptance','EA',FALSE,FALSE,10,5,100,25,'USD','standard','active','FERT','test')""",
                  (sku,TAG,TAG))
                checks.append({'stage':'material','status':'PASS'})
                # Validate core/new enterprise schemas without inventing business records that bypass their APIs.
                tables=['vector_gl_accounts','vector_journal_entries','vector_journal_lines','vector_accounting_periods','vector_payment_schedules','vector_payment_runs','vector_payment_run_items','vector_bank_execution_batches','vector_bank_execution_items','vector_supplier_payments','vector_p2p_audit_events','vector_inventory','vector_bom','vector_work_centers','vector_routings',
                  'vector_production_orders','vector_demand_forecasts','vector_sop_plans','vector_suppliers',
                  'vector_purchase_orders','vector_quality_inspections','vector_warehouse_tasks',
                  'vector_shipments','vector_returns','vector_sustainability_metrics','vector_shopfloor_events',
                  'vector_nonconformance','vector_transport_exceptions','vector_capacity_calendar','vector_setup_times']
                for t in tables:
                    exists=c.execute('SELECT to_regclass(%s) r',(t,)).fetchone()['r']
                    checks.append({'stage':t,'status':'PASS' if exists else 'FAIL'})
                # Financial-integrity invariants: these are read-only production checks.
                unbalanced=c.execute("""SELECT COUNT(*) n FROM (
                  SELECT j.id FROM vector_journal_entries j JOIN vector_journal_lines l ON l.journal_id=j.id
                  WHERE j.status IN ('posted','reversed') GROUP BY j.id
                  HAVING SUM(l.debit)<>SUM(l.credit)) q""").fetchone()['n']
                checks.append({'stage':'gl_balanced_journals','status':'PASS' if unbalanced==0 else 'FAIL','violations':unbalanced})
                duplicate_auto=c.execute("""SELECT COUNT(*) n FROM (
                  SELECT source_type,source_id FROM vector_journal_entries
                  WHERE source_type IS NOT NULL AND source_id IS NOT NULL AND status='posted'
                  GROUP BY source_type,source_id HAVING COUNT(*)>1) q""").fetchone()['n']
                checks.append({'stage':'automatic_gl_idempotency','status':'PASS' if duplicate_auto==0 else 'FAIL','violations':duplicate_auto})
                overpaid=c.execute("""SELECT COUNT(*) n FROM (
                  SELECT i.id,i.amount,COALESCE(SUM(CASE WHEN p.status='posted' THEN p.amount ELSE 0 END),0) paid
                  FROM vector_supplier_invoices i LEFT JOIN vector_supplier_payments p ON p.invoice_id=i.id
                  GROUP BY i.id,i.amount HAVING COALESCE(SUM(CASE WHEN p.status='posted' THEN p.amount ELSE 0 END),0)>i.amount) q""").fetchone()['n']
                checks.append({'stage':'no_gross_overpayments','status':'PASS' if overpaid==0 else 'FAIL','violations':overpaid})
                stranded=c.execute("""SELECT COUNT(*) n FROM vector_bank_execution_batches b
                  JOIN vector_payment_runs r ON r.id=b.run_id
                  WHERE b.status='rejected' AND r.status NOT IN ('ready_for_bank','cancelled')""").fetchone()['n']
                checks.append({'stage':'rejected_bank_runs_recoverable','status':'PASS' if stranded==0 else 'FAIL','violations':stranded})
                status='PASS' if all(x['status']=='PASS' for x in checks) else 'FAIL'
                return {'run_id':run_id,'tag':TAG,'test_sku':sku,'status':status,'checks':checks,'generated_at':_now()}
            except Exception as e:
                raise HTTPException(500,{'run_id':run_id,'status':'FAIL','error':str(e)})

    @app.get('/v1/acceptance')
    def acceptance_page(request:Request):
        signed_in=bool(request.cookies.get(VECTOR_SSO_COOKIE,''))
        return HTMLResponse("""<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><title>VECTOR Acceptance</title>
<style>body{font-family:-apple-system,system-ui;background:#0b0d10;color:#fff;padding:28px}button{font-size:20px;padding:16px 22px;border:0;border-radius:12px}pre{white-space:pre-wrap;background:#171a20;padding:16px;border-radius:12px}</style>
<h1>VECTOR Final Acceptance</h1><p>Run the authenticated production integrity checks.</p><a href="/v1/acceptance/login" style="display:block;margin-bottom:16px"><button type="button">Sign in with JANUS</button></a><form method="post" action="/v1/acceptance/run"><button type="submit">Run Final Acceptance</button></form><pre>Sign in with JANUS first, then run the test. Your token stays in a secure HttpOnly cookie.</pre>""")

    @app.delete('/v1/acceptance/purge')
    def purge_acceptance(authorization:str|None=Header(None)):
        auth('vector.admin',authorization)
        deleted={}
        with conn() as c:
            # Only records uniquely tagged by this acceptance harness.
            for table in ['vector_materials']:
                try:
                    n=c.execute(f"DELETE FROM {table} WHERE name=%s AND description=%s",(TAG,TAG)).rowcount
                    deleted[table]=n
                except Exception:
                    deleted[table]=0
        return {'status':'purged','tag':TAG,'deleted':deleted}
