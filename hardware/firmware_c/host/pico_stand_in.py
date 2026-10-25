"""Stand-in for the Pico 2 W on a laptop.

Subscribes to the setpoint topic exactly as the firmware does and pushes every payload through the
REAL src/converter.c (built as build-host/converter_cli), ticking its clock once a second so the 5 s
command lease and the ramp behave as on the board. Writes one CSV row per event.

    make -C host converter_cli
    python3 host/pico_stand_in.py --broker 127.0.0.1 --duration 120 --log /tmp/pico.csv
"""
import argparse
import csv
import subprocess
import threading
import time
from pathlib import Path

import paho.mqtt.client as mqtt

HERE = Path(__file__).resolve().parent
CLI = HERE.parent / "build-host" / "converter_cli"
DEFAULT_TOPIC = "v2g/oxhack-4000/setpoint"      # src/mqtt_client.c MQTT_COMMAND_TOPIC


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--broker", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--topic", default=DEFAULT_TOPIC)
    ap.add_argument("--duration", type=float, default=120.0, help="seconds to run before exiting")
    ap.add_argument("--tick", type=float, default=1.0, help="clock tick for converter_update()")
    ap.add_argument("--log", type=Path, default=Path("pico_stand_in.csv"))
    a = ap.parse_args()
    if not CLI.exists():
        raise SystemExit(f"{CLI} missing: run `make -C host converter_cli` first")

    proc = subprocess.Popen([str(CLI)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
    lock = threading.Lock()
    t0 = time.monotonic()
    rows = []

    def step(kind: str, payload: str = "") -> None:
        ms = int((time.monotonic() - t0) * 1000)
        with lock:
            proc.stdin.write(f"{kind} {ms} {payload}\n".rstrip() + "\n")
            proc.stdin.flush()
            while True:                                   # skip converter.c's own "[converter] ..." lines
                out = proc.stdout.readline().split()
                if not out or out[0] == "S":
                    break
        _, _, accepted, mode, req, act, soc = out
        rows.append({"ms": ms, "event": kind, "payload": payload, "accepted": accepted, "mode": mode,
                     "requested_kw": req, "actual_kw": act, "soc": soc})
        if kind == "C":
            print(f"[{ms:7d} ms] {'accepted' if accepted == '1' else 'REJECTED'} {payload} -> {mode} "
                  f"req {req} kW", flush=True)

    def on_connect(client, userdata, flags, reason_code, properties):
        client.subscribe(a.topic, qos=1)
        print(f"[stand-in] subscribed to {a.topic} on {a.broker}:{a.port}", flush=True)

    def on_message(client, userdata, msg):
        step("C", msg.payload.decode("utf-8", "replace"))

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="oxhack-pico-stand-in")
    client.on_connect, client.on_message = on_connect, on_message
    client.connect(a.broker, a.port, 60)
    client.loop_start()
    try:
        while time.monotonic() - t0 < a.duration:
            time.sleep(a.tick)
            step("T")
    finally:
        client.loop_stop()
        client.disconnect()
        proc.stdin.close()
        proc.wait(timeout=5)
        with open(a.log, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]) if rows else ["ms"])
            w.writeheader()
            w.writerows(rows)
        n_cmd = sum(r["event"] == "C" for r in rows)
        n_ok = sum(r["event"] == "C" and r["accepted"] == "1" for r in rows)
        print(f"[stand-in] {n_cmd} commands received, {n_ok} accepted by converter.c; log {a.log}", flush=True)


if __name__ == "__main__":
    main()
