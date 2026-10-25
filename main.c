#include <stdio.h>
#include <stdint.h>
#include "pico/stdlib.h"
#include "wifi.h"
#include "mqtt_client.h"
#include "converter.h"
#include "power_stage.h"

// MQTT-only operation. No local scheduler or autonomous charging.
// A valid SET_POWER command must be refreshed at least every 5 seconds.
#define TELEMETRY_PERIOD_MS 1000u

int main(void) {
    stdio_init_all();
    sleep_ms(1500);
    printf("[main] MQTT-only V2G: startup STOPPED; no dummy scheduler\n");
    printf("[main] Battery values remain SIMULATED, not physical measurements\n");

    if (!wifi_init()) return 1;
    if (!mqtt_service_init()) return 1;
    uint32_t now = to_ms_since_boot(get_absolute_time());
    converter_init(now);
    power_stage_init();
    uint32_t last_telemetry = now;
    char cmd[80];

    while (true) {
        now = to_ms_since_boot(get_absolute_time());
        wifi_update(now);
        mqtt_service_update(now, wifi_is_connected());
        bool mqtt_ok = mqtt_service_ready();

        // Commands are accepted only while the MQTT session is ready.
        if (mqtt_service_take_command(cmd, sizeof cmd)) {
            if (!mqtt_ok) {
                printf("[main] ignoring command while MQTT disconnected\n");
            } else if (!converter_handle_command(cmd, now)) {
                printf("[main] rejected command: %s\n", cmd);
            }
        }

        if (!power_stage_fault_input_ok()) converter_trip_fault();
        converter_update(now, mqtt_ok);
        v2g_status_t s = converter_get_status();
        power_stage_update(s, now, mqtt_ok);

        if ((uint32_t)(now - last_telemetry) >= TELEMETRY_PERIOD_MS) {
            last_telemetry = now;
            char payload[384];
            int len = snprintf(payload, sizeof payload,
                "{\"source\":\"mqtt_only\",\"mode\":\"%s\",\"fault\":%s,"
                "\"battery_soc\":%.4f,\"power_kw\":%.3f,"
                "\"requested_power_kw\":%.3f,\"battery_current_a\":%.3f,"
                "\"battery_voltage_v\":%.1f,\"simulation\":true,"
                "\"sensor_source\":\"simulated_battery\"}",
                converter_mode_name(s.mode), s.fault_latched ? "true" : "false",
                (double)s.battery_soc, (double)s.actual_power_kw,
                (double)s.requested_power_kw, (double)s.battery_current_a,
                (double)s.battery_voltage_v);
            if (len > 0 && (size_t)len < sizeof payload) {
                if (mqtt_ok) {
                    mqtt_service_publish_telemetry(payload);
                    mqtt_service_publish_status(payload);
                }
                printf("[telemetry] %s\n", payload);
            }
        }
        sleep_ms(10);
    }
}
