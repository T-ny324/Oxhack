#ifndef V2G_MQTT_CLIENT_H
#define V2G_MQTT_CLIENT_H
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
bool mqtt_service_init(void);
void mqtt_service_update(uint32_t now_ms, bool wifi_connected);
bool mqtt_service_ready(void);
bool mqtt_service_take_command(char *out, size_t cap);
bool mqtt_service_publish_telemetry(const char *json);
bool mqtt_service_publish_spec(const char *json);
bool mqtt_service_publish_status(const char *json);
bool mqtt_service_publish_headline(const char *json);  // retained
bool mqtt_service_publish_plan(const char *json);      // retained
void mqtt_service_shutdown(void);
#endif
