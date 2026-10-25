// Host-only stubs for wifi / MQTT / power stage so main.c runs unmodified on a PC.
//   make -C host demo            -> offline run (no network at all)
//   make -C host demo-mqtt       -> same, but a scripted MQTT link appears at t=60 s
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include "wifi.h"
#include "mqtt_client.h"
#include "power_stage.h"

uint32_t host_now_ms;
#ifndef HOST_RUN_MS
#define HOST_RUN_MS 300000u
#endif
void sleep_ms(uint32_t ms) {
    host_now_ms += ms;
    if (host_now_ms > HOST_RUN_MS) { printf("[host] virtual run ended at %u ms\n", host_now_ms); exit(0); }
}

#ifdef HOST_MQTT
#define MQTT_UP_MS 60000u
static bool up(void) { return host_now_ms >= MQTT_UP_MS; }
#else
static bool up(void) { return false; }
#endif
#define RINGBUF 1024u

bool wifi_init(void) { return true; }
void wifi_update(uint32_t now) { (void)now; }
bool wifi_is_connected(void) { return up(); }
void wifi_shutdown(void) {}

bool mqtt_service_init(void) { return true; }
void mqtt_service_update(uint32_t now, bool w) { (void)now; (void)w; }
bool mqtt_service_ready(void) { return up(); }
bool mqtt_service_take_command(char *out, size_t cap) {
#ifdef HOST_MQTT
    static uint32_t next = 64000u; static int n;
    static const char *script[] = {"SET_POWER +3.00","SET_POWER +3.00","SET_POWER +3.00","STOP","AUTO"};
    if (!up() || host_now_ms < next || n >= 5) return false;
    snprintf(out, cap, "%s", script[n++]);
    printf("[host-mqtt] <- command '%s'\n", out);
    next += 2000u;
    return true;
#else
    (void)out; (void)cap; return false;
#endif
}
static bool pub(const char *name, const char *topic_suffix, const char *json) {
    size_t need = strlen(json) + strlen(topic_suffix) + 24 + 8;
    if (!up()) return false;
    if (need > RINGBUF) { printf("[host-mqtt] %s TOO BIG (%zu B)\n", name, need); return false; }
    if (strcmp(name, "telemetry") != 0 && strcmp(name, "status") != 0)
        printf("[host-mqtt] -> %s (%zu B) ok\n", name, strlen(json));
    return true;
}
bool mqtt_service_publish_telemetry(const char *j) { return pub("telemetry", "/telemetry", j); }
bool mqtt_service_publish_status(const char *j)    { return pub("status", "/status", j); }
bool mqtt_service_publish_spec(const char *j)      { return pub("spec", "/spec", j); }
bool mqtt_service_publish_headline(const char *j)  { return pub("headline", "/headline", j); }
bool mqtt_service_publish_plan(const char *j)      { return pub("plan", "/plan", j); }
void mqtt_service_shutdown(void) {}

static int last_dir = 99;
void power_stage_init(void) { printf("[power-stage] (host stub)\n"); }
bool power_stage_fault_input_ok(void) { return true; }
void power_stage_update(v2g_status_t s, uint32_t now_ms, bool ok) {
    (void)now_ms;
    int dir = (!ok || s.fault_latched) ? 0 : s.actual_power_kw > 0.001f ? 1 : s.actual_power_kw < -0.001f ? -1 : 0;
    if (dir != last_dir) {
        last_dir = dir;
        float duty = fminf(fabsf(s.actual_power_kw) / 7.4f, 1.0f) * 80.0f;
        printf("[led] %s%s (t=%.1fs, p=%+.2f kW, duty~%.0f%%)\n",
               dir > 0 ? "GREEN charge" : dir < 0 ? "BLUE discharge" : "all off",
               dir ? "" : "", host_now_ms / 1000.0, (double)s.actual_power_kw, (double)duty);
    }
}
void power_stage_shutdown(void) {}
