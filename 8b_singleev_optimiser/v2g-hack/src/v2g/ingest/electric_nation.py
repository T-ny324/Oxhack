"""Electric Nation (GreenFlux 3-minute meter values) -> standard sessions table (S2).

    python -m v2g.ingest.electric_nation      # writes data/processed/sessions.parquet

Source: data/raw/electric_nation/GreenFlux3Minute.csv (UK home-charging trial, 2017-18).
The file is METER READINGS, not sessions: one row every 3 minutes per charger, sent only
while a car is connected (Data Explanation PDF). So a session is a run of readings, and:
    arrival_utc   = first reading
    departure_utc = last reading + 3 min (each reading covers a 3-minute period)
    energy_kwh    = sum(MaxAmpsUsed) x V x 3 min   (amps -> kW needs a voltage; 230 V)

Findings from step 1 that shape this code (see docs/optimisation_problem.md):
  * TimeStamp is true UTC (a continuous charger has no gap/duplicate at the clock change).
    AdjustedTimeStamp (local) switches offset at 00:00 UTC instead of 01:00 UTC on
    clock-change nights, so it is NOT used: local time is derived from UTC when needed.
  * 8.6 % of rows are duplicated (same charger + timestamp, almost always identical):
    dedupe first, or energy is double-counted.
  * 1.45 M rows have no TransactionID but look like real plug-ins (readings only exist
    while connected): rebuilt as sessions by time contiguity, flagged origin="untagged".
  * Some transactions are never closed and run for months with silent gaps of days
    between readings: split where readings go silent (silence = car not connected).
  * One plug-in is sometimes logged as several back-to-back transactions: merge them.
  * Zero-energy sessions are KEPT: a plugged-in car that draws nothing (battery full, or
    charging delayed by the trial's smart charging) is still available for V2G.

Output columns: vehicle_id (= ChargerID: one home charger per trial household; there is no
vehicle ID in the data), arrival_utc, departure_utc, energy_kwh, trip_kwh_next, origin,
n_fragments, source = "electric_nation".
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
RAW = ROOT / "data" / "raw" / "electric_nation" / "GreenFlux3Minute.csv"
OUT = ROOT / "data" / "processed" / "sessions.parquet"
READING_MIN = 3                                  # GreenFlux 3-minute data


@dataclass(frozen=True)
class IngestConfig:
    voltage_v: float = 230.0                     # UK nominal, single phase (16 A / 32 A chargers)
    max_amps: float = 35.0                       # 32 A charger rating + 10 %; above = bad reading
    # Same TransactionID but silent > 6 h -> car left. Shorter silences (mostly 1-3 h, starting
    # 20:00-01:00 UTC when cars are parked at home) look like comms dropouts: splitting there
    # would invent ~480 fake overnight trips. Gaps > 1 day look like a real new plug-in.
    tx_split_gap_min: float = 360.0
    untagged_gap_min: float = 15.0               # no TransactionID: new session after 15 min silence
    merge_gap_min: float = 10.0                  # back-to-back transactions <= 10 min apart = one plug-in
    min_duration_min: float = 5.0                # shorter sessions are broken, dropped
    min_charge_kwh: float = 0.1                  # below this a session "took no energy" (meter noise)


# =============================================================================== load
def load_readings(path: Path = RAW) -> pd.DataFrame:
    """Raw CSV -> readings (charger, ts_utc tz-aware, amps, tx). Only the columns we use."""
    df = pd.read_csv(path, usecols=["ChargerID", "TimeStamp", "MaxAmpsUsed", "TransactionID"],
                     na_values=["NULL"], encoding="utf-8-sig", engine="pyarrow")
    return pd.DataFrame({
        "charger": df["ChargerID"].astype("category"),
        # Source says UTC and step 1 verified it, so localize as UTC: no DST ambiguity exists.
        "ts_utc": pd.to_datetime(df["TimeStamp"]).dt.tz_localize("UTC"),
        "amps": pd.to_numeric(df["MaxAmpsUsed"], errors="coerce"),
        "tx": pd.to_numeric(df["TransactionID"], errors="coerce").astype("Int64"),
    })


# ====================================================================== readings
def clean_readings(r: pd.DataFrame, cfg: IngestConfig, stats: dict) -> pd.DataFrame:
    """Drop duplicate and physically impossible readings."""
    stats["readings_raw"] = len(r)
    # Prefer the copy that carries a TransactionID when duplicates disagree.
    r = r.sort_values(["charger", "ts_utc", "tx"], na_position="last")
    dup = r.duplicated(["charger", "ts_utc"], keep="first")
    stats["readings_dropped_duplicate"] = int(dup.sum())
    r = r[~dup]
    bad = r["amps"].isna() | (r["amps"] < 0) | (r["amps"] > cfg.max_amps)
    stats["readings_dropped_impossible_amps"] = int(bad.sum())
    r = r[~bad]
    stats["readings_kept"] = len(r)
    return r.reset_index(drop=True)


def readings_to_sessions(r: pd.DataFrame, cfg: IngestConfig, stats: dict) -> pd.DataFrame:
    """Group consecutive readings of one charger into sessions.

    A new session starts when the charger changes, the TransactionID changes (tagged <->
    untagged counts as a change), or the readings go silent for longer than the gap allowed
    (6 h within a transaction, 15 min for untagged readings, which have no ID to vouch for
    continuity). Input must be sorted by charger, ts_utc (clean_readings does this).
    """
    gap_min = r["ts_utc"].diff().dt.total_seconds() / 60
    tx = r["tx"].fillna(-1)
    allowed = np.where(r["tx"].isna(), cfg.untagged_gap_min, cfg.tx_split_gap_min)
    same_charger = r["charger"].eq(r["charger"].shift())
    same_tx = tx.eq(tx.shift())
    new = ~same_charger | ~same_tx | (gap_min > allowed)
    # how many sessions exist only because a transaction went silent (reported, not hidden)
    stats["tx_splits_at_silent_gap"] = int((same_charger & same_tx & (gap_min > allowed)
                                            & r["tx"].notna()).sum())
    sid = new.cumsum()
    g = r.groupby(sid, sort=False)
    s = pd.DataFrame({
        "vehicle_id": g["charger"].first().astype(str),
        "arrival_utc": g["ts_utc"].min(),
        "departure_utc": g["ts_utc"].max() + pd.Timedelta(minutes=READING_MIN),
        "energy_kwh": g["amps"].sum() * cfg.voltage_v * (READING_MIN / 60) / 1000,
        "tagged": g["tx"].first().notna(),
    }).reset_index(drop=True)
    stats["sessions_from_readings"] = len(s)
    stats["sessions_untagged"] = int((~s["tagged"]).sum())
    return s


def merge_fragments(s: pd.DataFrame, cfg: IngestConfig, stats: dict) -> pd.DataFrame:
    """One plug-in logged as several back-to-back sessions (<= merge_gap apart) -> one."""
    s = s.sort_values(["vehicle_id", "arrival_utc"]).reset_index(drop=True)
    gap = (s["arrival_utc"] - s.groupby("vehicle_id")["departure_utc"].shift())
    new = gap.isna() | (gap > pd.Timedelta(minutes=cfg.merge_gap_min))
    g = s.groupby(new.cumsum(), sort=False)
    m = pd.DataFrame({
        "vehicle_id": g["vehicle_id"].first(),
        "arrival_utc": g["arrival_utc"].min(),
        "departure_utc": g["departure_utc"].max(),
        "energy_kwh": g["energy_kwh"].sum(),
        "origin": np.where(g["tagged"].any(), "transaction", "untagged"),
        "n_fragments": g.size(),
    }).reset_index(drop=True)
    stats["sessions_merged_away"] = len(s) - len(m)
    return m


def clean_sessions(s: pd.DataFrame, cfg: IngestConfig, stats: dict) -> pd.DataFrame:
    """Drop only genuinely broken sessions; zero-energy sessions stay (V2G availability)."""
    dur = s["departure_utc"] - s["arrival_utc"]
    bad_order = dur <= pd.Timedelta(0)
    stats["dropped_departure_le_arrival"] = int(bad_order.sum())
    s = s[~bad_order]
    short = (s["departure_utc"] - s["arrival_utc"]) < pd.Timedelta(minutes=cfg.min_duration_min)
    stats["dropped_under_min_duration"] = int(short.sum())
    s = s[~short].sort_values(["vehicle_id", "arrival_utc"]).reset_index(drop=True)
    # Overlap: a session starting before the previous one (same vehicle) has ended. Readings
    # are grouped in time order so this should not happen; kept as a safety rule. Repeat
    # until clean, because dropping one session can expose the next.
    n_overlap = 0
    while True:
        prev_dep = s.groupby("vehicle_id")["departure_utc"].shift()
        ov = prev_dep.notna() & (s["arrival_utc"] < prev_dep)
        if not ov.any():
            break
        n_overlap += int(ov.sum())
        s = s[~ov].reset_index(drop=True)
    stats["dropped_overlap"] = n_overlap
    return s


# ============================================================== trip-energy proxy
def add_trip_proxy(s: pd.DataFrame, cfg: IngestConfig) -> pd.DataFrame:
    """trip_kwh_next = energy used between this session's departure and the next arrival.

    We never see the trip, only the charge that replaces it, so we use charge energy as a
    proxy: energy charged in a session ~= energy driven since the previous session. Each
    session's energy is therefore assigned to the gap BEFORE it, i.e. to the previous
    session's trip_kwh_next (the same meaning as in synthetic.py).

    Zero-energy sessions: a plug-in that takes no energy (battery already full, or the
    trial's smart charging delayed it past departure) does NOT mean the trip before it used
    0 kWh: the energy is simply recharged at a later session. Copying 0 would create fake
    0 kWh trips and bias the energy prediction low (the dangerous direction). So we
    accumulate across consecutive sessions up to the next session that takes energy:
    trip_kwh_next = energy of the first LATER session with energy >= min_charge_kwh.
    In a run like [charge, 0, 0, charge 12 kWh] every trip in the run gets 12 kWh. That is
    the energy of all those trips together, so it is an upper bound on each one: deliberately
    on the safe side, and the total of trip_kwh_next can exceed total energy charged.

    The last session(s) of each vehicle have no later charge: trip_kwh_next = NaN (unknown).
    """
    s = s.sort_values(["vehicle_id", "arrival_utc"]).reset_index(drop=True)
    charged = s["energy_kwh"].where(s["energy_kwh"] >= cfg.min_charge_kwh)
    nxt = charged.groupby(s["vehicle_id"]).shift(-1)           # next session's charge
    s["trip_kwh_next"] = nxt.groupby(s["vehicle_id"]).bfill()   # skip zero-energy ones
    return s


# =========================================================================== build
def build_sessions(readings: pd.DataFrame, cfg: IngestConfig = IngestConfig()) -> tuple[pd.DataFrame, dict]:
    stats: dict = {}
    r = clean_readings(readings, cfg, stats)
    s = readings_to_sessions(r, cfg, stats)
    s = merge_fragments(s, cfg, stats)
    s = clean_sessions(s, cfg, stats)
    s = add_trip_proxy(s, cfg)
    s["source"] = "electric_nation"

    zero = s["energy_kwh"] < cfg.min_charge_kwh
    dur_h = (s["departure_utc"] - s["arrival_utc"]).dt.total_seconds() / 3600
    stats.update({
        "sessions_final": len(s), "vehicles": int(s["vehicle_id"].nunique()),
        "first_arrival_utc": str(s["arrival_utc"].min()), "last_departure_utc": str(s["departure_utc"].max()),
        "zero_energy_sessions": int(zero.sum()), "exactly_0_kwh_sessions": int((s["energy_kwh"] == 0).sum()),
        "zero_energy_duration_h_q": dur_h[zero].quantile([.1, .25, .5, .75, .9]).round(2).tolist(),
        "duration_h_q": dur_h.quantile([.1, .25, .5, .75, .9]).round(2).tolist(),
        "energy_kwh_q": s["energy_kwh"].quantile([.1, .25, .5, .75, .9]).round(2).tolist(),
        "sessions_over_72h": int((dur_h > 72).sum()),
        "trip_kwh_next_nan": int(s["trip_kwh_next"].isna().sum()),
        "config": asdict(cfg),
    })
    cols = ["vehicle_id", "arrival_utc", "departure_utc", "energy_kwh", "trip_kwh_next",
            "origin", "n_fragments", "source"]
    return s[cols], stats


def main():
    readings = load_readings()
    sessions, stats = build_sessions(readings)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    sessions.to_parquet(OUT, index=False)
    with open(OUT.with_name("sessions_ingest_report.json"), "w") as f:
        json.dump(stats, f, indent=2)
    print(json.dumps(stats, indent=2))
    print(f"wrote {OUT} ({len(sessions)} sessions)")


if __name__ == "__main__":
    main()
