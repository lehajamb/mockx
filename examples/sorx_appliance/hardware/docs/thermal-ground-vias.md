# Exposed-pad ground vias

**NOT READY FOR FABRICATION.** These are electrical-connectivity candidates.
The filled/capped process, assembly stencil, thermal performance and layer
stack remain qualification gates.

`tools/thermal_ground_vias.py` only permits via-in-pad on U13 pad49 and U2
pad25 when they are the expected component and GND. It preserves every
existing trace, rejects foreign copper on any copper layer, checks other
pads and physical drill spacing, and records exact new via UUIDs. Native
before/after DRC must show fewer required opens and no new other finding.
The independent verifier must subsequently confirm actual plane connectivity.

For **U13 KSZ9131RNX**, the starting pattern is a 5x5 field at 1.00mm pitch,
with 0.30mm holes and 0.60mm lands. Microchip's
[DS00002841D, Figure 7-3, page 145](https://ww1.microchip.com/downloads/en/DeviceDoc/00002841D.pdf)
shows the 0.30mm/1.00mm field and calls for filled or tented vias to prevent
solder loss. The tool records every position blocked by retained copper.
A partial field is not a claim that the manufacturer's thermal design has
been met; it remains a reviewed layout deviation requiring closure.

For **U2 STUSB4500**, the
[manufacturer datasheet, Table 1](https://www.st.com/resource/en/datasheet/stusb4500.pdf)
requires the exposed pad to connect to GND. A 2x2 field at 1.00mm pitch,
0.30 mm holes and 0.60mm lands is an engineering choice. An exact via count
is not attributed to ST.

The candidate process is **filled and copper-capped via-in-pad**. The wide
exposed-pad mask opening does not tent the top of an ordinary open drilled
via. A normal unfilled drill export therefore must not be sent for assembly
without explicit fabricator/assembler acceptance of the fill, cap, planarity,
stencil and reflow process. This can increase fabrication cost and changes
the assembly assumptions relative to the imported design.

The only geometry exception is the target footprint's own non-copper paste
aperture geometry: paste windows do not constitute foreign electrical
copper, and the selected filled/capped process is needed at these locations.
All real copper pads, opposite-side copper, holes and via keepouts retain
their exclusions. A failing native fixture reproduced the paste-window false
obstacle before the correction; three regression cases then pass.
