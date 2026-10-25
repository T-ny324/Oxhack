"""App smoke test (CONTEXT.md §12): all three views, both modes and toggles render without errors."""
from pathlib import Path

from streamlit.testing.v1 import AppTest

# Streamlit resolves relative paths against THIS file, so build an absolute one.
APP = str(Path(__file__).resolve().parents[1] / "dashboard" / "app.py")


def test_app_runs_all_modes():
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception
    assert [t.label.strip() for t in at.tabs] == ["📱  ExtraEnergy app".strip(), "📊  Analysis".strip(),
                                                   "🔧  Hardware".strip()]
    at.slider[1].set_value(80).run()
    assert not at.exception
    at.toggle[0].set_value(False).run()
    assert not at.exception
    at.radio[0].set_value("Max savings").run()
    assert not at.exception


# The owner view at default settings with the converter spec at provenance "simulation" (6.6 kW, 97.5 % at
# rated load; hardware/sim). They were £1,410 / £307 / £477 with the placeholder spec (7.4 kW, 95.5 %);
# a spec change moves them, a refactor must not.
OWNER_METRICS = {"Savings vs a normal charger": "£1,393/yr",
                 "…of which needs our bidirectional hardware": "£321/yr",
                 "Cost of peace of mind": "£410/yr"}


def owner_metrics(at):
    return {m.label: m.value for m in at.metric if m.label in OWNER_METRICS}


def test_owner_numbers_unchanged_by_refactor():
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert owner_metrics(at) == OWNER_METRICS


def test_app_runs_without_assumptions_file(monkeypatch):
    """§8.I: the app must never crash because a config file is missing. It falls back to its
    built-in defaults (the same values), says so, and shows the same numbers."""
    import streamlit as st
    real_exists = Path.exists
    monkeypatch.setattr(Path, "exists",
                        lambda self: False if self.name == "assumptions.yaml" else real_exists(self))
    st.cache_data.clear()                      # don't reuse the cached copy of the real file
    try:
        at = AppTest.from_file(APP, default_timeout=60).run()
        assert not at.exception
        assert any("built-in defaults" in m.value for m in at.markdown)
        assert owner_metrics(at) == OWNER_METRICS
    finally:
        st.cache_data.clear()
