#ifndef V2G_LWIPOPTS_H
#define V2G_LWIPOPTS_H

// One-core, polled lwIP RAW API as used by pico_cyw43_arch_lwip_poll.
// Must be on the target include path for ALL lwIP compilation units.
#define NO_SYS                         1
#define LWIP_SOCKET                    0
#define LWIP_NETCONN                   0
// Use lwIP's own heap (as the Pico SDK examples do) rather than libc malloc.
#define MEM_LIBC_MALLOC                0
#define MEM_ALIGNMENT                  4
#define MEM_SIZE                       12000
#define MEMP_NUM_TCP_SEG               32
#define MEMP_NUM_ARP_QUEUE             10
#define PBUF_POOL_SIZE                 24
#define LWIP_ARP                       1
#define LWIP_ETHERNET                  1
#define LWIP_ICMP                      1
#define LWIP_RAW                       1
#define LWIP_IPV4                      1
#define LWIP_TCP                       1
#define LWIP_UDP                       1
#define LWIP_DNS                       1
#define LWIP_DHCP                      1
#define LWIP_TCP_KEEPALIVE             1
#define LWIP_NETIF_TX_SINGLE_PBUF      1
#define LWIP_NETIF_STATUS_CALLBACK     1
#define LWIP_NETIF_LINK_CALLBACK       1
#define LWIP_NETIF_HOSTNAME            1
#define DHCP_DOES_ARP_CHECK            0
#define LWIP_DHCP_DOES_ACD_CHECK       0
#define TCP_MSS                        1460
#define TCP_WND                        (8 * TCP_MSS)
#define TCP_SND_BUF                    (8 * TCP_MSS)
#define TCP_SND_QUEUELEN               ((4 * TCP_SND_BUF + TCP_MSS - 1) / TCP_MSS)
#define MQTT_REQ_MAX_IN_FLIGHT         5

// CRITICAL: lwIP normally allocates exactly enough timers for core services.
// Its MQTT client needs an additional sys_timeout for its cyclic keepalive.
// The extra slot prevents:
//   sys_timeout: timeout != NULL, pool MEMP_SYS_TIMEOUT is empty
// Two extra slots allow a little headroom during connect/reconnect.
// Ref: pico-sdk/lib/lwip/doc/mqtt_client.txt
#define MEMP_NUM_SYS_TIMEOUT           (LWIP_NUM_SYS_TIMEOUT_INTERNAL + 4)

// lwIP's MQTT output ring buffer defaults to 256 bytes. mqtt_publish() returns
// ERR_MEM for any message whose topic+payload does not fit, and your 388-byte
// spec JSON (plus topic) never fits, so the old main loop retried it every 10 ms forever.
#define MQTT_OUTPUT_RINGBUF_SIZE       1024
#define LWIP_CHKSUM_ALGORITHM          3

#endif
