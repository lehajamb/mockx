#!/usr/bin/env python3
"""SORX carrier power budget. Run: python3 tools/power_budget.py > docs/power-budget-table.md
All figures are design allowances (W) unless a source is given in docs/power-budget.md."""
EFF_5V0 = 0.92   # 15-20 V -> 5 V synchronous buck, allowance
EFF_3V3 = 0.90   # 5 V -> 3.3 V buck (incl. 1.8 V LDO loss), allowance
PATH_LOSS_W = 0.6  # TVS leakage + eFuse/blocking FET I2R + PD controller quiescent, allowance

# (load, typical W, max W, note)
RAIL_3V3 = [
    ("Intel I210-AT (order-entry NIC, 1000BASE-T active)", 0.60, 0.80, "allowance"),
    ("KSZ9131RNX mgmt PHY incl. 1.2 V core + 1.8 V RGMII I/O", 0.50, 0.75, "allowance"),
    ("USB 3 hub, 2 downstream ports", 0.40, 0.60, "allowance"),
    ("microSD card (boot)", 0.20, 0.66, "200 mA card peak"),
    ("Front-panel LEDs x7 @ 5 mA", 0.08, 0.15, "allowance"),
    ("RS-232 xcvr, console mux, PPS buffer, watchdog, temp sensor, I210 flash", 0.10, 0.20, "allowance"),
]
RAIL_5V0_DIRECT = [
    ("SoM VCC - Verdin iMX8M Plus (measured 4.43 mean / 5.26 peak; datasheet max 6.3)", 4.43, 6.30, "Toradex"),
    ("USB-A port 1 VBUS @ 900 mA", 0.00, 4.50, "USB 3.x 900 mA"),
    ("USB-A port 2 VBUS @ 900 mA", 0.00, 4.50, "USB 3.x 900 mA"),
    ("Fan, 40 mm 5 V PWM", 0.50, 1.00, "allowance"),
]
VERDIN_FAMILY_SUSTAINED = 8.25
VERDIN_FAMILY_PEAK = 12.5

def total(rows, col): return sum(r[col] for r in rows)

def chain(som_w, usb_w, fan_w, r3v3_w):
    p5 = som_w + usb_w + fan_w + r3v3_w / EFF_3V3
    pin = p5 / EFF_5V0 + PATH_LOSS_W
    return p5, pin

print("### 3.3 V rail (5 V -> 3.3 V buck, gated by SoM CTRL_PWR_EN_MOCI)\n")
print("| Load | Typ W | Max W | Basis |\n|---|---:|---:|---|")
for n,t,m,b in RAIL_3V3: print(f"| {n} | {t:.2f} | {m:.2f} | {b} |")
t33, m33 = total(RAIL_3V3,1), total(RAIL_3V3,2)
print(f"| **3.3 V total** | **{t33:.2f}** | **{m33:.2f}** | {m33/3.3:.2f} A max -> 3 A converter |")

print("\n### 5 V rail (VSYS 15/20 V -> 5 V buck)\n")
print("| Load | Typ W | Max W | Basis |\n|---|---:|---:|---|")
for n,t,m,b in RAIL_5V0_DIRECT: print(f"| {n} | {t:.2f} | {m:.2f} | {b} |")
print(f"| 3.3 V buck input ({EFF_3V3:.0%} eff.) | {t33/EFF_3V3:.2f} | {m33/EFF_3V3:.2f} | from table above |")
t5 = total(RAIL_5V0_DIRECT,1) + t33/EFF_3V3
m5 = total(RAIL_5V0_DIRECT,2) + m33/EFF_3V3
print(f"| **5 V total** | **{t5:.2f}** | **{m5:.2f}** | {m5/5:.2f} A max |")

print("\n### Input power vs. USB-C PD contract\n")
cases = [
 ("Typical: SoM stress mean, no USB devices", 4.43, 0.0, 0.5, t33),
 ("Max, Verdin iMX8M Plus: SoM 6.3 W, both USB-A at 900 mA", 6.30, 9.0, 1.0, m33),
 ("Max, any Verdin SoM sustained (8.25 W)", VERDIN_FAMILY_SUSTAINED, 9.0, 1.0, m33),
 ("Peak, any Verdin SoM (12.5 W transient)", VERDIN_FAMILY_PEAK, 9.0, 1.0, m33),
]
contracts = [("15 V / 2 A", 30.0), ("15 V / 3 A", 45.0), ("20 V / 2.25 A", 45.0), ("20 V / 3 A", 60.0)]
print("| Case | 5 V rail W | 5 V rail A | Input W | " + " | ".join(f"{n} ({w:.0f} W)" for n,w in contracts) + " |")
print("|---|---:|---:|---:|" + "---:|"*len(contracts))
for name, som, usb, fan, r33 in cases:
    p5, pin = chain(som, usb, fan, r33)
    cells = " | ".join(f"{pin/w:.0%}" for _,w in contracts)
    print(f"| {name} | {p5:.2f} | {p5/5:.2f} | {pin:.2f} | {cells} |")
p5, pin = chain(VERDIN_FAMILY_PEAK, 9.0, 1.0, m33)
print(f"\nInput current at worst case: {pin/15:.2f} A @ 15 V, {pin/20:.2f} A @ 20 V. 5 V converter sized for {p5/5:.2f} A -> 6 A part.")
p5d, pind = chain(6.30, 9.0, 1.0, m33)
print(f"Heat dissipated inside the enclosure at the iMX8M Plus max case (input minus the 9 W exported on USB-A): {pind-9.0:.2f} W.")
