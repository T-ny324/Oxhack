// Host-only: print one dummy day's three plans, optionally as CSV for tools/check_against_lp.py
#include "scheduler.h"
#include "headline.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(int argc, char **argv) {
    uint32_t day = (argc > 1) ? (uint32_t)atoi(argv[1]) : 0;
    bool csv = (argc > 2 && strcmp(argv[2], "--csv") == 0);
    sched_params_t p; sched_default_params(&p);
    static dummy_day_t d; static sched_plan_t v2g, smart, dumb;
    dummy_make_day(&d, day);
    sched_plan(SCHED_V2G, &d, &p, &v2g);
    sched_plan(SCHED_SMART_CHARGE, &d, &p, &smart);
    sched_plan(SCHED_DUMB_CHARGE, &d, &p, &dumb);
    if (csv) {
        printf("# net_v2g=%.6f standby=%.6f\n", (double)v2g.net_gbp, (double)v2g.standby_gbp);
        printf("slot,buy,sell,plugged,p_kw,soc\n");
        for (int t = 0; t < DAY_SLOTS; t++)
            printf("%d,%.4f,%.4f,%d,%.4f,%.4f\n", t, (double)d.buy_gbp_mwh[t],
                   (double)d.sell_gbp_mwh[t], d.plugged[t], (double)v2g.p_kw[t], (double)v2g.soc[t]);
        return 0;
    }
    printf("day %u (%s night), arrive SoC %.2f, need %.2f by 08:00\n", (unsigned)day,
           d.windy_night ? "windy" : "normal", (double)d.soc_arrival, (double)d.soc_required);
    printf("slot  clock  buy   sell   dumb   smart    V2G    SoC(V2G)\n");
    for (int t = 0; t < d.depart_slot; t++) {
        int hh, mm; dummy_slot_clock(t, &hh, &mm);
        printf("%3d  %02d:%02d %6.1f %6.1f %6.2f %7.2f %7.2f   %.3f\n", t, hh, mm,
               (double)d.buy_gbp_mwh[t], (double)d.sell_gbp_mwh[t], (double)dumb.p_kw[t],
               (double)smart.p_kw[t], (double)v2g.p_kw[t], (double)v2g.soc[t + 1]);
    }
    printf("net GBP: dumb %.3f | smart %.3f | V2G %.3f  (V2G exports %.1f kWh, degradation %.3f)\n",
           (double)dumb.net_gbp, (double)smart.net_gbp, (double)v2g.net_gbp,
           (double)v2g.kwh_out, (double)v2g.degradation_gbp);
    headline_t h; headline_compute(&h, &p, 7);
    char s[600]; headline_sentence(s, sizeof s, &h);
    printf("\n%s\n", s);
    return 0;
}
