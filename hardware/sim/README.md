# hardware/sim

S1 Hardware spec (CONTEXT.md §9), owned by Abdullah & Issa. LTspice schematics and the measurements
exported from them. Everything below the rule is Abdullah's hardware README, moved here from the
`AAbid0-hardware` branch so that its relative links resolve; the AC/DC stage schematic he committed
to the repo root is here too.

## Files

| File | Origin (branch upload name) | What it is |
|---|---|---|
| [`ltspice/dc_link_baseline.asc`](ltspice/dc_link_baseline.asc) | `EV_V2G_G2V_DC_DC_LINK.asc` | Step 1: 700 V link and 400 V battery model, no power transfer |
| [`ltspice/combined_bidirectional.asc`](ltspice/combined_bidirectional.asc) | `EV_V2G_G2VCombined_Bidirectional_Convertor.asc` | Bidirectional synchronous buck-boost DC/DC, `MODE` 0 = G2V, 1 = V2G |
| [`ltspice/acdc_grid_stage.asc`](ltspice/acdc_grid_stage.asc) | `EV_V2G_G2V_Step6_ACDC_Convergence_Fix.asc` | Step 6: full-bridge bidirectional AC/DC, 230 V 50 Hz to 700 V, 6.6 kW target, behavioural current controller |
| [`hardware_config.json`](hardware_config.json) | same | Circuit values, PWM settings, mode and sign conventions, file map |
| [`efficiency.md`](efficiency.md) | `Hardware_Efficiency.md` | Per-stage efficiency from the two independent LTspice runs: AC/DC 97.71 / 97.66 %, DC/DC 99.79 % |
| [`data/simulation_summary.csv`](data/simulation_summary.csv) | same | One steady-state operating point per direction (40 to 60 ms window) |
| [`data/pwm_cycle_metrics.csv`](data/pwm_cycle_metrics.csv) | same | One row per 20 µs switching cycle, 3000 per mode |

## What flowed into the contract (`config/converter_spec.yaml`, provenance `simulation`)

| Spec field | Value | Source |
|---|---|---|
| `topology` | two-stage AC/DC + 700 V link + buck-boost DC/DC | the three schematics |
| `grid_side` | 1 phase, 230 V, 50 Hz | `acdc_grid_stage.asc` (`VGRID` 325.27 V peak) |
| `power.p_charge_max_kw`, `p_discharge_max_kw` | 6.6 kW | `hardware_config.json` `continuous_power_target_w`; AC/DC sim `PSET=6600` |
| `efficiency_curve` at rated load | 97.50 % charge, 97.45 % discharge | `efficiency.md`: AC/DC x DC/DC, each stage simulated on its own with ideal switch models, so an upper estimate |

Still placeholder in the spec, and why: the **part-load shape** of the efficiency curve (the v0.1
roll-off, scaled to meet the simulated rated-load value and capped there, so no point exceeds what was
simulated), **dynamics** (the runs are open-loop fixed duty or a behavioural controller, no measured
step response), **standby loss** and **cost** (no BOM yet). The app's Hardware tab shows the simulated
operating points next to the spec so the two are not confused.

---

# Bidirectional EV DC/DC Converter | Hardware & Simulation

LTspice model and shareable performance data for an intelligent EV energy-storage system with **grid-to-vehicle (G2V)** charging and **vehicle-to-grid (V2G)** discharge.

## System overview

```text
230 V AC grid
    ↕
Bidirectional AC/DC conversion (grid interface)
    ↕
700 V DC link
    ↕
Combined synchronous buck/boost DC/DC stage
    ↕
400 V nominal EV battery
```

**Scope:** The supplied LTspice schematics and measurements cover the DC link and bidirectional **DC/DC** stage. The 700 V bus is represented by an ideal source/sink, not a model of the AC grid interface.

## DC link

The **DC link** is a 700 V intermediate DC bus connecting the bidirectional AC/DC and DC/DC converter stages. It includes a **1,000 µF capacitor** to smooth voltage ripple and buffer short-term differences between the power entering and leaving the link. This enables both converter stages to be controlled largely independently.

- **G2V:** Grid-side conversion feeds the DC link; the DC/DC stage draws from the link to charge the EV battery.
- **V2G:** The battery feeds the DC link through the DC/DC stage; the grid-side inverter would then export power to the grid.
- **Energy buffering:** The capacitor temporarily absorbs/releases energy as converter power changes. The battery, not the DC-link capacitor, provides bulk energy storage.

## Combined DC/DC converter

The model is a **non-isolated, synchronous bidirectional buck-boost converter** using two controllable switches (**SH**, **SL**), their antiparallel diodes (**D2**, **D3**), a **680 µH inductor (L1)**, and input/output capacitors.

- **G2V (buck, `MODE=0`):** Voltage steps down from the 700 V link towards the battery's 400 V operating level. Positive `I(L1)` represents charging.
- **V2G (boost, `MODE=1`):** The battery transfers energy back to the 700 V bus. Negative `I(L1)` represents discharge in the schematic's current reference direction.
- **PWM:** 50 kHz switching, 100 ns dead time; initial duty cycles 57.97% for G2V and 43.7% for V2G.
- **Controller integration:** A Raspberry Pi Pico 2 W can supervise mode selection and use measured current/voltage to regulate power through a closed-loop controller. The included LTspice runs use fixed PWM duty cycles.

### Hardware and simulation parameters

| Parameter | Value |
|---|---:|
| DC-link voltage | 700 V |
| Battery open-circuit voltage | 400 V |
| Battery capacity (project reference) | 60 kWh |
| Battery internal resistance (model) | 0.1 Ω |
| DC-link capacitor (C1) | 1,000 µF |
| Battery-side capacitor (C2) | 470 µF |
| Inductor (L1) | 680 µH |
| Switching frequency | 50 kHz |
| PWM dead time | 100 ns |
| Model switch on-resistance | 0.04 Ω each |
| Nominal project power target | 6.6 kW |
| Additional project test target | 7 kW |

For programmatic access, use [`hardware_config.json`](hardware_config.json). The file separates circuit component values, control parameters, power targets and telemetry definitions.

## LTspice results

Measured from the **uploaded LTspice time-series exports**, using **time-weighted averages from 40 to 60 ms**. Inductor ripple is measured over the last complete 20 µs switching period.

| Metric | G2V charging (`MODE=0`) | V2G discharging (`MODE=1`) |
|---|---:|---:|
| Mean battery terminal voltage | 402.15 V | 397.75 V |
| DC-link voltage | 700 V | 700 V |
| Mean inductor current | +21.53 A | -22.54 A |
| Battery terminal power | +8.66 kW | -8.96 kW |
| DC-link power exchange | 8.68 kW drawn | 8.94 kW exported |
| Inductor ripple, peak-to-peak | 5.03 A | 5.05 A |
| Model DC/DC stage efficiency | 99.79% | 99.79% |

### What the simulations show

**G2V / charging:** Positive inductor current transfers energy from the 700 V DC link to the EV battery; battery terminal voltage rises above the 400 V open-circuit value because the battery model includes internal resistance. At steady state, the model transfers approximately **8.66 kW** into the battery terminals.

**V2G / discharging:** Negative inductor current indicates reverse power transfer. Battery terminal voltage falls below 400 V because of the internal resistance, while approximately **8.94 kW** is exported to the DC-link source/sink.

**Switching behaviour:** Both runs show the expected triangular inductor-current ripple at 50 kHz. The DC-link voltage stays at 700 V because the DC-link supply is fixed at 700 V in this model.

**Power targets:** These open-loop operating points are approximately 8.7–9.0 kW. The controller will need to regulate the current/power command to reach the target.


## Data for the controller and ML teams

The integration outputs are deliberately small and machine-readable:

| File | Purpose |
|---|---|
| [`hardware_config.json`](hardware_config.json) | Static circuit values, project targets, PWM settings, mode selection and signal conventions |
| [`data/simulation_summary.csv`](data/simulation_summary.csv) | One measured steady-state operating point for each direction |
| [`data/pwm_cycle_metrics.csv`](data/pwm_cycle_metrics.csv) | One row **per 20 µs switching cycle**, including voltage, current, ripple and power (3,000 cycles per mode) |
| [`ltspice/combined_bidirectional.asc`](ltspice/combined_bidirectional.asc) | Editable combined converter schematic |
| [`ltspice/dc_link_baseline.asc`](ltspice/dc_link_baseline.asc) | Initial DC-link and battery baseline schematic |

**Sign conventions:** `battery_terminal_power_avg_w > 0` means charging, `< 0` means discharging. `dc_link_source_power_avg_w < 0` means the DC-link source supplies power (G2V), while `> 0` means it absorbs power (V2G). Both power fields are signed and should not be added directly without accounting for direction.


```
