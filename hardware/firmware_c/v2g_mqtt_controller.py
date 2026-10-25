"""Simple laptop-side V2G decision maker for the Wokwi Pico 2 W.

Install: python3 -m pip install paho-mqtt
Run:     python3 v2g_mqtt_controller.py

Edit INPUTS or replace decide_power_kw() with your ML model later.
Positive power = charge; negative power = discharge; zero = stop.
"""
import time
import paho.mqtt.client as mqtt

BROKER = "test.mosquitto.org"
PORT = 1883
TOPIC = "v2g/oxhack-4000/setpoint"
SEND_EVERY_SECONDS = 2

# Change these numbers to test different decisions.
INPUTS = {
    "battery_soc": 0.60,          # 0.0 to 1.0 (60%)
    "electricity_price_gbp_kwh": 0.12,
    "grid_demand": 0.70,          # 0.0 to 1.0
}

MIN_SOC = 0.20
MAX_SOC = 0.90
CHEAP_PRICE = 0.15
EXPENSIVE_PRICE = 0.30
HIGH_GRID_DEMAND = 0.80
POWER_KW = 2.0


def decide_power_kw(inputs):
    """Replace only this function with an ML prediction later."""
    soc = inputs["battery_soc"]
    price = inputs["electricity_price_gbp_kwh"]
    demand = inputs["grid_demand"]

    if not (0.0 <= soc <= 1.0 and 0.0 <= demand <= 1.0 and price >= 0):
        return 0.0  # Invalid data: stop
    if soc > MIN_SOC and (price >= EXPENSIVE_PRICE or demand >= HIGH_GRID_DEMAND):
        return -POWER_KW
    if soc < MAX_SOC and price <= CHEAP_PRICE:
        return POWER_KW
    return 0.0


def main():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="oxhack-laptop-controller")
    client.connect(BROKER, PORT, keepalive=60)
    client.loop_start()
    try:
        while True:
            power = decide_power_kw(INPUTS)
            command = f"SET_POWER {power:+.1f}"
            info = client.publish(TOPIC, command, qos=1, retain=False)
            info.wait_for_publish(timeout=5)
            if not info.is_published():
                print("MQTT publish not confirmed; retrying", flush=True)
            else:
                print(f"Inputs: {INPUTS} | {command} -> {BROKER}/{TOPIC}", flush=True)
            time.sleep(SEND_EVERY_SECONDS)
    except KeyboardInterrupt:
        print("\nStopping converter...")
        client.publish(TOPIC, "SET_POWER +0.0", qos=1).wait_for_publish(timeout=5)
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
