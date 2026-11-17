# Programming and recovery requirements - Codex B

**NOT READY FOR FABRICATION. No approved PD NVM image, I210 NVM image,
programmer execution log or recovery-fixture qualification is supplied.**

## STUSB4500

Use the ST-supported NVM configuration workflow and readback procedure from
[ST's software resources](https://github.com/usb-c/STUSB4500). Do not assume the
factory NVM implements this board's power policy. Proposed policy: prefer 20V/3A,
accept 15V/3A, mandatory 5V first PDO, POWER_ONLY_ABOVE_5V enabled, no9V load.
Review source capabilities, eFuse overload behavior and the approximately 1.075%
sense-divider offset before producing the final binary/register image. Pin16
now controls shutdown through Q5; programming is required before full-load use.

The first programming operation cannot depend on the SoM booting. The eFuse
load is inhibited until a suitable contract and the I2C pullups use the SoM's
+1V8 rail. A qualified fixture must power the PD controller through the intended
input, provide controlled1.8V I2C pullups with the SoM absent or isolated, and
avoid backpowering other rails. J2 is an optional 6-pin1.8V service header:
1=+1V8,2=AUX_TXD,3=AUX_RXD,4=I2C1_SDA,5=I2C1_SCL,6=GND. Its target-power
injection and connected devices require fixture qualification. Do not attach
3.3V/5V I2C pullups. The7-bit ST address is0x28 for grounded address pins.

Release records: tool/version/license, complete configuration source and image,
SHA-256, fixture drawing, commands, controller identity, write/verify status,
readback hash, power-cycle verification and measured5/15/20V contract behavior.
NVM endurance and programming-voltage requirements must follow the ST guide.

## Intel I210-AT

The board uses external SPI flash. Obtain the correct supported **binary** NVM
image for copper I210-AT and the approved programming utility. Intel's
[I210/I211 FAQ](https://cdrdv2-public.intel.com/334026/ethernet-controller-i210-i211-faq.pdf)
identifies image package513655 and tools348742, with EEUPDATE for production
and LANConf for development. Flash binaries and iNVM text programming are
not interchangeable. Do not burn one-time iNVM as a substitute for a missing
flash image. Tool access and support for the selected host architecture must
be established; availability on this AArch64 SoM has not been demonstrated.

Record image version/hash/license, target flash geometry, exact programming
command, unique authorized MAC allocation, programmed identity/checksum,
readback, cold-boot enumeration and link/PTP validation. A blank device may
not bind the normal networking driver; `ethtool -E` is not a proven blank-device
manufacturing procedure. No downloaded or generated NVM asset is claimed here.

## Recovery and console

USB-C DEBUG connects to FT230X UART only. J9 carries SoM USB_1 using a Type-A
host connector. **An ordinary USB A-to-A cable is prohibited.** The service
fixture below intentionally disconnects the VBUS conductor at J9.

| Fixture connection | Appliance destination | Purpose |
|---|---|---|
| PC USB D- | J9 pin2, USB1_D_N | USB_1 data |
| PC USB D+ | J9 pin3, USB1_D_P | USB_1 data |
| PC USB GND | J9 pin4 and J14 pin2 | Common signal reference |
| PC USB VBUS | J14 pin1 only | Presence sense via JP1 and R83; not appliance power |
| **No connection** | J9 pin1, USB1_VBUS | Prevent host/board power contention |
| Cable shield | Connector shield/chassis per fixture EMC review | Screen; not a power return substitute |

The device-side Type-A service plug must have pin1 physically unconnected;
verify insulation to PC VBUS with a meter before attachment. Maintain a short
90ohm differential cable pair; do not route data through loose breadboard wires.
The fixture is defined here but has not been built or signal-integrity tested.

| Configuration | JP1 | JP2 | RECOVERY button |
|---|---|---|---|
| Normal host operation | Shunt1-2 | Fitted1-2 | Released |
| Recovery service | Shunt2-3 | Removed | Held throughout service |

Move shunts only with both power inputs and host cable disconnected. JP1 routes
host VBUS sense through the existing10k R83 to the SoM USB_1_VBUS input; it does
not connect PC VBUS to +5V_USB or J9 VBUS. JP2 removes the USB_1_ID ground strap
through R84; the existing pullup option R85 remains DNP. Confirm module input
bias and ROM device-role behavior on the exact selected SoM revision.

U40 computes USB1_EN_SAFE = USB1_EN AND RECOVERY#. Holding RECOVERY low therefore
inhibits U20/U21's host-power enable path. R114=22k holds the logic node low when
the 1.8V gate is unpowered. This interlock supplements physical VBUS isolation;
it does not replace it. Verify that the TPS2065 enable is low during all
power-up/down sequences and that J9 has discharged before plugging the fixture.

After the interface is qualified, follow the selected module's
[Toradex recovery requirements](https://docs1.toradex.com/110977-verdin_imx8m_plus_v1.1_datasheet.pdf?v=2):
remove bootable microSD, assert pin246 CTRL_RECOVERY_MICO# through no more than1k
at **cold power-on**, and connect the qualified USB_1 fixture. A RESET tap alone
is not the documented entry sequence. Hold RECOVERY for the entire fixture
session to keep the hardware source interlock active. Use the separate USB-C
DEBUG connection if a UART log is needed. Select and hash a matching Toradex
Easy Installer/recovery loader; retain actual enumeration and recovery from
invalid boot media before closing the gate. Restore normal shunts before host use.
