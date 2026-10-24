from __future__ import annotations

from pathlib import Path

import streamlit as st

from shared.schema import ConverterSpec
from src.v2g.cli import load_converter_spec

st.set_page_config(page_title="V2G Value Dashboard", layout="wide")

st.title("V2G value dashboard")
st.caption("Placeholder dashboard scaffold for Oxhack")

spec: ConverterSpec = load_converter_spec()

st.metric("Max charge power", f"{spec.power.p_charge_max_kw} kW")
st.metric("Max discharge power", f"{spec.power.p_discharge_max_kw} kW")
st.metric("BOM cost", f"£{spec.cost.bom_cost}")

st.write("This dashboard is intentionally minimal and should be expanded with forecasts, fleet outputs and grid metrics.")
