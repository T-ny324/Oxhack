#include "dummy_data.h"
#include <math.h>

// Hourly import price, GBP/MWh, index = clock hour. DUMMY shape: cheap
// overnight, morning shoulder, deep afternoon dip, sharp 17:00-20:00 peak.
static const float HOURLY[24] = {
     70,  55,  42,  35,  30,  38,  75, 125, 140, 110,  95,  85,
     80,  78,  82,  95, 130, 190, 240, 215, 160, 120,  95,  80
};
#define EXPORT_FEE_GBP_MWH 10.0f   // dummy network/export fee

static float lcg_unit(uint32_t *x) {   // deterministic noise in [-1, 1]
    *x = *x * 1664525u + 1013904223u;
    return (float)((*x >> 8) & 0xFFFFu) / 32767.5f - 1.0f;
}

void dummy_slot_clock(int slot, int *hh, int *mm) {
    int minutes = (18 * 60 + slot * 30) % (24 * 60);
    if (hh) *hh = minutes / 60;
    if (mm) *mm = minutes % 60;
}

void dummy_make_day(dummy_day_t *d, uint32_t day_index) {
    uint32_t rng = 12345u + day_index * 7919u;
    unsigned dow = day_index % 7u;
    bool windy   = (dow == 2u || dow == 5u);
    bool weekend = (dow == 5u || dow == 6u);

    d->day_index = day_index;
    d->windy_night = windy;
    d->depart_slot = PLUG_SLOTS;
    d->soc_arrival = 0.30f + 0.05f * (float)(day_index % 3u);   // 0.30/0.35/0.40
    d->soc_required = 0.80f;

    for (int s = 0; s < DAY_SLOTS; s++) {
        float hf = 18.0f + 0.5f * (float)s;
        int h0 = (int)hf % 24, h1 = (h0 + 1) % 24;
        float frac = hf - floorf(hf);
        float p = HOURLY[h0] * (1.0f - frac) + HOURLY[h1] * frac;
        if (windy && h0 >= 0 && h0 < 6 && s >= 12)   // after midnight only
            p = p * 0.3f - 12.0f;
        if (weekend && h0 >= 17 && h0 < 21) p *= 0.85f;
        p += 6.0f * lcg_unit(&rng);
        d->buy_gbp_mwh[s]  = p;
        d->sell_gbp_mwh[s] = p - EXPORT_FEE_GBP_MWH;
        d->plugged[s] = (s < PLUG_SLOTS);
    }
}
