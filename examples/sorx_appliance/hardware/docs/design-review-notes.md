# Design review - Codex B

**NOT READY FOR FABRICATION.** The original review remains in the immutable
baseline archive. This active document supersedes its claims that the design
was complete and its unsafe reverse-polarity and USB recovery instructions.

Use the common [R01-R09 release criteria](../../docs/release-criteria.md), the
fresh [verification report](verification-report.md),
[electrical change evidence](electrical-corrections.md),
[programming and recovery requirements](programming-and-recovery.md), and the
mechanical review before judging readiness. Existing Gerbers and passing unit
tests do not close design-release gates.

Implemented electrical corrections include the exact FRAM voltage/package
variant and symbol, 6.04k PHY bias resistor, Toradex-derived TE2309409-2 land,
reverse-protected auxiliary PD supply, contract-to-eFuse gating, a precision
positive sense-clamp candidate, recovery-source interlock and fixture-selection
headers. The BOM includes module/heatsink hardware and marks unquoted lines
explicitly. The power budget now follows the three actual independent3A bucks.

Still requiring independent engineering closure:

- Full manufacturer pin/package audit, including exact TE customer drawing,
  module1.8V tolerance/current guarantee and remaining I210, magnetics, power,
  ESD and strap details. Library pin resolution is not manufacturer validation.
- Positive and negative surge protection, actual initial 5V PD startup,
  capacitor bias/ESR, rail sequencing, overload and source-current margins.
- Approved STUSB4500 and I210 programming assets, a validated programming host
  and fixture, unique MAC provisioning and readback evidence.
- Current required-net connectivity, drill/clearance findings, high-speed paths
  and interface-specific constraints. Review each GND item independently.
- Approved six-layer fabrication stackup and impedance construction, mechanical
  fit/clearances and supplier component geometry, plus complete sourcing.
- USB recovery fixture execution, firmware/device-tree setup, all functional
  interfaces, environmental/thermal and electrical physical bring-up tests.

The Type-A USB2 port retains host functionality. Recovery uses the explicitly
isolated service fixture and interlock; no ordinary A-to-A cable is permitted.
DEBUG is UART only. No USB hubs, carrier1.8V LDO or single6A5V converter are
present in the implemented electrical model.
