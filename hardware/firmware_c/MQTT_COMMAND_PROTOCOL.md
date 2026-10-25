# MQTT command-only protocol

- `send_data.py` on second device publishes compact JSON to `v2g/oxhack-4000/input`.
- `receive_data.py` on Mac validates and forwards the command to `v2g/oxhack-4000/setpoint`.
- Pico subscribes to `/setpoint` and accepts `command`, `mode`, and `power_kw` only in the canonical compact order emitted by the receiver.
- Example: `{"command":"SET_POWER","mode":"CHARGE","power_kw":2.0}`
- Modes: `CHARGE`, `DISCHARGE`, `STOP`. Power is a nonnegative magnitude (kW); STOP requires zero.
- **No telemetry is sent back to the ML model by either Python program.** Pico does not publish telemetry to MQTT. Local serial telemetry remains for Wokwi debugging.
- Pico starts stopped and times out if fresh commands stop arriving for 5 seconds. The sender refreshes every 2 seconds.
- Run `python3 -m pip install paho-mqtt` on both devices, then run `python3 receive_data.py` on Mac, `python3 send_data.py` on second device, and Wokwi firmware.
- Public broker is insecure; simulation only. Battery measurements in Pico remain simulated. This is not a physical safety controller.
