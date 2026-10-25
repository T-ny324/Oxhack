# oxhack V2G Pico 2 W: offline-first version (dummy data)

The converter is driven by a built-in scenario, so the LEDs and serial output work
with **no network**. MQTT is optional.

```
dummy day (prices, plug-in window, SoC)  ->  DP optimiser  ->  SET_POWER per slot
        ->  converter state machine  ->  GP2 green / GP4 blue / GP8 yellow LEDs
        ->  serial [plan] / [slot] / [telemetry] / [headline]   (+ MQTT if it connects)
```

## Run it on your Mac without the Pico SDK
```bash
./test_host.sh                 # unit tests, AddressSanitizer + UBSan
make -C host demo              # the REAL main.c on a virtual clock (offline)
make -C host demo-mqtt         # same, with a scripted MQTT link + override commands
make -C host plan DAY=2        # one day's table: plug-in vs smart-charge vs V2G
python3 tools/check_against_lp.py 7   # DP vs exact LP (needs numpy, scipy)
```

## Build for Wokwi
Same as before: CLion -> Reset Cache and Reload -> Rebuild, then Wokwi: Start Simulator.
Expect `[headline]`, `[plan]` and `[slot ..]` lines within ~3 s, before Wi-Fi even joins.
One simulated half-hour = 3 s (`SLOT_MS` in main.c). A plugged-in window is 84 s.

## What is dummy
Everything in `src/dummy_data.c` (prices, plug-in times, arrival SoC), the 60 kWh / GBP 6000
battery, the 2036 fleet numbers in `src/headline.c`. Spec numbers mirror
`config/converter_spec.yaml` v0.1 (provenance: placeholder). Annual figures are
7 dummy days x 365, not a forecast.

## Model limits
- Efficiency is fixed at the 100%-load point of the spec curve (95.5% / 95.0%). Real
  efficiency is lower at light load, so results are a little optimistic.
- Departure SoC is a hard constraint. Between arrival and departure the optimiser may take
  the pack down to 20%. Raise `soc_min` in `sched_default_params()` if drivers need a reserve.
- During an MQTT override the SoC is frozen (the plan owns the SoC model); it jumps back to
  the plan when AUTO resumes.

## MQTT topics (prefix v2g/oxhack-4000)
In: `/setpoint` = `SET_POWER +2.0` | `SET_POWER -2.0` | `STOP` (override for 5 s) | `AUTO`.
Out: `/telemetry`, `/status`, `/spec` (retained), `/headline` (retained), `/plan` (retained), `/online`.

## If MQTT still will not connect: read the serial log
| Log line | Meaning |
|---|---|
| `[mqtt] DNS: host -> a.b.c.d` | DNS worked; note the IP |
| `[mqtt] connecting to host (ip):1883` then no reply for 20 s | TCP SYN is not being answered: gateway or broker path |
| `[mqtt] no CONNACK from X ... trying next broker` | rotates hivemq -> emqx -> mosquitto |
| `[mqtt] TCP+MQTT up in N ms` | success |

For a packet-level answer: click the Wi-Fi icon in the Wokwi simulator, **Download PCAP file**,
open it in Wireshark with filter `tcp.port == 1883`. A SYN with no SYN-ACK confirms the
network path, not the firmware. The Wokwi Private Gateway (paid plan) reaches a broker on
your Mac as `host.wokwi.internal`.
