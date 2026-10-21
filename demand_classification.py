from fastapi import Header
import math
def install_demand_classification_routes(app,conn,auth):
 @app.get('/v1/planning/demand-classification/{sku}')
 def classify(sku:str,authorization:str|None=Header(None)):
  auth('vector.planning.read',authorization)
  with conn() as c:rows=c.execute("SELECT period_start,quantity FROM vector_demand_forecasts WHERE sku=%s ORDER BY period_start",(sku,)).fetchall()
  vals=[float(r['quantity']) for r in rows]
  if not vals:return {'sku':sku,'classification':'insufficient_data','observations':0}
  nz=[(i,v) for i,v in enumerate(vals) if v>0]
  if not nz:return {'sku':sku,'classification':'intermittent','adi':None,'cv2':None,'recommended_models':['Croston','SBA','TBS','ADIDA']}
  intervals=[nz[i][0]-nz[i-1][0] for i in range(1,len(nz))]
  adi=sum(intervals)/len(intervals) if intervals else 1.0
  mean=sum(v for _,v in nz)/len(nz);sd=(sum((v-mean)**2 for _,v in nz)/len(nz))**0.5;cv2=(sd/mean)**2 if mean else 0
  if adi<1.32 and cv2<0.49:cl='smooth';models=['Exponential Smoothing','Moving Average']
  elif adi<1.32 and cv2>=0.49:cl='erratic';models=['Holt','Holt-Winters']
  elif adi>=1.32 and cv2<0.49:cl='lumpy';models=['Croston','SBA','TBS','ADIDA']
  else:cl='intermittent';models=['Croston','SBA','TBS','ADIDA']
  return {'sku':sku,'classification':cl,'adi':round(adi,4),'cv2':round(cv2,4),'observations':len(vals),'nonzero_observations':len(nz),'recommended_models':models}
