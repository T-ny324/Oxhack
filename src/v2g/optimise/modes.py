"""S5 Single-EV optimise (CONTEXT.md §9). App mode registry: mode → constraint builder (§8.B.1).

Inputs : config/assumptions.yaml (optimiser.mode, target_soc)
Outputs: constraint blocks used by lp.py / mpc.py

TODO: the app's modes live in lp.py; 8b_singleev_optimiser/v2g-hack/src/v2g/optimise/daily.py has MODES / floor_for_mode for the weekly optimiser.
"""
