# The single-vehicle optimisation problem (§8.B), in plain terms

## What we decide
For every half-hour slot `t` while the car is plugged in:

- `P_ch[t]`  — how hard to charge (kW, ≥ 0)
- `P_dis[t]` — how hard to sell back (kW, ≥ 0)

Everything else (state of charge, money) follows from those two choices.

## What we know (inputs)
| Symbol | Meaning | Source |
|---|---|---|
| `p_buy[t]`, `p_sell[t]` | price to import / export (£/MWh) | prices (+ tariff adders), or a forecast |
| `plugged[t]` | 1 if the car is at the charger | driver setting or learned habits (below) |
| `drive[t]` | energy used driving in slot t (kWh) | driver setting or learned habits |
| `E_cap` | battery size (kWh) | assumptions |
| `η_ch`, `η_dis` | charge / discharge efficiency | converter spec (hardware team) |
| `P_max_ch`, `P_max_dis` | charger rating (kW) | converter spec |

## Objective: minimise the owner's energy bill
```
minimise   Σ_t [ p_buy[t]·P_ch[t]  −  p_sell[t]·P_dis[t] ] · Δt
```
Savings = what an ordinary "plug in and charge" charger would cost, minus this.
Battery wear is **out of scope** for the optimiser (the hardware side handles it).

## Constraints
1. **Battery physics** (energy in, energy out, energy driven)
   `SoC[t+1] = SoC[t] + (η_ch·P_ch[t] − P_dis[t]/η_dis)·Δt / E_cap − drive[t]/E_cap`
2. **Battery limits:** `SoC_min ≤ SoC[t] ≤ SoC_max` (e.g. 20 % – 90 %)
3. **Charger limits:** `0 ≤ P_ch[t] ≤ plugged[t]·P_max_ch`, `0 ≤ P_dis[t] ≤ plugged[t]·P_max_dis`
4. **Not both at once:** charge OR discharge in a slot (a yes/no switch → makes it a MILP;
   only matters when prices go negative)
5. **Driver's needs** — set by the app's driver-habits option (below)

## When we solve: plan 38 h, act 24 h
- Every day at **10:00** (UK time) a new price forecast arrives (see `price_forecast_contract.md`).
- The optimiser **plans** from 10:00 today to midnight tomorrow (≈ 38 h), but only **executes**
  the first 24 h (to 10:00 tomorrow). At 10:00 tomorrow it re-plans with the car's real SoC.
- **Why not just plan 10:00 → 10:00 with a minimum-SoC rule at 10:00?** That keeps the driver safe,
  but loses money: the best commuter trade (charge cheap overnight → sell at the *next* evening's
  peak) crosses the 10:00 boundary, so a window that ends at 10:00 never sees the reason to buy
  extra overnight. The extra 14 h of look-ahead fixes that for a few lines of code.
- Stretch: run both versions and report the £ difference ("why look-ahead matters").

## Driver-habits options = which version of constraint 5 we use
| Option | What the user does | Constraint 5 |
|---|---|---|
| **A. I set a minimum** | picks a minimum % (e.g. 80 %) | `SoC[t] ≥ min %` whenever plugged in. V2G only sells energy above that line. No prediction needed. |
| **B. Learn my habits** | nothing — we learn from last week's driving | `SoC ≥ predicted trip energy + reserve` at each **predicted** departure. More freedom → more £. |

Same objective, same solver; the option only changes constraint 5.
The £ difference between A and B is the **"cost of peace of mind"** — a nice demo number.

## Option B: the habits model (discriminative ML) — implemented, see "Habits model: results"

### What it does
Predicts, from last week's driving, **when the car will be plugged in next week and how much
energy each trip will use**. The optimiser then schedules charging/selling inside those predicted
plug-in windows, using Tony's price forecast, subject to constraints 1–5.

```
last week's driving ──► habits model ──► next week: plug-in windows + trip energy
                                               │
Tony's price forecast ─────────────────────────┼──► optimiser ──► charge / sell schedule
converter spec (hardware) ─────────────────────┘
```

### Why discriminative
It is conditional prediction, `p(next week | last week, calendar)`: inputs in, targets out.
A generative model (sampling whole plausible weeks) is only needed for stress-testing; stretch goal.

### Set-up
- **Unit of prediction:** one row per driver per day (avoids flickering per-slot predictions).
- **Features:** day of week; what the driver did on the same weekday last week (departure time,
  return time, energy used); last week's averages and variability; public holiday flag.
- **Targets (per day):**
  1. departure time
  2. return (plug-in) time
  3. **trip energy (kWh)** → sets how full the battery must be at departure
- **From predictions to constraint 5:** at each predicted departure,
  `SoC ≥ SoC_min + predicted trip energy / E_cap + reserve`.

### Loss function: penalise under-charging heavily
Getting it wrong is not symmetric. Under-predicting what the driver needs strands them;
over-predicting only costs a little lost V2G revenue.

| Target | Dangerous error | Loss |
|---|---|---|
| Trip energy | predicting **too little** | asymmetric loss: under-predictions weighted `w` × more than over-predictions (e.g. `w = 10`) |
| Departure time | predicting **too late** | asymmetric loss: late predictions weighted `w` × more than early ones |
| Return time | predicting **too early** (plan relies on a car that isn't there yet) | asymmetric loss: early predictions weighted `w` × more |

Asymmetric loss with weight `w` is the pinball (quantile) loss at quantile `w / (w + 1)`, e.g.
`w = 9` (our default, `HabitsConfig.asym_weight_w`) → the 90th percentile of trip energy, the
10th percentile of departure time and the 90th of plug-in time. Standard tools support it directly
(LightGBM / scikit-learn `objective="quantile"`), and `w` is a dial between safety and savings.

### Model ladder
1. **Baseline — "same as last week"** (no ML). Copies last week's pattern exactly.
2. Quantile linear regression with the asymmetric loss.
3. Gradient boosting (LightGBM, quantile objective).

### Evaluation (what we report)
- **Shortfall rate:** % of departures where the car had less energy than the trip needed. Target ≈ 0.
- **Mean shortfall (kWh)** when it does happen.
- **Cost of caution:** £/yr given up versus perfect knowledge of the driver's week.
- Compare every model against the baseline on all three.

**Pitch:** the ML model is a *safe predictor*: it gives the driver more reliability than just
copying last week (far fewer shortfalls) while keeping most of the savings.

### Data for training and validation
One week of one driver is only 7 rows, so we train **one shared model across many drivers**;
each driver's last week enters as features, so predictions stay personal.

| Dataset | What it is | Use |
|---|---|---|
| **Electric Nation** (UK) | home-charging trial, UK households | closest match to our home-charger product; primary if accessible |
| **Electric Chargepoint Analysis** (UK) | UK domestic charge-point usage data | UK home sessions; training / validation |
| **ElaadNL open data** (NL) | public charge-point sessions, Netherlands | large volume of sessions; pre-training / robustness check |
| **ACN-Data** (Caltech, US) | workplace / campus sessions with user-entered departure times and energy | good for testing departure-time prediction; workplace, not home |

Check licences and access early. If none arrives in time, use our synthetic drivers (labelled).

**Splits (no leakage):**
- split by **time**: train on earlier weeks, validate on later weeks;
- and by **driver**: hold out whole drivers so we test on people the model has never seen.

## Habits model: results

**Headline (63 test drivers the model never saw, one simulated week each, synthetic prices):**
the ML model leaves the driver short on **4.1 %** of departures vs **17.2 %** for "copy last
week" (≈ 4× fewer), while keeping **51 %** of the savings that perfect knowledge of the
driver's week would give (£1.99 vs £3.91 per week). The pitch claim holds on safety; on
savings it is "about half", not "most".

### Data used
**Electric Nation, GreenFlux 3-minute meter values**: UK home-charging trial, 2017-03 → 2018-12,
329 home chargers (one per household; ChargerID = `vehicle_id`). The file is meter readings sent
only while a car is connected, so sessions are rebuilt from runs of readings:
plug-in = first reading, plug-out = last reading + 3 min, energy = Σ amps × 230 V × 3 min.
Code: `src/v2g/ingest/electric_nation.py` → `data/processed/sessions.parquet` (committed; the
1.5 GB raw CSVs are git-ignored, see `data/raw/electric_nation/README.md`).

Findings that shaped the cleaning:
- `TimeStamp` is true UTC (verified at the clock changes). The file's own local-time column
  switches offset at 00:00 UTC instead of 01:00 UTC on clock-change nights, so it is not used;
  local time is derived from UTC.
- The 15-minute file cannot tell us when a charger was online (7,675 days with a plug-in have no
  15-minute rows), so it is not used.

| Cleaning step | Count |
|---|---|
| Raw readings | 16,696,926 |
| Duplicate readings removed (same charger + timestamp; would inflate energy ~9 %) | 719,605 |
| Impossible current (< 0 or > 35 A) | 21 readings |
| Sessions built (incl. 6,075 from readings with no TransactionID, kept and flagged) | 69,104 |
| Transactions split where readings go silent > 6 h (never-closed transactions; shorter gaps are overnight comms drop-outs) | +198 |
| Back-to-back fragments merged (≤ 10 min apart = one plug-in) | −6,489 |
| Dropped: shorter than 5 min | 458 |
| Dropped: departure ≤ arrival, or overlapping on the same vehicle | 0 |
| **Final sessions / vehicles** | **62,157 / 328** |

Zero-energy sessions (1,233, median 2.9 h long) are **kept**: a plugged-in car that takes no
energy is still available for V2G. Multi-day sessions (502 over 72 h) are kept: mostly real
holiday parking.

**Trip-energy proxy:** energy charged at a session ≈ energy driven since the previous one, so
each trip gets the energy of the next session that actually took energy (zero-energy plug-ins
are skipped rather than creating fake 0 kWh trips; this is an upper bound per trip, i.e. on the
safe side).

### Dataset and splits
One row per vehicle per local day. Prediction is made at Monday 00:00 for the next 7 days using
only the 7 days before (anything not yet known then, e.g. the departure of a car still plugged
in, is masked; a test perturbs the future and checks features don't move). A day is used only
if it and the previous week are observed (inside the vehicle's active period, not inside a
> 14-day gap with no sessions). Code: `src/v2g/habits/features.py`.

| Split | Vehicle-days | Vehicles | Period | Plug-in rate |
|---|---|---|---|---|
| train | 54,936 | 252 | 2017-03 → 2018-09-23 | 0.53 |
| validation (last 12 weeks of the training vehicles) | 14,665 | 208 | 2018-09-24 → 2018-12-16 | 0.55 |
| test (whole vehicles held out) | 15,687 | 63 | 2017-03 → 2018-12 | 0.46 |

Targets per day: plugs in? / plug-in time / departure time / trip energy, for the day's
longest session; times are hours after local midnight (> 24 = a later day).

### Prediction accuracy
Shortfall = actual trip energy > predicted + 1 kWh, or actual departure > 15 min earlier than
predicted (tolerances in `HabitsConfig`). Coverage = share of actuals on the safe side of the
prediction (target 0.90 at w = 9). Pinball: lower is better.

| Held-out **test** drivers (7,223 departures) | Baseline: copy last week | Linear quantile | **LightGBM quantile (chosen)** |
|---|---|---|---|
| **Shortfall rate** | 62.9 % | 17.9 % | 19.3 % |
| Mean energy shortfall when short | 9.7 kWh | 8.1 kWh | 8.2 kWh |
| Pinball: energy / departure / plug-in | 4.03 / 3.64 / 1.73 | 1.65 / 1.78 / 0.55 | 1.59 / 1.73 / 0.53 |
| Coverage: energy / departure / plug-in | 0.46 / 0.53 / 0.50 | 0.89 / 0.89 / 0.90 | 0.87 / 0.87 / 0.87 |
| Plugs in today? AUC / false alarms | 0.62 / 35 % | 0.70 / 26 % | 0.71 / 25 % |

LightGBM was chosen on validation (shortfall 15.9 % vs 18.4 % linear, 60.8 % baseline); on the
test drivers the two ML models are close. Training takes ~6 s (LightGBM, all rows) and ~23 s
(linear, 10k-row sample: its solver scales badly).

**Where the safety comes from:** with w = 1 (a plain median) LightGBM has 62.8 % shortfalls,
the same as the baseline, so most of the safety comes from the asymmetric loss. But the ML
model buys it more cheaply: giving the baseline fixed safety margins tuned to the same
shortfall rate needs +12 kWh, departure −12 h and plug-in +4 h, reserving 21.6 kWh per trip vs
17.2 kWh, and leaves no usable plug-in window.

| w (quantile) | 1 (0.50) | 3 (0.75) | **9 (0.90)** | 19 (0.95) |
|---|---|---|---|---|
| Validation shortfall rate | 62.8 % | 33.0 % | **15.9 %** | 10.5 % |
| Mean energy reserved per trip (actual 10.9 kWh) | 9.9 | 13.4 | **17.2** | 20.1 |

### Cost of caution (plan with predicted habits, reality = true habits)
61 test vehicle-weeks (one was infeasible even with perfect knowledge and is skipped for every
strategy), Monday 10:00 → Monday 10:00, synthetic prices, 60 kWh battery. The optimiser
re-plans at 10:00 and **whenever the car really plugs in** (the charger sees it), so the
plug-in time only matters for planning ahead. While plugged in it holds the predicted trip
energy at the predicted (P10) departure; if that departure is unknown or already past, it
charges to the trip requirement first and sells only above it. Execution: power is 0 if the
car is really away, SoC is replayed with the true trips, every real departure is checked.

| Strategy | Saving vs normal charger | Shortfall rate (> 1 kWh) | Share of perfect saving |
|---|---|---|---|
| Normal charger (plug in, charge to full) | £0 | 0.4 % | – |
| Perfect knowledge of habits | £3.91 / week | 2.3 % | 100 % |
| Copy last week | £3.18 / week | 17.2 % | 81 % |
| **LightGBM, standard mode (product default)** | **£1.99 / week** | **4.1 %** | **51 %** |
| LightGBM, "always ready" (trip energy held throughout every session) | £1.40 / week | 2.3 % | 36 % |

Cost of caution (standard mode) = **£1.91 / week** vs perfect knowledge. Even perfect knowledge
has 2.3 % shortfalls: physically impossible days (a short stop before a big trip) and proxy
artefacts, so ≈ 2 % is the floor for any strategy.

A bug worth knowing about: the first version of the plan assumed a car whose predicted
departure had already passed would "leave any moment", so it charged for 30 min and then sat
idle all night (15 % shortfalls). Late-evening arrivals often come after their day's P10
departure. The fix (charge to the requirement first when the departure is unknown) brought it
to 4.1 %.

### Limitations
- **Old trial, small batteries:** 2017–19 cars (mostly 16 A / 32 A home chargers, smaller
  batteries than today). The simulation uses the 60 kWh battery from `config/assumptions.yaml`.
- **Charge energy is a proxy for trip energy:** we never see the trip. Public or work charging
  is invisible, so some "trips" are really several trips; 10 simulated trips exceeded the
  42 kWh usable battery and were capped.
- **One session per day is predicted** (the day's longest). Short extra top-ups are not
  predicted; on arrival the plan uses that day's main-session predictions, which is what
  drives most remaining shortfalls (departures earlier than the P10 prediction).
- **Synthetic prices**, so the £ figures are indicative; rerun `python -m v2g.habits.evaluate`
  once `data/processed/prices.parquet` exists (it is picked up automatically).
- **Electric Nation is UK home charging only**; no other dataset was used for training.

### Reproduce
```
python -m v2g.ingest.electric_nation   # sessions.parquet       (~20 s, needs the raw CSVs)
python -m v2g.habits.features          # habits_dataset.parquet (~1 s)
python -m v2g.habits.baseline          # baseline on validation
python -m v2g.habits.model             # fit, choose on validation, save artefacts/habits/ (~40 s)
python -m v2g.habits.evaluate          # test metrics + cost of caution (~35 s)
```
- **Setup:** Python ≥ 3.11 (we use `uv venv --python 3.12 .venv` then
  `uv pip install -e ".[dev]"`). On macOS LightGBM also needs the OpenMP runtime:
  `brew install libomp`.
- **Real prices:** once `data/processed/prices.parquet` lands, rerun
  `python -m v2g.habits.evaluate`. It picks the file up automatically and replaces the
  synthetic £ figures in `artefacts/habits/test_report.json`.

## Toy example (4 slots, car plugged in throughout, 10 kWh battery, η = 1 for simplicity)
| Slot | Price £/MWh | Best action |
|---|---|---|
| 1 | 30 (cheap night) | charge |
| 2 | 40 | charge |
| 3 | 150 (evening peak) | sell |
| 4 | 60 | sell what's left |

Buy cheap, sell at the peak. A 10 kW charger can only empty half the battery in one
half-hour, so the rest is sold in slot 4: £60 is still more than the £30–40 it cost.
Under option A with an 80 % minimum, the car can only sell what it holds above 80 %.
(This exact case is a unit test: `tests/test_lp.py::test_toy_example_buys_cheap_sells_peak`.)

## How we solve it
- **It's a Linear Program (LP).** The objective (price × energy) and constraints 1–3 and 5 are
  all linear, so the problem is convex and any optimum found is the global optimum.
- **Constraint 4 is the exception.** It needs a yes/no variable, which makes it a mixed-integer
  LP (MILP), and that is not convex. It only matters when charging and selling at the same time
  could pay, which only happens with negative prices. So we add yes/no variables **only for
  those slots**.
- **No closed form.** Which limits end up binding ("battery full here", "charger maxed there")
  is part of the answer, so we need an iterative algorithm.
- **Simplex.** An LP's optimum sits at a corner of the feasible region. Simplex walks from corner
  to better neighbouring corner until none is better. We use HiGHS's **dual simplex**
  (`scipy.optimize.linprog(method="highs-ds")`). MILP windows use **branch-and-bound**
  (`scipy.optimize.milp`), which solves an LP with dual simplex on each branch.
- **Why not gradient descent?** The objective is linear, so its gradient is just the price
  vector and never shrinks. All the work is in the constraints, and projecting onto them is hard
  because the SoC chain links every slot to the next. Simplex gives the exact answer in milliseconds.
- **Size:** a 38 h window is 76 slots → ~300 variables. Each solve takes milliseconds.
- **Fleet (Tony's ADMM question):** cars that only react to prices are independent, so the fleet
  is just the sum of per-car solves, run in parallel. ADMM / dual decomposition is useful when cars
  **share a limit** (a street transformer, a fleet MW target). Then a coordinator does
  (sub)gradient steps on a "price signal" for that limit while each car solves its own problem.
  It needs convexity, so drop the yes/no variables there. Stretch goal for the local-transformer demo.

## Implementation notes (code: `src/v2g/optimise/`)
| File | What it does |
|---|---|
| `lp.py` | `solve_window()`: builds and solves ONE planning window. Heavily commented with the theory. |
| `daily.py` | `run_daily()`: plan 38 h, act 24 h, repeat. Both driver options. Normal-charger baseline. Bill accounting. `run_daily_vs_reality()`: plan with predicted habits, execute against the true ones (re-plans on arrival, logs departure shortfalls). |
| `../habits/` | Habits model: `features.py` (dataset), `baseline.py`, `model.py`, `evaluate.py` (metrics + cost of caution), `predict.py` (`predict_next_week()` → sessions table). |
| `../../scripts/demo_one_car.py` | One car, one week, all three strategies → `artefacts/demo/one_car_week.{png,json}` |
| `../../tests/test_lp.py` | 7 checks: toy example, physics, limits, no simultaneous charge/sell, both options, impossible day |

**How the maths maps to code**
- **Decision vector** `x = [P_ch | P_dis | SoC | shortfall]` for one window; scipy's
  standard form is `min c·x  s.t.  A_eq x = b_eq,  A_ub x ≤ b_ub,  lb ≤ x ≤ ub`.
- **Constraint 1 (physics)** → `A_eq`, one row per slot.
- **Constraints 2–3 (battery, charger limits)** → variable bounds (`lb`, `ub`). "Only while plugged in"
  is simply an upper bound of 0 on power when the car is away.
- **Constraint 4** → binary `z[t]` only on slots where `loop_could_pay()` is true.
- **Constraint 5 (driver's needs)** → `A_ub`: `SoC[t] + shortfall[t] ≥ floor[t]`.
  - The two app options only differ in how `floor[t]` is built (`floor_set_minimum`,
    `floor_learned`), so the optimiser itself is the same for both.
  - **Soft, but effectively hard:** shortfall costs £5/kWh, far above anything selling earns. The
    floor is always met when physically possible. On an impossible day (e.g. the car arrives nearly
    empty just before it's needed) we still get a schedule ("charge flat out") and a reported
    shortfall, instead of a crash.
- **Reported SoC** is recomputed from the powers with the physics equation, so the energy balance
  is exact.
- **Efficiency** is fixed at the converter's rated-power value × battery efficiency (a linear
  model needs one number). Arbitrage runs the charger flat out almost all the time, so this is a
  good single operating point.
- **Fair comparison:** strategies can end the week with different amounts in the battery (the
  normal charger ends full), so leftover energy is credited at the week's average import price
  (`adj_bill`).

**Current limitations (v0)**
- Data is **synthetic** (labelled). Prices: the optimiser sees actuals ("perfect forecast") until
  Tony's forecast parquet lands. Then `buy_fc` / `sell_fc` come from it and bills still settle on actuals.
- Option B in the demo script still uses the **true trips as its "prediction"**. The habits
  model now exists (see "Habits model: results"); runs with predicted habits go through
  `run_daily_vs_reality`, which handles the car leaving earlier than predicted.
- Tariff = wholesale + fixed adders from `config/assumptions.yaml` (placeholder values).

**First result (synthetic, one commuter, one week, 60 % minimum for option A)**
| Strategy | Weekly saving vs normal charger |
|---|---|
| Option A: driver sets 60 % minimum | ≈ £9.80 |
| Option B: learned habits (perfect prediction) | ≈ £14.00 |

Indicative only: synthetic prices, placeholder tariff. The A–B gap is the "cost of peace of mind".
