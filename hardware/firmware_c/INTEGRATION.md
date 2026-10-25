# How the firmware joins the pipeline

Eesah's Pico 2 W firmware (this folder, from the `microcont` branch) is **MQTT-only**: it starts
stopped and executes whatever signed power setpoint arrives on `v2g/oxhack-4000/setpoint`, dropping
it 5 s after the last refresh (`src/converter.c`). It makes no decisions. The decision comes from the
data pipeline:

```
Elexon prices + NESO/Elexon day-ahead forecasts
   -> price forecast, model lear_x            (S3, src/v2g/forecast, artefacts/forecasts/)
   -> single-EV LP optimiser                  (S5, 8b_singleev_optimiser, artefacts/schedules/lear_x.parquet)
   -> src/v2g/control/schedule_sender.py      p_signed_kw per half-hour -> {"command":"SET_POWER","mode":...,"power_kw":...}
   -> MQTT  v2g/oxhack-4000/input
   -> receive_data.py (this folder)           validates, forwards unchanged to v2g/oxhack-4000/setpoint
   -> firmware (Wokwi / Pico)                 src/converter.c ramps to the setpoint; LEDs on GP2 / GP4
```

`schedule_sender.py` replaces the hand-typed `MODE` / `POWER_KW` in `send_data.py`. It reads the
converter ratings from `config/converter_spec.yaml` (the contract) and clamps to them; `receive_data.py`
and `converter.c` still carry the v0.1 placeholder limit of 7.4 kW as a hard-coded guard.

## Run it

```bash
# 1. the pipeline artefacts (already committed): make optimise-backtest
# 2. look at a day's commands without a broker
python -m v2g.control.schedule_sender --dry-run --date 2026-01-15

# 3. live, against Eesah's broker (Wokwi firmware + relay running as in MQTT_COMMAND_PROTOCOL.md)
python receive_data.py                                   # Mac relay, as before
python -m v2g.control.schedule_sender --date 2026-01-15 --slot-seconds 2   # 2 s per half-hour for a demo; 1800 = real time
```

## Check it on a laptop with no Pico

`host/pico_stand_in.py` subscribes like the firmware and feeds every payload through the real
`src/converter.c` (compiled on the host as `build-host/converter_cli`), ticking its clock so the lease
and the ramp behave as on the board. With a local broker (any MQTT 3.1.1 broker; `pip install amqtt`
gives one) the whole chain runs offline:

```bash
make -C host converter_cli
amqtt -c broker.yaml &                                   # listener 127.0.0.1:1883, anonymous
python -c "import receive_data as r; r.BROKER='127.0.0.1'; r.main()" &
python host/pico_stand_in.py --broker 127.0.0.1 --duration 120 --log /tmp/pico.csv &
python -m v2g.control.schedule_sender --broker 127.0.0.1 --date 2026-01-15 --slot-seconds 2 --refresh-seconds 1 --log /tmp/sent.csv
```

`tests/test_control_schedule_sender.py` (repo root) pins the wire format against what `converter.c`
parses and runs `receive_data.on_message` on the sender's payloads.

## Known gaps

- `receive_data.py`, `send_data.py`, `v2g_mqtt.sh` and `converter.c` hard-code 7.4 kW; the spec is now
  6.6 kW. They should read `config/converter_spec.yaml` (or at least be regenerated from it).
- `host/host_stubs.c` and `v2g_mqtt_controller.py` still speak the old text protocol (`SET_POWER +2.0`),
  which the current firmware rejects; `make -C host demo-mqtt` therefore shows rejected commands.
- The Pico build needs `PICO_SDK_PATH` (the `pico/pico-sdk` submodule is not vendored here).
- Telemetry is not published back to MQTT, so the pipeline cannot yet observe real SoC.
