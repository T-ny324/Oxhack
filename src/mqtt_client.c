#include "mqtt_client.h"
#include "wifi.h"

#include <stdio.h>
#include <string.h>
#include "pico/stdlib.h"
#include "lwip/apps/mqtt.h"
#include "lwip/dns.h"
#include "lwip/ip_addr.h"

// One broker shared with the laptop script. Never silently switch brokers.
#ifndef MQTT_BROKER_HOST
#define MQTT_BROKER_HOST "test.mosquitto.org"
#endif
#define BROKER_HOST MQTT_BROKER_HOST
#ifndef V2G_TOPIC_PREFIX
#define V2G_TOPIC_PREFIX "v2g/oxhack-4000"
#endif
#ifndef MQTT_CLIENT_ID
#define MQTT_CLIENT_ID "oxhack-pico2w-4000"
#endif

#define MQTT_COMMAND_TOPIC    V2G_TOPIC_PREFIX "/setpoint"
#define MQTT_TELEMETRY_TOPIC  V2G_TOPIC_PREFIX "/telemetry"
#define MQTT_SPEC_TOPIC       V2G_TOPIC_PREFIX "/spec"
#define MQTT_STATUS_TOPIC     V2G_TOPIC_PREFIX "/status"
#define MQTT_HEADLINE_TOPIC   V2G_TOPIC_PREFIX "/headline"
#define MQTT_PLAN_TOPIC       V2G_TOPIC_PREFIX "/plan"
#define MQTT_ONLINE_TOPIC     V2G_TOPIC_PREFIX "/online"
#define MQTT_PORT_NUMBER      1883u
#define MQTT_RETRY_MS         3000u
#define MQTT_CONNECT_TIMEOUT_MS 20000u
#define MQTT_MAX_COMMAND      80u

static mqtt_client_t *client;
static struct mqtt_connect_client_info_t client_info;
static ip_addr_t broker_ip;
static bool connected;
static bool subscribed;
static bool connecting;
static bool dns_pending;
static bool attempted;
static uint32_t last_attempt_ms;
static uint32_t connect_started_ms;

// lwIP may split an MQTT message across several payload callbacks.
static char incoming[MQTT_MAX_COMMAND];
static size_t incoming_len;
static size_t expected_len;
static bool incoming_valid;
static char pending_command[MQTT_MAX_COMMAND];
static bool command_pending;

static void start_connection(void);

static const char *status_name(mqtt_connection_status_t st) {
    switch (st) {
        case MQTT_CONNECT_ACCEPTED: return "accepted";
        case MQTT_CONNECT_REFUSED_PROTOCOL_VERSION: return "refused: protocol version";
        case MQTT_CONNECT_REFUSED_IDENTIFIER: return "refused: client id";
        case MQTT_CONNECT_REFUSED_SERVER: return "refused: server unavailable";
        case MQTT_CONNECT_REFUSED_USERNAME_PASS: return "refused: user/pass";
        case MQTT_CONNECT_REFUSED_NOT_AUTHORIZED_: return "refused: not authorised";
        case MQTT_CONNECT_DISCONNECTED: return "disconnected";
        case MQTT_CONNECT_TIMEOUT: return "lwIP TCP/MQTT timeout";
        default: return "unknown";
    }
}

static void incoming_publish_cb(void *arg, const char *topic, u32_t total_len) {
    (void)arg;
    incoming_len = 0;
    expected_len = total_len;
    incoming_valid = (strcmp(topic, MQTT_COMMAND_TOPIC) == 0 &&
                      total_len > 0 && total_len < sizeof(incoming));
}

static void incoming_data_cb(void *arg, const u8_t *data, u16_t len, u8_t flags) {
    (void)arg;
    if (incoming_valid) {
        if (incoming_len + len > expected_len ||
            incoming_len + len >= sizeof(incoming)) {
            incoming_valid = false;
        } else {
            if (len) memcpy(incoming + incoming_len, data, len);
            incoming_len += len;
        }
    }
    if (flags & MQTT_DATA_FLAG_LAST) {
        if (incoming_valid && incoming_len == expected_len &&
            memchr(incoming, '\0', incoming_len) == NULL) {
            incoming[incoming_len] = '\0';
            // STOP is not displaced by later commands in the same polling cycle.
            if (!command_pending || strcmp(incoming, "STOP") == 0 ||
                strcmp(pending_command, "STOP") != 0) {
                memcpy(pending_command, incoming, incoming_len + 1);
                command_pending = true;
                printf("[mqtt] command received: %s\n", incoming);
            }
        }
        incoming_valid = false;
    }
}

static void subscribe_cb(void *arg, err_t err) {
    (void)arg;
    subscribed = (err == ERR_OK);
    printf("[mqtt] subscribe result: %d\n", err);
    if (subscribed) {
        const char online[] = "1";
        err_t rc = mqtt_publish(client, MQTT_ONLINE_TOPIC,
                                online, 1, 0, 1, NULL, NULL);
        if (rc != ERR_OK)
            printf("[mqtt] online publish failed: %d\n", rc);
    }
}

static void connection_cb(mqtt_client_t *mqtt, void *arg,
                          mqtt_connection_status_t status) {
    (void)arg;
    connecting = false;
    connected = (status == MQTT_CONNECT_ACCEPTED);
    subscribed = false;
    command_pending = false;
    incoming_valid = false;
    if (!connected) {
        printf("[mqtt] %s: %s (%d)\n", BROKER_HOST, status_name(status), (int)status);
        return;
    }
    printf("[mqtt] TCP+MQTT up in %u ms; subscribing to %s\n",
           (unsigned)(to_ms_since_boot(get_absolute_time()) - connect_started_ms),
           MQTT_COMMAND_TOPIC);
    err_t err = mqtt_subscribe(mqtt, MQTT_COMMAND_TOPIC, 1,
                               subscribe_cb, NULL);
    if (err != ERR_OK)
        printf("[mqtt] subscribe start failed: %d\n", err);
}

static void start_connection(void) {
    if (!client || !wifi_is_connected() || connecting || connected)
        return;
    connecting = true; // Set before calling lwIP: callbacks may run promptly.
    connect_started_ms = to_ms_since_boot(get_absolute_time());
    printf("[mqtt] connecting to %s (%s):%u\n", BROKER_HOST,
           ipaddr_ntoa(&broker_ip), (unsigned)MQTT_PORT_NUMBER);
    err_t err = mqtt_client_connect(client, &broker_ip,
                                    MQTT_PORT_NUMBER, connection_cb,
                                    NULL, &client_info);
    if (err != ERR_OK) {
        connecting = false;
        printf("[mqtt] connect start failed: %d\n", err);
    }
}

static void dns_cb(const char *name, const ip_addr_t *addr, void *arg) {
    (void)arg;
    dns_pending = false;
    if (!addr || !wifi_is_connected()) {
        printf("[mqtt] DNS failed or Wi-Fi lost\n");
        return;
    }
    broker_ip = *addr;
    printf("[mqtt] DNS: %s -> %s\n", name ? name : "?", ipaddr_ntoa(addr));
    start_connection();
}

bool mqtt_service_init(void) {
    // Called ONCE, after wifi_init; do not allocate a new client on retries.
    if (client) return true;
    client = mqtt_client_new();
    if (!client) {
        printf("[mqtt] allocation failed\n");
        return false;
    }
    memset(&client_info, 0, sizeof(client_info));
    client_info.client_id = MQTT_CLIENT_ID;
    client_info.keep_alive = 30;
    client_info.will_topic = MQTT_ONLINE_TOPIC;
    client_info.will_msg = "0";
    client_info.will_qos = 0;
    client_info.will_retain = 1;
    mqtt_set_inpub_callback(client, incoming_publish_cb,
                            incoming_data_cb, NULL);
    printf("[mqtt] client id=%s, prefix=%s\n", MQTT_CLIENT_ID, V2G_TOPIC_PREFIX);
    return true;
}

void mqtt_service_update(uint32_t now_ms, bool wifi_up) {
    if (!client) return;

    if (!wifi_up) {
        // Cancel an in-flight TCP connection before trying again after
        // Wi-Fi returns. This prevents accumulating MQTT keepalive timers.
        if (connected || connecting) mqtt_disconnect(client);
        connected = subscribed = connecting = command_pending = false;
        dns_pending = false;
        // Require a fresh cooldown on Wi-Fi reconnection.
        attempted = false;
        return;
    }
    if (connected && !mqtt_client_is_connected(client)) {
        printf("[mqtt] TCP session lost\n");
        connected = subscribed = command_pending = false;
    }
    if (connecting &&
        (uint32_t)(now_ms - connect_started_ms) >= MQTT_CONNECT_TIMEOUT_MS) {
        printf("[mqtt] no CONNACK from %s after %u ms; retrying same broker\n",
               BROKER_HOST, (unsigned)MQTT_CONNECT_TIMEOUT_MS);
        mqtt_disconnect(client);           // fires connection_cb(DISCONNECTED)
        connecting = false;
        last_attempt_ms = now_ms;
    }
    if (connected || connecting || dns_pending) return;
    if (attempted &&
        (uint32_t)(now_ms - last_attempt_ms) < MQTT_RETRY_MS) return;

    attempted = true;
    last_attempt_ms = now_ms;
    err_t err = dns_gethostbyname(BROKER_HOST, &broker_ip, dns_cb, NULL);
    if (err == ERR_OK) {
        printf("[mqtt] DNS cached: %s -> %s\n", BROKER_HOST, ipaddr_ntoa(&broker_ip));
        start_connection();
    } else if (err == ERR_INPROGRESS) {
        dns_pending = true;
        printf("[mqtt] resolving %s\n", BROKER_HOST);
    } else {
        printf("[mqtt] DNS start failed: %d\n", err);
    }
}

bool mqtt_service_ready(void) {
    return client && connected && subscribed &&
           mqtt_client_is_connected(client);
}

bool mqtt_service_take_command(char *dest, size_t cap) {
    if (!dest || !command_pending) return false;
    size_t len = strlen(pending_command);
    if (cap <= len) return false;
    memcpy(dest, pending_command, len + 1);
    command_pending = false;
    return true;
}

static uint32_t last_pub_err_ms;

static bool publish_json(const char *topic, const char *json, bool retained) {
    if (!json || !mqtt_service_ready()) return false;
    size_t len = strlen(json);
    if (len == 0 || len > UINT16_MAX) return false;
    uint32_t now = to_ms_since_boot(get_absolute_time());
    // Fail loudly if a message can never fit lwIP's output ring buffer.
    if (len + strlen(topic) + 8 > MQTT_OUTPUT_RINGBUF_SIZE) {
        if ((uint32_t)(now - last_pub_err_ms) > 5000u) {
            last_pub_err_ms = now;
            printf("[mqtt] %s: %u-byte payload exceeds MQTT_OUTPUT_RINGBUF_SIZE=%u\n",
                   topic, (unsigned)len, (unsigned)MQTT_OUTPUT_RINGBUF_SIZE);
        }
        return false;
    }
    err_t rc = mqtt_publish(client, topic, json, (u16_t)len, 0,
                            retained ? 1 : 0, NULL, NULL);
    if (rc != ERR_OK && (uint32_t)(now - last_pub_err_ms) > 5000u) {
        last_pub_err_ms = now;
        printf("[mqtt] publish to %s failed: err=%d\n", topic, (int)rc);
    }
    return rc == ERR_OK;
}

bool mqtt_service_publish_status(const char *json) {
    return publish_json(MQTT_STATUS_TOPIC, json, false);
}
bool mqtt_service_publish_telemetry(const char *json) {
    return publish_json(MQTT_TELEMETRY_TOPIC, json, false);
}
bool mqtt_service_publish_spec(const char *json) {
    return publish_json(MQTT_SPEC_TOPIC, json, true);
}
bool mqtt_service_publish_headline(const char *json) {
    return publish_json(MQTT_HEADLINE_TOPIC, json, true);
}
bool mqtt_service_publish_plan(const char *json) {
    return publish_json(MQTT_PLAN_TOPIC, json, true);
}

void mqtt_service_shutdown(void) {
    if (!client) return;
    mqtt_disconnect(client);
    mqtt_client_free(client);
    client = NULL;
    connected = subscribed = connecting = dns_pending = false;
    command_pending = false;
}
