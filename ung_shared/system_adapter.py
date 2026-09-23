"""System adapter for the shared UNG Frame + Propagation Engine.
Role: VECTOR local/global warehouse and movement frame adapter
"""
from ung_shared.frame_propagation import FrameGraph, FrameTransform, propagation_product

frames=FrameGraph()

def register_frame(source:str,destination:str,matrix:list,timestamp:str|None=None,version:str="ung-frame-v1"):
    x=FrameTransform(source,destination,matrix,timestamp,version);frames.add(x)
    return {"source":source,"destination":destination,"timestamp":timestamp,"version":version}

def convert_position(position:list[float],source:str,destination:str):
    return {"position":frames.convert(position,source,destination),"source_frame":source,"destination_frame":destination,"provenance":"DERIVED"}

def link_timing(origin_m:list[float],destination_m:list[float],speed_mps:float=299792458.0):
    return propagation_product(origin_m,destination_m,speed_mps)
