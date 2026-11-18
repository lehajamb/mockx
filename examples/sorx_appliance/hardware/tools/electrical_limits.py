#!/usr/bin/env python3
"""Conservative static DC bounds for Codex B. Not a surge or SPICE simulation.

References: docs/electrical-corrections.md. Assumes a 35.5 V VBUS positive
clamp bound; parasitic overshoot, negative events are open. LT3010 reverse-output protection is a manufacturer requirement, not a simulated result.
"""
import json


def calculate():
    # TL431BI section6.12: reference limits, 34mV temperature span,
    # 4uA initial reference current plus2.5uA temperature span.
    rtop_lo, rtop_hi = 82000 * .99, 82000 * 1.01
    rbot_lo, rbot_hi = 10000 * .99, 10000 * 1.01
    ref_hi = 2.507 + .034
    # Conservative negative cathode coefficient across the full36V range.
    ref_lo = 2.483 - .034 - (10 - 2.483) * .0027 - (36 - 10) * .002
    clamp_lo = ref_lo * (1 + rtop_lo / rbot_hi)
    clamp_hi = ref_hi * (1 + rtop_hi / rbot_lo) + 6.5e-6 * rtop_hi
    rseries_min = 1000 * .99
    fault_vbus = 35.5
    series_current_max = (fault_vbus - clamp_lo) / rseries_min
    series_power_max = series_current_max ** 2 * rseries_min
    # Off-state divider effect only; controller leakage/discharge excluded.
    normal20_sense = 20 * (82000 + 10000) / (1000 + 82000 + 10000)
    aux_min=1.237*(1+24900*.99/(10000*1.01))
    aux_max=1.313*(1+24900*1.01/(10000*.99))+100e-9*24900*1.01
    # Board-input envelope, not an assertion about every source/cable.
    source_min=4.75
    standby_current_max=.050+.0033+.001 # regulator load, GND current, PD sense allowance
    cable_loop_resistance_max=.5
    input_min=source_min-standby_current_max*cable_loop_resistance_max
    dropout_max=.55
    return {
        'status': 'STATIC CALCULATION ONLY; R05 BLOCKED',
        'assumed_positive_vbus_bound_v': fault_vbus,
        'tl431_clamp_dc_lower_v': clamp_lo,
        'tl431_clamp_dc_upper_v': clamp_hi,
        'stusb_absolute_positive_limit_v': 28.0,
        'positive_dc_margin_v': 28.0 - clamp_hi,
        'series_resistor_worst_dc_power_w': series_power_max,
        'tl431_conservative_power_bound_w': clamp_hi * series_current_max,
        'sense_at_20v_divider_only_v': normal20_sense,
        'sense_divider_only_error_percent': (1 - normal20_sense / 20) * 100,
        'ldo_regulated_output_min_v': aux_min,
        'ldo_regulated_output_max_v': aux_max,
        'ldo_setpoint_nominal_v':1.275*(1+24900/10000)+50e-9*24900,
        'source_5v_minimum_assumption_v':source_min,
        'standby_current_budget_a':standby_current_max,
        'cable_loop_resistance_budget_ohm':cable_loop_resistance_max,
        'board_input_minimum_under_assumptions_v':input_min,
        'ldo_dropout_max_at_50ma_v':dropout_max,
        'ldo_startup_dc_lower_under_assumptions_v':min(aux_min,input_min-dropout_max),
        'ldo_startup_dc_requirement_passes_under_assumptions': min(aux_min,input_min-dropout_max)>4.1,
        'ldo_total_load_qualified':False,
        'ldo_startup_waveform_qualified':False,
        'ldo_bleeder_minimum_current_a':aux_min/(3900*1.01),
        'pd_shutdown_aux_present_off_max_v':110.1e-6*4700*1.01,
        'pd_shutdown_aux_absent_max_v':10.1e-6*4700*1.01,
        'pd_gate_drive_minimum_under_assumptions_v':min(aux_min,input_min-dropout_max)-.4,
        'pd_gate_transition_and_full_temperature_on_state_qualified':False,
        'ldo_reverse_output_protection': 'LT3010 manufacturer specified; board waveform validation pending',
        'negative_transient_protection_guaranteed': False,
        'surge_waveforms_simulated': False,
        'imon_gain_nominal_v_per_a': 27.9e-6 * 9100,
        'imon_at_6a_max_v': 6 * 30.14e-6 * 9100 * 1.01,
        'imon_source_current_at_6a_max_a': 6 * 30.14e-6,
        'recovery_unpowered_node_max_v': 11e-6 * 22000 * 1.01,
        'recovery_high_load_max_a': 1.95 / (22000 * .99) + 1e-6,
        'recovery_u40_voh_min_v': 1.65 - .15,
        'recovery_u20_vih_required_v': 1.48,
        'physical_measurements_completed': False,
    }


if __name__ == '__main__':
    print(json.dumps(calculate(), indent=2))
