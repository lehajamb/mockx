#!/usr/bin/env python3
"""Conservative power-only completion candidates with per-connection widths.

Never reroutes existing copper, never treats In2 as an outer layer, and never
accepts a new non-connectivity DRC finding. Widths are declared engineering
allocations; thermal qualification of the complete current path remains open.
"""
from pathlib import Path
import argparse
import hashlib
import json
import math
import shutil
import sys


def policy(ends):
    net=ends[0][2]
    descriptions=" ".join(e[3] for e in ends)
    if net=="+4V5_PD": return .25, .05, False, "auxiliary regulator maximum output"
    if net=="+1V8": return .30, .25, False, "SoM1.8V carrier allocation; total load qualification pending"
    if net=="USB_REC_VBUS": return .25, .001, False, "isolated recovery sense input, not a power source"
    if net=="USB1_VBUS" and "JP1" in descriptions:
        return .25, .001, False, "JP1 detection-only branch; main host current excluded"
    if net in ("VBUS_DBG_RAW","VBUS_DBG"):
        return .35, .10, False, "USB debug UART island allocation"
    if net=="VBUS_IN":
        if any(ref in descriptions for ref in ("U38","C136")):
            return .25, .06, False, "auxiliary regulator input/decoupling branch"
        if "R12" in descriptions:
            return .25, .036, False, "PD sense/discharge resistor branch"
        return 2.0, 3.0, True, "main input trunk; outer layers only, thermal qualification pending"
    if net=="+5V": return 1.5, 3.0, True, "SoM converter trunk; outer layers only, thermal qualification pending"
    if net=="+3V3": return 1.0, 1.0, True, "carrier3.3V trunk; outer layers only"
    if net=="+1V2_PHY": return .25, .30, False, "short PHY supply-pin escape; complete rail review pending"
    if net=="OE_0V9": return .50, .60, False, "I210 core branch allocation; load qualification pending"
    return None


def endpoint_supported(ends):
    return all(e[1] and all(layer in "FBC" for layer in e[1]) for e in ends)


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    import pcbnew
    import route_completion as rc
    from verification_core import parse_drc
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source",type=Path)
    parser.add_argument("output",type=Path)
    parser.add_argument("evidence",type=Path)
    args=parser.parse_args()
    source=args.source.resolve(); output=args.output.resolve(); evidence=args.evidence.resolve()
    if output.exists() or source==output: raise ValueError("Output must be a new checkpoint")
    if evidence.exists(): raise ValueError("Evidence directory must be new")
    if not source.with_suffix(".kicad_pro").is_file(): raise ValueError("Source project is required")
    evidence.mkdir(parents=True); output.parent.mkdir(parents=True,exist_ok=True)
    for suffix in (".kicad_pro",".kicad_dru"):
        if source.with_suffix(suffix).is_file(): shutil.copy2(source.with_suffix(suffix),output.with_suffix(suffix))
    board=pcbnew.LoadBoard(str(source)); source_hash=sha(source)
    records=[]
    def report(name):
        path=evidence/(name+".txt")
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())
        pcbnew.WriteDRCReport(board,str(path),pcbnew.EDA_UNITS_MILLIMETRES,True)
        return path,parse_drc(path.read_text())
    before_path,before=report("before-drc")
    old_other={f["fingerprint"] for f in before["findings"] if f["section"]!="unconnected pads"}
    todo=rc.parse_unconnected(before_path)
    widths=sorted({policy(e)[0] for e in todo if policy(e)})
    for width in widths:
        rc.W_TRK=width
        grid=rc.Grid(board);grid.load(board)
        current_path,current=report("width-%s-before"%width)
        pairs=[e for e in rc.parse_unconnected(current_path) if policy(e) and policy(e)[0]==width]
        pairs.sort(key=lambda e: math.dist(e[0][0],e[1][0]))
        for ends in pairs:
            w,amperes,outer,reason=policy(ends)
            entry=dict(net=ends[0][2],width_mm=w,current_allocation_a=amperes,
                       rationale=reason,endpoints=ends,accepted_route=False)
            records.append(entry)
            if not endpoint_supported(ends):
                entry["refusal"]="Explicit In2 or unknown-layer endpoint requires reviewed physical transition; no relabeling"
                print("REFUSED",entry["net"],entry["refusal"],flush=True);continue
            (a,la,net,da),(b,lb,_,db)=ends
            result=grid.route(net,a,la,b,lb,rip=False)
            if result is None: result=grid.route(net,b,lb,a,la,rip=False)
            if result is None:
                entry["refusal"]="No clearance-safe path at the declared width";print("NO PATH",net,flush=True);continue
            runs,vias=result
            if outer and (vias or any(layer not in "FB" for layer,points in runs)):
                entry["refusal"]="Main-current route would require unqualified via or internal-layer current path"
                print("REFUSED TRANSITION",net,flush=True);continue
            rc.commit(board,grid,net,board.GetNetsByName()[net],runs,vias,width)
            entry.update(accepted_route=True,runs=runs,vias=vias)
            print("ROUTED",net,width,"mm",len(vias),"vias",flush=True)
    after_path,after=report("after-drc")
    new_other=[f for f in after["findings"] if f["section"]!="unconnected pads" and f["fingerprint"] not in old_other]
    accepted=not new_other and after["counts"]["unconnected pads"]<=before["counts"]["unconnected pads"]
    pcbnew.SaveBoard(str(output),board)
    result=dict(status="ACCEPTED FOR FURTHER DEVELOPMENT" if accepted else "REJECTED",fabrication_ready=False,
                source=str(source),source_sha256=source_hash,output=str(output),output_sha256=sha(output),
                tool_sha256=sha(__file__),router_sha256=sha(rc.__file__),before=before["counts"],after=after["counts"],
                new_other_findings=new_other,routes=records,thermal_qualification=False)
    (evidence/"result.json").write_text(json.dumps(result,indent=2)+"\n")
    print(json.dumps({k:result[k] for k in ("status","before","after")}),flush=True)
    return 0 if accepted else 1


if __name__=="__main__": raise SystemExit(main())
