#ifndef V2G_POWER_STAGE_H
#define V2G_POWER_STAGE_H

#include <stdbool.h>
#include <stdint.h>
#include "converter.h"

// DEMONSTRATION OUTPUTS ONLY: connect to LEDs, scope or logic analyser.
// Do NOT connect these directly to MOSFET gates or a power bridge.
#define PIN_CHARGE_PWM      2u  // Pico physical pin 4
#define PIN_DISCHARGE_PWM   4u  // Pico physical pin 6
#define PIN_DRIVER_INHIBIT  6u  // Pico physical pin 9; held LOW permanently
#define PIN_FAULT_N         7u  // Pico physical pin 10; active low to GND
#define PIN_RUN_LED         8u  // Pico physical pin 11

void power_stage_init(void);
// False means the simulated external fault input is asserted.
bool power_stage_fault_input_ok(void);
// Render the converter's simulated signed power as mutually exclusive PWM.
// now_ms used to enforce a demonstrator-only neutral interval on reversal.
void power_stage_update(v2g_status_t status, uint32_t now_ms, bool mqtt_ready);
void power_stage_shutdown(void);

#endif
