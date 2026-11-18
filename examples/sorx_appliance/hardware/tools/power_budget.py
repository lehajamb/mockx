#!/usr/bin/env python3
"""Codex B rail allocation estimates; not measured or manufacturer maxima."""
from design import DESIGN

EFF_5V = .92
EFF_3V3 = .90
PATH_ALLOWANCE_W = .60
# Conservative allocation, not proof of the SoM's external1.8V output budget.
EXTERNAL_1V8_ALLOWANCE_W = .45
USB_AND_FAN_W = 10.0
CARRIER_3V3_W = 2.56


def calculate(som_w=8.25):
    rail5 = som_w + EXTERNAL_1V8_ALLOWANCE_W
    return {
        '+5V': (rail5, rail5 / 5),
        '+5V_USB': (USB_AND_FAN_W, USB_AND_FAN_W / 5),
        '+3V3': (CARRIER_3V3_W, CARRIER_3V3_W / 3.3),
        'input_w': rail5 / EFF_5V + USB_AND_FAN_W / EFF_5V + CARRIER_3V3_W / EFF_3V3 + PATH_ALLOWANCE_W,
    }


def main():
    bucks = [p for p in DESIGN.parts.values() if p.mpn == 'LMR33630ADDAR']
    assert len(bucks) == 3 and all(p.pins['2'] == 'VSYS' for p in bucks)
    print('# Power budget - Codex B estimates\n')
    print('**NOT READY FOR FABRICATION.** Allocation arithmetic only; load, thermal and transient qualification remain open.\n')
    print('There are three independent 3 A LMR33630 converters supplied directly by VSYS. '+
          '+5V is always enabled after the eFuse; +5V_USB and +3V3 use PWR_EN. '+
          '+1V8 comes from the SoM. Debug USB power supplies only its UART island.\n')
    print('| Rail | Sustained allocation W | Current A | Converter rating A |')
    print('|---|---:|---:|---:|')
    result = calculate()
    for rail in ('+5V', '+5V_USB', '+3V3'):
        watts, amps = result[rail]
        print(f'| {rail} | {watts:.2f} | {amps:.2f} | 3 |')
    print('\nAssumptions: SoM8.25W sustained /12.5W transient planning envelopes; '+
          '0.45W for carrier1.8V loads; two900mA USB load envelopes plus1Wfan; '+
          '2.56Wcarrier3.3V load allocation;92%5V and90%3.3V efficiency;0.6Winput-path allowance. '+
          'These are not guaranteed component maxima. USB2.0 protocol limits remain applicable.\n')
    print('| Case | Estimated input W | Input A at 15V | Input A at 20V |')
    print('|---|---:|---:|---:|')
    for label, som in [('Planning sustained',8.25), ('Planning transient',12.5)]:
        watts=calculate(som)['input_w']
        print(f'| {label} | {watts:.2f} | {watts/15:.2f} | {watts/20:.2f} |')
    print('\nRequire a qualified 15V/3A or 20V/3A PD source, prefer 20V. '+
          'The eFuse nominal 2.2A setting and pulse-current behavior must be verified against the source contract. '+
          'Its current-limit tolerance, startup inrush and fault pulses are not included in this arithmetic. '+
          'See [electrical corrections](electrical-corrections.md) and [programming requirements](programming-and-recovery.md).\n')
    print('The former model included nonexistent hubs, a cascaded3.3V converter and a 6A 5V converter; '+
          'those assumptions were removed. Blank 5V-only input does not light a powered-system LED. '+
          'All rail-load maxima,1.8V tolerance/current, capacitor bias, efficiency curves and temperature margins '+
          'must be independently established before approving R05.')


if __name__ == '__main__':
    main()
