# Independent electrical corrections - Codex B

**NOT READY FOR FABRICATION.** These are implemented design changes, with limited
static verification. R02 and R05 remain open as described below. Board connectivity,
ERC, routing and generated-file coherence are separate checks in the release report.

## Reproduced defects and implemented changes

| Item | Submitted baseline | Codex change | Evidence / remaining qualification |
|---|---|---|---|
| U1 FRAM | CY15B104QN (1.8-3.6 V), narrow3.9mm SOIC, nominal 1.8V rail | CY15V104QN-50SXI (1.71-1.89V), wide5.23mm SOIC; pin7 named DNU | Exact variant/package checked in Infineon datasheet. DNU may float or connect VDD; R11 changed to 0ohm for a direct VDD tie; the custom symbol labels DNU correctly. SoM1.8V guaranteed tolerance, load/transient budget still needed. |
| KSZ9131 ISET | 12.1k to GND | 6.04k1% | Microchip DS00002841C ISET requirement; regression protects value. |
| J1 socket | Incorrect locating-hole sizes and hold-down positions/lands | Generator uses Toradex guideFig114 specifically for TE2309409-2 | Peg diameters1.10/1.60, spacing68.8; hold-down spacing67.6,3.5x4.6lands. The exact TE revision 6 land pattern and supplier STEP have now been reviewed; see [socket land validation](te-socket-land-validation.md) and the mechanical evidence. Fabricator tolerance acceptance remains required. |
| UART names | CTS/RTS names swapped on unused socket pins | Pins133/141RTS;135/143CTS | No routed-net change. |
| U2 supply | Raw VBUS directly on28V-absolute-max VDD | U38 LT3010EMS8E#PBF supplies +4V5_PD |80V-input LDO with reverse-input/output protection; ADJ pin2 uses R117/R118=24.9k/10k. Detailed negative/startup/discharge issues below. |
| PD load enable | PD_EN_SNK# only reached pullup and testpoint | Q5 PMV65XP connects auxiliary voltage to SHDN only when PD enable is asserted | Pullup R15 now +4V5_PD; R1164.7k defaults SHDN low; requires programmed POWER_ONLY_ABOVE_5V and a validated startup sequence. |
| PD voltage sense |1k series resistor with no bounded positive clamp |U39TL431BIPK,82k/10k divider; R12 enlarged1206 | Tolerance calculation below; transient/negative protection is not established. |
| Raw/input capacitors |35V capacitors in35.5V clamp domain |C12/C1361uF50V;C18 10uF50V | Primary sourcing/derating qualification is incomplete; see sourcing status. |
| USB3 ESD topology | Long branch routes to U19; optional pass-through pads unused | U19 adds same-net copper pads10/9/7/6 for TX_P/TX_N/RX_P/RX_N | TI Rev.O Table4-2 permits this routing; the custom symbol marks only those physical NC pads passive. No internal IC connectivity is assumed; actual PCB bridges are required. |
| IMON ADC range | R21=24.9k gives excessive ADC voltage during permitted overload pulses | R21=9.1k; nominal gain0.25389V/A | Below1.663V through the full6A monitor specification with worst gain/resistor tolerance; unpowered SoM injection still requires qualification. |
| Mounting | Module/heatsink hardware absent |H5-H8Wurth standoffs;HS1Toradex23111101;SC1-SC4M2x10 assembly lines | Mechanical report owns fit and tolerance qualification. |

## Power sequence and limits

VBUS_IN supplies U38 before the load. Its +4V5_PD output powers U2 VDD, R15
and Q5 source. U2 VSYS stays grounded. Released PD_EN_SNK# leaves Q5 off;
R116 holds SHDN low. A valid permitted contract pulls the gate low, turns Q5
on and raises SHDN, subject to UVLO/OVP and fault state. Auxiliary-rail loss
also leaves SHDN low in the static bound. +5V powers the SoM; PWR_EN then
controls separate +5V_USB and +3V3 converters. +1V8 comes from the module.
U38's EP is GND and needs a thermal path; R1153.9k guarantees its minimum load.
See [the power audit](power-and-recovery-audit.md) for the regulator tolerance,
conditional startup envelope, manufacturer monitoring reconciliation and limits.
The recovery gate U40 uses 1.8V logic and a 22k output pulldown (R114).
With 1% tolerance, 10uA U40 Ioff and 1uA U20 input leakage, its unpowered
node is bounded by 0.245V versus U20's full-temperature 0.65V VIL maximum.
At 1.95V the total high-state load is below 91uA, so U40's 100uA VOH
specification applies. Across its 1.65-1.95V operating band, the full-temperature
VOH floor is 1.50V versus U20's 1.48V VIH requirement at 3.6V. These are
steady-state logic bounds; qualify the actual rail ramps and input transitions.

PD proposal: require a source offering15V/3A and/or 20V/3A, prefer 20V. Program
5V only as the mandatory first PDO; inhibit load at 5V and 9V. The programmed
voltage window must include the sense-divider offset and all source/resistor/
controller tolerances. A 2A contract cannot simply be equated with a nominal 2.2A
eFuse setting. TPS26631 supports pulse current above its nominal limit; the
source/cable/connector response under overload must be qualified.

`python tools/electrical_limits.py` evaluates a deliberately explicit positive
DC tolerance model: TL431BI reference 2.483-2.507V,34mV full-temperature span,
reference-current bound 6.5uA and 1% resistors. It gives21.480-24.336V for the
reference/divider term and3.664V headroom to U2's28V absolute maximum. Under
the assumed35.5V raw clamp, R12 may dissipate0.199W and U39 a conservative
0.345W. These are calculation results, not qualification of the actual circuit.
Current-dependent regulation, temperature derating, parasitics and tolerance
correlation need review. In particular, R12's1206 rating must be checked at
the enclosure's worst ambient rather than using its room-temperature rating.

At20V, the divider alone lowers the sensed value to 19.785V (1.075%). U2 input
leakage and discharge current are additional effects. Do not copy the original
NVM settings without reviewing these effects. U38 has a nominal4.451V output with a conservative regulated4.256-4.651V
bound. The stated source/cable/current assumptions yield a4.173V startup
DC floor. This closes the dropout arithmetic, not waveform qualification. ST's published idle current is not a guaranteed
maximum active load and was not used as one.

## R05 blockers that must remain visible

1. **Initial5V startup and discharge qualification:** LT3010 has explicit
   reverse-input/output protection, including an 80V input/output differential
   rating. This closes the analytically identified ordinary-detach reverse
   voltage defect in the intermediate regulator selection. R115=3.9k gives
   at least1.080mA at the conservative4.256V bound with1% resistance, matching the regulated-output test
   conditions. Retain detach/startup waveform tests for the assembled design;
   minimum initialVBUS, activePD load, output bias and thermal limits remain
   to be established. A controller idle-current number is not an active maximum.
2. **Negative input transients:** D2 is unidirectional. Its forward voltage can
   exceed the -0.3V limit on U2's sense path; U38 now tolerates reverse input. TL431 is a positive shunt,
   not proof of negative clamping. A verified negative-transient solution is
   required, with source impedance and pulse energy specified.
3. **Positive surge:**35.5V is a TVS test-point bound, not a universal waveform
   ceiling. Verify TVS current, wiring inductance, TL431 turn-on/stability,
   U2 input overshoot, R12 pulse energy, U39 thermal behavior and eFuse turnoff.
   No transient simulation or physical waveform has been produced here.
4. **Startup/PD:** confirm U38 at minimum initial VBUS, output-capacitance DC
   bias and ramp; verify SHDN stays asserted until the permitted contract.
   Qualify loss of PD auxiliary power while VBUS is still high. Qualify both
   plug orientations,5V-only sources, renegotiation and brownout.
5. **Programming:** approved NVM artifacts/readback and an independently
   powered1.8V I2C fixture are missing. Firmware availability is a release
   requirement, not a promise that programming later will work.
6. **Recovery:** U40 now inhibits USB1 host-power enable while RECOVERY is
   held low; JP1 selects an isolated host-VBUS sense input and JP2 releases
   the ID ground strap. The specified service fixture preserves both host
   ports and UART console. Validate fixture insulation, USB_1 ROM behavior,
   all power sequences and role control before claiming a recovery pass.

Whole-port reverse-polarity tolerance is **not specified**. The downstream
reverse-blocking FET does not protect components before it. The former
instruction to apply-20V and expect no current has been removed.

See [power and recovery audit](power-and-recovery-audit.md) for independent pin checks, layout requirements, truth table and unresolved operating cases.

## Pin/package validation scope

Independently checked pin definitions: FRAM1CS/2SO/3WP/4GND/5SI/6SCK/7DNU/8VDD;
U38MS8E1OUT/2ADJ/3NC/4GND/5SHDN/6NC/7NC/8IN/9EP; U39PK1REF/2ANODE/3CATHODE;
Q5SOT23 1G/2S/3D; eFuse PWP13SHDN and external reverse-blocking topology;
U2QFN24VDD/18VBUS_VS_DISCH/16VBUS_EN_SNK/22VSYS. KiCad resolves every declared
symbol pin, but this is not a complete independent audit of every manufacturer
pinout on the board. FRAM rail guarantees and the remaining critical pin/package audit are R02 open items.
The exact TE drawing review is complete; fabrication process tolerances and the
physical module/socket assembly remain separate qualifications.

## Manufacturer references

- [Infineon CY15B104QN/CY15V104QN](https://www.infineon.com/assets/row/public/documents/10/49/infineon-cy15b104qn-cy15v104qn-4-mb-excelon-lp-ferroelectric-ram-f-ram-serial-spi-512k-8-industrial-datasheet-en.pdf), voltage variants, package and DNU pin.
- [Microchip KSZ9131RNX](https://ww1.microchip.com/downloads/en/DeviceDoc/00002841C.pdf), ISET.
- [Toradex carrier guide](https://docs.toradex.com/108140-verdin-carrier-board-design-guide.pdf), V1.2Fig114.
- [ST STUSB4500](https://www.st.com/resource/en/datasheet/stusb4500.pdf), DS12499Rev8, VDD/sense limits and load-enable behavior.
- [ADI LT3010](https://www.analog.com/media/en/technical-documentation/data-sheets/lt3010-3010-5.pdf), Rev.J (January2026), including the revision history, electrical limits and MS8E drawing05-08-1662K. The Rev.J changes concern ordering/temperature notes; the selected package and protection requirements remain applicable.
- [TI SN74LVC1G08](https://www.ti.com/lit/ds/symlink/sn74lvc1g08.pdf), SCES217AA, 1.8V logic and partial-power-down behavior.
- [TI SN74LV1T34](https://www.ti.com/lit/ds/symlink/sn74lv1t34.pdf), SCLS743E, translator thresholds and input leakage.
- [TI TL431](https://www.ti.com/lit/ds/symlink/tl431.pdf), SLVS543S sections5,6.12 and stability guidance.
- [TI TPS2663](https://www.ti.com/lit/ds/symlink/tps2663.pdf), SLVSE94G, PWP pins, current limit and reverse-blocking circuit.
- [Nexperia PMV65XP](https://assets.nexperia.com/documents/data-sheet/PMV65XP.pdf), PMOS pinout and150C leakage bound.

Downloaded-source hashes are in `validation/electrical/manufacturer-sources.json`.
Only actually retrieved sources have hashes. Missing documents remain explicitly
unverified rather than receiving invented hashes or approvals.

## Latest manufacturer confidence review

The current [Verdin iMX8M Plus V1.1 datasheet](https://docs.toradex.com/116795-verdin_imx8mp_datasheet.pdf), Table 12, lists PWR_1V8_MOCI on pin 214 as a 1.8 V output with a 250 mA maximum load. That establishes the output load ceiling; the reviewed table does not supply a guaranteed minimum/maximum output-voltage range. Do not infer a tolerance from the nominal voltage. The FRAM requires 1.71-1.89 V at its pins, including module regulation, board drop and transients. Obtain the module-level guaranteed range and finish the aggregate carrier load budget before closing this compatibility item.

For U13, Microchip DS00002841D Figure 7-3 is a recommended land pattern with 0.30 mm thermal holes at 1.00 mm pitch. Its soldering note qualifies the vias with “if used”; the drawing does not establish that all 25 illustrated sites are mandatory electrical connections. Section 6.3 gives thermal data for a different JEDEC 2S2P test board. The five clear U13 thermal vias on the accepted layout close that GND group, while the partial field's thermal performance and assembly process remain unqualified. Do not apply the published junction-to-ambient value directly to this PCB. Source: [Microchip KSZ9131RNX, revision D](https://ww1.microchip.com/downloads/en/DeviceDoc/00002841D.pdf), pages 122 and 145.

Current sourcing observations and the exact service-header/shunt selections supersede the earlier procurement snapshot; see [critical sourcing review](critical-sourcing-review.md). They are not a complete approved orderable BOM.
