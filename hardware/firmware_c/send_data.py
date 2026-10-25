"""Run on second device. Only this device chooses mode and power."""
import json
import time
import paho.mqtt.client as mqtt

BROKER = "broker.emqx.io"
PORT = 1883
TOPIC = "v2g/oxhack-4000/input"
MODE = "CHARGE"  # CHARGE, DISCHARGE, STOP
POWER_KW = 2.0
INTERVAL_SECONDS = 2


def make_command():
    mode = MODE.upper()
    if mode not in ("CHARGE", "DISCHARGE", "STOP"):
        raise ValueError("MODE must be CHARGE, DISCHARGE or STOP")
    if not isinstance(POWER_KW, (int, float)) or not 0 <= POWER_KW <= 7.4:
        raise ValueError("POWER_KW must be between 0 and 7.4")
    return {"command": "SET_POWER", "mode": mode,
            "power_kw": 0.0 if mode == "STOP" else float(POWER_KW)}


def main():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="oxhack-data-sender")
    client.connect(BROKER, PORT, 60)
    client.loop_start()
    try:
        while True:
            if client.is_connected():
                payload = json.dumps(make_command(), separators=(",", ":"))
                info = client.publish(TOPIC, payload, qos=1, retain=False)
                info.wait_for_publish(timeout=5)
                print("Sent:", payload, "confirmed:", info.is_published(), flush=True)
            else:
                print("Waiting for broker...", flush=True)
            time.sleep(INTERVAL_SECONDS)
    except KeyboardInterrupt:
        if client.is_connected():
            payload = json.dumps({"command":"SET_POWER","mode":"STOP","power_kw":0.0}, separators=(",", ":"))
            client.publish(TOPIC, payload, qos=1).wait_for_publish(timeout=5)
    finally:
        client.loop_stop()
        client.disconnect()

if __name__ == "__main__":
    main()
