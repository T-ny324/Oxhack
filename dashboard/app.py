"""ExtraEnergy — starter app (owner view + analysis view + hardware view).

Run from the repo root:
    pip install -r requirements.txt
    streamlit run dashboard/app.py

How Streamlit works: this whole script reruns top to bottom every time a widget
changes. Widgets return values; charts are Plotly figures. Slow functions are
wrapped in @st.cache_data so they only recompute when their inputs change.

Everything marked PLACEHOLDER is synthetic and should be replaced by a reader for
the matching pipeline artefact (see CONTEXT.md §9). Each reader must fall back to
the placeholder if its file is missing, so the app never crashes on stage.

Sign convention (CONTEXT.md §6.3): + = grid → vehicle (charging), − = vehicle → grid.
"""

from __future__ import annotations

import math
import time
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yaml
from plotly.subplots import make_subplots

ROOT = Path(__file__).resolve().parents[1]
# Streamlit puts dashboard/ on the import path, not the repo root: add the root (for `shared`)
# and src/ (for `v2g`) so the same imports work locally and on Streamlit Community Cloud.
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from shared.schema import Assumptions  # noqa: E402
from shared.units import p_per_kwh_to_per_mwh, per_mwh_to_per_kwh  # noqa: E402
from v2g.control import schedule_sender as ss  # noqa: E402
from v2g.optimise import lp  # noqa: E402

DT_H = 0.5                      # the app's half-hour grid (must match assumptions.yaml dt_h)
START_HOUR = 16                 # slot 0 = 16:00 today; 48 slots cover to 16:00 tomorrow
N_SLOTS = 48

GREEN = "#16A34A"
GREEN_DARK = "#0F5132"
AMBER = "#F59E0B"
INK = "#0F2E1C"
MUTED = "#6B7F72"

st.set_page_config(page_title="ExtraEnergy", page_icon="⚡", layout="wide")

# ---------------------------------------------------------------------------
# Styling: a phone-shaped container for the owner view, plus small badges.
# `st.container(key="phone")` gets the CSS class `st-key-phone`.
# ---------------------------------------------------------------------------
st.markdown(
    f"""
<style>
.block-container {{ padding-top: 2rem; }}
.st-key-phone {{
  max-width: 400px; margin: 0 auto; background: #FFFFFF;
  border: 10px solid #10251A; border-radius: 40px;
  padding: 22px 20px 26px 20px; box-shadow: 0 18px 50px rgba(15,46,28,.18);
}}
.ee-brand {{ font-weight: 800; font-size: 1.45rem; color: {GREEN_DARK}; letter-spacing: -.02em; }}
.ee-brand span {{ color: {GREEN}; }}
.ee-tag {{ color: {MUTED}; font-size: .85rem; margin-top: -4px; }}
.ee-hero {{ background: linear-gradient(135deg, {GREEN} 0%, #0E7A3A 100%); color: white;
  border-radius: 18px; padding: 16px 18px; margin: 10px 0 6px 0; }}
.ee-hero .big {{ font-size: 2.3rem; font-weight: 800; line-height: 1.05; }}
.ee-hero .small {{ font-size: .85rem; opacity: .9; }}
.ee-status {{ background: #E6F4EA; border-radius: 12px; padding: 10px 12px; font-size: .9rem; color: {INK}; }}
.ee-badge {{ display:inline-block; padding: 2px 8px; border-radius: 999px; font-size: .72rem;
  font-weight: 700; letter-spacing: .03em; }}
.ee-ok {{ background:#DCFCE7; color:#166534; }}
.ee-headline {{ background:#FFFFFF; border-left: 6px solid {GREEN}; border-radius: 10px;
  padding: 16px 20px; font-size: 1.12rem; line-height: 1.55; color:{INK};
  box-shadow: 0 4px 18px rgba(15,46,28,.07); }}
.ee-headline b {{ color: {GREEN_DARK}; }}
.ee-soon {{ border: 1px dashed {GREEN}; border-radius: 12px; padding: 8px 12px; margin-top: 12px;
  font-size: .82rem; color: {GREEN_DARK}; background: #F6FBF7; }}
</style>
""",
    unsafe_allow_html=True,
)


def badge(provenance: str) -> str:
    # Values not yet backed by evidence (placeholder / assumption) get no badge in the presentation build.
    if provenance in ("placeholder", "assumption"):
        return ""
    return f'<span class="ee-badge ee-ok">{provenance.upper()}</span>'


# ---------------------------------------------------------------------------
# Inputs. Replace each placeholder with an artefact reader (keep the fallback).
# ---------------------------------------------------------------------------
@st.cache_data
def load_spec() -> tuple[dict, str]:
    """Hardware contract (CONTEXT.md §6.1). Falls back to built-in defaults."""
    path = ROOT / "config" / "converter_spec.yaml"
    if path.exists():
        return yaml.safe_load(path.read_text()), "config/converter_spec.yaml"
    return {
        "provenance": "placeholder", "topology": "DAB + totem-pole (built-in default)",
        "power": {"p_charge_max_kw": 7.4, "p_discharge_max_kw": 7.4},
        "efficiency_curve": {"load_fraction": [1.0], "eta_charge": [0.955], "eta_discharge": [0.95]},
        "standby_w": 10, "dynamics": {"response_time_s": 1.0},
        "cost": {"bom_cost": 900, "install_cost": 500, "unidirectional_baseline_cost": 800},
        "lifetime_years": 10,
    }, "built-in defaults"


DEFAULT_ASSUMPTIONS = {          # fallback only; the real values live in config/assumptions.yaml
    "region": "GB", "currency": "GBP", "dt_h": 0.5,
    "battery": {"e_cap_kwh": 60.0, "reserve_kwh": 6.0, "soc_min": 0.20, "soc_max": 0.90,
                "c_deg_per_mwh": 20.0},
    "owner": {"plug_days_per_year": 300},
    "tariff": {"export_price_fraction": 0.85},
    "optimiser": {"mode": "guaranteed_charge", "target_soc": 0.60},
}


@st.cache_data
def load_assumptions() -> tuple[dict, str, str | None]:
    """Global assumptions (config/assumptions.yaml), validated by shared/schema.py.
    Returns (assumptions, source, problem). Falls back to built-in defaults if the file is
    missing or invalid, and says why, so the app never crashes on stage."""
    path = ROOT / "config" / "assumptions.yaml"
    if not path.exists():
        return DEFAULT_ASSUMPTIONS, "built-in defaults", None
    try:
        return Assumptions.model_validate(yaml.safe_load(path.read_text())).model_dump(), \
            "project config file", None
    except Exception as e:                      # invalid file: show it, keep running
        return DEFAULT_ASSUMPTIONS, "built-in defaults", f"config/assumptions.yaml is invalid: {e}"


@st.cache_data
def load_prices(export_fraction: float) -> tuple[np.ndarray, np.ndarray, str]:
    """PLACEHOLDER agile-style tariff, 48 half-hours from 16:00. Returns £/MWh.

    TODO(S3): read artefacts/forecasts/price_quantiles.parquet (P50) instead.
    """
    hours = (START_HOUR + DT_H * np.arange(N_SLOTS)) % 24
    p = np.empty(N_SLOTS)
    for i, h in enumerate(hours):
        if 16 <= h < 19:
            p[i] = 33 + 6 * math.sin(math.pi * (h - 16) / 3)      # evening peak
        elif 19 <= h < 23:
            p[i] = 25 - 2.2 * (h - 19)
        elif h >= 23 or h < 1:
            p[i] = 15
        elif 1 <= h < 5:
            p[i] = 9.5 + 1.0 * math.cos(math.pi * (h - 1) / 4)   # overnight trough
        elif 5 <= h < 7:
            p[i] = 13 + 3 * (h - 5)
        else:
            p[i] = 21 + 2 * math.sin(math.pi * (h - 7) / 9)
    p_kwh_buy = p                                  # p/kWh
    p_kwh_sell = export_fraction * p_kwh_buy       # ASSUMPTION export = a fraction of import
    return p_per_kwh_to_per_mwh(p_kwh_buy), p_per_kwh_to_per_mwh(p_kwh_sell), "placeholder"


def slot_label(i: int) -> str:
    minutes = int((START_HOUR * 60 + 30 * i) % (24 * 60))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def time_to_slot(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    minutes = (h * 60 + m - START_HOUR * 60) % (24 * 60)
    return minutes // 30


# ---------------------------------------------------------------------------
# Optimiser (CONTEXT.md §8.B + §8.B.1). The maths lives in src/v2g/optimise/lp.py so it can be
# tested without Streamlit; these thin wrappers only add Streamlit's cache (same inputs →
# no re-solve), which keeps the sliders, the tornado and the target curve fast.
# ---------------------------------------------------------------------------
@st.cache_data
def solve_schedule(**kwargs) -> dict:
    return lp.solve_schedule(**kwargs)


def evaluate(params: dict) -> dict:
    """All three strategies for one set of inputs → £/day, £/yr and payback."""
    buy_all, sell_all, _ = load_prices(EXPORT_FRACTION)
    return lp.evaluate(params, buy_all, sell_all, solve=solve_schedule)


HABITS_REPORT = ROOT / "8b_singleev_optimiser" / "v2g-hack" / "artefacts" / "habits" / "test_report.json"


@st.cache_data
def load_habits_report() -> tuple[dict | None, str]:
    """Habits model results (Option B: learns when the car is plugged in and how far it
    drives, trained on Electric Nation). Returns (report, source); report is None if the
    artefact is missing, and the app shows a note instead of crashing."""
    if HABITS_REPORT.exists():
        import json
        return json.loads(HABITS_REPORT.read_text()), "habits model artefact"
    return None, "placeholder"


SESSIONS_REPORT = ROOT / "8b_singleev_optimiser" / "v2g-hack" / "data" / "processed" / "sessions_ingest_report.json"
FORECAST_DIR = ROOT / "artefacts" / "forecasts"            # Tony's S3 outputs (summary: FORECAST_MODELS.md)
REVENUE_JSON = ROOT / "artefacts" / "schedules" / "revenue_per_vehicle.json"

# The price-forecast ladder in plain words (from artefacts/forecasts/FORECAST_MODELS.md).
FORECAST_MODELS = [
    ("naive_weekly", "Copy last week", "the price at the same time 7 days ago"),
    ("naive_daily", "Copy yesterday", "yesterday's price (last week's at weekends)"),
    ("lear", "LEAR", "a regression on the last few days' prices"),
    ("lear_x", "LEAR + forecasts", "LEAR plus tomorrow's demand, wind and solar forecasts"),
]


@st.cache_data
def load_sessions_report() -> dict | None:
    """How much real charging data the habits model learned from (sessions, households)."""
    import json
    return json.loads(SESSIONS_REPORT.read_text()) if SESSIONS_REPORT.exists() else None


@st.cache_data
def load_forecast_results() -> dict | None:
    """Tony's one-year price-forecast backtest: error per model (eval_*.csv) and the £/yr each
    model earns once the optimiser uses it (revenue_per_vehicle.json). None if missing."""
    import json
    try:
        ev = pd.concat([pd.read_csv(FORECAST_DIR / "eval_naive.csv"), pd.read_csv(FORECAST_DIR / "eval_lear.csv")])
        overall = ev[ev["slice_type"] == "overall"].set_index("model")
        rev = json.loads(REVENUE_JSON.read_text())["per_model"]
        return {"mae": overall["mae"].to_dict(),
                "saving": {k: v["saving_vs_unmanaged_gbp"] for k, v in rev.items()},
                "pct_perfect": {k: v["pct_of_perfect_saving"] for k, v in rev.items()}}
    except (FileNotFoundError, KeyError, ValueError):
        return None


SIM_DIR = ROOT / "hardware" / "sim"                        # S1: Abdullah's LTspice work (hardware/sim/README.md)


@st.cache_data
def load_sim_results() -> dict | None:
    """Hardware track's LTspice evidence: one steady-state operating point per direction
    (simulation_summary.csv), the per-switching-cycle waveforms (pwm_cycle_metrics.csv) and the circuit
    values (hardware_config.json). None if any file is missing; the Hardware tab then says so."""
    import json
    try:
        return {"summary": pd.read_csv(SIM_DIR / "data" / "simulation_summary.csv"),
                "cycles": pd.read_csv(SIM_DIR / "data" / "pwm_cycle_metrics.csv"),
                "hw": json.loads((SIM_DIR / "hardware_config.json").read_text())}
    except (FileNotFoundError, ValueError, KeyError):
        return None


SCHEDULE_PARQUET = ROOT / "artefacts" / "schedules" / "lear_x.parquet"   # S5 output the firmware executes
LED_BASE_KW, LED_DUTY_MAX = 7.4, 80.0        # power_stage.c: DEMO_POWER_BASE_KW, DUTY_MAX_PERCENT


@st.cache_data
def schedule_date_range() -> tuple | None:
    """London dates covered by the lear_x schedule, for the day picker. None if the artefact is missing."""
    try:
        issues = pd.DatetimeIndex(pd.read_parquet(SCHEDULE_PARQUET, columns=["issue_time_utc"])["issue_time_utc"].unique())
    except (FileNotFoundError, ValueError, KeyError):
        return None
    local = issues.tz_convert("Europe/London").date
    return min(local), max(local)


@st.cache_data
def load_led_day(date_iso: str) -> list[dict] | None:
    """One London day of the lear_x optimiser's schedule as the exact firmware commands that
    src/v2g/control/schedule_sender.py publishes, plus the LED duty power_stage.c derives from each.
    None if the artefact is missing or the day is outside it."""
    try:
        df = ss.load_schedule("lear_x", SCHEDULE_PARQUET)
        issue = ss.pick_issue(df, date_iso)
        points = ss.setpoints_for_issue(df, issue, ss.load_spec())
    except (FileNotFoundError, ValueError, KeyError, SystemExit):
        return None
    day = df[df["issue_time_utc"] == issue]
    return [{"t": p.ts_utc.tz_convert("Europe/London").strftime("%a %H:%M"), "mode": p.mode, "kw": p.power_kw,
             "duty": round(min(p.power_kw / LED_BASE_KW, 1.0) * LED_DUTY_MAX), "payload": p.payload,
             "soc": round(float(soc), 3), "plugged": bool(plugged)}
            for p, soc, plugged in zip(points, day["soc"], day["plugged"])]


def led_animation_html(slots: list[dict]) -> str:
    """Three LEDs as the Pico drives them (power_stage.c): green GP2 while charging, blue GP4 while
    discharging, brightness = PWM duty; yellow GP8 RUN whenever either is lit; a short neutral gap on
    reversal. Pure HTML/JS so it animates without rerunning the app."""
    import json
    data = json.dumps(slots)
    return f"""
<div id="v2g" style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:{INK};padding:4px 2px">
 <div style="display:flex;gap:28px;align-items:center;flex-wrap:wrap">
  <svg width="330" height="150" viewBox="0 0 330 150" aria-label="Pico LEDs">
   <defs><filter id="glow" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="6"/></filter></defs>
   <rect x="6" y="6" width="318" height="138" rx="14" fill="#10251A"/>
   <text x="165" y="28" fill="#9FB8A8" font-size="11" text-anchor="middle">RASPBERRY PI PICO 2 W · logic-level indicator LEDs</text>
   <g id="led-ch"><circle cx="70" cy="78" r="26" fill="{GREEN}" filter="url(#glow)" opacity=".08"/><circle cx="70" cy="78" r="18" fill="{GREEN}" opacity=".15"/></g>
   <g id="led-dis"><circle cx="165" cy="78" r="26" fill="#3B82F6" filter="url(#glow)" opacity=".08"/><circle cx="165" cy="78" r="18" fill="#3B82F6" opacity=".15"/></g>
   <g id="led-run"><circle cx="260" cy="78" r="26" fill="{AMBER}" filter="url(#glow)" opacity=".08"/><circle cx="260" cy="78" r="18" fill="{AMBER}" opacity=".15"/></g>
   <text x="70" y="124" fill="#DCE8DF" font-size="11" text-anchor="middle">GP2 · charging</text>
   <text x="165" y="124" fill="#DCE8DF" font-size="11" text-anchor="middle">GP4 · selling to grid</text>
   <text x="260" y="124" fill="#DCE8DF" font-size="11" text-anchor="middle">GP8 · RUN</text>
  </svg>
  <div style="min-width:260px;flex:1">
   <div id="clock" style="font-size:30px;font-weight:800;letter-spacing:-.02em"></div>
   <div id="mode" style="font-size:15px;margin:2px 0 6px 0"></div>
   <div style="font-size:12px;color:{MUTED}">MQTT · v2g/oxhack-4000/setpoint</div>
   <code id="payload" style="display:block;font-size:12px;background:#F3F7F4;border-radius:8px;padding:6px 8px;margin:2px 0 6px 0"></code>
   <div id="duty" style="font-size:13px"></div>
  </div>
 </div>
 <div style="display:flex;gap:12px;align-items:center;margin:10px 0 4px 0;flex-wrap:wrap">
  <button id="play" style="background:{GREEN};color:#fff;border:0;border-radius:999px;padding:6px 16px;font-weight:700;cursor:pointer">▶ Play</button>
  <label style="font-size:12px">speed <select id="speed" style="font-size:12px"><option value="700">0.7 s per half-hour</option><option value="350">0.35 s</option><option value="1400">1.4 s</option></select></label>
  <input id="pos" type="range" min="0" max="{len(slots) - 1}" value="0" style="flex:1;min-width:160px">
 </div>
 <div id="timeline" style="display:flex;gap:2px;align-items:flex-end;height:46px;margin-top:6px"></div>
 <div style="display:flex;justify-content:space-between;font-size:11px;color:{MUTED}"><span>{slots[0]["t"] if slots else ""}</span><span>{slots[len(slots) // 2]["t"] if slots else ""}</span><span>{slots[-1]["t"] if slots else ""}</span></div>
</div>
<script>
(function() {{
  const D = {data}, BASE = {LED_BASE_KW}, DMAX = {LED_DUTY_MAX};
  const $ = id => document.getElementById(id);
  const leds = {{ch: $("led-ch"), dis: $("led-dis"), run: $("led-run")}};
  const tl = $("timeline"); let i = 0, timer = null, last = null;
  D.forEach((s, k) => {{
    const b = document.createElement("div"); b.style.flex = "1"; b.style.cursor = "pointer"; b.style.borderRadius = "2px";
    b.style.height = Math.max(4, s.duty / DMAX * 44) + "px";
    b.style.background = s.mode === "CHARGE" ? "{GREEN}" : s.mode === "DISCHARGE" ? "#3B82F6" : "#D7E2DA";
    b.title = s.t + "  " + s.mode + " " + s.kw + " kW"; b.onclick = () => {{ i = k; render(true); }};
    tl.appendChild(b);
  }});
  function set(g, on, frac) {{ const [glow, core] = g.children; glow.setAttribute("opacity", on ? 0.25 + 0.65 * frac : 0.08); core.setAttribute("opacity", on ? 0.45 + 0.55 * frac : 0.15); }}
  function off() {{ set(leds.ch, false, 0); set(leds.dis, false, 0); set(leds.run, false, 0); }}
  function paint(s) {{
    const frac = s.duty / DMAX;
    off();
    if (s.mode === "CHARGE") set(leds.ch, true, frac);
    if (s.mode === "DISCHARGE") set(leds.dis, true, frac);
    if (s.mode !== "STOP") set(leds.run, true, 1);
    $("clock").textContent = s.t;
    $("mode").innerHTML = s.mode === "CHARGE" ? "<b style='color:{GREEN_DARK}'>Charging</b> " + s.kw.toFixed(1) + " kW from the grid"
      : s.mode === "DISCHARGE" ? "<b style='color:#1D4ED8'>Selling to the grid</b> " + s.kw.toFixed(1) + " kW"
      : (s.plugged ? "<b>Idle</b>, plugged in" : "<b>Idle</b>, car away");
    $("payload").textContent = s.payload;
    $("duty").textContent = "PWM duty " + s.duty + " % (|P| / " + BASE + " kW × " + DMAX + " %) · battery " + Math.round(s.soc * 100) + " %";
    Array.from(tl.children).forEach((b, k) => b.style.outline = k === i ? "2px solid {INK}" : "none");
    $("pos").value = i;
  }}
  function render(jump) {{
    const s = D[i];
    if (!jump && last && last.mode !== s.mode && last.mode !== "STOP" && s.mode !== "STOP") {{
      off(); setTimeout(() => paint(s), 160);        // the firmware's neutral interval on reversal, stretched to be visible
    }} else paint(s);
    last = s;
  }}
  function stop() {{ clearInterval(timer); timer = null; $("play").textContent = "▶ Play"; }}
  $("play").onclick = () => {{
    if (timer) return stop();
    if (i >= D.length - 1) i = 0;
    $("play").textContent = "❚❚ Pause";
    timer = setInterval(() => {{ if (i >= D.length - 1) return stop(); i++; render(false); }}, +$("speed").value);
  }};
  $("speed").onchange = () => {{ if (timer) {{ stop(); $("play").onclick(); }} }};
  $("pos").oninput = e => {{ i = +e.target.value; render(true); }};
  render(true);
}})();
</script>"""


def chain_diagram_svg() -> str:
    """The pipeline from market data to the LEDs as six blocks: four in the data/ML track, two in the
    hardware track. Inline SVG so it scales with the column and needs no library."""
    blocks = [  # (title, subtitle, fill, text colour)
        ("Market data", "prices · wind · demand", "#E6F4EA", INK),
        ("Price forecast", "lear_x model", "#CDEBD6", INK),
        ("Optimiser", "48 setpoints a day", "#A9DDB9", INK),
        ("Sender", "JSON over MQTT", GREEN, "#FFFFFF"),
        ("Relay", "checks · forwards", "#FDE68A", INK),
        ("Pico 2 W", "converter → LEDs", AMBER, "#FFFFFF"),
    ]
    w, h, gap, x0, y0 = 180, 112, 34, 8, 54
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {x0 * 2 + 6 * w + 5 * gap} 196" width="100%" role="img" '
             'aria-label="Pipeline from market data to the Pico LEDs" '
             'style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;display:block">',
             '<defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="9" markerHeight="9" '
             f'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{MUTED}"/></marker></defs>']
    # track bands
    data_w, hw_x = 4 * w + 3 * gap, x0 + 4 * w + 4 * gap
    parts.append(f'<rect x="{x0 - 4}" y="{y0 - 34}" width="{data_w + 8}" height="{h + 48}" rx="18" fill="#F6FBF7"/>')
    parts.append(f'<rect x="{hw_x - 4}" y="{y0 - 34}" width="{2 * w + gap + 8}" height="{h + 48}" rx="18" fill="#FFFBEB"/>')
    parts.append(f'<text x="{x0 + 12}" y="{y0 - 12}" font-size="14" font-weight="700" fill="{GREEN_DARK}" '
                 'letter-spacing=".08em">DATA &amp; ML TRACK</text>')
    parts.append(f'<text x="{hw_x + 12}" y="{y0 - 12}" font-size="14" font-weight="700" fill="#92400E" '
                 'letter-spacing=".08em">HARDWARE TRACK</text>')
    for i, (title, sub, fill, colour) in enumerate(blocks):
        x = x0 + i * (w + gap)
        parts.append(f'<rect x="{x}" y="{y0}" width="{w}" height="{h}" rx="14" fill="{fill}"/>')
        parts.append(f'<text x="{x + w / 2}" y="{y0 + 50}" text-anchor="middle" font-size="22" font-weight="800" '
                     f'fill="{colour}">{title}</text>')
        parts.append(f'<text x="{x + w / 2}" y="{y0 + 80}" text-anchor="middle" font-size="15" fill="{colour}" '
                     f'opacity=".85">{sub}</text>')
        if i < len(blocks) - 1:
            parts.append(f'<line x1="{x + w + 3}" y1="{y0 + h / 2}" x2="{x + w + gap - 3}" y2="{y0 + h / 2}" '
                         f'stroke="{MUTED}" stroke-width="3" marker-end="url(#arr)"/>')
    parts.append("</svg>")
    return "".join(parts)


def steps_diagram_svg(steps: list[tuple[str, str]]) -> str:
    """A top-to-bottom block diagram for a narrow column: one block per (title, subtitle), arrows
    between, the last block in the strong green as the output. Inline SVG string for st.image."""
    from html import escape
    fills = ["#E6F4EA", "#D6EEDD", "#C2E6CC", "#A9DDB9", "#8FD3A4", "#6FC58A"]
    w, h, gap, x0, y0 = 444, 58, 22, 8, 8
    n = len(steps)
    total_h = y0 * 2 + n * h + (n - 1) * gap
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w + 2 * x0} {total_h}" width="100%" role="img" '
             'style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;display:block">',
             '<defs><marker id="arrv" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" '
             f'orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="{MUTED}"/></marker></defs>']
    for i, (title, sub) in enumerate(steps):
        y = y0 + i * (h + gap)
        last = i == n - 1
        fill, colour = (GREEN, "#FFFFFF") if last else (fills[min(i, len(fills) - 1)], INK)
        parts.append(f'<rect x="{x0}" y="{y}" width="{w}" height="{h}" rx="12" fill="{fill}"/>')
        parts.append(f'<text x="{x0 + 18}" y="{y + 25}" font-size="19" font-weight="800" fill="{colour}">'
                     f'{escape(title)}</text>')
        parts.append(f'<text x="{x0 + 18}" y="{y + 46}" font-size="13" fill="{colour}" opacity=".85">'
                     f'{escape(sub)}</text>')
        if not last:
            cx = x0 + w / 2
            parts.append(f'<line x1="{cx}" y1="{y + h + 2}" x2="{cx}" y2="{y + h + gap - 1}" stroke="{MUTED}" '
                         'stroke-width="3" marker-end="url(#arrv)"/>')
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Shared parameters (spec + assumptions)
# ---------------------------------------------------------------------------
spec, spec_source = load_spec()
eta_ch_spec = float(spec["efficiency_curve"]["eta_charge"][-1])      # ASSUMPTION: operate near rated power
eta_dis_spec = float(spec["efficiency_curve"]["eta_discharge"][-1])
cost = spec["cost"]
premium_spec = cost["bom_cost"] + cost["install_cost"] - cost["unidirectional_baseline_cost"]
cfg, cfg_source, cfg_problem = load_assumptions()
if cfg_problem:
    st.warning(cfg_problem + " Using built-in defaults.")
EXPORT_FRACTION = cfg["tariff"]["export_price_fraction"]
_, _, price_source = load_prices(EXPORT_FRACTION)

bat = cfg["battery"]
ASSUME = dict(e_cap=bat["e_cap_kwh"], reserve_kwh=bat["reserve_kwh"], soc_min=bat["soc_min"],
              soc_max=bat["soc_max"],
              c_deg=float(per_mwh_to_per_kwh(bat["c_deg_per_mwh"])))   # £/kWh of throughput (§8.C)
PLUG_DAYS = cfg["owner"]["plug_days_per_year"]
DEFAULT_MODE = cfg["optimiser"]["mode"]
DEFAULT_TARGET_PCT = int(min(90, max(30, 5 * round(cfg["optimiser"]["target_soc"] * 20))))   # slider: 30–90 in 5s

tab_app, tab_analysis, tab_hw = st.tabs(["📱  ExtraEnergy app", "📊  Analysis", "🔧  Hardware"])

# ===========================================================================
# OWNER VIEW
# ===========================================================================
with tab_app:
    left, mid, right = st.columns([1, 1.25, 1])
    with left:
        st.markdown("#### What the owner sees")
        st.caption("Phone-sized product view. Every number is computed live from the "
                   "spec file and an optimiser run.")
        st.markdown(f"Spec: `{spec_source}` {badge(spec.get('provenance', 'placeholder'))}<br>"
                    f"Settings: `{cfg_source}`<br>"
                    f"Prices: synthetic half-hourly tariff {badge(price_source)}", unsafe_allow_html=True)
        st.markdown("**Demo controls** (outside the phone)")
        arrive = st.select_slider("Car plugged in at", [slot_label(i) for i in range(0, 12)], value="17:00")
        depart = st.select_slider("Leaves at", [slot_label(i) for i in range(26, 36)], value="07:30")
        trip = st.slider("Tomorrow's driving (kWh)", 4, 30, 12, help="≈ 3.5 miles per kWh")

    with mid:
        with st.container(key="phone"):
            st.markdown('<div class="ee-brand">Extra<span>Energy</span></div>'
                        '<div class="ee-tag">Making energy work for you</div>', unsafe_allow_html=True)
            mode_label = st.radio("How should we run your charger?",
                                  ["Max savings", "Savings + guaranteed charge"],
                                  index=1 if DEFAULT_MODE == "guaranteed_charge" else 0)
            mode = "max_savings" if mode_label == "Max savings" else "guaranteed_charge"
            target, always_ready = DEFAULT_TARGET_PCT / 100, True
            if mode == "guaranteed_charge":
                target = st.slider("Keep my battery at least", 30, 90, DEFAULT_TARGET_PCT, step=5, format="%d%%") / 100
                always_ready = st.toggle("Always ready (not just by departure)", value=True,
                                         help="On: we only ever sell energy above your target. "
                                              "Off: we just make sure you hit it by the time you leave.")

            params = dict(**ASSUME, p_ch=spec["power"]["p_charge_max_kw"], p_dis=spec["power"]["p_discharge_max_kw"],
                          eta_ch=eta_ch_spec, eta_dis=eta_dis_spec, trip_kwh=float(trip),
                          arrive_slot=time_to_slot(arrive), depart_slot=time_to_slot(depart),
                          mode=mode, target=target, always_ready=always_ready,
                          plug_days=PLUG_DAYS, premium=premium_spec)
            st.session_state["owner_params"] = params
            r = evaluate(params)

            if not r["ok"]:
                st.error("Can't meet that target before you leave. Try a lower target or a later departure.")
            else:
                v2g = r["v2g"]
                st.markdown(
                    f'<div class="ee-hero"><div class="small">You save about</div>'
                    f'<div class="big">£{r["v2g_yr"]:,.0f}<span style="font-size:1rem"> / year</span></div>'
                    f'<div class="small">vs a normal charger · £{(r["base_day"] - v2g["cost"]):.2f} tonight</div></div>',
                    unsafe_allow_html=True)

                # Plain-English status line
                sell_slots = np.where(v2g["p_dis"] > 0.05)[0]
                a = params["arrive_slot"]
                if len(sell_slots):
                    s0, s1 = slot_label(a + sell_slots[0]), slot_label(a + sell_slots[-1] + 1)
                    sell_txt = f"Selling {v2g['exported_kwh']:.0f} kWh to the grid {s0}–{s1}"
                else:
                    sell_txt = "No selling tonight: prices don't make it worth it"
                if mode == "guaranteed_charge" and always_ready and v2g["ready_slot"] is not None:
                    ready_txt = f"at {target:.0%} or more from {slot_label(a + v2g['ready_slot'])}"
                else:
                    ready_txt = f"{v2g['soc'][-1]:.0%} charged by {depart}"
                st.markdown(f'<div class="ee-status">⚡ {sell_txt}<br>🔋 {ready_txt}</div>',
                            unsafe_allow_html=True)

                # Tonight's plan chart
                buy_all, _, _ = load_prices(EXPORT_FRACTION)
                d = params["depart_slot"]
                labels = [slot_label(i) for i in range(a, d)]
                fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.08,
                                    row_heights=[0.6, 0.4], specs=[[{"secondary_y": True}], [{}]])
                colors = [GREEN if v >= 0 else AMBER for v in v2g["p_net"]]
                fig.add_bar(x=labels, y=v2g["p_net"], marker_color=colors, name="kW", row=1, col=1)
                fig.add_scatter(x=labels, y=buy_all[a:d] / 10, mode="lines", line=dict(color=INK, width=2),
                                name="p/kWh", row=1, col=1, secondary_y=True)
                fig.add_scatter(x=labels, y=v2g["soc"][1:] * 100, mode="lines", fill="tozeroy",
                                line=dict(color=GREEN, width=2), fillcolor="rgba(22,163,74,.15)",
                                name="Battery %", row=2, col=1)
                if mode == "guaranteed_charge":
                    fig.add_hline(y=target * 100, line_dash="dot", line_color=GREEN_DARK, row=2, col=1)
                fig.update_layout(height=330, margin=dict(l=0, r=0, t=26, b=0), showlegend=False,
                                  plot_bgcolor="white", paper_bgcolor="white", bargap=0.15,
                                  title=dict(text="Tonight's plan", font=dict(size=14), x=0))
                fig.update_yaxes(title_text="kW", row=1, col=1, gridcolor="#EEF2EF", zeroline=True,
                                 zerolinecolor="#B8C4BC")
                fig.update_yaxes(title_text="p/kWh", row=1, col=1, secondary_y=True, showgrid=False)
                fig.update_yaxes(title_text="%", range=[0, 100], row=2, col=1, gridcolor="#EEF2EF")
                fig.update_xaxes(nticks=8, row=2, col=1)
                st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
                st.caption("🟢 charging  🟠 selling to the grid  — line: price")

                if mode == "guaranteed_charge":
                    st.markdown(f"**Cost of peace of mind:** £{r['peace_cost_yr']:,.0f}/yr "
                                f"vs Max savings (£{r['v2g_free_yr']:,.0f}/yr).")
                else:
                    st.markdown("We'll always keep enough for your next trip plus a safety reserve.")

            st.markdown('<div class="ee-soon">🚐 <b>Coming soon:</b> ExtraEnergy for fleets. '
                        'Run every vehicle in your depot from one app.</div>', unsafe_allow_html=True)

    with right:
        st.markdown("#### Talking points")
        if r["ok"]:
            st.metric("Savings vs a normal charger", f"£{r['v2g_yr']:,.0f}/yr")
            st.metric("…of which needs our bidirectional hardware", f"£{r['increment_yr']:,.0f}/yr",
                      help="Smart charging alone (no selling back) would save "
                           f"£{r['smart_yr']:,.0f}/yr.")
            st.metric("Cost of peace of mind", f"£{r['peace_cost_yr']:,.0f}/yr")
        st.caption("Synthetic tariff and typical battery settings. Swap in real artefacts as stages land.")

# ===========================================================================
# ANALYSIS VIEW (CONTEXT.md §8: the data / ML / optimisation track)
# ===========================================================================
with tab_analysis:
    op = st.session_state.get("owner_params")
    st.markdown("### From our software to the owner's bill")
    st.caption("Owner settings come from the app tab. Drag the what-if sliders to see every number update.")

    c1, c2 = st.columns([1, 2.2])
    with c1:
        with st.container(border=True):
            st.markdown(f"**Hardware what-if** {badge(spec.get('provenance', 'placeholder'))}",
                        unsafe_allow_html=True)
            st.caption("Starts from the converter spec (Hardware tab).")
            p_dis = st.slider("Discharge rating (kW)", 3.7, 22.0, float(spec["power"]["p_discharge_max_kw"]), 0.1)
            eta_shift = st.slider("Efficiency shift (points)", -3.0, 3.0, 0.0, 0.5)
            premium = st.slider("Price premium over a normal charger (£)", 0, 2000, int(premium_spec), 50)
            st.markdown("**Owner settings**")
            plug_days = st.slider("Days a year plugged in overnight", 150, 365, PLUG_DAYS)

    ap = dict(op)
    ap.update(p_dis=p_dis, p_ch=max(op["p_ch"], p_dis), eta_ch=min(0.995, eta_ch_spec + eta_shift / 100),
              eta_dis=min(0.995, eta_dis_spec + eta_shift / 100), premium=premium, plug_days=plug_days)
    ra = evaluate(ap)

    with c2:
        if ra["ok"]:
            eta_peak = max(spec["efficiency_curve"]["eta_discharge"]) * 100 + eta_shift
            pay = f"{ra['payback_yr']:.1f} years" if math.isfinite(ra["payback_yr"]) else "never (at these inputs)"
            exported_yr = ra["v2g"]["exported_kwh"] * plug_days
            st.markdown(
                f'<div class="ee-headline">Our <b>{p_dis:.1f} kW</b>, <b>{eta_peak:.1f}%</b>-efficient charger, '
                f'run by ExtraEnergy, saves an owner <b>£{ra["v2g_yr"]:,.0f}/yr</b> vs a normal charger. '
                f'The bidirectional premium of <b>£{premium:,}</b> pays back in <b>{pay}</b>, and each car '
                f'sells <b>{exported_yr:,.0f} kWh/yr</b> back to the grid when it needs it most.</div>',
                unsafe_allow_html=True)
            st.caption("This sentence is the integration test (CONTEXT.md §1). Synthetic prices.")

        g1, g2 = st.columns(2)
        with g1:      # §8.G owner economics
            if ra["ok"]:
                fig = go.Figure(go.Bar(
                    x=["Normal charger", "Smart charging", "ExtraEnergy V2G"],
                    y=[0, ra["smart_yr"], ra["v2g_yr"]],
                    marker_color=["#CBD5CF", "#86EFAC", GREEN],
                    text=[f"£{v:,.0f}" for v in (0, ra["smart_yr"], ra["v2g_yr"])], textposition="outside"))
                fig.update_layout(title=dict(text="Owner savings per year", font=dict(size=14)), height=300,
                                  margin=dict(l=0, r=0, t=40, b=0), plot_bgcolor="white", yaxis_title="£/yr",
                                  yaxis=dict(gridcolor="#EEF2EF"))
                st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
        with g2:      # §8.A prices (Tony's forecast replaces this)
            buy_all, sell_all, _ = load_prices(EXPORT_FRACTION)
            labels = [slot_label(i) for i in range(N_SLOTS)]
            fig = go.Figure()
            fig.add_scatter(x=labels, y=buy_all / 10, mode="lines", line=dict(color=INK, width=2), name="Buy")
            fig.add_scatter(x=labels, y=sell_all / 10, mode="lines", line=dict(color=GREEN, width=2, dash="dot"),
                            name="Sell")
            fig.update_layout(title=dict(text="Electricity prices, next 24 h (p/kWh)", font=dict(size=14)),
                              height=300, margin=dict(l=0, r=0, t=40, b=0), plot_bgcolor="white",
                              yaxis=dict(gridcolor="#EEF2EF", title="p/kWh"),
                              legend=dict(orientation="h", y=-0.2))
            fig.update_xaxes(nticks=8)
            st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
            st.caption(f"Synthetic tariff {badge(price_source)}. TODO(S3): Tony's price forecast "
                       "(artefacts/forecasts/price_quantiles.parquet).", unsafe_allow_html=True)

    # ---- §8.B the optimiser (8b_singleev_optimiser/v2g-hack/docs/optimisation_problem.md)
    st.markdown("### The optimiser: how it decides")
    st.caption("For every half-hour the car is plugged in, it chooses **how hard to charge** and **how hard "
               "to sell back**. Everything else (battery level, the bill) follows from those two numbers.")
    o1, o2 = st.columns(2, gap="large")
    with o1:
        with st.container(border=True):
            st.markdown("##### What it minimises: the owner's bill")
            st.latex(r"\min_{P^{ch}_t,\;P^{dis}_t}\;\sum_t \Big[\,p^{buy}_t P^{ch}_t \;-\; p^{sell}_t P^{dis}_t"
                     r"\;+\; c_{deg}\,(P^{ch}_t + P^{dis}_t)\Big]\,\Delta t")
            st.markdown("Pay for what you import, earn for what you export, and charge every kWh that "
                        f"passes through the battery a small wear cost (c_deg = {ASSUME['c_deg'] * 100:.0f} p/kWh). "
                        "Savings = a normal charger's bill minus this.",
                        unsafe_allow_html=True)
            st.markdown(
                "**The rules it must follow**\n"
                "1. **Energy adds up.** The battery only changes by what goes in (minus charging losses), "
                "what comes out (plus selling losses) and what the car uses driving.\n"
                f"2. **Battery stays in its healthy range**, between {ASSUME['soc_min']:.0%} and "
                f"{ASSUME['soc_max']:.0%} full.\n"
                "3. **Charger limits.** It can only charge or sell while the car is plugged in, and never "
                "faster than the charger is rated for.\n"
                "4. **Charge or sell, never both** in the same half-hour (otherwise it could waste energy on "
                "purpose when prices go negative).\n"
                "5. **The driver comes first.** *Max savings:* always enough for the next trip plus a reserve "
                "when the car leaves. *Guaranteed charge:* never below the owner's chosen target while "
                "plugged in.")
    with o2:
        with st.container(border=True):
            st.markdown("##### How it's solved: simplex, in milliseconds")
            a_s, d_s = op["arrive_slot"], op["depart_slot"]
            buy_all, sell_all, _ = load_prices(EXPORT_FRACTION)
            t0 = time.perf_counter()
            live = lp.solve_schedule(buy=tuple(buy_all[a_s:d_s]), sell=tuple(sell_all[a_s:d_s]),
                                     **{k: op[k] for k in ("p_ch", "p_dis", "eta_ch", "eta_dis", "e_cap",
                                                           "trip_kwh", "reserve_kwh", "soc_min", "soc_max",
                                                           "c_deg", "mode", "target", "always_ready")},
                                     allow_v2g=True)
            solve_ms = (time.perf_counter() - t0) * 1000
            n_slots = d_s - a_s
            s1, s2 = st.columns(2)
            s1.metric("Tonight's plan", f"{n_slots} half-hours", delta=f"{3 * n_slots + 2} decision variables",
                      delta_color="off")
            s2.metric("Solved in", f"{solve_ms:.0f} ms", delta="live, on this page", delta_color="off")
            st.markdown(
                "- **A linear program.** The bill and every rule are straight-line formulas, so the best plan "
                "is guaranteed to be the global best, not just a good guess.\n"
                "- **Simplex (HiGHS).** The best plan sits at a corner of the allowed region; simplex walks "
                "corner to better corner until none is better. Exact answer, no tuning.\n"
                "- **Yes/no switches only where needed:** 'charge OR sell' only bites when prices go negative, "
                "and 'always ready' needs one. Those few switches make it a MILP, solved by branch-and-bound.\n"
                "- **Why not gradient descent?** The bill is linear, so its gradient never shrinks; all the "
                "work is in the constraints, which simplex handles exactly.\n"
                "- **Plan 38 h, act 24 h.** Every morning at 10:00 (new price forecast) it plans to midnight "
                "tomorrow, acts on the first 24 h, then re-plans with the real battery level, and again the "
                "moment the car plugs in.")

    # ---- the loss function behind the predictions the optimiser plans around
    with st.container(border=True):
        report_l = load_habits_report()[0]
        w_l = report_l["config"].get("asym_weight_w", 9) if report_l else 9
        tau = w_l / (w_l + 1)
        st.markdown("##### Loss function: penalise under-charging heavily")
        l1, l2 = st.columns([1.35, 1], gap="large")
        with l1:
            st.markdown("The optimiser plans around **predicted** trips and departure times. Getting those wrong "
                        "is not symmetric: predicting too little strands the driver, predicting too much only "
                        "costs a little lost income. So the predictions are trained with an **asymmetric "
                        "(pinball) loss**, where a dangerous error costs "
                        f"**w = {w_l:g}×** a safe one:")
            st.latex(r"L_\tau(y,\hat y)=\begin{cases}\tau\,(y-\hat y) & y \ge \hat y\ \text{(predicted too low)}\\"
                     r"(1-\tau)\,(\hat y-y) & y < \hat y\ \text{(predicted too high)}\end{cases}"
                     rf"\qquad \tau=\tfrac{{w}}{{w+1}}={tau:.1f}")
            st.dataframe(pd.DataFrame({
                "Dangerous error": ["predicting too little", "predicting too late", "predicting too early"],
                "So we predict the": [f"P{tau * 100:.0f} (high end)", f"P{(1 - tau) * 100:.0f} (early end)",
                                      f"P{tau * 100:.0f} (late end)"],
            }, index=["Trip energy", "Departure time", "Plug-in time"]), width="stretch")
            st.markdown("**How it feeds the optimiser:** at each predicted (early-side) departure, the battery "
                        "must hold the cautious (high-side) trip estimate plus a safety reserve, on top of its "
                        "minimum level.")
        with l2:
            err = np.linspace(-10, 10, 81)            # actual − predicted (kWh)
            fig = go.Figure()
            fig.add_scatter(x=err, y=np.where(err >= 0, tau * err, (tau - 1) * err), mode="lines",
                            line=dict(color=GREEN, width=3), name=f"Asymmetric (w = {w_l:g})")
            fig.add_scatter(x=err, y=0.5 * np.abs(err), mode="lines", line=dict(color=MUTED, width=2, dash="dot"),
                            name="Symmetric")
            fig.add_annotation(x=7, y=tau * 7, text="car left short:<br>heavy penalty", showarrow=False,
                               yshift=26, font=dict(size=11, color=GREEN_DARK))
            fig.add_annotation(x=-7, y=(1 - tau) * 7, text="a bit too cautious:<br>small penalty",
                               showarrow=False, yshift=26, font=dict(size=11, color=MUTED))
            fig.update_layout(title=dict(text="Penalty vs prediction error (trip energy)", font=dict(size=14)),
                              height=300, margin=dict(l=0, r=0, t=40, b=0), plot_bgcolor="white",
                              xaxis=dict(title="actual − predicted (kWh)", gridcolor="#EEF2EF", zeroline=True),
                              yaxis=dict(title="penalty", gridcolor="#EEF2EF"),
                              legend=dict(orientation="h", y=-0.3))
            st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
            st.caption("w is a dial between safety and savings: w = 1 (a plain median) leaves 63% of trips short "
                       "in validation; w = 9 cuts that to 16%. Source: "
                       "8b_singleev_optimiser/v2g-hack/docs/optimisation_problem.md")

    # ---- §8.B.1 app modes: the cost of peace of mind as the target rises
    st.markdown("#### The cost of peace of mind")
    targets = list(range(30, 95, 5))
    curve = []
    for t in targets:
        pp = dict(ap, mode="guaranteed_charge", target=t / 100)
        rr = evaluate(pp)
        curve.append(rr["v2g_yr"] if rr["ok"] else None)
    fig = go.Figure()
    fig.add_scatter(x=targets, y=curve, mode="lines+markers", line=dict(color=GREEN, width=3),
                    name="Guaranteed charge")
    if ra["ok"]:
        fig.add_hline(y=ra["v2g_free_yr"], line_dash="dot", line_color=INK,
                      annotation_text=f"Max savings £{ra['v2g_free_yr']:,.0f}/yr", annotation_position="top left")
    fig.update_layout(height=260, margin=dict(l=0, r=0, t=10, b=0), plot_bgcolor="white", showlegend=False,
                      xaxis=dict(title="Target the owner picks (%)", gridcolor="#EEF2EF"),
                      yaxis=dict(title="£/yr saved", gridcolor="#EEF2EF"))
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
    st.caption("Savings in guaranteed-charge mode as the owner raises their target, at the app tab's "
               "trip and times. The gap to the dotted line is the cost of peace of mind.")

    # ---- How ExtraEnergy learns: the two learning models, at presentation level
    st.markdown("### How ExtraEnergy learns")
    st.caption("Two models feed the optimiser: one predicts **prices**, one predicts the **owner**. "
               "Each is compared with a simple 'copy the past' rule, the benchmark it has to beat.")
    lc1, lc2 = st.columns(2, gap="large")

    with lc1:      # §8.A price forecasting (Tony)
        with st.container(border=True):
            st.markdown("##### 1 · Forecasting tomorrow's prices")
            st.markdown("Every morning at **10:00** we forecast the wholesale electricity price for every "
                        "half-hour until midnight tomorrow, so the car knows when power will be cheap and "
                        "when it will be worth selling.")
            st.image(steps_diagram_svg([
                ("Market data", "Elexon prices · day-ahead wind, solar and demand forecasts"),
                ("Clean and align", "every half-hour in UTC, gaps filled, only what was known by 10:00"),
                ("Features", "the last few days' prices · tomorrow's wind and demand · calendar"),
                ("LEAR model", "a lasso regression per half-hour, refitted every week"),
                ("Price forecast", "P50 path plus a P10–P90 band from past errors, into the optimiser"),
            ]), width="stretch")
            fr = load_forecast_results()
            if fr is None:
                st.info("Forecast results not found (artefacts/forecasts/). Run `make forecast-lear-x`.")
            else:
                st.dataframe(pd.DataFrame(
                    {"How it predicts": [idea for _, _, idea in FORECAST_MODELS],
                     "Typical error (£/MWh)": [round(fr["mae"].get(k, float("nan")), 1) for k, _, _ in FORECAST_MODELS]},
                    index=[name for _, name, _ in FORECAST_MODELS]), width="stretch")
                names = [name for _, name, _ in FORECAST_MODELS] + ["Perfect foresight"]
                keys = [k for k, _, _ in FORECAST_MODELS] + ["perfect_foresight"]
                vals = [fr["saving"].get(k, 0.0) for k in keys]
                fig = go.Figure(go.Bar(
                    y=names, x=vals, orientation="h",
                    marker_color=["#CBD5CF", "#CBD5CF", "#86EFAC", GREEN, "#E6F4EA"],
                    marker_line=dict(color=["#CBD5CF"] * 4 + [GREEN], width=[0, 0, 0, 0, 2]),
                    text=[f"£{v:,.0f}" for v in vals], textposition="outside", cliponaxis=False))
                # Room for the label beyond the longest bar, and a horizontal range slider under the axis.
                x_max = max(vals + [1.0]) * 1.22
                fig.update_layout(title=dict(text="Owner savings a year, by price forecast", font=dict(size=14)),
                                  height=300, margin=dict(l=0, r=56, t=36, b=0), plot_bgcolor="white",
                                  xaxis=dict(title="£/yr vs a normal charger", gridcolor="#EEF2EF",
                                             range=[0, x_max], rangeslider=dict(visible=True, thickness=0.10,
                                                                                range=[0, x_max])))
                st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
                best, naive = fr["mae"].get("lear_x"), fr["mae"].get("naive_daily")
                if best and naive:
                    st.markdown(f"✅ Our best model is **{1 - best / naive:.0%} more accurate** than copying "
                                f"yesterday, and captures **{fr['pct_perfect'].get('lear_x', 0):.0f}%** of the "
                                "saving a perfect forecast would give.")
                st.caption("Tony's one-year backtest (Oct 2025 – Sep 2026, real GB prices, forecasts issued as if "
                           f"live). {ASSUME['e_cap']:g} kWh car, {spec['power']['p_charge_max_kw']:g} kW charger "
                           f"(converter spec v{spec.get('spec_version', '?')}); estimated tariff adders. "
                           "More: artefacts/forecasts/FORECAST_MODELS.md", unsafe_allow_html=True)

    with lc2:      # §8.B Option B: the habits model
        with st.container(border=True):
            st.markdown("##### 2 · Learning the owner's habits")
            st.markdown("From last week's charging, we predict next week: **when the car plugs in, when it "
                        "leaves, and how much energy each trip needs**. The owner never has to enter a timetable.")
            report, sessions = load_habits_report()[0], load_sessions_report()
            logs_txt = (f"Electric Nation trial: {sessions['sessions_final']:,} sessions, {sessions['vehicles']} households"
                        if sessions else "Electric Nation trial: real UK home charging")
            st.image(steps_diagram_svg([
                ("Charging logs", logs_txt),
                ("Sessions", "plug-in time, departure time and energy per visit, in local time"),
                ("Weekly features", "each car's last 7 days, as known on Monday 00:00"),
                ("LightGBM", "does it plug in? when? when does it leave? how much energy?"),
                ("Cautious plan", "P90 trip energy, P10 departure, into the optimiser's 'ready by' rule"),
            ]), width="stretch")
            data_txt = (f"**{sessions['sessions_final']:,} real charging sessions** from **{sessions['vehicles']} UK "
                        "households** (Electric Nation trial)") if sessions else "real UK home-charging data"
            w = report["config"].get("asym_weight_w", 9) if report else 9
            st.markdown(f"- **Learns from** {data_txt}, one shared model, personal predictions.\n"
                        "- **Model:** gradient-boosted trees (LightGBM) that read each driver's last week.\n"
                        f"- **Cautious by design:** running short costs the driver far more than a little lost "
                        f"income, so it is trained to plan for a bigger trip and an earlier departure "
                        f"(a shortfall counts {w:g}× more than a surplus).")
            if report is None:
                st.info("Habits model results not found (run `python -m v2g.habits.evaluate`).")
            else:
                cc = report["cost_of_caution"]
                m1, m2 = st.columns(2)
                m1.metric("Trips left short", f"{cc['lgbm']['shortfall_rate']:.1%}",
                          delta=f"vs {cc['baseline']['shortfall_rate']:.1%} copying last week",
                          delta_color="off")
                m2.metric("Savings kept", f"{cc['lgbm']['share_of_perfect_savings']:.0%}",
                          delta="vs knowing the week perfectly", delta_color="off")
                st.markdown(f"✅ **{cc['baseline']['shortfall_rate'] / cc['lgbm']['shortfall_rate']:.0f}× fewer** "
                            "trips where the car is left short than simply copying last week.")
                st.caption("Tested on 63 drivers the model never saw, one simulated week each; prices "
                           "synthetic. More: "
                           "8b_singleev_optimiser/v2g-hack/docs/optimisation_problem.md", unsafe_allow_html=True)

    # ---- §8.H tornado: which input moves the payback most?
    st.markdown("#### What moves the payback most?")
    if ra["ok"] and math.isfinite(ra["payback_yr"]):
        swings = {
            "Discharge rating ±50%": ("p_dis", 0.5, 1.5),
            "Efficiency ±2 pts": ("eta", -0.02, 0.02),
            "Hardware premium ±25%": ("premium", 0.75, 1.25),
            "Battery wear cost ±50%": ("c_deg", 0.5, 1.5),
            "Days plugged in ±20%": ("plug_days", 0.8, 1.2),
        }
        rows = []
        for name, (key, lo, hi) in swings.items():
            vals = []
            for f in (lo, hi):
                pp = dict(ap)
                if key == "eta":
                    pp["eta_ch"] = min(0.995, ap["eta_ch"] + f)
                    pp["eta_dis"] = min(0.995, ap["eta_dis"] + f)
                elif key == "p_dis":
                    pp["p_dis"] = ap["p_dis"] * f
                    pp["p_ch"] = max(ap["p_ch"], pp["p_dis"])
                else:
                    pp[key] = ap[key] * f
                rr = evaluate(pp)
                vals.append(min(rr["payback_yr"], 30) if rr["ok"] else 30)
            rows.append((name, min(vals), max(vals)))
        rows.sort(key=lambda x: x[2] - x[1])
        base_pb = ra["payback_yr"]
        fig = go.Figure()
        fig.add_bar(y=[r[0] for r in rows], x=[base_pb - r[1] for r in rows], base=[r[1] for r in rows],
                    orientation="h", marker_color=GREEN, name="Better case")
        fig.add_bar(y=[r[0] for r in rows], x=[r[2] - base_pb for r in rows], base=[base_pb] * len(rows),
                    orientation="h", marker_color=AMBER, name="Worse case")
        fig.add_vline(x=base_pb, line_color=INK, line_width=2)
        fig.update_layout(barmode="overlay", height=280, margin=dict(l=0, r=0, t=10, b=0),
                          plot_bgcolor="white", xaxis=dict(title="Payback (years)", gridcolor="#EEF2EF"),
                          legend=dict(orientation="h", y=-0.3))
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
        st.caption("One-at-a-time swings around the current settings. Payback capped at 30 years for display. "
                   f"Battery wear: {ASSUME['c_deg'] * 100:.0f} p/kWh of throughput (§8.C).",
                   unsafe_allow_html=True)
    else:
        st.info("At these inputs the V2G increment doesn't pay back, so there's no tornado to show.")

# ===========================================================================
# HARDWARE VIEW (CONTEXT.md §6.1, §7: the converter spec from Abdullah & Issa)
# ===========================================================================
with tab_hw:
    prov = spec.get("provenance", "placeholder")
    st.markdown(f"### Converter spec {badge(prov)}", unsafe_allow_html=True)
    st.caption(f"Read from `{spec_source}`. The hardware team owns this file; the app and every "
               "calculation update automatically when it changes.")
    st.markdown(f"**Topology:** {spec.get('topology', '—')}  \n"
                f"**Architecture:** {spec.get('architecture', '—')} · "
                f"**Spec version:** {spec.get('spec_version', '—')}")

    pw, dyn = spec.get("power", {}), spec.get("dynamics", {})
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Charge rating", f"{pw.get('p_charge_max_kw', 0):.1f} kW")
    k2.metric("Discharge rating", f"{pw.get('p_discharge_max_kw', 0):.1f} kW")
    k3.metric("Peak efficiency (discharge)", f"{max(spec['efficiency_curve']['eta_discharge']) * 100:.1f}%")
    k4.metric("Response time", f"{dyn.get('response_time_s', 0):g} s")

    h1, h2 = st.columns([1.6, 1])
    with h1:
        ec = spec["efficiency_curve"]
        fig = go.Figure()
        fig.add_scatter(x=[f * 100 for f in ec["load_fraction"]], y=[e * 100 for e in ec["eta_charge"]],
                        mode="lines+markers", line=dict(color=GREEN, width=3), name="Charging")
        fig.add_scatter(x=[f * 100 for f in ec["load_fraction"]], y=[e * 100 for e in ec["eta_discharge"]],
                        mode="lines+markers", line=dict(color=AMBER, width=3), name="Selling to the grid")
        fig.update_layout(title=dict(text="Efficiency vs load", font=dict(size=14)), height=300,
                          margin=dict(l=0, r=0, t=40, b=0), plot_bgcolor="white",
                          xaxis=dict(title="% of rated power", gridcolor="#EEF2EF"),
                          yaxis=dict(title="Efficiency (%)", gridcolor="#EEF2EF"),
                          legend=dict(orientation="h", y=-0.25))
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
    with h2:
        gs, bs = spec.get("grid_side", {}), spec.get("battery_side", {})
        st.dataframe(pd.DataFrame({"Value": [
            f"{gs.get('phases', '—')} phase, {gs.get('v_ac_rms', '—')} V, {gs.get('f_hz', '—')} Hz",
            f"{bs.get('v_dc_min', '—')}–{bs.get('v_dc_max', '—')} V DC",
            f"{pw.get('q_max_kvar', '—')} kvar",
            f"{spec.get('standby_w', '—')} W",
            f"{dyn.get('ramp_kw_per_s', '—')} kW/s",
            f"{spec.get('lifetime_years', '—')} years",
        ]}, index=["Grid side", "Battery side", "Reactive power", "Standby loss", "Ramp rate", "Lifetime"]),
            width="stretch")

    # --- Circuit diagrams from the hardware track (hardware/docs/diagrams/). Skipped quietly if missing.
    diagrams = ROOT / "hardware" / "docs" / "diagrams"
    block, grid_sch, dcdc_sch = (diagrams / f for f in
                                 ("system_block_diagram.jpeg", "grid_side_full_bridge.jpeg", "dc_dc_buck_boost.jpeg"))
    if block.exists():
        st.markdown("#### How the converter is built")
        d1, d2 = st.columns([1, 1.2])
        d1.image(str(block), caption="Two stages share a 700 V DC link: a grid-side full bridge and a "
                                     "bidirectional buck/boost to the battery.", width="stretch")
        d2.markdown("Power flows both ways through the same two stages:\n\n"
                    "- **Charging (G2V):** the full bridge rectifies the 230 V grid onto the DC link; the "
                    "DC-DC stage bucks it down to the battery.\n"
                    "- **Selling (V2G):** the DC-DC stage boosts the battery up to the link; the full bridge "
                    "inverts it back to 50 Hz.\n\n"
                    "Both stages switch at 50 kHz. The schematics below are what the LTspice runs simulate.")
    if grid_sch.exists() or dcdc_sch.exists():
        e1, e2 = st.columns(2)
        if grid_sch.exists():
            e1.image(str(grid_sch), caption="Grid-side AC/DC: full bridge, 2.2 mH grid inductor, unipolar PWM "
                                            "current control.", width="stretch")
        if dcdc_sch.exists():
            e2.image(str(dcdc_sch), caption="Battery-side DC-DC: half-bridge, 680 µH. Buck to charge, boost "
                                            "to sell.", width="stretch")

    # --- Hardware track evidence: the LTspice runs in hardware/sim/. Shown next to the spec so the simulated
    # DC/DC-stage numbers are not mistaken for the full-chain values the optimiser uses.
    st.markdown("#### What the simulation has pinned down")
    sim = load_sim_results()
    if sim is None:
        st.info("No simulation results found (hardware/sim/data/). The spec above is the contract; the fields "
                "not yet simulated in `config/converter_spec.yaml` are waiting on the hardware track.")
    else:
        hw, summ, cyc = sim["hw"], sim["summary"].set_index("mode"), sim["cycles"]
        el, sw = hw["electrical"], hw["switching"]
        t0, t1 = summ["analysis_start_s"].iloc[0] * 1000, summ["analysis_end_s"].iloc[0] * 1000
        st.caption(f"LTspice, hardware track: {hw['system']['converter_stage_modelled']}; the grid stage is "
                   f"simulated separately. {el['dc_link_nominal_voltage_v']:.0f} V link, "
                   f"{el['battery_open_circuit_voltage_v']:.0f} V battery with {el['battery_internal_resistance_ohm']} Ω, "
                   f"{el['inductance_h'] * 1e6:.0f} µH, {sw['frequency_hz'] / 1000:.0f} kHz, open-loop fixed duty; "
                   f"steady state averaged over {t0:.0f}–{t1:.0f} ms. "
                   f"{hw['evidence']['performance_status'].capitalize()}. Details: hardware/sim/README.md.")
        s1, s2 = st.columns([1, 1.5])
        with s1:
            def _row(label, col, scale=1.0, fmt="{:,.2f}"):
                return [label] + [fmt.format(summ.loc[m, col] * scale) for m in ("G2V", "V2G")]
            table = pd.DataFrame([
                _row("Battery terminal voltage (V)", "battery_terminal_voltage_avg_v", fmt="{:,.1f}"),
                _row("Inductor current, mean (A)", "inductor_current_avg_a"),
                _row("Inductor ripple, pk-pk (A)", "inductor_current_ripple_pp_a"),
                _row("Battery terminal power (kW)", "battery_terminal_power_avg_w", 1e-3),
                _row("DC-link source power (kW)", "dc_link_source_power_avg_w", 1e-3),
                _row("DC/DC stage loss (W)", "dc_dc_stage_loss_w", fmt="{:,.1f}"),
                _row("DC/DC stage efficiency (%)", "model_stage_efficiency_percent"),
            ], columns=["", "G2V charging", "V2G discharging"]).set_index("")
            st.dataframe(table, width="stretch")
            st.caption("Signs follow §6.3: + = grid → vehicle. The DC-link figure is the ideal source's, so it is "
                       "negative while it supplies (G2V). Stage efficiency is the DC/DC stage alone; the spec's "
                       "rated-load figure multiplies it by the AC/DC stage's own run (hardware/sim/efficiency.md).")
        with s2:
            fig = go.Figure()
            for mode, colour, name in (("G2V", GREEN, "G2V: charging"), ("V2G", AMBER, "V2G: discharging")):
                d = cyc[cyc["mode"] == mode]
                fig.add_scatter(x=d["time_start_s"] * 1000, y=d["battery_terminal_power_avg_w"] / 1000,
                                mode="lines", line=dict(color=colour, width=2), name=name)
            fig.add_vrect(x0=t0, x1=t1, fillcolor="#EEF2EF", opacity=0.6, line_width=0,
                          annotation_text="steady-state window", annotation_position="top left")
            fig.update_layout(title=dict(text="Battery terminal power, one point per switching cycle",
                                         font=dict(size=14)),
                              height=330, margin=dict(l=0, r=0, t=40, b=0), plot_bgcolor="white",
                              xaxis=dict(title="Time (ms)", gridcolor="#EEF2EF"),
                              yaxis=dict(title="kW (+ charging)", gridcolor="#EEF2EF"),
                              legend=dict(orientation="h", y=-0.25))
            st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})

    st.markdown("#### Where each spec number comes from")
    st.markdown(
        "| Spec field | Source |\n|---|---|\n"
        f"| Topology; grid side ({spec.get('grid_side', {}).get('v_ac_rms', '—')} V, "
        f"{spec.get('grid_side', {}).get('f_hz', '—')} Hz); charge and discharge rating "
        f"({pw.get('p_charge_max_kw', 0):g} kW) | {badge('simulation')} hardware/sim/ltspice/*.asc, "
        "hardware_config.json |\n"
        f"| Efficiency at rated load ({spec['efficiency_curve']['eta_charge'][-1] * 100:.1f} % charging, "
        f"{spec['efficiency_curve']['eta_discharge'][-1] * 100:.1f} % selling) | {badge('simulation')} AC/DC stage × "
        "DC/DC stage, each simulated on its own with ideal switches (hardware/sim/efficiency.md); an upper estimate |",
        unsafe_allow_html=True)

    # --- The controller stage (Eesah, hardware/firmware_c): what executes the schedule, shown as its LEDs.
    st.markdown("#### The controller and its LEDs")
    st.markdown(
        "The last link is a **Raspberry Pi Pico 2 W** running Eesah's firmware (`hardware/firmware_c/`). "
        "It makes no decisions. Every half-hour the optimiser's setpoint for the car arrives as one MQTT "
        "message, the firmware ramps its simulated converter to it at 5 kW/s and drops back to zero if no "
        "fresh command arrives within 5 s (`converter.c`). `power_stage.c` then renders the signed power as "
        "two mutually exclusive 1 kHz PWM outputs: **green on GP2 while charging, blue on GP4 while selling to "
        "the grid**, brightness equal to the PWM duty, with a **yellow RUN LED on GP8** whenever either is lit "
        "and a short neutral gap on every reversal. This is a logic-level demonstrator: the LEDs show PWM "
        "duty, not real power, and the gate-driver inhibit on GP6 is held low so nothing can energise a bridge.")
    st.image(chain_diagram_svg(), width="stretch")      # SVG string; st.html would sanitise the <svg> away
    st.caption("Verified end to end on a laptop with the real `converter.c` (`hardware/firmware_c/INTEGRATION.md`); "
               "the LED stage itself runs on the board or in Wokwi.")
    cl, cr = st.columns([1.55, 1])
    with cl:
        rng = schedule_date_range()
        if rng is None:
            st.info("No schedule artefact found (artefacts/schedules/lear_x.parquet): run `make optimise-backtest` "
                    "to replay a day through the controller here.")
        else:
            import datetime as _dt
            default_day = _dt.date(2025, 10, 4) if rng[0] <= _dt.date(2025, 10, 4) <= rng[1] else rng[0]
            day = st.date_input("Day to replay (lear_x schedule, London time)", value=default_day,
                                min_value=rng[0], max_value=rng[1], key="led_day")
            slots = load_led_day(day.isoformat())
            if not slots:
                st.info("No schedule for that day.")
            else:
                st.iframe(led_animation_html(slots), height=330)
                n_ch, n_dis = sum(s["mode"] == "CHARGE" for s in slots), sum(s["mode"] == "DISCHARGE" for s in slots)
                st.caption(f"{len(slots)} half-hours from the 10:00 issue: {n_ch} charging, {n_dis} selling, "
                           f"{len(slots) - n_ch - n_dis} idle. These are the exact commands "
                           "`make control-send` publishes; on the Wokwi board the same bytes light the same LEDs.")
    with cr:
        st.markdown("**Pico pinout** (`hardware/firmware_c/PINOUT_AND_TEST.md`)")
        st.dataframe(pd.DataFrame({
            "Pin": ["GP2 · pin 4", "GP4 · pin 6", "GP8 · pin 11", "GP7 · pin 10", "GP6 · pin 9"],
            "Role": ["PWM, green LED: charging", "PWM, blue LED: selling to grid", "RUN indicator, yellow LED",
                     "Fault button, active low (latches FAULT)", "Gate-driver inhibit, held LOW, not connected"],
        }).set_index("Pin"), width="stretch")
