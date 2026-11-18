#!/usr/bin/env python3
"""Export final-PCB placement envelopes plus a hash-bound provenance sidecar.

F.Fab/B.Fab rectangles are CAD envelopes, not vendor-qualified component models.
"""
import argparse
import hashlib
import json
from pathlib import Path


def export_parts(board_path, output_path):
    import pcbnew
    board_path, output_path = Path(board_path), Path(output_path)
    source_hash = hashlib.sha256(board_path.read_bytes()).hexdigest()
    board = pcbnew.LoadBoard(str(board_path))
    if board is None:
        raise RuntimeError("Cannot load PCB")
    records = []
    seen = set()
    for footprint in board.GetFootprints():
        ref = footprint.GetReference()
        if ref in seen:
            raise ValueError(f"Duplicate component reference: {ref}")
        seen.add(ref)
        xs, ys = [], []
        basis = "fabrication-layer graphics"
        for graphic in footprint.GraphicalItems():
            if graphic.GetLayer() in (pcbnew.F_Fab, pcbnew.B_Fab) and isinstance(graphic, pcbnew.FP_SHAPE):
                bounds = graphic.GetBoundingBox()
                xs += [bounds.GetLeft(), bounds.GetRight()]
                ys += [bounds.GetTop(), bounds.GetBottom()]
        if not xs:
            basis = "pad bounding box; body model absent"
            for pad in footprint.Pads():
                bounds = pad.GetBoundingBox()
                xs += [bounds.GetLeft(), bounds.GetRight()]
                ys += [bounds.GetTop(), bounds.GetBottom()]
        if not xs:
            raise ValueError(f"No envelope geometry for {ref}")
        position = footprint.GetPosition()
        records.append(dict(ref=ref, value=footprint.GetValue(), fp=str(footprint.GetFPID().GetLibItemName()),
            side="B" if footprint.GetLayer() == pcbnew.B_Cu else "F", x=pcbnew.ToMM(position.x), y=pcbnew.ToMM(position.y),
            rot=footprint.GetOrientationDegrees(), box=[pcbnew.ToMM(v) for v in (min(xs),min(ys),max(xs),max(ys))],
            envelope_basis=basis))
    if hashlib.sha256(board_path.read_bytes()).hexdigest() != source_hash:
        raise RuntimeError("PCB changed during mechanical export")
    records.sort(key=lambda item:item["ref"])
    data=(json.dumps(records,indent=2)+"\n").encode()
    output_path.parent.mkdir(parents=True,exist_ok=True)
    temporary=output_path.with_suffix(output_path.suffix+".tmp")
    temporary.write_bytes(data)
    temporary.replace(output_path)
    metadata={"schema_version":1,"source_pcb_sha256":source_hash,"parts_sha256":hashlib.sha256(data).hexdigest(),
              "footprints":len(records),"pcbnew_version":pcbnew.GetBuildVersion(),
              "limitations":"XY envelopes derive from PCB fabrication graphics or pad bounds; maximum component heights are separate qualification inputs."}
    metadata_path=output_path.with_suffix(output_path.suffix+".meta.json")
    metadata_path.write_text(json.dumps(metadata,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(metadata))
    return metadata


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pcb",type=Path)
    parser.add_argument("output",type=Path)
    args=parser.parse_args()
    export_parts(args.pcb,args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
