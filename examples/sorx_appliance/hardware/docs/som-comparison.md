# SoM comparison and selection

Checked 2026-11-16. Prices and stock are from distributor pages and search snapshots on that date; they are not a cart-level stock check (that comes with the BOM).

## Pick

**Toradex Verdin iMX8M Plus Quad 4GB IT (Toradex PN 0063, Mouser 145-0063)** — NXP i.MX 8M Plus, 4× Cortex-A53 at 1.6 GHz, 4 GB LPDDR4, 32 GB eMMC, −40 to +85 °C, no radio.

## Hard requirements first

| Requirement | LS1028A | i.MX 8M Plus | AM62x | AM64x |
|---|---|---|---|---|
| ≥ 4 Cortex-A cores | **Fail** (2× A72) | Pass (4× A53) | Pass (4× A53) | **Fail** (2× A53) |
| ≥ 3 GbE ports with hardware timestamps | Pass (up to 6 from the SoC) | Pass (2 native + PCIe NIC) | **Fail** (2 native, no PCIe, USB 2.0 only) | Pass |
| ≥ 2 GB RAM | Pass | Pass | Pass | Pass |

Two of the three suggested families fall on core count, and AM62x has no path to a third timestamping GbE port. i.MX 8M Plus is the only suggested family that passes all three.

## Detail

| | NXP LS1028A | NXP i.MX 8M Plus | NXP i.MX 8M Plus | TI AM62x | TI AM64x | NXP i.MX 95 |
|---|---|---|---|---|---|---|
| SoM | Kontron SMARC-sAL28 | **Toradex Verdin iMX8M Plus Q 4GB IT** | Variscite VAR-SOM-MX8M-PLUS | PHYTEC phyCORE-AM62x | PHYTEC phyCORE-AM64x | Toradex Verdin iMX95 |
| Cores | 2× A72, 1.3 GHz | 4× A53, 1.6 GHz | 4× A53, 1.6–1.8 GHz | 4× A53, 1.4 GHz | 2× A53, 1.0 GHz | 6× A55, 1.8 GHz |
| RAM / eMMC | 2–4 GB / 8–32 GB | 4 GB / 32 GB | up to 8 GB / up to 128 GB | 2 GB / 32 GB | — | 4–16 GB / up to 128 GB |
| Native GbE | 2 on SMARC pins; 4 more via QSGMII; 5 TSN | 2: one with PHY on module (TSN), one RGMII | 2: one with PHY on module, one RGMII | 2 (CPSW3G) | 2 (CPSW3G) + PRU-ICSSG ports | 2 GbE + 10GbE (SoC) |
| Path to 3rd GbE | Native | PCIe Gen3 ×1 → Intel I210 | PCIe Gen3 ×1 → Intel I210 | None with HW timestamps | PCIe Gen2 ×1 or ICSSG | Native / PCIe |
| IEEE 1588 | Yes, all ports | Yes, both MACs; 1588 event pins on edge connector | Yes (same SoC) | Yes (CPSW) | Yes (CPSW, 802.1AS) | Yes |
| TSN MAC | Yes (ENETC + 4-port switch) | Yes on ETH_1 (ENET_QOS) | Yes on one port | TSN-capable switch | Yes | Yes |
| PREEMPT_RT | NXP Real-Time Edge¹ | NXP Real-Time Edge (PREEMPT_RT 6.12/6.18); Toradex ships PREEMPT_RT Torizon builds | NXP Real-Time Edge; vendor Yocto¹ | TI Linux-RT SDK¹ | TI Linux-RT SDK¹ | NXP Real-Time Edge |
| US channel | WDL Systems (US reseller); Mouser listing marked obsolete | Mouser; Toradex webshop (direct) | Variscite webshop (direct) | PHYTEC America (direct, quote); Avnet shows 0 | PHYTEC America (direct, quote) | Mouser lists as factory special order |
| Stock seen | Not available at Mouser | 4GB IT: 0–1 at Mouser, backorder/quote; 4GB Wi-Fi variant: 75 | Not published | Not published | Not published | Not available at Mouser |
| Factory lead time | Unknown | 12–15 weeks (Mouser) | Unknown | Unknown | Unknown | Unknown |
| Price, qty 1 | $320–$522 | $187–$333 (two Mouser listings disagree) | $340–$390 | Quote only (dev kit $349) | Quote only (dev kit $399) | ~$499 (8GB Wi-Fi variant, EU reseller) |
| Price, volume | — | Quote | "from $78" | Quote | Quote | Quote |
| Power | SoC 7.2 W | Module 1.5–6.3 W; 4.43 W mean / 5.26 W peak measured at full stress | Similar (same SoC) | Lower (a Verdin AM62 Dual peaks near 3 W) | — | Higher; within Verdin 8.25 W / 12.5 W envelope |
| Verdict | Rejected: 2 cores | **Selected** | Runner-up; not pin-compatible | Rejected: no 3rd PTP port | Rejected: 2 cores | Upgrade path, same socket |

¹ From general knowledge, not re-verified against a vendor page in this pass.

## Why this SoM

1. **Interfaces fit without glue.** ETH_1 arrives as MDI from an on-module KSZ9131 PHY on the TSN-capable ENET_QOS MAC. ETH_2 is RGMII from the second MAC. Both MACs support IEEE 1588. The single PCIe Gen3 lane takes an Intel I210-AT for the third port, which adds its own PTP clock and SDP pins for PPS.
2. **Time sync is wired, not improvised.** 1588 capture inputs are documented on edge-connector pins for both MACs, so the PPS input can discipline the native MACs as well as the I210.
3. **US channel named in the brief.** Mouser carries the Verdin iMX8M Plus line, and Toradex sells direct through its webshop.
4. **Commodity socket.** The carrier needs one 260-pin DDR4 SODIMM socket (Toradex recommends TE 2309409-2), multi-sourced and cheap.
5. **Headroom on the same carrier.** Verdin iMX95 (6× Cortex-A55, 1.8 GHz) uses the same form factor. Designing the 5 V rail to the Verdin family envelope (8.25 W sustained, 12.5 W peak) keeps that door open. Pin-level compatibility of the "reserved"-class interfaces used here (RGMII, PCIe) must be checked before relying on it.
6. **Board-level details already on the module.** RX8130 RTC with a VCC_BACKUP pin, optional TMP1075 sensor, wide 3.135–5.5 V input.

## What the pick does not give you

- **Not sub-10 µs.** Cortex-A53 with kernel networking under PREEMPT_RT is a tens-to-hundreds-of-microseconds platform; Toradex support quotes worst-case scheduling latency "typically below 100 µs" for i.MX 8. No tick-to-trade figure exists until the board is measured.
- **Exactly 4 cores.** Two isolated cores is the brief's minimum and all this SoC can spare; the reference stack has about five hot threads.
- **Stock of the non-Wi-Fi 4 GB SKU is thin at Mouser.** The Wi-Fi variant is stocked; using it means disabling a radio in a trading appliance. Expect to order direct or wait 12–15 weeks for volume.
- **SoM price dominates the BOM** and is unsettled ($187–$333 at qty 1).
- **Default boot device is eMMC.** Booting from microSD is done in the bootloader order or by leaving the eMMC boot partitions empty; hardware supports both. This is an OS-image decision, noted for bring-up.

## Alternates considered and left out

- **SMARC 2.1 carrier** (Toradex SMARC iMX8M Plus, Variscite VAR-SMARC-MX8M-PLUS, iWave): multi-vendor second source and both PHYs on module. Left out because the Toradex non-Wi-Fi SMARC SKU is not yet orderable, the 314-pin MXM3 connector costs more, and cross-vendor drop-in still needs per-vendor BSP work.
- **Faster cores** (LS1046A 4× A72, Raspberry Pi CM5 4× A76): better single-thread speed, but outside the suggested list, fewer or no TSN MACs, and a more complex carrier. Worth a second look only if measured latency on this board is the blocker.

## Sources

- Toradex Verdin iMX8M Plus datasheet (Ethernet, 1588 pins, USB, RTC, VCC range, 1.5–6.3 W, thermal, connector): <https://docs.toradex.com/116795-verdin_imx8mp_datasheet.pdf>
- Toradex Verdin iMX8M Plus product page (4× A53, 2× GbE one with TSN, PCIe Gen3 ×1, SKUs): <https://www.toradex.com/computer-on-modules/verdin-arm-family/nxp-imx-8m-plus>
- Toradex measured power: <https://developer.toradex.com/hardware/hardware-resources/power-consumption/verdin-imx8m-plus-power-consumption> and <https://community.toradex.com/t/current-consumption-of-verdin-imx8m-plus-quad-4gb-it-toradex/30041>
- Verdin family power envelope (8.25 W / 12.5 W): <https://community.toradex.com/t/verdin-imx8mp-power-consumption-clarifications/22328>
- Mouser, Verdin iMX8M Plus Quad 4GB IT: <https://www.mouser.com/en/ProductDetail/Toradex/Verdin-iMX8M-Plus-Quad-4GB-IT?qs=2FehpBK1j94zl9FtAXR5gQ%3D%3D> and <https://www.mouser.com/ProductDetail/Toradex/0063?qs=TuK3vfAjtkWbVsH/6sZQEQ%3D%3D>
- Toradex PREEMPT_RT on Verdin iMX8M Plus: <https://community.toradex.com/t/real-time-scheduler-in-torizoncore-container/24544>; latency remark: <https://www.toradex.com/community/questions/50585/best-interface-for-low-latency-data-transfer-from.html>
- NXP Real-Time Edge release notes (PREEMPT_RT versions, i.MX 8M Plus): <https://www.nxp.com/rtedge>
- Toradex Verdin iMX95: <https://www.toradex.com/computer-on-modules/verdin-arm-family/nxp-imx95>
- LS1028A (2× A72, 7.2 W, TSN switch): <https://www.mouser.com/pdfDocs/LS1028A_status_external.pdf>; Kontron SMARC-sAL28: <https://www.kontron.com/products/boards-and-standard-form-factors/smarc/smarc-sal28.html>; US prices: <https://www.wdlsystems.com/embedded/com/custitem_ff_processor/NXP-Layerscape>; Mouser status: <https://www.mouser.com/ProductDetail/Kontron/51011-0408-13-2-3?qs=QNEnbhJQKvZ%2BU2hmGdfU8g%3D%3D>
- TI AM625 (4× A53, 2 external GbE ports, two USB 2.0): <https://ti.com/product/AM625/part-details/AM6254ATCGHAALW>
- TI AM6442 (2× A53 at 1.0 GHz, CPSW3G, PCIe Gen2, USB 3.1): <https://www.ti.com/product/AM6442>
- PHYTEC phyCORE-AM62x: <https://www.phytec.com/product/phycore-am62x/>; phyCORE-AM64x: <https://www.phytec.com/product/phycore-am64x/>
- Variscite VAR-SOM-MX8M-PLUS prices: <https://shop.variscite.com/product/system-on-module/var-som-mx8m-plus-system-on-module/>
- Intel I210-AT at Mouser (2,349 tray stock seen): <https://www.mouser.com/c/semiconductors/communication-networking-ics/ethernet-ics/?m=Intel&series=I210>
