from datetime import datetime, timezone
from fastapi import Header
def init_supply_chain_kpis(conn):
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS vector_kpi_observations(
        id BIGSERIAL PRIMARY KEY,kpi_code TEXT NOT NULL,period_start DATE NOT NULL,period_end DATE NOT NULL,
        numerator NUMERIC NULL,denominator NUMERIC NULL,value NUMERIC NOT NULL,unit TEXT NOT NULL,dimensions JSONB NOT NULL DEFAULT '{}'::jsonb,created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
KPI_CATALOG=[
('inventory_turnover','Inventory Turnover Ratio'),('order_fulfillment_cycle_time','Order Fulfillment Cycle Time'),('perfect_order_rate','Perfect Order Rate'),('supply_chain_cost_ratio','Supply Chain Cost Ratio'),('inventory_accuracy','Inventory Accuracy'),('on_time_delivery','On-Time Delivery Rate'),('stockout_rate','Stockout Rate'),('warehouse_utilization','Warehouse Utilization'),('supplier_lead_time','Supplier Lead Time'),('freight_cost_per_unit','Freight Cost per Unit'),('return_rate','Return Rate'),('demand_forecast_accuracy','Demand Forecast Accuracy'),('backorder_rate','Backorder Rate'),('fill_rate','Fill Rate'),('procurement_cycle_time','Procurement Cycle Time'),('supplier_performance','Supplier Performance Score'),('inventory_holding_cost','Inventory Holding Cost'),('dock_to_stock_time','Dock-to-Stock Time'),('transportation_utilization','Transportation Utilization'),('cash_to_cash_cycle_time','Cash-to-Cash Cycle Time'),
('mape','MAPE'),('wmape','WMAPE'),('forecast_bias','Forecast Bias'),('otif','OTIF'),('weeks_of_supply','Weeks of Supply'),('service_level','Service Level'),('capacity_utilization','Capacity Utilization'),('available_hours','Available Hours'),('oee','OEE')]
def install_supply_chain_kpi_routes(app,conn,auth):
    @app.get('/v1/analytics/kpis/catalog')
    def catalog(authorization:str|None=Header(None)):
        auth('vector.analytics.read',authorization);return [{'code':c,'name':n} for c,n in KPI_CATALOG]
    @app.get('/v1/analytics/kpis/snapshot')
    def snapshot(authorization:str|None=Header(None)):
        auth('vector.analytics.read',authorization)
        with conn() as c:
            inv=c.execute('SELECT count(*) skus,COALESCE(sum(qty),0) units FROM vector_inventory').fetchone()
            returns=c.execute('SELECT count(*) n FROM vector_returns').fetchone()['n']
            forecasts=c.execute('SELECT count(*) n FROM vector_demand_forecasts').fetchone()['n']
            cap=c.execute('SELECT COALESCE(sum(capacity_per_day),0) cap FROM vector_work_centers').fetchone()['cap']
            return {'as_of':datetime.now(timezone.utc),'catalog_size':len(KPI_CATALOG),'operational_snapshot':{'inventory_skus':inv['skus'],'inventory_units':float(inv['units']),'returns':returns,'forecast_records':forecasts,'configured_daily_capacity_hours':float(cap)},'note':'KPI values require corresponding operational transactions and observations; missing data is not fabricated.'}
    @app.get('/v1/analytics/kpis/{code}')
    def history(code:str,authorization:str|None=Header(None)):
        auth('vector.analytics.read',authorization)
        with conn() as c:return c.execute('SELECT * FROM vector_kpi_observations WHERE kpi_code=%s ORDER BY period_end DESC LIMIT 100',(code,)).fetchall()
