"""The optimiser's schedule reaches the firmware unchanged: command format, sign convention, clamp to the
spec, and acceptance by the relay in hardware/firmware_c/receive_data.py with the payload forwarded byte
for byte."""
import re
import sys
from pathlib import Path

import pandas as pd
import pytest

from v2g.control import schedule_sender as ss

REPO = Path(__file__).resolve().parents[1]
SCHEDULE = REPO / "artefacts" / "schedules" / "lear_x.parquet"
FIRMWARE = REPO / "hardware" / "firmware_c"
# What src/converter.c's sscanf accepts: this exact key order, upper-case mode, non-negative magnitude.
FIRMWARE_RE = re.compile(r'^\{"command":"SET_POWER","mode":"(CHARGE|DISCHARGE|STOP)","power_kw":[0-9]+(\.[0-9]+)?\}$')
SPEC = ss.load_spec()
TS = pd.Timestamp("2026-01-15 18:00", tz="UTC")


def test_sign_convention_and_stop():
    c = ss.setpoint(TS, 3.0, SPEC)
    d = ss.setpoint(TS, -2.5, SPEC)
    s = ss.setpoint(TS, 0.0, SPEC)
    assert (c.mode, c.power_kw) == ("CHARGE", 3.0)          # + = grid -> vehicle (§6.3)
    assert (d.mode, d.power_kw) == ("DISCHARGE", 2.5)
    assert (s.mode, s.power_kw) == ("STOP", 0.0)
    assert s.payload == '{"command":"SET_POWER","mode":"STOP","power_kw":0.0}'
    for p in (c, d, s):
        assert FIRMWARE_RE.match(p.payload), p.payload


def test_clamped_to_the_spec_not_to_a_typed_number():
    big, neg = ss.setpoint(TS, 99.0, SPEC), ss.setpoint(TS, -99.0, SPEC)
    assert big.power_kw == SPEC.power.p_charge_max_kw and big.clamped
    assert neg.power_kw == SPEC.power.p_discharge_max_kw and neg.clamped
    assert not ss.setpoint(TS, SPEC.power.p_charge_max_kw, SPEC).clamped


@pytest.mark.skipif(not SCHEDULE.exists(), reason="run `make optimise-backtest` first")
def test_a_whole_lear_x_day_is_firmware_parsable_and_within_spec():
    df = ss.load_schedule("lear_x", SCHEDULE)
    points = ss.setpoints_for_issue(df, ss.pick_issue(df, None), SPEC)
    assert len(points) == 48
    for p in points:
        assert FIRMWARE_RE.match(p.payload), p.payload
        assert 0 <= p.power_kw <= max(SPEC.power.p_charge_max_kw, SPEC.power.p_discharge_max_kw)
        assert not p.clamped                      # the optimiser already respected the spec
    assert {p.mode for p in points} >= {"CHARGE", "STOP"}


@pytest.mark.skipif(not (FIRMWARE / "receive_data.py").exists(), reason="firmware tree not present")
def test_relay_accepts_and_forwards_unchanged():
    sys.path.insert(0, str(FIRMWARE))
    receive_data = pytest.importorskip("receive_data")      # Eesah's relay; needs paho-mqtt
    forwarded = []

    class FakeClient:
        def publish(self, topic, payload, qos=1, retain=False):
            forwarded.append((topic, payload, retain))
            return type("Info", (), {"rc": 0})()

    class Msg:
        def __init__(self, payload: str):
            self.payload = payload.encode()

    for p in (ss.setpoint(TS, SPEC.power.p_charge_max_kw, SPEC), ss.setpoint(TS, -1.25, SPEC), ss.STOP):
        receive_data.on_message(FakeClient(), None, Msg(p.payload))
        assert forwarded[-1] == (receive_data.PICO_TOPIC, p.payload, False)
    assert len(forwarded) == 3
    receive_data.on_message(FakeClient(), None, Msg('{"command":"SET_POWER","mode":"CHARGE","power_kw":9.9}'))
    assert len(forwarded) == 3                    # over the relay's limit: rejected, not forwarded
