from pathlib import Path
import yaml

from shared.schema import ConverterSpec


ROOT = Path(__file__).resolve().parents[1]


def load_converter_spec(path: str = "config/converter_spec.yaml") -> ConverterSpec:
    with open(ROOT / path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return ConverterSpec(**data)


def test_load_spec() -> None:
    spec = load_converter_spec()
    assert spec.spec_version == "0.1"
    assert 0 < spec.power.p_charge_max_kw < 100
    assert spec.efficiency_curve.eta_charge[0] < 1
