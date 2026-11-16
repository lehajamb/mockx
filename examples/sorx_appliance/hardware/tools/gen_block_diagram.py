#!/usr/bin/env python3
"""Generates docs/block-diagram.svg (SORX carrier, plane-split block diagram)."""
W, H = 1300, 900
C = {"fwd": ("#e8f5e9", "#2e7d32"), "mgmt": ("#e3f2fd", "#1565c0"), "ctrl": ("#f5f5f5", "#424242"),
     "time": ("#f3e5f5", "#6a1b9a"), "pwr": ("#fff3e0", "#e65100"), "som": ("#fffde7", "#f9a825")}
out = []
def box(x, y, w, h, lines, kind, bold=True):
    fill, stroke = C[kind]
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>')
    n = len(lines); y0 = y + h/2 - (n-1)*7.5 + 4
    for i, t in enumerate(lines):
        wt = "600" if (bold and i == 0) else "400"
        out.append(f'<text x="{x+w/2}" y="{y0+i*15}" text-anchor="middle" font-size="12" font-weight="{wt}" fill="#212121">{t}</text>')
def arrow(x1, y1, x2, y2, label=None, both=True, lx=None, ly=None):
    ms = ' marker-start="url(#a)"' if both else ''
    out.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#616161" stroke-width="1.5" marker-end="url(#a)"{ms}/>')
    if label:
        out.append(f'<text x="{lx if lx is not None else (x1+x2)/2}" y="{ly if ly is not None else min(y1,y2)-5}" text-anchor="middle" font-size="10.5" fill="#616161">{label}</text>')
def text(x, y, t, size=12, weight="400", anchor="start", fill="#212121"):
    out.append(f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-size="{size}" font-weight="{weight}" fill="{fill}">{t}</text>')

text(W/2, 30, "SORX single-board appliance — carrier block diagram (rev A, pre-schematic)", 17, "700", "middle")
# column geometry
LC, LI, SL, SR, RI, RC = 30, 245, 455, 665, 885, 1090   # x of: left conn, left IC, SoM left col, SoM right col, right IC, right conn
CW, IW, SW, BH = 190, 180, 185, 44
rows = [150, 212, 274, 336, 398, 460, 522]
# plane headers
text(LC, 70, "FORWARDING PLANE", 13, "700", fill=C["fwd"][1]); text(LC, 86, "market data in · order entry out · time", 11, fill="#616161")
text(RC+CW, 70, "MANAGEMENT PLANE", 13, "700", "end", C["mgmt"][1]); text(RC+CW, 86, "out-of-band mgmt · console · recovery · health", 11, "400", "end", "#616161")
# SoM
out.append(f'<rect x="{SL-18}" y="60" width="{SR+SW+18-(SL-18)}" height="530" rx="10" fill="{C["som"][0]}" stroke="{C["som"][1]}" stroke-width="2"/>')
mx = (SL-18 + SR+SW+18)/2
text(mx, 82, "CONTROL PLANE — Toradex Verdin iMX8M Plus Quad 4GB IT (SoM)", 13, "700", "middle")
text(mx, 100, "NXP i.MX 8M Plus · 4× Cortex-A53 1.6 GHz · 4 GB LPDDR4 · PREEMPT_RT Linux", 11.5, anchor="middle")
text(mx, 116, "cores 0–1: control + management  |  cores 2–3: isolated fast path (feed → book → SOR → FIX)", 11.5, anchor="middle")
text(mx, 132, "260-pin DDR4 SODIMM edge connector", 11, anchor="middle", fill="#616161")
L = [("ETH_1: ENET_QOS + PHY", "KSZ9131 · TSN · IEEE 1588"), ("PCIE_1: PCIe Gen3 ×1", "100 MHz refclk from SoM"),
     ("1588 event input + GPIO", "PPS capture"), ("eMMC 32 GB (on module)", "order journal + logs"),
     ("SPI", "journal head (optional)"), ("GPIO_x", "FEED/FIX/SYS/ALARM status"), ("CTRL_PWR_EN / RESET", "power sequencing")]
R = [("ETH_2: ENET1 (FEC) RGMII", "IEEE 1588"), ("UART_3", "Linux console"), ("USB_1 OTG (USB 2.0)", "recovery / gadget"),
     ("USB_2 host (SuperSpeed)", ""), ("SD_1 (3.3/1.8 V)", ""), ("I²C · PWM_1 · GPIO", ""), ("RX8130 RTC", "VCC_BACKUP")]
for y, (a, b) in zip(rows, L): box(SL, y, SW, BH, [a] + ([b] if b else []), "ctrl")
for y, (a, b) in zip(rows, R): box(SR, y, SW, BH, [a] + ([b] if b else []), "ctrl")
m = lambda y: y + BH/2
# left: forwarding
box(LC, rows[0], CW, BH, ["MD-IN RJ45 (GbE magjack)", "market data · multicast/IGMP"], "fwd")
arrow(LC+CW, m(rows[0]), SL, m(rows[0]), "MDI 4 pairs, 100 Ω diff")
box(LC, rows[1], CW, BH, ["OE RJ45 (GbE magjack)", "order entry · FIX 4.4"], "fwd")
box(LI, rows[1], IW, BH, ["Intel I210-AT", "GbE · 1588 · SDP · SPI flash"], "fwd")
arrow(LC+CW, m(rows[1]), LI, m(rows[1])); arrow(LI+IW, m(rows[1]), SL, m(rows[1]))
box(LC, rows[2], CW, BH, ["PPS IN (SMA, 50 Ω)", "1 PPS from GPS/grandmaster"], "time")
box(LI, rows[2], IW, BH, ["ESD + Schmitt buffer", "1:3 fan-out"], "time")
arrow(LC+CW, m(rows[2]), LI, m(rows[2]), both=False); arrow(LI+IW, m(rows[2]), SL, m(rows[2]), both=False)
arrow(LI+IW/2, rows[2], LI+IW/2, rows[1]+BH, "SDP0", both=False, lx=LI+IW/2+24, ly=rows[2]-6)
box(LI, rows[4], IW, BH, ["SPI MRAM/FRAM (optional)", "µs-class durable write"], "fwd", True)
arrow(LI+IW, m(rows[4]), SL, m(rows[4]))
box(LC, rows[5], CW, BH, ["Front-panel LEDs (light pipes)", "FEED A/B · FIX A/B"], "fwd")
arrow(SL, m(rows[5]), LC+CW, m(rows[5]), both=False)
# right: management
box(RI, rows[0], IW, BH, ["KSZ9131RNX PHY", "RGMII 1.8 V"], "mgmt"); box(RC, rows[0], CW, BH, ["MGMT RJ45 (GbE magjack)", "management · SSH/SNMP/NTP"], "mgmt")
arrow(SR+SW, m(rows[0]), RI, m(rows[0])); arrow(RI+IW, m(rows[0]), RC, m(rows[0]))
box(RI, rows[1], IW, BH, ["Console mux + RS-232 xcvr", "USB wins when plugged"], "mgmt"); box(RC, rows[1], CW, BH, ["CONSOLE RJ45", "Cisco-pinout RS-232"], "mgmt")
arrow(SR+SW, m(rows[1]), RI, m(rows[1])); arrow(RI+IW, m(rows[1]), RC, m(rows[1]))
box(RI, rows[2], IW, BH, ["USB 2.0 hub + USB-UART", "bus-powered from host"], "mgmt"); box(RC, rows[2], CW, BH, ["USB-C DEBUG", "console + recovery"], "mgmt")
arrow(SR+SW, m(rows[2]), RI, m(rows[2])); arrow(RI+IW, m(rows[2]), RC, m(rows[2]))
arrow(RI+IW/2, rows[2], RI+IW/2, rows[1]+BH, "UART", lx=RI+IW/2+24, ly=rows[2]-6)
box(RI, rows[3], IW, BH, ["USB 3 hub (2-port)", "2× 900 mA limit switches"], "mgmt"); box(RC, rows[3], CW, BH, ["2× USB-A 3.0", "peripherals · recovery media"], "mgmt")
arrow(SR+SW, m(rows[3]), RI, m(rows[3])); arrow(RI+IW, m(rows[3]), RC, m(rows[3]))
box(RC, rows[4], CW, BH, ["microSD slot", "boot media"], "mgmt"); arrow(SR+SW, m(rows[4]), RC, m(rows[4]))
box(RI, rows[5], IW, BH, ["HW watchdog · temp sensor", "fan PWM/tach · LED driver"], "mgmt"); box(RC, rows[5], CW, BH, ["PWR · SYS · ALARM LEDs", "RESET button · 4-pin fan"], "mgmt")
arrow(SR+SW, m(rows[5]), RI, m(rows[5])); arrow(RI+IW, m(rows[5]), RC, m(rows[5]))
box(RC, rows[6], CW, BH, ["CR2032 holder", "RTC backup"], "time"); arrow(RC, m(rows[6]), SR+SW, m(rows[6]), both=False)
# power
py = 660
out.append(f'<rect x="20" y="{py-42}" width="{W-40}" height="230" rx="10" fill="none" stroke="{C["pwr"][1]}" stroke-dasharray="6 4" stroke-width="1.2"/>')
text(30, py-22, "POWER", 13, "700", fill=C["pwr"][1])
P = [(40, 190, ["USB-C PWR receptacle", "PD sink: 20 V pref., 15 V min"]), (270, 210, ["TVS + eFuse", "reverse-polarity · OVP/UVLO · ILIM"]),
     (520, 190, ["5 V buck, 6 A", "VSYS 15/20 V → 5 V0"]), (750, 200, ["3.3 V buck, 3 A", "gated by SoM CTRL_PWR_EN"]), (990, 180, ["1.8 V LDO", "RGMII PHY I/O"])]
for x, w, l in P: box(x, py, w, 50, l, "pwr")
for (x, w, _), (x2, _, _) in zip(P, P[1:]): arrow(x+w, py+25, x2, py+25, both=False)
box(40, py+78, 190, 44, ["PD sink controller", "CC1/CC2 · strap-configured"], "pwr"); arrow(135, py+78, 135, py+50)
box(520, py+78, 190, 58, ["→ SoM VCC (≤ 12.5 W peak)", "→ 2× USB-A VBUS (9 W)", "→ fan (1 W)"], "pwr", False); arrow(615, py+50, 615, py+78, both=False)
box(750, py+78, 200, 58, ["→ I210 · mgmt PHY · USB hubs", "→ microSD · LEDs · logic", "3.2 W max"], "pwr", False); arrow(850, py+50, 850, py+78, both=False)
text(990, py+96, "Budget: 22.1 W max (iMX8M Plus),", 11.5); text(990, py+112, "28.9 W transient ceiling (any Verdin SoM).", 11.5)
text(990, py+128, "Contract ≥ 30 W; 45 W source recommended.", 11.5)
text(270, py+96, "USB-C DEBUG is a separate bus-powered", 11, fill="#616161"); text(270, py+111, "island: console works with main power off.", 11, fill="#616161")
# legend
lx = 40; ly = H-22
for name, k in [("Forwarding plane", "fwd"), ("Management plane", "mgmt"), ("Control plane (SoM)", "som"), ("Time sync", "time"), ("Power", "pwr")]:
    out.append(f'<rect x="{lx}" y="{ly-11}" width="14" height="14" rx="3" fill="{C[k][0]}" stroke="{C[k][1]}"/>'); text(lx+20, ly, name, 11.5); lx += 170
svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="Helvetica, Arial, sans-serif">'
       '<defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#616161"/></marker></defs>'
       f'<rect width="{W}" height="{H}" fill="#ffffff"/>' + "".join(out) + '</svg>')
open("docs/block-diagram.svg", "w").write(svg)
