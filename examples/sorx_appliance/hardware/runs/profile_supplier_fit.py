import sys,time
sys.path.insert(0,"/work/hardware/tools")
import gen_mech as g
old=g.intersection_volume
def timed(a,b,label):
 print("BEGIN",label,flush=True); start=time.monotonic(); result=old(a,b,label); print("END",label,round(time.monotonic()-start,3),result,flush=True); return result
g.intersection_volume=timed
g.P=g.load_parts(g.PARTS_PATH)
g.main(check_only=True)
