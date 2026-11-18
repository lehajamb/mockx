# SORX architecture - Codex B

**NOT READY FOR FABRICATION.** [Generated overview](block-diagram.svg).
Electrical interfaces below describe the model; functionality is not yet measured.

| Appliance interface | Implemented circuit |
|---|---|
| MD-IN | Verdin ETH_1 onboard PHY MDI to J4 magnetics/RJ45 |
| OE | Verdin PCIe to Intel I210-AT and J6 magnetics/RJ45 |
| MGMT | Verdin ETH_2 RGMII to KSZ9131 and J7 magnetics/RJ45 |
| PPS | SMA input, protection and buffers to I210 SDP0 and selected SoM inputs; alternate functions need firmware validation |
| USB1 front label | J8 SuperSpeed host connected directly to SoM USB_2; TPS2065 current switch |
| USB2 front label | J9 USB2.0 host connected to SoM USB_1; recovery interlock and isolated service fixture |
| USB-C DEBUG | FT230X UART console only, host-powered debug island |
| RJ45 CONSOLE | RS232 transceiver and UART receive logic |
| Storage | On-module eMMC; microSD; SPI CY15V104QN FRAM |
| Supervision | Watchdog, temperature sensor, I2C expander, status LEDs, fan control and RTC backup |

USB-C PWR supplies the 80V-rated LT3010 4.5V auxiliary regulator, STUSB4500 and
PD-sense protection candidate before the load. PD_EN_SNK# drives Q5 to inhibit
the TPS26631 eFuse until an allowed contract. VSYS feeds three separate 3A
LMR33630 bucks: +5V for the module, and +5V_USB / +3V3 enabled by PWR_EN.
The module supplies +1V8; +3V3 supplies the PHY 1.2V LDO.

No USB hub is present. Recovery requires the wiring and shunt settings in
[programming and recovery](programming-and-recovery.md). The power assumptions
and unresolved transient limits are in [power budget](power-budget.md) and
[electrical corrections](electrical-corrections.md).
