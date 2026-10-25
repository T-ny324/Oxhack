import os
import paho.mqtt.client as mqtt

# Must be the SAME broker the Pico connected to (see the Pico's [mqtt] connecting... log line).
BROKER = os.environ.get("MQTT_BROKER", "broker.emqx.io")
PORT = 1883

INPUT_TOPIC = "v2g/oxhack-4000/input"      # where send_data.py publishes
PICO_TOPIC = "v2g/oxhack-4000/setpoint"    # what the Pico listens to

# ---- LOOKUP TABLE: command name from send_data.py -> exact text the Pico understands ----
# Positive kW = charge (grid -> car), negative = discharge. The Pico rejects |kW| > 7.4.
COMMANDS = {
    "charge":         "SET_POWER +2.00",
    "charge_fast":    "SET_POWER +7.40",
    "discharge":      "SET_POWER -2.00",
    "discharge_fast": "SET_POWER -7.40",
    "stop":           "STOP",
    "auto":           "AUTO",
}
# To add a command, add a line above, e.g.  "charge_slow": "SET_POWER +1.00",
# ----------------------------------------------------------------------------------------


def translate(text):
    """Return the Pico command for a command name, or None if it's not in the table."""
    return COMMANDS.get(text.strip().lower())


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        client.subscribe(INPUT_TOPIC, qos=1)
        print(f"Receiver connected to {BROKER}; listening on {INPUT_TOPIC}")
        print("Known commands:", ", ".join(COMMANDS))
    else:
        print("Connection failed:", reason_code)


def on_message(client, userdata, msg):
    name = msg.payload.decode("utf-8", errors="replace").strip()
    pico_command = translate(name)
    if pico_command is None:
        print(f"Ignored unknown command '{name}'. Known: {', '.join(COMMANDS)}")
        return
    client.publish(PICO_TOPIC, pico_command, qos=1, retain=False)
    print(f"'{name}' -> {pico_command} (forwarded to Pico)")


def main():
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="oxhack-4000-receiver"
    )
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(BROKER, PORT, 60)
    client.loop_forever()


if __name__ == "__main__":
    main()
