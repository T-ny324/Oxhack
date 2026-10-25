#include "converter.h"
#include <ctype.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

// Placeholder 0.1 design contract. NOT verified physical ratings.
#define CHARGE_LIMIT_KW     7.4f
#define DISCHARGE_LIMIT_KW  7.4f
#define RAMP_KW_S           5.0f
#define COMMAND_LEASE_MS    5000u
#define SIM_BATTERY_KWH     60.0f  // additional example-only SoC model assumption
#define EPS_KW              0.001f
#define SOC_MIN_TRIP        0.20f
#define SOC_MAX_TRIP        0.90f
#define SOC_TRIP_TOL        0.0005f // scheduler plans sit exactly ON the limits; don't trip on float error

static v2g_status_t s;
static uint32_t last_update, last_command;
static bool lease_active;

void converter_init(uint32_t now_ms) {
    s = (v2g_status_t){0};
    s.battery_voltage_v = 350.0f; // demo value, within placeholder 250..450V range
    s.grid_voltage_rms_v = 230.0f; // NOT measured; placeholder
    s.battery_soc = 0.60f;        // NOT measured; placeholder
    s.data_simulated = true;
    s.mode = V2G_STOPPED;
    last_update = last_command = now_ms;
    lease_active = false;
}

// Server sends 'SET_POWER +2.5', 'SET_POWER -2.5', or 'STOP'.
// Accept only strict ASCII decimal values in this demonstration.
bool converter_handle_command(const char *text, uint32_t now_ms) {
    if (!text || s.fault_latched) return false;
    float target = 0.0f;
    if (strcmp(text, "STOP") != 0) {
        if (strncmp(text, "SET_POWER ", 10) != 0) return false;
        const char *start = text + 10;
        if (!(*start == '+' || *start == '-' || isdigit((unsigned char)*start))) return false;
        char *end = NULL;
        target = strtof(start, &end);
        if (end == start || !isfinite(target) || *end != '\0') return false;
        if (target > CHARGE_LIMIT_KW || target < -DISCHARGE_LIMIT_KW) return false;
    }
    // No actual power stage is enabled by this module.
    bool changed = fabsf(target - s.requested_power_kw) > 0.0005f;
    s.requested_power_kw = target;
    last_command = now_ms;
    lease_active = target != 0.0f;
    if (changed)   // lease refreshes (every second) stay quiet
        printf("[converter] signed power request %+0.3f kW\n", (double)target);
    return true;
}

void converter_update(uint32_t now_ms, bool mqtt_ready) {
    uint32_t elapsed = (uint32_t)(now_ms - last_update);
    last_update = now_ms;
    if (!mqtt_ready || (lease_active && (uint32_t)(now_ms - last_command) > COMMAND_LEASE_MS)) {
        s.requested_power_kw = 0.0f;
        lease_active = false;
    }
    if (s.fault_latched) {
        s.requested_power_kw = s.actual_power_kw = s.battery_current_a = 0.0f;
        s.mode = V2G_FAULT;
        return;
    }
    float dt = fminf((float)elapsed / 1000.0f, 0.2f);
    float delta = s.requested_power_kw - s.actual_power_kw;
    float max_delta = RAMP_KW_S * dt;
    if (delta > max_delta) delta = max_delta;
    if (delta < -max_delta) delta = -max_delta;
    s.actual_power_kw += delta;
    if (fabsf(s.actual_power_kw) <= EPS_KW) {
        s.actual_power_kw = 0.0f;
        s.mode = V2G_STOPPED;
    } else {
        s.mode = s.actual_power_kw > 0 ? V2G_CHARGING : V2G_DISCHARGING;
    }
    // Simplified 60 kWh ideal pack. This is NOT a battery/BMS model.
    s.battery_soc += s.actual_power_kw * dt / (3600.0f * SIM_BATTERY_KWH);
    if (s.battery_soc > SOC_MAX_TRIP + SOC_TRIP_TOL || s.battery_soc < SOC_MIN_TRIP - SOC_TRIP_TOL) {
        converter_trip_fault(); // trip on simulated boundaries
        return;
    }
    // Positive = charging the battery.
    s.battery_current_a = s.actual_power_kw * 1000.0f / s.battery_voltage_v;
}

void converter_trip_fault(void) {
    s.fault_latched = true;
    s.requested_power_kw = s.actual_power_kw = s.battery_current_a = 0.0f;
    s.mode = V2G_FAULT;
    lease_active = false;
}
void converter_set_soc(float soc) { if (!s.fault_latched) s.battery_soc = soc; }
v2g_status_t converter_get_status(void) { return s; }
const char *converter_mode_name(v2g_mode_t mode) {
    switch (mode) {
        case V2G_CHARGING: return "CHARGING";
        case V2G_DISCHARGING: return "DISCHARGING";
        case V2G_FAULT: return "FAULT";
        default: return "STOPPED";
    }
}
