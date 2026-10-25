# MQTT-only Wokwi firmware

- Broker: broker.emqx.io:1883
- Command topic: v2g/oxhack-4000/setpoint
- Commands: SET_POWER +2.0 (charge), SET_POWER -2.0 (discharge), SET_POWER +0.0 or STOP (stop).
- On startup, before the first command, the converter stays STOPPED.
- Commands must be refreshed every 2 seconds (the existing converter lease expires after 5 seconds).
- MQTT disconnect forces a stop; a fresh command is needed to restart.
- The dummy day, price optimizer, and autonomous scheduling are removed from the active build.
- Battery SoC (initially 0.60), voltage (350 V), current and power are still simulated by converter.c. No real battery measurements are supplied. The simulated battery SoC changes only in response to external power requests.
- This is Wokwi demonstration firmware, not a safe real-world power-converter controller.

## CLion
Open project, reload CMake, rebuild oxhack, then start Wokwi. Keep your existing send.py / receive.py using broker.emqx.io and the same topics. Stop any other controller publishing to the setpoint topic.
