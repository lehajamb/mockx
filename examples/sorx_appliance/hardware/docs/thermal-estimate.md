# Thermal estimate

Estimate only. No simulation and no measurement; the numbers below are to be confirmed during bring-up (see `bring-up-checklist.md`, step 9).

## Heat to remove

| Case | Input power | Leaves on USB-A | Dissipated inside |
|---|---:|---:|---:|
| Typical, no USB devices | 8.2 W | 0 | 8.2 W |
| Maximum, SoM at 6.3 W, both USB-A ports at 900 mA | 22.1 W | 9.0 W | 13.1 W |

Source: `power-budget.md`.

## Where it is dissipated (maximum case)

| Part | Loss | Package, path | Estimated rise |
|---|---:|---|---:|
| SoM (SoC about 4.5 W of 6.3 W) | 6.3 W | Heatsink on the SoC, forced air | See below |
| 5 V USB buck (U5) at 9 W out | 0.8 W | HSOIC-8, thermal pad to GND planes, about 43 K/W | +34 K |
| 5 V SoM buck (U4) at 6.3 W out | 0.55 W | Same | +24 K |
| 3.3 V buck (U6) at 3.2 W out | 0.35 W | Same | +15 K |
| I210 (U10, bottom side) | 0.8 W | QFN-64, pad to planes, about 30 K/W | +24 K |
| KSZ9131 (U13) | 0.5 W | QFN-48 | +15 K |
| 1.2 V LDO (U7) | 0.46 W | SOT-223, 60-90 K/W | +28 to +41 K |
| eFuse + blocking FET | 0.1 W | — | negligible |
| Everything else | about 3 W | spread | — |

## Airflow

- Fan: 40 x 40 x 10 mm, 5 V, 4-pin PWM, about 8 m3/h free air (Noctua NF-A4x10 5V PWM class), on the right wall, exhausting. Intake slots on the left wall. Air crosses the SoM heatsink side to side.
- Assume the enclosure halves the free-air flow: 4.1 m3/h = 1.14 l/s.
- Bulk air rise = P / (rho x cp x flow) = 13.1 / (1.16 x 1005 x 0.00114) = **9.9 K** at maximum load, 6.2 K typical.

## SoC junction temperature

Tj = T_ambient + air rise + P_soc x (R_jc + R_tim + R_sa), with R_jc about 1 K/W, thermal tape about 1.5 K/W, and a 40 x 28 x 10 mm finned heatsink in about 1 m/s of air at about 9 K/W.

| Ambient | Tj estimate | Margin to 105 C (industrial-grade limit) |
|---:|---:|---:|
| 25 C | 25 + 9.9 + 4.5 x 11.5 = 86.7 C | 18 K |
| 35 C | 96.7 C | 8 K |
| 45 C | 106.7 C | none |

## Conclusion

- With the fan at 100% and a 10 mm heatsink, all four cores at full load are sustainable up to about **35 C ambient**.
- For 45 C ambient, fit a taller heatsink. There is 15.6 mm of free height above a 10 mm heatsink, so a 20-25 mm part fits and brings R_sa to about 5 K/W (Tj about 89 C at 45 C).
- The Toradex Verdin heatsink needs the optional module standoffs, which rev A does not fit (see `design-review-notes.md`).
- Without the fan the heatsink alone is about 20 K/W and the SoC will throttle under sustained load. The fan tach is wired to the SoM so software can raise ALARM on fan failure; the PWM line fails to full speed.

## Sources

- SoM power: <https://developer.toradex.com/hardware/hardware-resources/power-consumption/verdin-imx8m-plus-power-consumption>
- SoM thermal note (heatsink required under high load): <https://docs.toradex.com/116795-verdin_imx8mp_datasheet.pdf>
- Heatsink, tape and package thermal resistances are typical catalogue values, not measured.
