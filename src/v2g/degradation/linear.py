"""S4 Degradation (CONTEXT.md §9). Linear wear cost per kWh of throughput (§8.C step 1).

Inputs : config/assumptions.yaml (battery)
Outputs: artefacts/degradation/c_deg.json (£/MWh)

TODO: c_deg = battery_cost / (cycle_life · 2 · E_cap · DoD).
"""
