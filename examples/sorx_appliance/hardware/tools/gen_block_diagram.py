#!/usr/bin/env python3
"""Generate a current, explicitly unqualified architecture overview."""
from pathlib import Path
from html import escape
import xml.etree.ElementTree as ET


def main():
    groups = [
        ('Network and timing', '#e8f5e9', [
            ('MD-IN / J4', 'SoM ETH_1 onboard PHY to magjack'),
            ('OE / J6', 'PCIe to I210-AT to magjack'),
            ('MGMT / J7', 'RGMII to KSZ9131 to magjack'),
            ('PPS / J5', 'Buffered capture paths; firmware pending')]),
        ('Compute and service', '#e3f2fd', [
            ('Verdin iMX8M Plus', 'CPU, RAM, eMMC; TE 260-pin socket'),
            ('USB host / J8 and J9', 'One SuperSpeed and one USB 2.0 port'),
            ('Recovery', 'U40 interlock; JP1/JP2; isolated fixture'),
            ('Console', 'USB-C UART and RJ45 RS232'),
            ('Storage and supervision', 'FRAM, microSD, RTC, watchdog, LEDs, fan')]),
        ('Input and rails', '#fff3e0', [
            ('USB-C PWR / J3', 'PD source: 15 V / 3 A or 20 V / 3 A'),
            ('PD auxiliary', 'LT3010 4.5V; STUSB4500; sense clamp candidate'),
            ('Contract-controlled load', 'Q5 inhibits TPS26631 eFuse before contract'),
            ('Three independent 3 A bucks', '+5V, +5V_USB and +3V3 from VSYS'),
            ('Low voltage rails', '+1V8 from module; PHY 1.2V from +3V3')]),
    ]
    parts=['<svg xmlns="http://www.w3.org/2000/svg" width="1350" height="650" viewBox="0 0 1350 650">',
           '<rect width="1350" height="650" fill="white"/>',
           '<g font-family="Arial,sans-serif" fill="#18212c">',
           '<text x="675" y="38" text-anchor="middle" font-size="23" font-weight="bold">SORX appliance - Codex B architecture</text>',
           '<text x="675" y="69" text-anchor="middle" font-size="18" fill="#a00000">NOT READY FOR FABRICATION - unqualified engineering candidate</text>']
    for col,(heading,color,items) in enumerate(groups):
        x=25+col*440
        parts.append(f'<text x="{x+210}" y="115" text-anchor="middle" font-size="20" font-weight="bold">{escape(heading)}</text>')
        for row,(name,detail) in enumerate(items):
            y=137+row*84
            parts.extend([f'<rect x="{x}" y="{y}" width="420" height="69" rx="7" fill="{color}" stroke="#667788"/>',
                          f'<text x="{x+210}" y="{y+25}" text-anchor="middle" font-size="17" font-weight="bold">{escape(name)}</text>',
                          f'<text x="{x+210}" y="{y+49}" text-anchor="middle" font-size="14">{escape(detail)}</text>'])
    parts.extend(['<text x="675" y="610" text-anchor="middle" font-size="15">See electrical corrections, programming/recovery and fresh R01-R09 evidence. No hardware measurements claimed.</text>', '</g></svg>'])
    svg='\n'.join(parts)+'\n'
    ET.fromstring(svg)
    target=Path(__file__).resolve().parents[1]/'docs/block-diagram.svg'
    target.write_text(svg,encoding='utf-8',newline='\n')
    print(target)


if __name__ == '__main__':
    main()
