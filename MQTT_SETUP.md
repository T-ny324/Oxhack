# MQTT setup (CLion + Wokwi + Pico 2 W)

This project uses **test.mosquitto.org:1883** for the simulation. Both the Pico firmware and `v2g_mqtt.sh` use the same broker and `v2g/oxhack-4000` topic prefix. This is a public, unencrypted broker: use only dummy data and never send credentials.

1. Open the project in CLion. Configure the Pico SDK by setting `PICO_SDK_PATH` in CMake options if necessary (e.g. `-DPICO_SDK_PATH=/absolute/path/to/pico-sdk`). Ensure the SDK has Pico 2 W support.
2. Reset/reload CMake, then rebuild the `oxhack` target. Confirm `cmake-build-debug/oxhack.uf2` and `.elf` exist. If CLion uses a different build directory, update `wokwi.toml`.
3. Start Wokwi using the `diagram.json` and `wokwi.toml` files, and wait for `[wifi] connected`, `[mqtt] TCP+MQTT up`, and `[mqtt] subscribe result: 0` in the serial console.
4. On macOS, install the CLI tools: `brew install mosquitto`. In one terminal run `bash v2g_mqtt.sh monitor`; in another run `bash v2g_mqtt.sh hold charge 2.0` or `bash v2g_mqtt.sh hold discharge 2.0`. Ctrl+C sends STOP. Use `bash v2g_mqtt.sh auto` to resume the automatic plan.
5. Verify the Wokwi serial console shows `[mqtt] command received: SET_POWER +2.0` and the monitor displays telemetry. Commands must not be retained.

**Safety behaviour:** once any remote command is accepted, the converter stays in remote mode. If command refreshes stop, it receives STOP and does **not** silently resume the automatic plan. Send `auto` explicitly to return to the dummy plan. The converter also requires an active MQTT session in remote mode.

**Troubleshooting:** If `[wifi] connected` never appears, investigate Wokwi networking first. If DNS fails, check Wokwi internet access. If MQTT does not connect, verify the broker is reachable from the simulator and the broker is available. If the subscriber sees commands but Wokwi does not, check the Pico subscribe result and exact topic prefix. Public brokers are not guaranteed to be available; a local Mac broker requires separate network routing (Wokwi cannot generally use `localhost` to reach your Mac directly).

**Verification limitation:** host unit tests check converter and scheduler logic only. They cannot establish live Wokwi network access or broker availability.
