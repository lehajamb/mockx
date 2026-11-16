# Bring-up checklist (rev A, first boards)

Work top to bottom. Stop at the first failed step. Record every measured value.

## 0. Before power

- [ ] Compare the bare PCB against `hw/fab/` and the SoM socket J1 against a real TE 2309409-2 (pads, pegs, hold-downs). **Do not fit the SoM until this passes.**
- [ ] Visual inspection: polarity of C23, C106, C110, D2, LEDs; orientation of U3, U10 (bottom side), U13, J1.
- [ ] Resistance to GND, board unpowered, SoM out: VBUS_IN, VSYS, +5V, +5V_USB, +3V3, +1V8, +1V2_PHY all > 100 ohm.

## 1. Input and protection (SoM not fitted)

- [ ] Plug a 45 W USB-C PD source into PWR. Expect VBUS_IN = 20 V (15 V on a source without a 20 V PDO).
- [ ] Factory STUSB4500 NVM requests 20 V at 1 A / 15 V at 1.5 A. Reprogram over I2C (0x28) to 20 V / 15 V at 2 A, power only above 5 V. Until then keep USB-A loads off.
- [ ] VSYS follows VBUS_IN. Check eFuse thresholds with a bench PD trigger or lab supply on TP VBUS_IN: turn-on at 12.1 V rising, cut-off at 22.6 V, current limit about 2.2 A. **Confirm R19 (ILIM), R21 (IMON) against the TPS26631 datasheet first.**
- [ ] Reverse polarity: apply -20 V, current limited to 50 mA, at TP VBUS_IN. Expect no current and no damage.
- [ ] +5V = 5.0 V +/-3% with VSYS present. +5V_USB and +3V3 stay at 0 V (they wait for the SoM).

## 2. Carrier rails (SoM not fitted)

- [ ] Pull PWR_EN to 1.8 V through 1 k from a bench supply. Expect +5V_USB = 5.0 V, +3V3 = 3.3 V, +1V2_PHY = 1.2 V.
- [ ] Ripple on each rail < 30 mV p-p at 1 A load. PWR LED on. ALARM LED on (fail-closed default).

## 3. SoM

- [ ] Fit the Verdin iMX8M Plus, CR2032 and heatsink. Power up. PWR_1V8 (TP +1V8) = 1.8 V, PWR_EN high, RESET_MOCI# high.
- [ ] Console on USB-C DEBUG (FT230X, 115200 8N1) shows the boot loader. Repeat on the RJ45 console with a Cisco rollover cable.
- [ ] Recovery: hold RECOVERY, tap RESET, connect USB 2 to a host with an A-to-A cable. The SoM enumerates as an NXP serial-download device.
- [ ] Boot from microSD (boot order set in the boot loader; default boot device on the SoM is eMMC).

## 4. Ethernet

- [ ] MD-IN (SoM ETH_1): link at 1000 Mb/s, both LEDs, iperf3 > 900 Mb/s, zero CRC errors in 10 minutes.
- [ ] MGMT (SoM ETH_2, KSZ9131 at MDIO address 7): same test. Confirm strap result in PHY registers (RGMII, all speeds).
- [ ] OE (I210): `lspci` shows 8086:1533 (or 1531 with blank flash). Program the flash image and MAC address, reboot, repeat the link test.
- [ ] Multicast: join 239.192.10.1 on MD-IN and confirm IGMP membership on the switch.

## 5. Time

- [ ] `ethtool -T` shows hardware timestamping on all three ports.
- [ ] 1 PPS, 3.3 V, into the SMA: `ts2phc` locks the I210 clock on SDP0; pps-gpio on GPIO_1 fires; ENET_QOS capture on pin 218.
- [ ] RTC keeps time across a 10-minute power-off.

## 6. USB, storage, I2C

- [ ] USB 1: SuperSpeed device enumerates at 5 Gb/s; 900 mA load holds VBUS > 4.75 V; short circuit raises USB2_OC#.
- [ ] USB 2: high-speed device enumerates; 900 mA load test.
- [ ] microSD: card detect, UHS mode after a warm reset (card power cycles through Q3/Q4).
- [ ] I2C_1 scan: 0x20 (TCA9534), 0x28 (STUSB4500), 0x48 (TMP1075).
- [ ] FRAM U1: read ID over SPI_1, write/read 64-byte record, measure write latency.

## 7. Front panel and supervision

- [ ] Each LED lights on command: SYS, FEED A, FEED B, FIX A, FIX B. ALARM turns off only when ALARM_OFF is driven high.
- [ ] Watchdog: set WD_EN high (60 s), stop kicking, expect a reset and ALARM on. Then pull WD_SET0_N low after WD_EN for the 10 s window. Never pull WD_SET0_N low first (that selects 30 ms).
- [ ] RESET button resets the SoM.
- [ ] Fan: tach reads on GPIO_5; PWM sweeps speed; floating PWM gives full speed.
- [ ] IMON_ADC tracks input current (calibrate against a bench meter).

## 8. Reference stack

- [ ] Build and run the MockX feed client and the two-venue SOR against a MockX host. FEED A/B and FIX A/B LEDs follow `LIVE` and logon state.
- [ ] Measure wire-in to wire-out latency with the hardware timestamps on MD-IN and OE, both clocks disciplined by PPS.

## 9. Thermal soak

- [ ] Lid on, 25 C ambient, `stress-ng --cpu 4` plus iperf3 on all ports plus 2 x 900 mA USB loads, 2 hours.
- [ ] Pass: SoC thermal zone < 95 C, no throttling, U5/U7 case < 85 C, no ALARM.
