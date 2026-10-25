#ifndef V2G_DUMMY_DATA_H
#define V2G_DUMMY_DATA_H
#include <stdbool.h>
#include <stdint.h>

// ALL DATA IN THIS MODULE IS SYNTHETIC. It exists so the firmware, the
// optimiser and the demo outputs work with no network and no real feeds.
// Replace dummy_make_day() with real ingest output (context §11) later.
//
// Planning horizon = one "EV day", starting when the car arrives home:
//   slot 0  = 18:00, slot 1 = 18:30 ... slot 47 = 17:30 next day
//   slots 0..PLUG_SLOTS-1 are plugged in (18:00 -> 08:00); the rest it is driven.
// Units follow context §6.3: prices in GBP/MWh, power kW, SoC fraction 0..1,
// sign convention + = grid -> vehicle (charging).
#define DAY_SLOTS   48
#define SLOT_H      0.5f
#define PLUG_SLOTS  28

typedef struct {
    float buy_gbp_mwh[DAY_SLOTS];   // import price
    float sell_gbp_mwh[DAY_SLOTS];  // export price (buy minus a network fee)
    bool  plugged[DAY_SLOTS];
    int   depart_slot;              // first slot where the car is unplugged
    float soc_arrival;              // SoC when plugged in (slot 0)
    float soc_required;             // SoC the driver needs at departure
    uint32_t day_index;
    bool  windy_night;              // true => near-zero/negative overnight prices
} dummy_day_t;

void dummy_make_day(dummy_day_t *d, uint32_t day_index);
void dummy_slot_clock(int slot, int *hh, int *mm);

#endif
