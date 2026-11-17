# Power and recovery circuit audit - Codex B

**NOT READY FOR FABRICATION.** This audit compares the resolved model to primary
manufacturer requirements. It does not establish PCB transient, EMC or thermal
performance. Raw pin mappings, ERC, netlist parity and commands are under
`validation/electrical/`; the board routing report is separate.

## Pin and connection review

| Device | Independently checked requirement | Result and remaining work |
|---|---|---|
| U38 LT3010 adjustable MS8E | 1=OUT, 2=ADJ, 3/6/7=NC, 4=GND, 5=SHDN, 8=IN, 9=GND exposed pad | Matches model. IN and SHDN connect VBUS_IN; ADJ connects PD_AUX_ADJ between R11724.9k and R11810k. Take feedback from the local output capacitor, away from switching nodes. |
| U39 TL431 PK | 1=REF, 2=anode, 3=cathode | Matches R112/R113 reference, GND and PD_VS respectively. This is TL431, not the different-pinout TL432. |
| U3 TPS26631 PWP | 1/2/3=IN; 4=B_GATE; 5=DRV; 6=IN_SYS; 7=UVLO; 8=OVP; 9/EP=GND; 10=dVdT; 11=ILIM; 12=MODE; 13=SHDN; 14=IMON; 15=FLT; 16=PGTH; 17=PGOOD; 18/19/20=OUT | Resolved KiCad pin map matches. Q1 source is raw VBUS, drain is VIN_PROT; Q2 uses source=VBUS, gate=DRV and drain=B_GATE, matching the external blocking topology. Exact Q1/Q2 package drawing sign-off remains open. |
| U4/U5/U6 LMR33630 ADDA | 1=PGND, 2=VIN, 3=EN, 4=PG, 5=FB, 6=VCC, 7=BOOT, 8=SW, EP=AGND | Matches all three instances. VCC has 1uF to GND, BOOT has100nF to SW. All three input pins use VSYS directly. PWR_EN has a pulldown; B5S_EN has a VSYS pullup. |
| U7 AZ1117CH-1.2 H package | 1=GND, 2/tab=OUT, 3=IN | Matches. This device is specified only from -20C junction temperature, so the board has no demonstrated -40C operating rating. Its minimum effective output capacitance is10uF; C46 is nominal 22uF, still requiring bias/tolerance confirmation. |
| U20 SN74LV1T34 DBV | 1=NC, 2=A, 3=GND, 4=Y, 5=VCC | Matches the recovery-enable translation path. Full-temperature thresholds, not the simplified front-page example, were used for the R114 calculation. |
| U21 TPS2065C DBV | 1=OUT, 2=GND, 3=FLT, 4=active-high EN, 5=IN | Matches. The selected C version has output discharge; fixture insulation remains mandatory despite this feature. |
| U40 SN74LVC1G08 DBV | 1=A, 2=B, 3=GND, 4=Y, 5=VCC | Matches USB1_EN, RECOVERY#, GND, USB1_EN_SAFE and +1V8. |

References: [ADI LT3010 Rev.J](https://www.analog.com/media/en/technical-documentation/data-sheets/lt3010-3010-5.pdf),
[TI TL431](https://www.ti.com/lit/ds/symlink/tl431.pdf),
[TI TPS2663](https://www.ti.com/lit/ds/symlink/tps2663.pdf),
[TI LMR33630](https://www.ti.com/lit/ds/symlink/lmr33630.pdf),
[Diodes AZ1117C](https://www.diodes.com/assets/Datasheets/AZ1117C.pdf),
[TI SN74LV1T34](https://www.ti.com/lit/ds/symlink/sn74lv1t34.pdf),
[TI TPS2065C](https://www.ti.com/lit/ds/symlink/tps2065c.pdf),
and [TI SN74LVC1G08](https://www.ti.com/lit/ds/symlink/sn74lvc1g08.pdf).

## Auxiliary regulator and PD enable

The selected U38 is **LT3010EMS8E#PBF**, adjustable, not the fixed-5V suffix.
Its nominal output is4.451V. R11724.9k/R11810k, both1%, combined with ADI's
full-temperature reference limits1.237-1.313V and100nA ADJ bias bound give
4.256-4.651V. This is inside STUSB4500's VDD operating range and TPS26631's
0-5V recommended SHDN range. R1153.9k guarantees at least1.080mA even before
controller/divider loads. Minimum effective output capacitance is1uF, ESR<=3ohm;
verify actual C137 bias/tolerance. IN and SHDN may be tied; reverse-input and
reverse-output protection covers the ordinary detach differential. EP requires GND.

ST DS12499 Rev8 section6.1.1 recommends VDD-only for monitoring; we retain it
with VSYS grounded. The supply is derived from incoming VBUS. Section2.2.12
identifies VBUS_VS_DISCH as the measurement input, and Table22 specifies the
high/low monitoring thresholds for any PDO with default VDD=5V. Together these
support regulated VDD with separate sensing; this is an engineering reading of
the manufacturer's stated conditions, not an ST review of this schematic.
No claim is made that VSYS-only would retain all monitoring behavior.

ADI RevJ note5 explicitly defines dropout output as VIN minus dropout.
The50mA maximum dropout is550mV. A **qualified-source envelope** of4.75V at the
source, <=0.5ohm cable/contact loop, and <=54.3mA before main-load enable gives
VBUS_IN>=4.72285V and auxiliary output>=4.17285V, above U2's4.1V minimum.
The current budget includes50mA regulator output,3.3mA regulator ground current
and1mA sense/other allowance. These are explicit design assumptions to verify,
not a claim about every USB charger/cable or an active-controller load guarantee.
C137 charging time, active load, NVM startup, brownout and temperature require
waveform validation. At20V/50mA, pass-device dissipation is about0.78W before
ground current;50mA is not an unconditional board thermal rating.

Q5 is **PMV65XP,215**, pins1=gate,2=source,3=drain, sourced from +4V5_PD.
R15 pulls its gate up to the source; R1164.7k pulls SHDN down. TI bounds SHDN
source current to10uA. Nexperia specifies100uA maximum off leakage at150C;
adding100nA gate leakage gives a conservative0.523V with R116+1%, below the
0.8V guaranteed-low threshold. With the auxiliary supply absent, the eFuse's
own bias alone gives <=0.048V; the PMOS body diode points from SHDN toward the
auxiliary rail and does not inject raw VBUS into SHDN. This removes the known
static fail-open mechanism of the earlier NMOS inversion.

| Auxiliary rail | PD_EN_SNK# | Q5 | SHDN request |
|---|---|---|---|
| Valid | Released/pulled high | Off | Low, load inhibited |
| Valid | Asserted low | On | Auxiliary voltage, load permitted |
| Absent | Either state | No high-side source | Low through R116 |

At the conditional startup floor and ST's0.4V low-output bound, PMOS gate drive
is at least3.773V. Nexperia provides on-resistance specifications at2.5V and1.8V
at25C, giving ample nominal drive for the approximately1mA SHDN pulldown load.
Do not extend that room-temperature resistance limit into an invented full-
temperature guarantee: qualify on-state drop, released-output leakage and all
rail transitions. The off-state leakage bound above uses the explicit150C
specification. NVM POWER_ONLY_ABOVE_5V remains mandatory, with readback evidence.

Sources: [STUSB4500 Rev8](https://www.st.com/resource/en/datasheet/stusb4500.pdf),
[LT3010 RevJ](https://www.analog.com/media/en/technical-documentation/data-sheets/lt3010-3010-5.pdf),
[TPS2663 RevG](https://www.ti.com/lit/ds/symlink/tps2663.pdf), and
[PMV65XP](https://assets.nexperia.com/documents/data-sheet/PMV65XP.pdf).

R20 is DNP, so MODE is open and TPS26631 uses **latch-off**, not auto-retry.
Resetting a latched fault requires a defined SHDN/power cycle. Its permitted
2x overload pulse means a 2.2A nominal setting does not prove compliance with a
2A or 3A source contract under every transient.

## TL431 sense clamp: what the calculation proves

The script bounds only the reference/divider contribution over resistance and
reference tolerances. It produces 21.480-24.336V. It is not a maximum transient
voltage specification. At35.5V raw input and the upper calculated clamp value,
the 1k+1% series resistor provides about 11.05mA. The divider uses less than 0.27mA,
leaving over10.7mA for the cathode before controller input loading. At lower
fault voltages, available cathode current changes; the specified minimum
regulating current is0.7mA and the voltage tables are characterized at 10mA.
Current-dependent regulation therefore remains outside the simple bound.

There is no intentional PD_VS-to-GND capacitor. Obtain U2 pin capacitance and
PCB parasitics and evaluate the applicable **TL431B** stability boundary, not
the older TL431/TL431A curve. Adding a capacitor without reviewing those curves
can cause oscillation. Neither small parasitic capacitance nor a room-temperature
step plot proves turn-on overshoot below 28V. Negative events are also unresolved:
a unidirectional TVS can forward-clamp below U2's -0.3V sense limit. U38's reverse
protection does not protect the separate PD_VS path.

The35.5V upstream TVS value applies at a specified test current; cable inductance,
source energy and waveform must be defined. R12's calculated0.199W and U39's
0.345W conservative DC figures require component derating and pulse review.
No SPICE waveforms, surge tests or oscilloscope measurements are claimed.

## IMON range and unpowered input

R21 changes from 24.9k to 9.1k. The full 6A specified monitor range, a conservative
30.14uA/A maximum gain and 1% resistance produce at most1.662V. The nominal
conversion is `current_A = voltage_V / 0.25389`, before gain/loading calibration.
At the ADC's +/-2.048V setting a 1mV code corresponds to about 3.94mA.

The selected SoM uses a 3.3V TLA2024; Toradex recommends keeping carrier ADC
signals within 0-1.8V. The original overload output exceeded this recommended
range; it was not automatically an immediate1.8V absolute-maximum violation.
See [Toradex module section5.16](https://docs1.toradex.com/110977-verdin_imx8m_plus_v1.1_datasheet.pdf?v=2).

IMON can source current before the module ADC rail is valid. Across the specified
6A monitor range this current is bounded by181uA, with part diverted through R21;
R22 remains1k. This is below the TLA2024's10mA pin-current absolute maximum but
**does not prove the module permits backfeeding its unpowered ADC rail**.
The ADC also requires its analog voltage limits to be observed. Measure startup,
shutdown and hot-plug injection or obtain a module-level guarantee; add proper
power-domain isolation if needed. The100nF filter is after R22, not directly on
IMON; verify its transient loading and ADC settling. Source: [TI TLA2024](https://www.ti.com/lit/ds/symlink/tla2024.pdf).

## Recovery interlock truth table

| USB1_EN | RECOVERY# | USB1_EN_SAFE | Host power request |
|---:|---:|---:|---|
| 0 | 0 | 0 | Off |
| 0 | 1 | 0 | Off |
| 1 | 0 | 0 | Off |
| 1 | 1 | 1 | On, subject to supplies and USB switch |

At +1V8=0, R114=22k bounds the unpowered logic node to 0.245V under the stated
U40/U20 leakage limits. High-state load stays below 100uA and the guaranteed
VOH/VIH comparison passes. During the analog rail ramp, logic behavior is not
assumed valid; the service fixture therefore physically omits J9 VBUS connection
to the PC. Hold RECOVERY throughout service, use JP1 service position, remove
JP2, and follow [the exact fixture procedure](programming-and-recovery.md).

## Physical routing requirements for the added circuit

These are engineering layout constraints, not measured current-temperature results.
Route +4V5_PD and its return with at least 0.25mm copper where space allows;
short pin escapes may use0.15mm. The intended auxiliary current is at most50mA,
subject to the thermal calculation above. Use a local GND connection for U38 EP,
C136/C137/C138 and U39 anode; place at least two nearby GND vias connecting both
reference planes. Keep C136 close to IN/GND, C137 close to OUT/GND, and ADJ
away from switching nodes. Use the actual manufacturer's thermal land.

Keep R12 and U39 adjacent to U2's sense pin; the protected segment must not
wander beside raw VBUS or converter SW nodes. Put R112/R113 close to REF with a
quiet anode return. The sense loop is not a high-current power path. Use a short
Q5 drain path to SHDN; place R116 at U3 SHDN, and R117/R118 by U38 ADJ. Route USB_REC_VBUS as an externally supplied5V sense net,
clearly distinct from +5V_USB; provide header clearance and insulation inspection.
Do not route the recovery data pair through the service power header.

Existing converter input current loops, SW copper, EP/PGND connections,
inductor saturation, capacitor effective values and copper temperature rise
still require layout/thermal review. Do not equate a clean connectivity report
with approval of those circuits.

## Sourcing evidence

This run performed **zero independent distributor stock checks**. Seven inherited
listing snapshots are historical evidence only. Unquoted references include
U1, C12/C136, C18, U38/U39, Q5, H5-H8, HS1, SC1-SC4, JP1/JP2 and J14. Exact header,
shunt and screw procurement selections remain open. Primary datasheet review
establishes requirements; it does not establish stock or lead time.
