"""Replay the optimiser's schedule to the charger over the firmware's MQTT command protocol.

Where this sits (CONTEXT.md §5): price forecast (S3, ``lear_x``) -> single-EV optimiser (S5,
``artefacts/schedules/<model>.parquet``) -> **this module** -> MQTT -> Pico 2 W firmware
(``hardware/firmware_c``, Eesah). It takes over the role of ``send_data.py``, whose MODE and
POWER_KW were typed in by hand: here every setpoint is the optimiser's ``p_signed_kw`` for the
half-hour, so the controller executes the schedule the forecast model produced. Nothing here
decides anything.

Wire format: the compact JSON that ``receive_data.py`` validates and forwards, and that
``src/converter.c`` parses byte for byte::

    {"command":"SET_POWER","mode":"CHARGE","power_kw":2.0}

Sign convention (§6.3): ``p_signed_kw`` > 0 is grid -> vehicle, so CHARGE; < 0 is DISCHARGE; 0 is
STOP with ``power_kw`` 0.0. Magnitudes are clamped to the converter spec's ratings (the contract),
never to a number typed here. The firmware drops its setpoint 5 s after the last command, so the
current slot's command is re-sent every ``--refresh-seconds``.

Usage::

    python -m v2g.control.schedule_sender --dry-run --slots 8
    python -m v2g.control.schedule_sender --date 2026-01-15 --broker 127.0.0.1 --slot-seconds 2
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yaml

from shared.schema import ConverterSpec

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_MODEL = "lear_x"                     # artefacts/forecasts/HANDOFF.md: the model the optimiser uses
DEFAULT_TOPIC = "v2g/oxhack-4000/input"      # receive_data.py listens here and forwards to .../setpoint
DEFAULT_BROKER = os.environ.get("MQTT_BROKER", "broker.emqx.io")   # hardware/firmware_c/MQTT_ONLY_README.md
RELAY_LIMIT_KW = 7.4                         # hard-coded in receive_data.py and converter.c (spec v0.1 value)
LEASE_S = 5.0                                # converter.c COMMAND_LEASE_MS
LOCAL_TZ = ZoneInfo("Europe/London")

log = logging.getLogger("schedule_sender")


@dataclass(frozen=True)
class Setpoint:
    ts_utc: pd.Timestamp | None
    mode: str            # CHARGE | DISCHARGE | STOP
    power_kw: float      # magnitude, >= 0
    clamped: bool = False

    @property
    def payload(self) -> str:
        return json.dumps({"command": "SET_POWER", "mode": self.mode, "power_kw": float(self.power_kw)},
                          separators=(",", ":"))


STOP = Setpoint(None, "STOP", 0.0)


def load_spec(path: Path = ROOT / "config" / "converter_spec.yaml") -> ConverterSpec:
    with open(path, encoding="utf-8") as fh:
        return ConverterSpec.model_validate(yaml.safe_load(fh))


def setpoint(ts_utc, p_signed_kw: float, spec: ConverterSpec, decimals: int = 3) -> Setpoint:
    """One half-hour of the schedule as a firmware command."""
    p = float(p_signed_kw)
    if abs(p) < 10 ** -decimals:
        return Setpoint(ts_utc, "STOP", 0.0)
    if p > 0:
        mode, limit = "CHARGE", spec.power.p_charge_max_kw
    else:
        mode, limit = "DISCHARGE", spec.power.p_discharge_max_kw
    return Setpoint(ts_utc, mode, round(min(abs(p), limit), decimals), abs(p) > limit + 1e-9)


def load_schedule(model: str = DEFAULT_MODEL, path: Path | None = None) -> pd.DataFrame:
    path = path or ROOT / "artefacts" / "schedules" / f"{model}.parquet"
    df = pd.read_parquet(path)
    if "model" in df.columns:
        df = df[df["model"] == model]
    return df.sort_values(["issue_time_utc", "ts_utc"]).reset_index(drop=True)


def pick_issue(df: pd.DataFrame, date: str | None) -> pd.Timestamp:
    """The daily issue whose London date is ``date`` (default: the first issue in the file)."""
    issues = pd.DatetimeIndex(df["issue_time_utc"].unique()).sort_values()
    if date is None:
        return issues[0]
    want = pd.Timestamp(date).date()
    local_dates = issues.tz_convert(LOCAL_TZ).date
    hits = [i for i, d in zip(issues, local_dates) if d == want]
    if not hits:
        raise SystemExit(f"no issue on {want}; the schedule covers {local_dates[0]} to {local_dates[-1]}")
    return hits[0]


def setpoints_for_issue(df: pd.DataFrame, issue, spec: ConverterSpec) -> list[Setpoint]:
    day = df[df["issue_time_utc"] == issue]
    return [setpoint(r.ts_utc, r.p_signed_kw, spec) for r in day.itertuples(index=False)]


def publish_schedule(points: list[Setpoint], *, broker: str, port: int, topic: str, slot_seconds: float,
                     refresh_seconds: float, log_path: Path | None = None) -> pd.DataFrame:
    """Send each slot's command when its slot starts and re-send it every ``refresh_seconds`` until the
    next slot; finish with STOP. Returns one row per slot (what was sent, when)."""
    import paho.mqtt.client as mqtt  # the firmware-side dependency; not needed for --dry-run

    if refresh_seconds >= LEASE_S:
        raise SystemExit(f"--refresh-seconds must be below the firmware's {LEASE_S:g} s command lease")
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="oxhack-schedule-sender")
    client.connect(broker, port, keepalive=60)
    client.loop_start()
    rows = []

    def send(sp: Setpoint) -> bool:
        info = client.publish(topic, sp.payload, qos=1, retain=False)
        info.wait_for_publish(timeout=5)
        return info.is_published()

    try:
        for sp in points:
            t_end = time.monotonic() + slot_seconds
            ok = send(sp)
            rows.append({"ts_utc": sp.ts_utc, "mode": sp.mode, "power_kw": sp.power_kw,
                         "sent_at": pd.Timestamp.now(tz="UTC"), "published": ok, "payload": sp.payload})
            log.info("%s  %-9s %5.2f kW  %s", sp.ts_utc, sp.mode, sp.power_kw, "ok" if ok else "NOT CONFIRMED")
            while (remaining := t_end - time.monotonic()) > 0:
                time.sleep(min(refresh_seconds, remaining))
                if remaining > refresh_seconds:
                    send(sp)                       # keep the firmware's lease alive, same command
    except KeyboardInterrupt:
        log.info("interrupted: sending STOP")
    finally:
        send(STOP)
        client.loop_stop()
        client.disconnect()
    out = pd.DataFrame(rows)
    if log_path is not None:
        out.to_csv(log_path, index=False)
    return out


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default=DEFAULT_MODEL, help="forecast model whose schedule to replay")
    ap.add_argument("--schedule", type=Path, help="parquet (default artefacts/schedules/<model>.parquet)")
    ap.add_argument("--date", help="London date of the daily issue to replay (default: first in file)")
    ap.add_argument("--slots", type=int, help="only the first N half-hours")
    ap.add_argument("--slot-seconds", type=float, default=1800.0,
                    help="wall seconds per half-hour slot; 1800 is real time, 2 is a quick demo")
    ap.add_argument("--refresh-seconds", type=float, default=2.0,
                    help="re-send cadence inside a slot (the firmware lease is 5 s)")
    ap.add_argument("--broker", default=DEFAULT_BROKER)
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--topic", default=DEFAULT_TOPIC)
    ap.add_argument("--log", type=Path, help="CSV of what was sent, one row per slot")
    ap.add_argument("--dry-run", action="store_true", help="print the commands, publish nothing")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    spec = load_spec()
    limit = max(spec.power.p_charge_max_kw, spec.power.p_discharge_max_kw)
    if limit > RELAY_LIMIT_KW:
        log.warning("spec rating %.1f kW exceeds the %.1f kW hard-coded in receive_data.py / converter.c",
                    limit, RELAY_LIMIT_KW)
    df = load_schedule(a.model, a.schedule)
    issue = pick_issue(df, a.date)
    points = setpoints_for_issue(df, issue, spec)[: a.slots]
    log.info("model %s, issue %s (%s London), %d slots, spec v%s ratings %.1f/%.1f kW",
             a.model, issue, issue.tz_convert(LOCAL_TZ), len(points), spec.spec_version,
             spec.power.p_charge_max_kw, spec.power.p_discharge_max_kw)
    if a.dry_run:
        for sp in points:
            print(f"{sp.ts_utc.tz_convert(LOCAL_TZ):%a %H:%M}  {sp.mode:9s} {sp.power_kw:5.2f} kW  {sp.payload}")
        return
    publish_schedule(points, broker=a.broker, port=a.port, topic=a.topic, slot_seconds=a.slot_seconds,
                     refresh_seconds=a.refresh_seconds, log_path=a.log)


if __name__ == "__main__":
    main()
