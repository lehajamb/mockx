# Power budget

Computed by `tools/power_budget.py` (re-run to regenerate the tables below). Date: 2026-11-16.

## Result

- **PD contract: request 20 V, accept 15 V, at 2 A or more.** A 30 W source is the minimum; a 45 W source is recommended.
- Worst case with the selected SoM and both USB-A ports at 900 mA: **22.1 W** at the input (1.5 A at 15 V, 1.1 A at 20 V).
- Transient ceiling if a hotter Verdin SoM is fitted later (12.5 W peak): **28.9 W**, 96% of a 15 V / 2 A contract. That is why 45 W is the recommendation.
- Typical running draw with no USB devices: about 8 W.
- Below 15 V (5 V or 9 V only sources) the eFuse UVLO keeps the board off and the PWR LED shows the fault.

## Power tree

```
USB-C PWR ─ PD sink (CC) ─ TVS ─ eFuse (reverse-polarity, OVP, UVLO, ILIM ≈ 2.2 A) ─ VSYS 15/20 V
VSYS ─ 5 V buck, 6 A ──┬─ SoM VCC
                       ├─ USB-A VBUS ×2 (900 mA switch each)
                       ├─ fan header
                       └─ 3.3 V buck, 3 A (enabled by SoM CTRL_PWR_EN_MOCI) ─┬─ I210, mgmt PHY, USB hubs, microSD, LEDs, logic
                                                                              └─ 1.8 V LDO ─ RGMII PHY I/O
USB-C DEBUG VBUS ─ 3.3 V LDO ─ USB 2.0 hub + USB-UART (separate island, host powered)
CR2032 ─ SoM VCC_BACKUP (RTC only)
```

### 3.3 V rail (5 V -> 3.3 V buck, gated by SoM CTRL_PWR_EN_MOCI)

| Load | Typ W | Max W | Basis |
|---|---:|---:|---|
| Intel I210-AT (order-entry NIC, 1000BASE-T active) | 0.60 | 0.80 | allowance |
| KSZ9131RNX mgmt PHY incl. 1.2 V core + 1.8 V RGMII I/O | 0.50 | 0.75 | allowance |
| USB 3 hub, 2 downstream ports | 0.40 | 0.60 | allowance |
| microSD card (boot) | 0.20 | 0.66 | 200 mA card peak |
| Front-panel LEDs x7 @ 5 mA | 0.08 | 0.15 | allowance |
| RS-232 xcvr, console mux, PPS buffer, watchdog, temp sensor, I210 flash | 0.10 | 0.20 | allowance |
| **3.3 V total** | **1.88** | **3.16** | 0.96 A max -> 3 A converter |

### 5 V rail (VSYS 15/20 V -> 5 V buck)

| Load | Typ W | Max W | Basis |
|---|---:|---:|---|
| SoM VCC - Verdin iMX8M Plus (measured 4.43 mean / 5.26 peak; datasheet max 6.3) | 4.43 | 6.30 | Toradex |
| USB-A port 1 VBUS @ 900 mA | 0.00 | 4.50 | USB 3.x 900 mA |
| USB-A port 2 VBUS @ 900 mA | 0.00 | 4.50 | USB 3.x 900 mA |
| Fan, 40 mm 5 V PWM | 0.50 | 1.00 | allowance |
| 3.3 V buck input (90% eff.) | 2.09 | 3.51 | from table above |
| **5 V total** | **7.02** | **19.81** | 3.96 A max |

### Input power vs. USB-C PD contract

| Case | 5 V rail W | 5 V rail A | Input W | 15 V / 2 A (30 W) | 15 V / 3 A (45 W) | 20 V / 2.25 A (45 W) | 20 V / 3 A (60 W) |
|---|---:|---:|---:|---:|---:|---:|---:|
| Typical: SoM stress mean, no USB devices | 7.02 | 1.40 | 8.23 | 27% | 18% | 18% | 14% |
| Max, Verdin iMX8M Plus: SoM 6.3 W, both USB-A at 900 mA | 19.81 | 3.96 | 22.13 | 74% | 49% | 49% | 37% |
| Max, any Verdin SoM sustained (8.25 W) | 21.76 | 4.35 | 24.25 | 81% | 54% | 54% | 40% |
| Peak, any Verdin SoM (12.5 W transient) | 26.01 | 5.20 | 28.87 | 96% | 64% | 64% | 48% |

Input current at worst case: 1.92 A @ 15 V, 1.44 A @ 20 V. 5 V converter sized for 5.20 A -> 6 A part.
Heat dissipated inside the enclosure at the iMX8M Plus max case (input minus the 9 W exported on USB-A): 13.13 W.

## Basis for the numbers

| Item | Value | Source |
|---|---|---|
| SoM, full stress | 4.43 W mean, 5.26 W peak | Toradex measurement, PN 0063 |
| SoM, datasheet range | 1.5–6.3 W | Verdin iMX8M Plus datasheet, Table 8 |
| Any Verdin SoM | 8.25 W sustained, 12.5 W peak | Verdin Family Specification, as quoted on the Toradex forum |
| USB-A | 900 mA per port at 5 V | Brief; USB 3.x port current |
| I210, PHY, hub, fan, LEDs, logic | Allowances | To be replaced with datasheet maxima when parts are fixed in the schematic |
| Converter efficiency | 92% (5 V), 90% (3.3 V) | Allowance; confirm against the chosen regulators |
| Input path loss | 0.6 W | Allowance for eFuse and blocking FET I²R, TVS leakage, PD controller |

The allowances total 3.5 W (2.5 W on the 3.3 V rail plus the 1 W fan). Doubling every one of them moves the worst case from 22.1 W to 26.2 W, still inside a 30 W contract.

## Thermal pointer

At the 22.1 W worst case, 9 W leaves through the USB-A ports, so about 13 W is dissipated inside the enclosure, of which up to 6.3 W is in the SoM. The full thermal estimate (heatsink, fan curve, enclosure rise) is a later deliverable.

## Sources

- <https://developer.toradex.com/hardware/hardware-resources/power-consumption/verdin-imx8m-plus-power-consumption>
- <https://community.toradex.com/t/current-consumption-of-verdin-imx8m-plus-quad-4gb-it-toradex/30041>
- <https://docs.toradex.com/116795-verdin_imx8mp_datasheet.pdf>
- <https://community.toradex.com/t/verdin-imx8mp-power-consumption-clarifications/22328>
