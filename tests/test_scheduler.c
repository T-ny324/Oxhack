#include "scheduler.h"
#include "headline.h"
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

static void check_physics(const dummy_day_t *d, const sched_params_t *p, const sched_plan_t *o) {
    for (int t = 0; t < DAY_SLOTS; t++) {
        float pk = o->p_kw[t];
        assert(pk <= p->p_charge_max_kw + 1e-3f && pk >= -p->p_discharge_max_kw - 1e-3f);
        if (!d->plugged[t]) assert(fabsf(pk) < 1e-6f);
        float dsoc = (pk >= 0 ? p->eta_charge * pk * SLOT_H / p->e_cap_kwh
                              : pk * SLOT_H / (p->eta_discharge * p->e_cap_kwh));
        assert(fabsf((o->soc[t + 1] - o->soc[t]) - dsoc) < 2e-4f);     // energy balance
        assert(o->soc[t] >= p->soc_min - 1e-4f && o->soc[t] <= p->soc_max + 1e-4f);
    }
    assert(o->soc[d->depart_slot] >= d->soc_required - 1e-3f);          // driver needs first
}

int main(void) {
    sched_params_t p; sched_default_params(&p);
    static dummy_day_t d; static sched_plan_t v2g, smart, dumb, again;

    for (uint32_t day = 0; day < 14; day++) {
        dummy_make_day(&d, day);
        assert(sched_plan(SCHED_V2G, &d, &p, &v2g));
        assert(sched_plan(SCHED_SMART_CHARGE, &d, &p, &smart));
        assert(sched_plan(SCHED_DUMB_CHARGE, &d, &p, &dumb));
        check_physics(&d, &p, &v2g);
        check_physics(&d, &p, &smart);
        for (int t = 0; t < DAY_SLOTS; t++) assert(smart.p_kw[t] >= -1e-6f);   // one-way
        assert(v2g.net_gbp >= smart.net_gbp - 1e-4f);     // V2G action set contains smart's
        assert(smart.net_gbp >= dumb.net_gbp - 0.03f);    // smart beats plug-in (grid slack)
        assert(v2g.kwh_out > 0.0f);                       // arbitrage actually happens
    }

    // Negative prices: windy night must contain negative slots and the plan must use them.
    dummy_make_day(&d, 2);
    int neg = -1;
    for (int t = 0; t < d.depart_slot; t++) if (d.buy_gbp_mwh[t] < 0) { neg = t; break; }
    assert(neg >= 0);
    sched_plan(SCHED_V2G, &d, &p, &v2g);
    assert(v2g.p_kw[neg] > 0.5f);
    assert(v2g.charge_cost_gbp >= 0.0f);

    // Determinism.
    dummy_make_day(&d, 3);
    sched_plan(SCHED_V2G, &d, &p, &v2g);
    sched_plan(SCHED_V2G, &d, &p, &again);
    assert(memcmp(v2g.p_kw, again.p_kw, sizeof v2g.p_kw) == 0);

    // Infeasible requirement is reported, not hidden.
    d.soc_required = 0.95f;
    assert(!sched_plan(SCHED_V2G, &d, &p, &v2g));
    assert(!v2g.feasible);

    // Headline: finite numbers, sentence fits.
    headline_t h; headline_compute(&h, &p, 7);
    char s[512], j[512];
    int n = headline_sentence(s, sizeof s, &h);
    assert(n > 0 && n < (int)sizeof s);
    assert(headline_json(j, sizeof j, &h) < (int)sizeof j);
    assert(isfinite(h.uplift_yr) && h.uplift_yr > 0 && h.payback_years > 0);
    assert(h.fleet_gw > 0);

    puts("scheduler + dummy data + headline tests passed");
    return 0;
}
