# Wokwi connection and pin test — oxhack 4000

This file covers **virtual LEDs, not an energised DAB power stage**.

| Pin | Connect to | Expected visual effect |
|---|---|---|
| GP2 / physical 4 | resistor 330 ohms, green LED anode; cathode to GND | Pulses for +ve kW charge |
| GP4 / physical 6 | resistor 330 ohms, blue LED anode; cathode to GND | Pulses for -ve kW discharge |
| GP6 / physical 9 | **no electrical connection** | Held LOW as a demonstrator inhibit |
| GP7 / physical 10 | normally-open button to GND, internal pull-up | Press to latch FAULT |
| GP8 / physical 11 | resistor 330 ohms, yellow LED anode; cathode to GND | High when indicator active |

Use `diagram.json` for the rendered circuit. Pins are BCM-style GP identifiers,
not physical header numbers. The simulated PWM is about 1 kHz and is not a
MOSFET-driving waveform.

On your Mac:

```bash
brew install mosquitto
cd /Users/33s/C-proj/oxhack
./v2g_mqtt.sh monitor
# in a separate terminal
./v2g_mqtt.sh hold charge 2.0
./v2g_mqtt.sh hold discharge 2.0
./v2g_mqtt.sh stop
```

Expected after MQTT subscription: charge -> green, discharge -> blue, stop ->
LEDs off once ramped down, fault -> outputs disabled and state latched until
simulator reset.

> **Real bidirectional converter design:** Your YAML calls for an isolated DAB
> plus a bidirectional grid-connected AC/DC converter. You cannot infer a safe
> pinout from the design specification alone. The actual schematic, gate-driver
> interfaces, bridge interlock scheme, isolated supplies, current/voltage
> sensors, and independent hardware shutdown are required first.
