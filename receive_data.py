"""Run on Mac. Stateless relay: no decisions and no telemetry to ML."""
import json
import math
import paho.mqtt.client as mqtt

BROKER = "broker.emqx.io"
PORT = 1883
INPUT_TOPIC = "v2g/oxhack-4000/input"
PICO_TOPIC = "v2g/oxhack-4000/setpoint"


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        client.subscribe(INPUT_TOPIC, qos=1)
        print("Receiver listening:", INPUT_TOPIC, flush=True)
    else:
        print("Connection failed:", reason_code, flush=True)


def on_message(client, userdata, msg):
    try:
        data = json.loads(msg.payload.decode("utf-8"))
        if not isinstance(data, dict) or set(data) != {"command", "mode", "power_kw"}:
            raise ValueError("Unexpected fields")
        mode = data["mode"]
        power = data["power_kw"]
        if data["command"] != "SET_POWER" or mode not in ("CHARGE", "DISCHARGE", "STOP"):
            raise ValueError("Invalid command or mode")
        if type(power) not in (float, int) or not math.isfinite(power) or not 0 <= power <= 7.4:
            raise ValueError("Invalid power")
        if mode == "STOP" and power != 0:
            raise ValueError("STOP must request zero power")
        # Canonical order for the small Pico JSON parser; no decisions here.
        payload = json.dumps({"command":"SET_POWER", "mode":mode, "power_kw":float(power)}, separators=(",", ":"))
        info = client.publish(PICO_TOPIC, payload, qos=1, retain=False)
        print("Forwarded:", payload, "result:", info.rc, flush=True)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        print("Rejected message:", exc, flush=True)


def main():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="oxhack-data-receiver")
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(BROKER, PORT, 60)
    client.loop_forever()

if __name__ == "__main__":
    main()
