#ifndef V2G_SCHEDULER_H
#define V2G_SCHEDULER_H
#include <stdbool.h>
#include "dummy_data.h"

// Single-vehicle schedule optimiser (context §8.B), solved by dynamic
// programming over a SoC grid. Chosen over a greedy rule because it is
//   * exact on the grid (finds the true best buy/sell pattern),
//   * never charges and discharges in the same slot, even at negative prices,
//   * enforces SoC limits and the departure requirement by construction,
//   * small enough for an MCU (~4 KB policy table, ~100k inner iterations).
typedef struct {
    float p_charge_max_kw, p_discharge_max_kw;  // converter_spec power.*
    float eta_charge, eta_discharge;            // spec efficiency at rated power
    float e_cap_kwh, soc_min, soc_max;
    float battery_cost_gbp, cycle_life, dod;    // -> c_deg (context §8.C step 1)
    float standby_w;                            // converter_spec standby_w
} sched_params_t;

typedef enum {
    SCHED_V2G,           // charge and discharge to maximise net money
    SCHED_SMART_CHARGE,  // one-way: charge in the cheapest slots only
    SCHED_DUMB_CHARGE    // plug in and charge at full power until full enough
} sched_kind_t;

typedef struct {
    float p_kw[DAY_SLOTS];       // signed grid power per slot, + = charging
    float soc[DAY_SLOTS + 1];    // SoC at the start of each slot
    float charge_cost_gbp, export_revenue_gbp, degradation_gbp, standby_gbp;
    float net_gbp;               // revenue - cost - degradation - standby
    float kwh_in, kwh_out;       // grid-side energy
    bool  feasible;              // false => requirement could not be met
} sched_plan_t;

void sched_default_params(sched_params_t *p);
bool sched_plan(sched_kind_t kind, const dummy_day_t *d,
                const sched_params_t *p, sched_plan_t *out);

#endif
