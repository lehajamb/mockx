# Manufacturing test plan

Scope: functional test of assembled carriers with a known-good SoM fitted in the test fixture, then final test of built units. No in-circuit test fixture is assumed for rev A; test pads exist on every rail.

## Equipment

- USB-C PD source, 45 W or more, with a current readout. Lab supply 0-25 V / 3 A for threshold tests.
- Host PC with three GbE ports (one supports multicast and PTP), a USB A-to-A cable, USB-C cable, Cisco rollover cable.
- 1 PPS source (GPS-disciplined or a PTP grandmaster PPS out), 3.3 V.
- Two USB loads, 900 mA each. USB 3 flash drive. microSD card with the test image.
- Golden Verdin iMX8M Plus with the test image (boots from microSD, runs the script over the console).

## Programming steps

| Step | Target | Method | Record |
|---|---|---|---|
| P1 | STUSB4500 NVM: 20 V / 15 V at 2 A, power only above 5 V | I2C from the SoM (0x28) | NVM readback |
| P2 | I210 flash: NVM image and unique MAC address | `ethtool -E` or Intel EEUPDATE from the SoM | MAC, checksum |
| P3 | Serial number label and record | Fixture | Serial, MACs, date |

## Test sequence and limits

| # | Test | Method | Pass limit |
|---|---|---|---|
| 1 | Shorts | Resistance to GND on TP VBUS_IN, VSYS, +5V, +5V_USB, +3V3, +1V8, +1V2_PHY before power | > 100 ohm |
| 2 | Input | PD source attached | VBUS_IN 19-21 V (or 14.25-15.75 V), idle input < 3 W without SoM |
| 3 | UVLO / OVP | Lab supply sweep on TP VBUS_IN (sample basis, 1 in 20) | on 11.5-12.7 V, off 21.9-23.3 V |
| 4 | Rails | DMM on test pads, SoM fitted | +5V, +5V_USB 4.85-5.15 V; +3V3 3.20-3.40 V; +1V8 1.71-1.89 V; +1V2_PHY 1.14-1.26 V |
| 5 | Console | Boot banner on USB-C DEBUG and on RJ45 console | Both receive and transmit |
| 6 | I2C | Scan I2C_1 | 0x20, 0x28, 0x48 present |
| 7 | MD-IN | 1000BASE-T link, iperf3 30 s each way | > 900 Mb/s, 0 CRC errors |
| 8 | MGMT | Same | Same |
| 9 | OE | PCIe enumeration, then same link test | Device 8086:1533, > 900 Mb/s, 0 CRC |
| 10 | PTP | Hardware timestamp capability on 3 ports; PPS captured on I210 SDP0 and SoM | 10 consecutive edges, period 1 s +/-100 ppm |
| 11 | USB 1 | SuperSpeed enumeration, 900 mA load | 5 Gb/s, VBUS > 4.75 V |
| 12 | USB 2 | High-speed enumeration, 900 mA load; recovery-mode enumeration (sample basis) | 480 Mb/s, VBUS > 4.75 V |
| 13 | USB over-current | Short VBUS on each port | OC flag within 1 s, recovers |
| 14 | microSD | Detect, write/read 16 MB | No errors |
| 15 | FRAM | Device ID, 1 kB pattern write/read | Match |
| 16 | LEDs | Each LED on in turn, camera or operator | 7 of 7 |
| 17 | ALARM default | Reset the SoM, observe before software starts | ALARM on |
| 18 | Watchdog | Enable, withhold kick | Reset in 60 s +/-20% |
| 19 | Buttons | RESET, RECOVERY | Reset observed; recovery pin low |
| 20 | Fan | PWM 30% and 100%, read tach | Speed ratio > 1.5, tach > 0 at 30% |
| 21 | Temperature | TMP1075 reading vs. fixture reference | within 3 K |
| 22 | RTC | Set, power off 60 s, read | drift < 2 s |
| 23 | Input current | IMON_ADC vs. PD source readout | within 10% |

## Final unit test (in enclosure)

- Repeat tests 5, 7-9, 11, 16, 20 with the lid on.
- 30-minute burn-in: `stress-ng --cpu 4`, links up, fan at 100%. Pass: no reset, no ALARM, SoC < 90 C at 25 C ambient.

## Coverage gaps

- No boundary scan; BGA-free carrier, so solder defects are found functionally.
- Eye quality on USB 3 and PCIe is not measured in production; qualify once per board revision with a scope.
- ESD and EMC are type tests, not production tests.
