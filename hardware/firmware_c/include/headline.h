#ifndef V2G_HEADLINE_H
#define V2G_HEADLINE_H
#include <stddef.h>
#include "scheduler.h"

// The "integration test sentence" from context §1, filled from computed numbers.
typedef struct {
    float p_rated_kw, eta_peak_pct, cost_gbp, premium_gbp;
    float v2g_net_day, smart_net_day, dumb_net_day;   // GBP/day, mean over n_days
    float uplift_yr;        // V2G minus smart one-way charging, GBP/yr  (the fair comparison)
    float vs_dumb_yr;       // V2G minus plug-in-and-charge, GBP/yr
    float payback_years;    // premium / uplift; <0 means never
    float kwh_out_day;      // mean exported energy per day
    int   days;
    int   fleet_year;
    float participation_pct, fleet_gw;
} headline_t;

void headline_compute(headline_t *h, const sched_params_t *p, int n_days);
int  headline_sentence(char *buf, size_t cap, const headline_t *h);
int  headline_json(char *buf, size_t cap, const headline_t *h);

#endif
