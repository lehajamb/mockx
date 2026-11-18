import cadquery as cq,json
p="/work/hardware/mech/vendor/te/2309409-2.step"
a=cq.importers.importStep(p).val();ss=a.Solids()
def bounds(s):
 b=s.BoundingBox();return [getattr(b,n) for n in ("xmin","xmax","ymin","ymax","zmin","zmax")]
print(json.dumps({"solids":len(ss),"assembly_bounds":bounds(a),"first_solids":[{"i":i,"bounds":bounds(s)} for i,s in enumerate(ss[:12])]}),flush=True)
