"""UNG shared matrix/operator engine.

Provides dimension-checked linear transforms, composed operators, provenance,
weighted fusion, covariance propagation, and domain-ready vector transforms.
No third-party numerical dependency is required.
"""
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from math import sqrt
from typing import Any

def _matrix(a):
    if not isinstance(a,list) or not a or not all(isinstance(r,list) and r for r in a):
        raise ValueError("matrix_required")
    w=len(a[0])
    if any(len(r)!=w for r in a): raise ValueError("ragged_matrix")
    return [[float(v) for v in r] for r in a]

def _vector(x): return [float(v) for v in x]

def matvec(a,x):
    a=_matrix(a); x=_vector(x)
    if len(a[0])!=len(x): raise ValueError(f"dimension_mismatch:{len(a[0])}!={len(x)}")
    return [sum(v*xj for v,xj in zip(row,x)) for row in a]

def matmul(a,b):
    a=_matrix(a); b=_matrix(b)
    if len(a[0])!=len(b): raise ValueError("dimension_mismatch")
    bt=list(zip(*b))
    return [[sum(x*y for x,y in zip(row,col)) for col in bt] for row in a]

def transpose(a): return [list(x) for x in zip(*_matrix(a))]

def compose(*operators):
    if not operators: raise ValueError("operator_required")
    out=_matrix(operators[0])
    for op in operators[1:]: out=matmul(_matrix(op),out)
    return out

def weighted_fusion(vectors,weights=None):
    if not vectors: raise ValueError("vectors_required")
    vs=[_vector(v) for v in vectors]; n=len(vs[0])
    if any(len(v)!=n for v in vs): raise ValueError("dimension_mismatch")
    ws=_vector(weights or [1.0]*len(vs))
    if len(ws)!=len(vs) or sum(ws)==0: raise ValueError("invalid_weights")
    z=sum(ws)
    return [sum(w*v[i] for w,v in zip(ws,vs))/z for i in range(n)]

def propagate_covariance(a,cov):
    # y=A x => Cov(y)=A Cov(x) A^T
    a=_matrix(a); return matmul(matmul(a,_matrix(cov)),transpose(a))

@dataclass
class TransformResult:
    output:list[float]
    input_dim:int
    output_dim:int
    operator_chain:list[str]
    provenance:dict[str,Any]
    timestamp:str

def transform(x,operator,name="linear",provenance=None):
    y=matvec(operator,x)
    return asdict(TransformResult(y,len(x),len(y),[name],provenance or {},
        datetime.now(timezone.utc).isoformat()))

DOMAIN_SCHEMAS={
 "supply_chain":{"inputs":["demand","inventory","lead_time","supplier_risk","capacity","transport_delay"],
                 "outputs":["shortage_risk","procurement_pressure","production_priority","logistics_risk"]},
 "supplier":{"inputs":["price","lead_time","quality","esg_compliance","reliability","risk"],
             "outputs":["cost_exposure","supply_risk","availability","sourcing_priority"]},
 "commercial":{"inputs":["orders","inventory","price","fulfillment_time"],
               "outputs":["revenue_pressure","margin_pressure","working_capital","fulfillment_risk"]},
 "sensor_fusion":{"inputs":["sensor_features"],"outputs":["fused_observation","anomaly_score"]},
 "geometry":{"inputs":["coordinates"],"outputs":["transformed_coordinates"]}
}

def operator_contract():
    return {"version":"1.0","operations":["matvec","matmul","compose","weighted_fusion","propagate_covariance"],
            "domains":DOMAIN_SCHEMAS,"accelerators":["cpu"],"future":["sparse","gpu","learned_operators","optimization"]}
