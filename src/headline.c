#include "headline.h"
#include <stdio.h>

// Mirror of config/converter_spec.yaml v0.1 (provenance: placeholder).
#define SPEC_P_RATED_KW        7.4f
#define SPEC_ETA_PEAK_PCT      96.0f
#define SPEC_BOM_COST          900.0f
#define SPEC_INSTALL_COST      500.0f
#define SPEC_BASELINE_COST     800.0f

// DUMMY fleet inputs. Stock, V2G-capable share and participation are three
// separate numbers (context §8.E); none of them is a forecast here.
#define DUMMY_FLEET_YEAR        2036
#define DUMMY_N_EV              10.0e6f
#define DUMMY_V2G_SHARE         0.25f
#define DUMMY_PARTICIPATION     0.10f
#define DUMMY_PLUGGED_AT_PEAK   0.60f   // Pr(plugged in | 18:00-20:00)
#define DUMMY_DERATE            0.10f

void headline_compute(headline_t *h, const sched_params_t *p, int n_days) {
    static dummy_day_t day;
    static sched_plan_t v2g, smart, dumb;
    float sv = 0, ss = 0, sd = 0, so = 0;
    int ok = 0;
    for (int k = 0; k < n_days; k++) {
        dummy_make_day(&day, (uint32_t)k);
        if (!sched_plan(SCHED_V2G, &day, p, &v2g)) continue;
        sched_plan(SCHED_SMART_CHARGE, &day, p, &smart);
        sched_plan(SCHED_DUMB_CHARGE, &day, p, &dumb);
        sv += v2g.net_gbp; ss += smart.net_gbp; sd += dumb.net_gbp; so += v2g.kwh_out;
        ok++;
    }
    if (ok == 0) ok = 1;
    h->days = ok;
    h->v2g_net_day = sv / (float)ok;
    h->smart_net_day = ss / (float)ok;
    h->dumb_net_day = sd / (float)ok;
    h->kwh_out_day = so / (float)ok;
    h->p_rated_kw = SPEC_P_RATED_KW;
    h->eta_peak_pct = SPEC_ETA_PEAK_PCT;
    h->cost_gbp = SPEC_BOM_COST + SPEC_INSTALL_COST;
    h->premium_gbp = h->cost_gbp - SPEC_BASELINE_COST;
    h->uplift_yr = (h->v2g_net_day - h->smart_net_day) * 365.0f;
    h->vs_dumb_yr = (h->v2g_net_day - h->dumb_net_day) * 365.0f;
    h->payback_years = (h->uplift_yr > 0.0f) ? h->premium_gbp / h->uplift_yr : -1.0f;
    h->fleet_year = DUMMY_FLEET_YEAR;
    h->participation_pct = DUMMY_PARTICIPATION * 100.0f;
    h->fleet_gw = DUMMY_N_EV * DUMMY_V2G_SHARE * DUMMY_PARTICIPATION *
                  DUMMY_PLUGGED_AT_PEAK * p->p_discharge_max_kw * (1.0f - DUMMY_DERATE) / 1.0e6f;
}

int headline_sentence(char *buf, size_t cap, const headline_t *h) {
    char pay[24];
    if (h->payback_years > 0) snprintf(pay, sizeof pay, "%.1f years", (double)h->payback_years);
    else snprintf(pay, sizeof pay, "never");
    return snprintf(buf, cap,
        "Our %.1f kW, %.0f%%-peak-efficiency converter, costing GBP %.0f, earns an owner "
        "GBP %.0f/yr over smart one-way charging (GBP %.0f/yr vs plug-in-and-charge) and "
        "pays back its GBP %.0f premium in %s. At %.0f%% participation the %d fleet could "
        "supply about %.2f GW of evening peak support. [DUMMY DATA, not a forecast]",
        (double)h->p_rated_kw, (double)h->eta_peak_pct, (double)h->cost_gbp,
        (double)h->uplift_yr, (double)h->vs_dumb_yr, (double)h->premium_gbp, pay,
        (double)h->participation_pct, h->fleet_year, (double)h->fleet_gw);
}

int headline_json(char *buf, size_t cap, const headline_t *h) {
    return snprintf(buf, cap,
        "{\"p_rated_kw\":%.1f,\"cost_gbp\":%.0f,\"v2g_net_gbp_day\":%.3f,"
        "\"smart_net_gbp_day\":%.3f,\"dumb_net_gbp_day\":%.3f,"
        "\"uplift_gbp_yr\":%.0f,\"vs_dumb_gbp_yr\":%.0f,\"payback_years\":%.2f,"
        "\"fleet_year\":%d,\"fleet_gw\":%.3f,\"days_averaged\":%d,"
        "\"provenance\":\"dummy\",\"simulation\":true}",
        (double)h->p_rated_kw, (double)h->cost_gbp, (double)h->v2g_net_day,
        (double)h->smart_net_day, (double)h->dumb_net_day, (double)h->uplift_yr,
        (double)h->vs_dumb_yr, (double)h->payback_years, h->fleet_year,
        (double)h->fleet_gw, h->days);
}
