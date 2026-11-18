# Power budget - Codex B estimates

**NOT READY FOR FABRICATION.** Allocation arithmetic only; load, thermal and transient qualification remain open.

There are three independent 3 A LMR33630 converters supplied directly by VSYS. +5V is always enabled after the eFuse; +5V_USB and +3V3 use PWR_EN. +1V8 comes from the SoM. Debug USB power supplies only its UART island.

| Rail | Sustained allocation W | Current A | Converter rating A |
|---|---:|---:|---:|
| +5V | 8.70 | 1.74 | 3 |
| +5V_USB | 10.00 | 2.00 | 3 |
| +3V3 | 2.56 | 0.78 | 3 |

Assumptions: SoM8.25W sustained /12.5W transient planning envelopes; 0.45W for carrier1.8V loads; two900mA USB load envelopes plus1Wfan; 2.56Wcarrier3.3V load allocation;92%5V and90%3.3V efficiency;0.6Winput-path allowance. These are not guaranteed component maxima. USB2.0 protocol limits remain applicable.

| Case | Estimated input W | Input A at 15V | Input A at 20V |
|---|---:|---:|---:|
| Planning sustained | 23.77 | 1.58 | 1.19 |
| Planning transient | 28.39 | 1.89 | 1.42 |

Require a qualified 15V/3A or 20V/3A PD source, prefer 20V. The eFuse nominal 2.2A setting and pulse-current behavior must be verified against the source contract. Its current-limit tolerance, startup inrush and fault pulses are not included in this arithmetic. See [electrical corrections](electrical-corrections.md) and [programming requirements](programming-and-recovery.md).

The former model included nonexistent hubs, a cascaded3.3V converter and a 6A 5V converter; those assumptions were removed. Blank 5V-only input does not light a powered-system LED. All rail-load maxima,1.8V tolerance/current, capacitor bias, efficiency curves and temperature margins must be independently established before approving R05.
