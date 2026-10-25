"""S5 Single-EV optimise (CONTEXT.md §9). Cheapest-hours timer (ladder step 1; the {uplift} baseline).

Inputs : prices, plugged-in window
Outputs: artefacts/schedules/rule_based.parquet

TODO: charge the k cheapest slots, sell the k dearest.
"""
