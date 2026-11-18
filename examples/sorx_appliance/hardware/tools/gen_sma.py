"""Exact 132289 land pattern from the manufacturer drawing (Rev B change table)."""
from pathlib import Path
from sx import parse, dump, first, find
NAME = "SMA_Amphenol_132289_Drawing"
SOURCE = Path("/usr/share/kicad/footprints/Connector_Coaxial.pretty/SMA_Amphenol_132289_EdgeMount.kicad_mod")
def footprint():
    shape = parse(SOURCE.read_text())
    shape[1] = NAME
    first(shape,"descr")[1] = "Amphenol 132289 manufacturer drawing: signal1.50x5.20, ground1.60x4.90, edge setbacks0.30/0.60mm. PCB nominal1.57mm; exact finished thickness tolerance and latest drawing revision require supplier/fabricator sign-off."
    for text in find(shape,"fp_text"):
        if text[1] == "value": text[2] = NAME
    for pad in find(shape,"pad"):
        signal = pad[1] == "1"
        first(pad,"size")[1:] = [1.5 if signal else 1.6, 5.2 if signal else 4.9]
        first(pad,"at")[1] = -.36 if signal else -.51
    return shape
def main():
    target = Path(__file__).resolve().parents[1] / "hw/sorx.pretty" / (NAME + ".kicad_mod")
    target.write_text(dump(footprint()) + "\n")
if __name__ == "__main__": main()
