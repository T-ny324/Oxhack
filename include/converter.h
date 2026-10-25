#ifndef V2G_CONVERTER_H
#define V2G_CONVERTER_H
#include <stdint.h>
#include <stdbool.h>

// Contract: positive kW = GRID -> EV (charge); negative = EV -> GRID.
typedef enum { V2G_STOPPED, V2G_CHARGING, V2G_DISCHARGING, V2G_FAULT } v2g_mode_t;
typedef struct {
    float battery_voltage_v;    // simulated for Wokwi; replace with ADC sensor
    float battery_current_a;    // positive into battery; simulated
    float battery_soc;          // fraction 0..1; placeholder until BMS is connected
    float grid_voltage_rms_v;   // placeholder; Pico does NOT measure grid in this demo
    float requested_power_kw;   // signed setpoint from external server
    float actual_power_kw;      // simulated signed power, not hardware measurement
    v2g_mode_t mode;
    bool fault_latched;
    bool data_simulated;
} v2g_status_t;
void converter_init(uint32_t now_ms);
bool converter_handle_command(const char *command, uint32_t now_ms);
void converter_update(uint32_t now_ms, bool mqtt_ready);
v2g_status_t converter_get_status(void);
const char *converter_mode_name(v2g_mode_t mode);
void converter_trip_fault(void);
// Scenario engine pushes its simulated SoC here (0..1). Ignored once FAULT is latched.
void converter_set_soc(float soc);
#endif
