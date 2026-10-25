**Updated MQTT instructions:** See [MQTT_SETUP.md](MQTT_SETUP.md).

# oxhack V2G Pico 2 W — Wokwi networking repair (team 4000)

This is a **simulation-only** copy of the previous oxhack demo, updated to
address `sys_timeout: timeout != NULL, pool MEMP_SYS_TIMEOUT is empty`.

## What changed

- `lwipopts.h` adds `MEMP_NUM_SYS_TIMEOUT (LWIP_NUM_SYS_TIMEOUT_INTERNAL + 2)`.
  lwIP's MQTT client requires at least one additional timeout beyond those
  consumed by the core stack. This missing setting is the **likely root cause**
  of the previously reported crash when MQTT connected.
- `wifi.c` checks `cyw43_tcpip_link_status()` (not merely Wi-Fi association).
  It waits for `CYW43_LINK_UP` (3, with an IP) and only logs link transitions.
  It does not repeatedly restart Wi-Fi association while DHCP is in progress.
- `mqtt_client.c` creates exactly one MQTT client, guards pending connection
  attempts, handles fragmented messages, and spaces reconnection attempts.
- CMake and `v2g_mqtt.sh` agree on the default topic prefix
  `v2g/oxhack-4000` and unique Pico client ID `oxhack-pico2w-4000`.
- `main.c`, `converter.c`, `power_stage.c`, `diagram.json` remain compatible
  with the previous low-voltage GPIO/LED demonstration.

## Install over your existing CLion project

1. Back up `/Users/33s/C-proj/oxhack` (especially custom changes).
2. Extract the ZIP and copy **only** its `main.c`, `src/`, `include/`,
   `lwipopts.h`, `CMakeLists.txt`, `diagram.json`, `wokwi.toml`, and
   `v2g_mqtt.sh` into the `oxhack` project. Do **not** replace your pico-sdk.
3. Verify the SDK import path in the included `CMakeLists.txt` matches your
   machine. It currently uses your previously shared path.
4. In CLion select **Tools > CMake > Reset Cache and Reload Project**, then
   **Build > Rebuild Project**. Do not merely rerun an old UF2 in Wokwi.
5. Start the Wokwi simulator from your configured CLion Wokwi integration, using `wokwi.toml`.
6. Expected logs (possibly with a delay during DHCP):

   ```text
   [wifi] TCP/IP link status = 2
   [wifi] TCP/IP link status = 3
   [wifi] connected
   [mqtt] connecting to test.mosquitto.org:1883
   [mqtt] connected; subscribing to v2g/oxhack-4000/setpoint
   [mqtt] subscribe result: 0
   ```

7. In macOS Terminal (with `brew install mosquitto` first):

   ```bash
   cd /Users/33s/C-proj/oxhack
   ./v2g_mqtt.sh monitor
   ```

   In a second terminal:

   ```bash
   ./v2g_mqtt.sh hold charge 2.0
   # Ctrl+C then:
   ./v2g_mqtt.sh hold discharge 2.0
   # or:
   ./v2g_mqtt.sh stop
   ```

## MQTT interface

Broker: `test.mosquitto.org:1883` (public, unauthenticated, **simulation only**).

| Topic | Direction | Format |
|---|---|---|
| `v2g/oxhack-4000/setpoint` | External server -> Pico | `SET_POWER +2.0`, `SET_POWER -2.0`, `STOP` |
| `v2g/oxhack-4000/telemetry` | Pico -> server | JSON, **simulated** sensor values |
| `v2g/oxhack-4000/status` | Pico -> server | JSON converter state |
| `v2g/oxhack-4000/spec` | Pico -> server | JSON subset of unverified spec, retained |
| `v2g/oxhack-4000/online` | Pico/broker -> server | 1 or 0 (retained last will) |

**Do not retain power setpoint commands.** Nonzero requests expire after 5
seconds unless refreshed. All estimated power values are model outputs, not
real delivered power. The gateway and broker used here are not secured.

## Physical GPIO pinout (Wokwi logic indicators only)

| GPIO | Physical header pin | Direction | Role |
|---|---:|---|---|
| GP2 | 4 | Output | PWM brightness for CHARGING (green LED + 330 ohms) |
| GP4 | 6 | Output | PWM brightness for DISCHARGING (blue LED + 330 ohms) |
| GP6 | 9 | Output | Gate-driver inhibit **permanently LOW**, not connected |
| GP7 | 10 | Input | Active-low simulated fault pushbutton to GND |
| GP8 | 11 | Output | RUN indicator (yellow LED + 330 ohms) |

The diagram also uses GND connections. Its LEDs display logic/PWM, not actual
power transfer. The `PWM` duty here is a brightness indicator, **not** a real
converter switching waveform or closed-loop controller.

### Do I need only three outputs to control the converter?

**No** for your documented DAB: the proposed isolated DAB uses *two full
bridges* with four MOSFETs each (eight gate-drive signals; actual MCU output
count depends on the driver/interlock topology). Its separate bidirectional
AC/DC stage requires additional switching and protection. A simple two-switch
bidirectional buck/boost stage might use two coordinated gate-drive signals
plus an enable output, but this is a *different converter topology*. Never
connect MCU GPIO directly to MOSFET gates, high-voltage electronics, or mains.

## If MQTT still fails

1. Confirm `[wifi] connected` prints before any MQTT attempt.
2. Confirm the firmware logs `[mqtt] connecting...`, then `[mqtt] connected`.
3. Check the broker separately from the Mac:
   `nc -vz test.mosquitto.org 1883`.
4. Confirm you rebuilt **lwIP** with the new `lwipopts.h` rather than using
   a stale `oxhack.uf2`.
5. Ensure `pico_cyw43_arch_lwip_poll` is the **only** CYW43 architecture
   target you link, and do not call lwIP raw APIs from another core/thread.
6. If the broker is inaccessible from Wokwi despite Wi-Fi IP connectivity,
   this is a networking/gateway issue, not a converter-control or PWM issue.

No live Wokwi/SDK build was executed in this container; verify on your Mac.

## References

- lwIP MQTT timer note: https://github.com/raspberrypi/pico-sdk/issues/1106
- lwIP `MEMP_NUM_SYS_TIMEOUT`: https://www.nongnu.org/lwip/2_1_x/group__lwip__opts__memp.html
- Pico SDK networking: https://www.raspberrypi.com/documentation/pico-sdk/networking.html
- Pico MQTT sample: https://github.com/raspberrypi/pico-examples
- Wokwi Pico 2 W: https://docs.wokwi.com/parts/board-pi-pico-2w

## Fast offline sanity check

From the extracted project on macOS/Linux:

```bash
./test_host.sh
```

This checks the simulated converter's C logic, but it **does not** verify
Wi-Fi, MQTT, PWM timing, Pico SDK linking, or Wokwi integration.
