"""Load this optimiser package under the alias ``ev_optimiser`` from the ROOT repository environment.

The repository root and this sub-project both define top-level packages ``v2g`` and ``shared``
with different contents, so ``v2g.optimise`` (here) and ``v2g.forecast`` (root) cannot both be
imported by their real names in one process. This loader imports the optimiser modules by file
path under a distinct package name, leaving every file in place. Nothing here is imported by the
forecast package (dependency direction stays optimiser -> forecast).

Usage from the root environment (root ``src`` and root on sys.path)::

    opt = bridge.load()          # opt.lp, opt.daily, opt.schema, opt.price_inputs, ...
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent          # .../src/v2g/optimise
SUB_ROOT = HERE.parents[2]                      # .../8b_singleev_optimiser/v2g-hack
ALIAS = "ev_optimiser"
MODULES = ("lp", "daily", "price_inputs", "availability", "rolling_backtest", "decision_value", "schedule_plot")


def _load_file(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load() -> types.SimpleNamespace:
    """Return a namespace with the optimiser modules, its config schema and the sub-project root."""
    if ALIAS not in sys.modules:
        package = types.ModuleType(ALIAS)
        package.__path__ = [str(HERE)]
        sys.modules[ALIAS] = package
    schema = _load_file(f"{ALIAS}_schema", SUB_ROOT / "shared" / "schema.py")
    modules = {name: importlib.import_module(f"{ALIAS}.{name}") for name in MODULES}
    return types.SimpleNamespace(schema=schema, sub_root=SUB_ROOT, **modules)
