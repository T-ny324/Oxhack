#include "scheduler.h"
#include <math.h>
#include <string.h>

#define SOC_STEP    0.0025f    // 0.25% of pack = 0.15 kWh for a 60 kWh pack
#define MAX_LEVELS  321        // supports soc_min..soc_max spans up to 0.80
#define MAX_ACT     26         // max SoC steps moved in one slot
#define NEG_INF     (-1.0e9f)

static float V_next[MAX_LEVELS], V_cur[MAX_LEVELS];
static int8_t policy[DAY_SLOTS][MAX_LEVELS];

void sched_default_params(sched_params_t *p) {
    // Mirrors config/converter_spec.yaml v0.1 (PLACEHOLDER provenance) plus
    // dummy battery numbers. eta = the 100% load point of the spec curve
    // because arbitrage runs near rated power.
    p->p_charge_max_kw = 7.4f;
    p->p_discharge_max_kw = 7.4f;
    p->eta_charge = 0.955f;
    p->eta_discharge = 0.95f;
    p->e_cap_kwh = 60.0f;
    p->soc_min = 0.20f;
    p->soc_max = 0.90f;
    p->battery_cost_gbp = 6000.0f;
    p->cycle_life = 3000.0f;
    p->dod = 0.7f;
    p->standby_w = 10.0f;
}

static float deg_per_kwh(const sched_params_t *p) {
    // c_deg = battery_cost / (cycle_life * 2 * E_cap * DoD)   (context §8.C)
    return p->battery_cost_gbp / (p->cycle_life * 2.0f * p->e_cap_kwh * p->dod);
}

// Grid-side power for moving `a` SoC steps in one slot (a>0 charge, a<0 discharge).
static float action_power_kw(int a, const sched_params_t *p) {
    float batt_kwh = (float)a * SOC_STEP * p->e_cap_kwh;
    return (a >= 0) ? batt_kwh / (p->eta_charge * SLOT_H)
                    : batt_kwh * p->eta_discharge / SLOT_H;
}

// Money and totals from p_kw[] and soc[] only, so every plan kind is scored
// by the same code (prevents the optimiser from grading its own homework).
static void score_plan(const dummy_day_t *d, const sched_params_t *p, sched_plan_t *o) {
    float deg = deg_per_kwh(p);
    o->charge_cost_gbp = o->export_revenue_gbp = o->degradation_gbp = 0.0f;
    o->standby_gbp = 0.0f; o->kwh_in = o->kwh_out = 0.0f;
    for (int t = 0; t < DAY_SLOTS; t++) {
        float pk = o->p_kw[t], batt = 0.0f;
        if (pk > 0.0f) {
            float kwh = pk * SLOT_H;
            o->kwh_in += kwh;
            o->charge_cost_gbp += d->buy_gbp_mwh[t] / 1000.0f * kwh;
            batt = kwh * p->eta_charge;
        } else if (pk < 0.0f) {
            float kwh = -pk * SLOT_H;
            o->kwh_out += kwh;
            o->export_revenue_gbp += d->sell_gbp_mwh[t] / 1000.0f * kwh;
            batt = kwh / p->eta_discharge;
        }
        o->degradation_gbp += deg * batt;
        if (d->plugged[t])
            o->standby_gbp += p->standby_w / 1000.0f * SLOT_H * d->buy_gbp_mwh[t] / 1000.0f;
    }
    o->net_gbp = o->export_revenue_gbp - o->charge_cost_gbp
               - o->degradation_gbp - o->standby_gbp;
}

static bool plan_dumb(const dummy_day_t *d, const sched_params_t *p, sched_plan_t *o) {
    float soc = d->soc_arrival;
    o->feasible = true;
    for (int t = 0; t < DAY_SLOTS; t++) {
        o->soc[t] = soc;
        o->p_kw[t] = 0.0f;
        if (t < d->depart_slot && d->plugged[t] && soc < d->soc_required) {
            float need = (d->soc_required - soc) * p->e_cap_kwh;
            float most = p->p_charge_max_kw * p->eta_charge * SLOT_H;
            float add = need < most ? need : most;
            o->p_kw[t] = add / (p->eta_charge * SLOT_H);
            soc += add / p->e_cap_kwh;
        }
    }
    o->soc[DAY_SLOTS] = soc;
    if (soc + 1e-4f < d->soc_required) o->feasible = false;
    score_plan(d, p, o);
    return o->feasible;
}

bool sched_plan(sched_kind_t kind, const dummy_day_t *d,
                const sched_params_t *p, sched_plan_t *o) {
    memset(o, 0, sizeof *o);
    if (kind == SCHED_DUMB_CHARGE) return plan_dumb(d, p, o);

    int D = d->depart_slot;
    if (D < 1 || D > DAY_SLOTS) return false;
    int N = (int)lroundf((p->soc_max - p->soc_min) / SOC_STEP) + 1;
    if (N < 2 || N > MAX_LEVELS) return false;

    int a_ch = (int)floorf(p->p_charge_max_kw * p->eta_charge * SLOT_H /
                           (SOC_STEP * p->e_cap_kwh) + 1e-4f);
    int a_dis = 0;
    if (kind == SCHED_V2G)
        a_dis = (int)floorf(p->p_discharge_max_kw * SLOT_H /
                            (p->eta_discharge * SOC_STEP * p->e_cap_kwh) + 1e-4f);
    if (a_ch > MAX_ACT) a_ch = MAX_ACT;
    if (a_dis > MAX_ACT) a_dis = MAX_ACT;

    int req = (int)ceilf((d->soc_required - p->soc_min) / SOC_STEP - 1e-3f);
    if (req < 0) req = 0;
    bool req_ok = (req <= N - 1);
    if (!req_ok) req = N - 1;

    float deg = deg_per_kwh(p);
    for (int i = 0; i < N; i++) V_next[i] = (i >= req) ? 0.0f : NEG_INF;

    // Backward induction over the plugged-in window.
    for (int t = D - 1; t >= 0; t--) {
        float rew[2 * MAX_ACT + 1];
        for (int a = -a_dis; a <= a_ch; a++) {
            float pk = action_power_kw(a, p);
            float grid_kwh = fabsf(pk) * SLOT_H;
            float money = (pk >= 0.0f) ? -d->buy_gbp_mwh[t] / 1000.0f * grid_kwh
                                       :  d->sell_gbp_mwh[t] / 1000.0f * grid_kwh;
            rew[a + MAX_ACT] = money - deg * fabsf((float)a) * SOC_STEP * p->e_cap_kwh;
        }
        bool can_move = d->plugged[t];
        for (int i = 0; i < N; i++) {
            float best = NEG_INF; int8_t best_a = 0;
            if (V_next[i] > NEG_INF / 2) { best = rew[MAX_ACT] + V_next[i]; }   // a = 0
            if (can_move) {
                int kmax = a_ch > a_dis ? a_ch : a_dis;
                for (int k = 1; k <= kmax; k++) {         // nearest-to-zero first => stable ties
                    for (int sgn = 1; sgn >= -1; sgn -= 2) {
                        int a = sgn * k;
                        if (a > a_ch || -a > a_dis) continue;
                        int j = i + a;
                        if (j < 0 || j >= N || V_next[j] <= NEG_INF / 2) continue;
                        float v = rew[a + MAX_ACT] + V_next[j];
                        if (v > best + 1e-7f) { best = v; best_a = (int8_t)a; }
                    }
                }
            }
            V_cur[i] = best;
            policy[t][i] = best_a;
        }
        memcpy(V_next, V_cur, sizeof(float) * (size_t)N);
    }

    int i0 = (int)lroundf((d->soc_arrival - p->soc_min) / SOC_STEP);
    if (i0 < 0) i0 = 0;
    if (i0 > N - 1) i0 = N - 1;
    if (!req_ok || V_next[i0] <= NEG_INF / 2) {
        plan_dumb(d, p, o);              // best effort: charge flat out
        o->feasible = false;
        return false;
    }

    int i = i0;
    for (int t = 0; t < DAY_SLOTS; t++) {
        o->soc[t] = p->soc_min + (float)i * SOC_STEP;
        if (t < D) {
            int a = policy[t][i];
            o->p_kw[t] = action_power_kw(a, p);
            i += a;
        }
    }
    o->soc[DAY_SLOTS] = p->soc_min + (float)i * SOC_STEP;
    o->feasible = true;
    score_plan(d, p, o);
    return true;
}
