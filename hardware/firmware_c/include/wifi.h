#ifndef V2G_WIFI_H
#define V2G_WIFI_H

#include <stdbool.h>
#include <stdint.h>

// Initialise the Pico 2 W wireless chip; call exactly once before mqtt_init().
bool wifi_init(void);

// Service lwIP and the CYW43 driver, then check/retry Wi-Fi connectivity.
// Call from the SAME core in the main loop, approximately every 10-20 ms.
void wifi_update(uint32_t now_ms);

// True only when the Wi-Fi driver reports CYW43_LINK_UP.
bool wifi_is_connected(void);

// Optional cleanup; call after shutting down MQTT.
void wifi_shutdown(void);

#endif
