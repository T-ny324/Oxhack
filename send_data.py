import os
import sys
import time
import paho.mqtt.client as mqtt

# Must be the SAME broker the Pico and receive_data.py use (see the Pico's [mqtt] connecting... log).
BROKER = os.environ.get("MQTT_BROKER", "broker.hivemq.com")
PORT = 1883
INPUT_TOPIC = "v2g/oxhack-4000/input"
SEND_EVERY_SECONDS = 2   # keep under 5 s: the Pico drops a SET_POWER after 5 s

# ---- THE COMMAND TO SEND (change this line, or pass it: python3 send_data.py discharge) ----
COMMAND = "charge"

# Valid names (receive_data.py translates these for the Pico):
#   charge          charge at 2 kW
#   charge_fast     charge at 7.4 kW
#   discharge       discharge 2 kW (car -> grid)
#   discharge_fast  discharge 7.4 kW
#   stop            zero power
#   auto            Pico follows its built-in dummy plan
# ---------------------------------------------------------------------------------------------


def main():
    command = (sys.argv[1] if len(sys.argv) > 1 else COMMAND).strip().lower()

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="oxhack-4000-sender"
    )
    client.connect(BROKER, PORT, 60)
    client.loop_start()

    try:
        while True:
            if client.is_connected():
                info = client.publish(INPUT_TOPIC, command, qos=1, retain=False)
                info.wait_for_publish(timeout=5)
                print(f"Sent '{command}'; confirmed={info.is_published()}", flush=True)
            else:
                print("Waiting for MQTT connection...")
            time.sleep(SEND_EVERY_SECONDS)
    except KeyboardInterrupt:
        print("Stopped sending.")
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
