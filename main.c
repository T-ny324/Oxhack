#include <math.h>
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include "pico/stdlib.h"
#include "wifi.h"
#include "mqtt_client.h"
#include "converter.h"
#include "power_stage.h"
#include "dummy_data.h"
#include "scheduler.h"
#include "headline.h"

// ---------------------------------------------------------------------------
// OFFLINE-FIRST DEMO. The converter is driven by a built-in scenario engine:
//   dummy day (prices, plug-in window, SoC)  ->  DP optimiser  ->  setpoints
// MQTT is an optional extra: it publishes results and can OVERRIDE the plan
// with a 5 s command lease ("SET_POWER +2.0", "STOP"); only "AUTO" resumes the plan.
// Everything printed/published is simulated and labelled as such.
// ---------------------------------------------------------------------------
#define SLOT_MS              3000u   // 1 simulated half-hour = 3 s (plugged window = 84 s)
#define CMD_REFRESH_MS       1000u   // converter command lease is 5 s: refresh well inside it
#define OVERRIDE_MS          5000u
#define TELEMETRY_PERIOD_MS  1000u
#define PUBLISH_RETRY_MS     2000u
#define HEADLINE_DAYS        7

static const char SPEC_JSON[] =
    "{\"spec_version\":\"0.1\",\"provenance\":\"placeholder\","
    "\"topology\":\"DAB + bidirectional AC-DC (concept)\","
    "\"architecture\":\"dc_offboard\","
    "\"grid_side\":{\"phases\":1,\"v_ac_rms\":230,\"f_hz\":50},"
    "\"battery_side\":{\"v_dc_min\":250,\"v_dc_max\":450},"
    "\"power\":{\"p_charge_max_kw\":7.4,\"p_discharge_max_kw\":7.4,\"q_max_kvar\":3.0},"
    "\"dynamics\":{\"response_time_s\":1.0,\"ramp_kw_per_s\":5.0},"
    "\"standby_w\":10,\"lifetime_years\":10}";

static sched_params_t params;
static dummy_day_t day;
static sched_plan_t plan, plan_smart, plan_dumb;
static uint32_t day_index, day_start_ms;
static bool override_active;
static uint32_t override_until_ms;
static char headline_text[640], headline_js[480];

static void print_plan(void) {
    printf("[plan] day %u (%s night): arrive SoC %.2f, need %.2f by 08:00\n",
           (unsigned)day.day_index, day.windy_night ? "windy" : "normal",
           (double)day.soc_arrival, (double)day.soc_required);
    printf("[plan] net GBP  plug-in %.2f | smart-charge %.2f | V2G %.2f  (V2G exports %.1f kWh)\n",
           (double)plan_dumb.net_gbp, (double)plan_smart.net_gbp,
           (double)plan.net_gbp, (double)plan.kwh_out);
    printf("[plan] slot,clock,buy_gbp_mwh,sell_gbp_mwh,p_kw,soc_end\n");
    for (int t = 0; t < day.depart_slot; t++) {
        int hh, mm; dummy_slot_clock(t, &hh, &mm);
        printf("[plan] %d,%02d:%02d,%.1f,%.1f,%+.2f,%.3f\n", t, hh, mm,
               (double)day.buy_gbp_mwh[t], (double)day.sell_gbp_mwh[t],
               (double)plan.p_kw[t], (double)plan.soc[t + 1]);
    }
}

static void new_day(uint32_t now) {
    dummy_make_day(&day, day_index);
    if (!sched_plan(SCHED_V2G, &day, &params, &plan))
        printf("[plan] WARNING: departure SoC not reachable; charging flat out\n");
    sched_plan(SCHED_SMART_CHARGE, &day, &params, &plan_smart);
    sched_plan(SCHED_DUMB_CHARGE, &day, &params, &plan_dumb);
    day_start_ms = now;
    print_plan();
}

static int format_plan_json(char *buf, size_t cap) {
    int n = snprintf(buf, cap, "{\"day\":%u,\"start\":\"18:00\",\"dt_h\":0.5,\"net_gbp\":%.3f,\"p_kw\":[",
                     (unsigned)day.day_index, (double)plan.net_gbp);
    for (int t = 0; t < day.depart_slot && n > 0 && (size_t)n < cap; t++)
        n += snprintf(buf + n, cap - (size_t)n, "%s%.1f", t ? "," : "", (double)plan.p_kw[t]);
    if (n > 0 && (size_t)n < cap)
        n += snprintf(buf + n, cap - (size_t)n, "],\"simulation\":true}");
    return n;
}

int main(void) {
    stdio_init_all();
    sleep_ms(1500);
    printf("V2G Pico 2 W prototype: DUMMY DATA, simulated measurements only\n");
    printf("[main] MQTT is optional: the demo runs from built-in data even with no network\n");

    // Heavy maths first, BEFORE Wi-Fi starts, so lwIP is not starved of cyw43_arch_poll().
    sched_default_params(&params);
    headline_t hl;
    headline_compute(&hl, &params, HEADLINE_DAYS);
    headline_sentence(headline_text, sizeof headline_text, &hl);
    headline_json(headline_js, sizeof headline_js, &hl);
    printf("[headline] %s\n", headline_text);

    if (!wifi_init()) return 1;
    if (!mqtt_service_init()) return 1;
    uint32_t now = to_ms_since_boot(get_absolute_time());
    converter_init(now);
    power_stage_init();
    new_day(now);

    uint32_t last_telemetry = now, last_cmd = now - CMD_REFRESH_MS, last_pub = now;
    int cur_slot = -1;
    bool sent_spec = false, sent_headline = false, sent_plan = false;
    char cmd[80];

    while (true) {
        now = to_ms_since_boot(get_absolute_time());
        wifi_update(now);
        mqtt_service_update(now, wifi_is_connected());
        bool mqtt_ok = mqtt_service_ready();

        // --- optional remote control ------------------------------------
        if (mqtt_service_take_command(cmd, sizeof cmd)) {
            if (strcmp(cmd, "AUTO") == 0) {
                override_active = false;
                printf("[main] AUTO: plan resumed\n");
            } else if (converter_handle_command(cmd, now)) {
                override_active = true;
                override_until_ms = now + OVERRIDE_MS;
            } else {
                printf("[main] rejected setpoint: %s\n", cmd);
            }
        }
        if (override_active && (int32_t)(now - override_until_ms) >= 0) {
            // Do not restart automatic power flow after a lost laptop command.
            // Remain in remote mode until an explicit AUTO command arrives.
            converter_handle_command("STOP", now);
            override_until_ms = now + OVERRIDE_MS;
            printf("[main] remote command expired: STOP (send AUTO to resume plan)\n");
        }

        // --- scenario clock ---------------------------------------------
        uint32_t elapsed = now - day_start_ms;
        int slot = (int)(elapsed / SLOT_MS);
        if (slot >= DAY_SLOTS) {
            day_index++;
            new_day(now);
            sent_plan = false;
            elapsed = 0; slot = 0; cur_slot = -1;
        }
        bool plugged = slot < day.depart_slot && day.plugged[slot];
        if (slot != cur_slot) {
            cur_slot = slot;
            int hh, mm; dummy_slot_clock(slot, &hh, &mm);
            printf("[slot %02d %02d:%02d] %s buy=%.1f sell=%.1f GBP/MWh plan=%+.2f kW SoC=%.3f%s\n",
                   slot, hh, mm, plugged ? "PLUGGED" : "driving", (double)day.buy_gbp_mwh[slot],
                   (double)day.sell_gbp_mwh[slot], (double)(plugged ? plan.p_kw[slot] : 0.0f),
                   (double)plan.soc[slot], override_active ? " [REMOTE OVERRIDE]" : "");
            last_cmd = now - CMD_REFRESH_MS;     // send the new setpoint immediately
        }

        // --- plan -> converter ------------------------------------------
        if (!override_active) {
            float frac = (float)(elapsed - (uint32_t)slot * SLOT_MS) / (float)SLOT_MS;
            converter_set_soc(plan.soc[slot] + (plan.soc[slot + 1] - plan.soc[slot]) * frac);
            if ((uint32_t)(now - last_cmd) >= CMD_REFRESH_MS) {
                last_cmd = now;
                float target = plugged ? plan.p_kw[slot] : 0.0f;
                char buf[32];
                if (fabsf(target) < 0.005f) snprintf(buf, sizeof buf, "STOP");
                else snprintf(buf, sizeof buf, "SET_POWER %+.2f", (double)target);
                if (!converter_handle_command(buf, now) && !converter_get_status().fault_latched)
                    printf("[main] converter rejected: %s\n", buf);
            }
        }

        // Active-low emergency-stop input on GP7; a press latches FAULT.
        if (!power_stage_fault_input_ok()) converter_trip_fault();
        bool drive_ok = override_active ? mqtt_ok : true;   // plan needs no network
        converter_update(now, drive_ok);
        v2g_status_t s = converter_get_status();
        power_stage_update(s, now, drive_ok);

        // --- outputs ------------------------------------------------------
        if ((uint32_t)(now - last_telemetry) >= TELEMETRY_PERIOD_MS) {
            last_telemetry = now;
            int hh, mm; dummy_slot_clock(slot, &hh, &mm);
            char payload[448];
            int len = snprintf(payload, sizeof payload,
                "{\"clock\":\"%02d:%02d\",\"day\":%u,\"plugged\":%s,\"source\":\"%s\","
                "\"mode\":\"%s\",\"fault\":%s,\"battery_soc\":%.4f,\"power_kw\":%.3f,"
                "\"requested_power_kw\":%.3f,\"battery_current_a\":%.3f,"
                "\"battery_voltage_v\":%.1f,\"buy_gbp_mwh\":%.1f,"
                "\"simulation\":true,\"sensor_source\":\"dummy_scenario\"}",
                hh, mm, (unsigned)day.day_index, plugged ? "true" : "false",
                override_active ? "mqtt_override" : "dummy_plan",
                converter_mode_name(s.mode), s.fault_latched ? "true" : "false",
                (double)s.battery_soc, (double)s.actual_power_kw,
                (double)s.requested_power_kw, (double)s.battery_current_a,
                (double)s.battery_voltage_v, (double)day.buy_gbp_mwh[slot]);
            if (len > 0 && (size_t)len < sizeof payload) {
                if (mqtt_ok) { mqtt_service_publish_telemetry(payload); mqtt_service_publish_status(payload); }
                printf("[telemetry] %s\n", payload);
            }
        }

        // One-off/retained publishes, retried gently (never a 10 ms hot loop).
        if (!mqtt_ok) { sent_spec = sent_headline = sent_plan = false; }
        else if ((uint32_t)(now - last_pub) >= PUBLISH_RETRY_MS) {
            last_pub = now;
            char pj[640];
            if (!sent_spec) sent_spec = mqtt_service_publish_spec(SPEC_JSON);
            else if (!sent_headline) sent_headline = mqtt_service_publish_headline(headline_js);
            else if (!sent_plan && format_plan_json(pj, sizeof pj) < (int)sizeof pj)
                sent_plan = mqtt_service_publish_plan(pj);
        }
        sleep_ms(10);
    }
}
