#include "wifi.h"

#include <stdio.h>
#include "pico/cyw43_arch.h"

// Wokwi's AP is open. Change credentials for a *real* Pico 2 W and do not
// commit secrets. This example is for isolated simulation only.
#ifndef WIFI_SSID
#define WIFI_SSID "Wokwi-GUEST"
#endif
#ifndef WIFI_PASSWORD
#define WIFI_PASSWORD ""
#endif
#ifndef WIFI_AUTH
#define WIFI_AUTH CYW43_AUTH_OPEN
#endif

#define WIFI_STATUS_PERIOD_MS    500u
#define WIFI_RETRY_PERIOD_MS   10000u
#define WIFI_JOIN_TIMEOUT_MS  30000u

static bool initialized;
static bool connected;
static bool join_in_progress;
static uint32_t last_status_ms;
static uint32_t last_attempt_ms;
static bool ever_attempted;
static int previous_status = 9999;

bool wifi_init(void) {
    if (initialized) return true;
    int rc = cyw43_arch_init();
    if (rc != 0) {
        printf("[wifi] CYW43 init failed: %d\n", rc);
        return false;
    }
    cyw43_arch_enable_sta_mode();
    initialized = true;
    printf("[wifi] ready, SSID=%s\n", WIFI_SSID);
    return true;
}

void wifi_update(uint32_t now_ms) {
    if (!initialized) return;

    // Required by pico_cyw43_arch_lwip_poll. MQTT and DNS callbacks are
    // dispatched by this single-threaded poll, not from an interrupt.
    cyw43_arch_poll();

    if ((uint32_t)(now_ms - last_status_ms) < WIFI_STATUS_PERIOD_MS)
        return;
    last_status_ms = now_ms;

    // Unlike cyw43_wifi_link_status(), this includes IP/DHCP readiness.
    // 0=down, 1=joined, 2=no IP yet, 3=up with IP, negative=error.
    int link = cyw43_tcpip_link_status(&cyw43_state, CYW43_ITF_STA);
    if (link != previous_status) {
        printf("[wifi] TCP/IP link status = %d\n", link);
        previous_status = link;
    }

    bool online = (link == CYW43_LINK_UP);
    if (online != connected)
        printf("[wifi] %s\n", online ? "connected" : "disconnected");
    connected = online;

    if (online) {
        join_in_progress = false;
        return;
    }

    // After a successful association the IP stack may take many seconds
    // to get DHCP (the user's Wokwi logs showed this exact behaviour).
    // Do NOT restart association repeatedly while link is JOIN/NOIP.
    if (join_in_progress &&
        (link == CYW43_LINK_JOIN || link == CYW43_LINK_NOIP) &&
        (uint32_t)(now_ms - last_attempt_ms) < WIFI_JOIN_TIMEOUT_MS) {
        return;
    }

    // If the previous attempt is still within its timeout, leave it alone.
    if (join_in_progress &&
        (uint32_t)(now_ms - last_attempt_ms) < WIFI_JOIN_TIMEOUT_MS &&
        link >= 0) {
        return;
    }

    // Avoid tight retry loops on link errors and timeouts.
    if (ever_attempted &&
        (uint32_t)(now_ms - last_attempt_ms) < WIFI_RETRY_PERIOD_MS)
        return;

    ever_attempted = true;
    join_in_progress = true;
    last_attempt_ms = now_ms;
    printf("[wifi] joining %s (link=%d)\n", WIFI_SSID, link);
    int rc = cyw43_arch_wifi_connect_async(WIFI_SSID, WIFI_PASSWORD, WIFI_AUTH);
    if (rc != 0) {
        join_in_progress = false;
        printf("[wifi] connect start failed: %d\n", rc);
    }
}

bool wifi_is_connected(void) { return initialized && connected; }

void wifi_shutdown(void) {
    if (!initialized) return;
    cyw43_arch_disable_sta_mode();
    cyw43_arch_deinit();
    initialized = connected = join_in_progress = ever_attempted = false;
    previous_status = 9999;
}
